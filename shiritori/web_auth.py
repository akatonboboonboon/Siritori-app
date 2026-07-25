"""NiceGUI/FastAPI authentication edge with secure opaque cookies."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import hmac
from html import escape
import logging
from pathlib import Path
import secrets
from threading import Lock
import time
from typing import TypeVar
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from fastapi import HTTPException, Request
from nicegui import app, ui
from starlette.responses import RedirectResponse

from .auth import (
    AuthService,
    InvalidCredentialsError,
    InvalidRegistrationError,
    InvalidSessionError,
    SessionPrincipal,
    UsernameUnavailableError,
    canonicalize_username,
)
from .database import GameRepository
from .lobby import LobbyError, LobbyService
from .models import RoomRole, RoomStatus as StoredRoomStatus
from .room_runtime import RoomRuntimeCapabilityError
from .rooms import (
    LexiconRoomService,
    Role,
    RoomCoordinator,
    RoomError,
    RoomEvent,
    RoomEventKind,
    RoomSnapshot,
    RoomStatus,
    RoomVersionConflict,
    SeatController,
    WordSubmissionStatus,
)
from .settings import Settings
from .solo import SoloGameAuthorizationError, SoloGameService


_PLATFORM_CSS = (
    Path(__file__).parent.parent / "assets" / "platform.css"
).read_text(encoding="utf-8")
_MAX_FORM_BYTES = 8_192
_PasswordResult = TypeVar("_PasswordResult")
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuthWebServices:
    auth: AuthService
    games: GameRepository
    settings: Settings
    solo: SoloGameService | None = None
    rooms: RoomCoordinator | None = None
    room_words: LexiconRoomService | None = None
    lobby: LobbyService | None = None


class CsrfProtector:
    """Small stateless signed-token helper for native HTML forms."""

    def __init__(self, secret: str, *, lifetime_seconds: int = 7_200) -> None:
        if len(secret) < 32:
            raise ValueError("CSRF secret must contain at least 32 characters")
        if lifetime_seconds < 60:
            raise ValueError("CSRF token lifetime must be at least 60 seconds")
        self._secret = secret.encode("utf-8")
        self.lifetime_seconds = lifetime_seconds

    def issue(self, subject: str, *, now: int | None = None) -> str:
        issued_at = int(time.time()) if now is None else int(now)
        nonce = secrets.token_urlsafe(18)
        payload = f"{issued_at}.{nonce}.{subject}"
        signature = hmac.new(
            self._secret, payload.encode("utf-8"), sha256
        ).hexdigest()
        return f"{issued_at}.{nonce}.{signature}"

    def verify(
        self, token: str, subject: str, *, now: int | None = None
    ) -> bool:
        try:
            issued_text, nonce, signature = str(token).split(".", 2)
            issued_at = int(issued_text)
        except (TypeError, ValueError):
            return False
        checked_at = int(time.time()) if now is None else int(now)
        if issued_at > checked_at + 60:
            return False
        if checked_at - issued_at > self.lifetime_seconds:
            return False
        payload = f"{issued_at}.{nonce}.{subject}"
        expected = hmac.new(
            self._secret, payload.encode("utf-8"), sha256
        ).hexdigest()
        return hmac.compare_digest(signature, expected)


class LoginAttemptLimiter:
    """Thread-safe, TTL/LRU-bounded in-process attempt limiter."""

    def __init__(
        self,
        *,
        attempts: int = 5,
        window_seconds: int = 60,
        max_keys: int = 4_096,
    ) -> None:
        if attempts < 1 or window_seconds < 1 or max_keys < 1:
            raise ValueError(
                "attempt limit, window, and max_keys must be positive"
            )
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = Lock()

    @staticmethod
    def _bounded_key(key: str) -> str:
        value = str(key)
        if len(value) <= 256:
            return value
        return "sha256:" + sha256(value.encode("utf-8")).hexdigest()

    def _prune_expired_lru(self, cutoff: float) -> None:
        while self._events:
            oldest_key = next(iter(self._events))
            events = self._events[oldest_key]
            while events and events[0] <= cutoff:
                events.popleft()
            if events:
                break
            self._events.popitem(last=False)

    def allow(self, key: str, *, now: float | None = None) -> bool:
        checked_at = time.monotonic() if now is None else now
        cutoff = checked_at - self.window_seconds
        bounded_key = self._bounded_key(key)
        with self._lock:
            self._prune_expired_lru(cutoff)
            events = self._events.get(bounded_key)
            if events is not None:
                while events and events[0] <= cutoff:
                    events.popleft()
                if not events:
                    self._events.pop(bounded_key, None)
                    events = None
            if events is not None and len(events) >= self.attempts:
                return False
            if events is None:
                while len(self._events) >= self.max_keys:
                    self._events.popitem(last=False)
                events = deque()
                self._events[bounded_key] = events
            events.append(checked_at)
            self._events.move_to_end(bounded_key)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(self._bounded_key(key), None)

    @property
    def tracked_key_count(self) -> int:
        with self._lock:
            return len(self._events)


class PasswordWorkLimiter:
    """Bound concurrent CPU/memory-heavy password hash operations."""

    def __init__(self, *, max_concurrency: int = 2) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def run(
        self,
        operation: Callable[..., _PasswordResult],
        /,
        *args,
        **kwargs,
    ) -> _PasswordResult:
        async with self._semaphore:
            return await asyncio.to_thread(operation, *args, **kwargs)

async def _read_form(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if media_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="unsupported form encoding")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        stripped_length = content_length.strip()
        if not stripped_length.isascii() or not stripped_length.isdigit():
            raise HTTPException(status_code=400, detail="invalid content length")
        if int(stripped_length) > _MAX_FORM_BYTES:
            raise HTTPException(status_code=413, detail="form is too large")

    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > _MAX_FORM_BYTES - len(body):
            raise HTTPException(status_code=413, detail="form is too large")
        body.extend(chunk)
    try:
        decoded = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise HTTPException(
            status_code=400, detail="invalid form encoding"
        ) from error
    try:
        values = parse_qs(
            decoded,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=12,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail="invalid form fields"
        ) from error
    return {name: entries[-1] for name, entries in values.items() if entries}

def _same_origin(request: Request) -> bool:
    source = request.headers.get("origin")
    if not source:
        referer = request.headers.get("referer")
        if referer:
            parts = urlsplit(referer)
            source = f"{parts.scheme}://{parts.netloc}"
    if not source:
        return False

    # Render preserves the public Host header and sets X-Forwarded-Proto. Do
    # not trust X-Forwarded-Host: a client-supplied value could redefine the
    # authority against which Origin is checked.
    source_parts = urlsplit(source)
    host = request.headers.get("host", "").strip()
    if not host or "," in host:
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto")
    scheme = (
        forwarded_proto.split(",", 1)[0].strip()
        if forwarded_proto
        else request.url.scheme
    ).casefold()
    if scheme not in {"http", "https"}:
        return False
    if (
        source_parts.scheme.casefold() != scheme
        or source_parts.username is not None
        or source_parts.password is not None
        or source_parts.path not in {"", "/"}
        or source_parts.query
        or source_parts.fragment
    ):
        return False
    target_parts = urlsplit(f"{scheme}://{host}")
    try:
        source_port = source_parts.port or (443 if scheme == "https" else 80)
        target_port = target_parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        return False
    return (
        source_parts.hostname is not None
        and target_parts.hostname is not None
        and source_parts.hostname.casefold() == target_parts.hostname.casefold()
        and source_port == target_port
    )


def _auth_rate_limit_keys(
    request: Request, username: str
) -> tuple[str, str]:
    """Return fixed-size IP and canonical-account bucket keys."""

    client_host = request.client.host if request.client else "unknown"
    ip_identity = str(client_host).strip().casefold() or "unknown"
    _, username_key = canonicalize_username(username)
    ip_key = "ip:" + sha256(ip_identity.encode("utf-8")).hexdigest()
    account_key = (
        "account:" + sha256(username_key.encode("utf-8")).hexdigest()
    )
    return ip_key, account_key

def _safe_next(value: str | None, *, default: str = "/lobby") -> str:
    candidate = str(value or "")
    parts = urlsplit(candidate)
    if (
        candidate.startswith("/")
        and not candidate.startswith("//")
        and "\\" not in candidate
        and "\r" not in candidate
        and "\n" not in candidate
        and "\x00" not in candidate
        and not parts.scheme
        and not parts.netloc
    ):
        return candidate
    return default


def _error_redirect(path: str, code: str, next_path: str) -> RedirectResponse:
    query = urlencode({"error": code, "next": _safe_next(next_path)})
    return RedirectResponse(f"{path}?{query}", status_code=303)


def _set_session_cookie(
    response: RedirectResponse,
    token: str,
    expires_at: datetime,
    settings: Settings,
) -> None:
    now = datetime.now(timezone.utc)
    expiry = (
        expires_at.replace(tzinfo=timezone.utc)
        if expires_at.tzinfo is None
        else expires_at.astimezone(timezone.utc)
    )
    max_age = max(1, int((expiry - now).total_seconds()))
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=max_age,
        expires=expiry,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _error_message(
    error_code: str | None, messages: Mapping[str, str]
) -> str | None:
    if error_code is None:
        return None
    return messages.get(error_code, "処理を完了できませんでした。もう一度お試しください。")


def _page_shell() -> None:
    ui.add_css(_PLATFORM_CSS)
    ui.add_head_html(
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
    )


async def session_principal_from_request(
    request: Request,
    auth: AuthService,
    settings: Settings,
) -> SessionPrincipal | None:
    """Resolve the DB-backed principal for a custom protected NiceGUI page."""

    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    try:
        return await asyncio.to_thread(auth.authenticate_session, token)
    except InvalidSessionError:
        return None


def _native_form(
    *,
    action: str,
    csrf_token: str,
    next_path: str,
    register: bool,
) -> str:
    display_field = (
        """
        <label class="native-field">表示名（任意）
          <input name="display_name" maxlength="40" autocomplete="name">
        </label>
        """
        if register
        else ""
    )
    submit_text = "アカウントを作る" if register else "ログイン"
    autocomplete = "new-password" if register else "current-password"
    return f"""
      <form class="native-form" action="{escape(action)}" method="post">
        <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
        <input type="hidden" name="next" value="{escape(next_path)}">
        <label class="native-field">ユーザー名
          <input name="username" minlength="3" maxlength="32"
                 autocomplete="username" required autofocus>
        </label>
        {display_field}
        <label class="native-field">パスワード
          <input name="password" type="password" minlength="10" maxlength="128"
                 autocomplete="{autocomplete}" required>
        </label>
        <button class="native-submit" type="submit">{submit_text}</button>
      </form>
    """


def register_auth_pages(
    services: AuthWebServices,
    *,
    limiter: LoginAttemptLimiter | None = None,
    ip_limiter: LoginAttemptLimiter | None = None,
    password_work_limiter: PasswordWorkLimiter | None = None,
) -> None:
    """Register authentication endpoints and minimal protected shells."""

    auth = services.auth
    games = services.games
    settings = services.settings
    solo = services.solo
    csrf = CsrfProtector(settings.session_secret)
    rooms = services.rooms
    room_words = services.room_words
    lobby = services.lobby
    account_attempts = limiter or LoginAttemptLimiter()
    ip_attempts = ip_limiter or LoginAttemptLimiter(attempts=20)
    password_work = password_work_limiter or PasswordWorkLimiter()

    def consume_auth_attempt(
        request: Request, username: str
    ) -> tuple[bool, str]:
        ip_key, account_key = _auth_rate_limit_keys(request, username)
        # Evaluate both buckets on every request so account/IP behavior does
        # not disclose which one reached its limit.
        ip_allowed = ip_attempts.allow(ip_key)
        account_allowed = account_attempts.allow(account_key)
        return ip_allowed and account_allowed, account_key

    async def principal_for(request: Request):
        return await session_principal_from_request(
            request, auth, settings
        )

    @ui.page("/login")
    async def login_page(request: Request):
        if await principal_for(request) is not None:
            return RedirectResponse("/lobby", status_code=303)
        _page_shell()
        next_path = _safe_next(request.query_params.get("next"))
        message = _error_message(
            request.query_params.get("error"),
            {
                "credentials": "ユーザー名またはパスワードが正しくありません。",
                "rate": "試行回数が多すぎます。少し待ってからお試しください。",
                "csrf": "フォームの有効期限が切れました。もう一度お試しください。",
            },
        )
        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("auth-card"):
                ui.label("ログイン").classes("auth-title")
                ui.label("保存した対局とオンライン部屋を利用できます。").classes(
                    "auth-copy"
                )
                if message:
                    ui.label(message).classes("auth-error").props(
                        "role='alert'"
                    )
                ui.html(
                    _native_form(
                        action="/auth/login",
                        csrf_token=csrf.issue("anonymous-login"),
                        next_path=next_path,
                        register=False,
                    )
                )
                with ui.row().classes("auth-links"):
                    ui.link("まず1人で試す", "/").classes("platform-link")
                    ui.link("新規登録", f"/register?next={next_path}").classes(
                        "platform-link"
                    )

    @ui.page("/register")
    async def register_page(request: Request):
        if await principal_for(request) is not None:
            return RedirectResponse("/lobby", status_code=303)
        _page_shell()
        next_path = _safe_next(request.query_params.get("next"))
        message = _error_message(
            request.query_params.get("error"),
            {
                "invalid": "入力条件を確認してください。パスワードは10文字以上です。",
                "unavailable": "そのユーザー名は使用できません。",
                "rate": "試行回数が多すぎます。少し待ってからお試しください。",
                "csrf": "フォームの有効期限が切れました。もう一度お試しください。",
            },
        )
        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("auth-card"):
                ui.label("新規登録").classes("auth-title")
                ui.label(
                    "ユーザー名は3〜32文字、パスワードは10〜128文字です。"
                ).classes("auth-copy")
                if message:
                    ui.label(message).classes("auth-error").props(
                        "role='alert'"
                    )
                ui.html(
                    _native_form(
                        action="/auth/register",
                        csrf_token=csrf.issue("anonymous-register"),
                        next_path=next_path,
                        register=True,
                    )
                )
                ui.link("ログインへ戻る", f"/login?next={next_path}").classes(
                    "platform-link"
                )

    @app.post("/auth/login")
    async def login_action(request: Request):
        form = await _read_form(request)
        next_path = _safe_next(form.get("next"))
        if (
            not _same_origin(request)
            or not csrf.verify(
                form.get("csrf_token", ""), "anonymous-login"
            )
        ):
            return _error_redirect("/login", "csrf", next_path)

        username = form.get("username", "")
        allowed, account_key = consume_auth_attempt(request, username)
        if not allowed:
            return _error_redirect("/login", "rate", next_path)
        try:
            issued = await password_work.run(
                auth.login, username, form.get("password", "")
            )
        except (InvalidCredentialsError, InvalidRegistrationError):
            return _error_redirect("/login", "credentials", next_path)
        # A valid login clears only its canonical account bucket. Clearing the
        # IP-wide bucket would let one successful account bypass that limit.
        account_attempts.reset(account_key)
        response = RedirectResponse(next_path, status_code=303)
        _set_session_cookie(
            response, issued.token, issued.expires_at, settings
        )
        return response

    @app.post("/auth/register")
    async def register_action(request: Request):
        form = await _read_form(request)
        next_path = _safe_next(form.get("next"))
        if (
            not _same_origin(request)
            or not csrf.verify(
                form.get("csrf_token", ""), "anonymous-register"
            )
        ):
            return _error_redirect("/register", "csrf", next_path)
        username = form.get("username", "")
        allowed, account_key = consume_auth_attempt(request, username)
        if not allowed:
            return _error_redirect("/register", "rate", next_path)
        try:
            account = await password_work.run(
                auth.register,
                username,
                form.get("password", ""),
                display_name=form.get("display_name") or None,
            )
            issued = await asyncio.to_thread(
                auth.issue_session, account.id
            )
        except InvalidRegistrationError:
            return _error_redirect("/register", "invalid", next_path)
        except UsernameUnavailableError:
            return _error_redirect("/register", "unavailable", next_path)
        account_attempts.reset(account_key)
        response = RedirectResponse(next_path, status_code=303)
        _set_session_cookie(
            response, issued.token, issued.expires_at, settings
        )
        return response

    @ui.page("/lobby")
    async def lobby_page(request: Request):
        principal = await principal_for(request)
        if principal is None:
            return RedirectResponse("/login?next=/lobby", status_code=303)
        _page_shell()
        logout_token = csrf.issue(principal.session_id)
        theme_options = {"all": "すべて"}
        if solo is not None:
            theme_options = {
                theme.theme_id: theme.label
                for theme in solo.themes.themes
            }
        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("platform-wrap"):
                with ui.element("header").classes("platform-header"):
                    with ui.column():
                        ui.label(
                            f"{principal.account.display_name} さん"
                        ).classes("auth-title")
                        ui.label("オンライン対戦ロビー").classes(
                            "platform-muted"
                        )
                    ui.html(
                        f"""
                        <form class="logout-form" action="/auth/logout" method="post">
                          <input type="hidden" name="csrf_token"
                                 value="{escape(logout_token)}">
                          <button class="logout-button" type="submit">ログアウト</button>
                        </form>
                        """
                    )
                with ui.element("section").classes("dashboard-grid"):
                    with ui.column().classes("dashboard-card"):
                        ui.label("部屋を作る・参加する").classes("aside-title")
                        room_name_input = ui.input(
                            label="部屋名",
                            value=f"{principal.account.display_name}の部屋",
                        ).props("outlined maxlength=64").classes("w-full")
                        room_theme_select = ui.select(
                            options=theme_options,
                            value="all",
                            label="テーマ",
                        ).props("outlined options-dense").classes("w-full")
                        room_players_select = ui.select(
                            options={
                                number: f"{number}人"
                                for number in range(2, 9)
                            },
                            value=2,
                            label="最大人数",
                        ).props("outlined options-dense").classes("w-full")
                        room_timer_select = ui.select(
                            options={
                                "unlimited": "無制限",
                                "3": "3秒",
                                "10": "10秒",
                                "30": "30秒",
                                "60": "1分",
                                "180": "3分",
                            },
                            value="unlimited",
                            label="1手の制限時間",
                        ).props("outlined options-dense").classes("w-full")
                        spectator_switch = ui.switch(
                            "観戦を許可する", value=True
                        )
                        room_code_input = ui.input(
                            label="参加コード",
                            placeholder="例: ABC234",
                        ).props(
                            "outlined maxlength=12 autocomplete=off"
                        ).classes("w-full")
                        room_error = ui.label("").classes(
                            "platform-muted"
                        ).props("role='alert' aria-live='assertive'")
                        room_busy = False

                        def room_turn_seconds() -> int | None:
                            value = room_timer_select.value
                            if value == "unlimited":
                                return None
                            if (
                                isinstance(value, str)
                                and value.isdigit()
                            ):
                                seconds = int(value)
                                if 3 <= seconds <= 180:
                                    return seconds
                            raise ValueError("invalid room timer")

                        async def create_room() -> None:
                            nonlocal room_busy
                            if room_busy:
                                return
                            room_busy = True
                            create_room_button.disable()
                            room_error.set_text("")
                            try:
                                current_principal = await principal_for(
                                    request
                                )
                                if current_principal is None:
                                    ui.navigate.to(
                                        "/login?next=/lobby"
                                    )
                                    return
                                if lobby is None:
                                    raise RuntimeError(
                                        "lobby service is unavailable"
                                    )
                                theme_key = room_theme_select.value
                                max_players = room_players_select.value
                                if not isinstance(theme_key, str):
                                    raise ValueError("invalid theme")
                                if (
                                    type(max_players) is not int
                                    or not 2 <= max_players <= 8
                                ):
                                    raise ValueError("invalid player count")
                                room = await asyncio.to_thread(
                                    lobby.create_pvp_room,
                                    current_principal.account.id,
                                    name=str(
                                        room_name_input.value or ""
                                    ),
                                    max_players=max_players,
                                    allow_spectators=bool(
                                        spectator_switch.value
                                    ),
                                    theme_key=theme_key,
                                    turn_seconds=room_turn_seconds(),
                                )
                            except (LobbyError, TypeError, ValueError):
                                LOGGER.exception("invalid room setup")
                                room_error.set_text(
                                    "部屋の設定を確認してください。"
                                )
                            except Exception:
                                LOGGER.exception("failed to create room")
                                room_error.set_text(
                                    "部屋を作成できませんでした。"
                                )
                            else:
                                ui.navigate.to(
                                    f"/room/{room.room_code}"
                                )
                            finally:
                                room_busy = False
                                create_room_button.enable()

                        async def join_room(
                            *,
                            as_spectator: bool,
                        ) -> None:
                            nonlocal room_busy
                            if room_busy:
                                return
                            room_busy = True
                            room_error.set_text("")
                            try:
                                current_principal = await principal_for(
                                    request
                                )
                                if current_principal is None:
                                    ui.navigate.to(
                                        "/login?next=/lobby"
                                    )
                                    return
                                if lobby is None:
                                    raise RuntimeError(
                                        "lobby service is unavailable"
                                    )
                                join_method = (
                                    lobby.join_as_spectator
                                    if as_spectator
                                    else lobby.join_as_player
                                )
                                room = await asyncio.to_thread(
                                    join_method,
                                    current_principal.account.id,
                                    str(room_code_input.value or ""),
                                )
                            except (LobbyError, TypeError, ValueError):
                                LOGGER.exception("failed to join room")
                                room_error.set_text(
                                    "参加コードまたは部屋の状態を確認してください。"
                                )
                            except Exception:
                                LOGGER.exception(
                                    "unexpected room join failure"
                                )
                                room_error.set_text(
                                    "部屋へ参加できませんでした。"
                                )
                            else:
                                ui.navigate.to(
                                    f"/room/{room.room_code}"
                                )
                            finally:
                                room_busy = False

                        create_room_button = ui.button(
                            "部屋を作る",
                            icon="add",
                            on_click=create_room,
                        ).props("unelevated no-caps").classes("w-full")
                        with ui.row().classes("w-full gap-2"):
                            ui.button(
                                "対戦参加",
                                on_click=lambda: join_room(
                                    as_spectator=False
                                ),
                            ).props("outline no-caps").classes("grow")
                            ui.button(
                                "観戦参加",
                                on_click=lambda: join_room(
                                    as_spectator=True
                                ),
                            ).props("outline no-caps").classes("grow")
                    with ui.column().classes("dashboard-card"):
                        ui.label("保存したBot戦").classes("aside-title")
                        ui.link("保存一覧を開く", "/saved-games").classes(
                            "platform-link"
                        )
                    with ui.column().classes("dashboard-card"):
                        ui.label("1人でBot戦").classes("aside-title")
                        ui.label(
                            "遊ぶ単語のテーマを選びます。"
                        ).classes("platform-muted")
                        theme_select = ui.select(
                            options=theme_options,
                            value="all",
                            label="テーマ",
                        ).props("outlined options-dense").classes("w-full")
                        bot_count_select = ui.select(
                            options={
                                number: f"{number}体"
                                for number in range(1, 8)
                            },
                            value=1,
                            label="Botの数",
                        ).props("outlined options-dense").classes("w-full")
                        difficulty_select = ui.select(
                            options={
                                "normal": "ふつう",
                                "hard": "むずかしい",
                            },
                            value="normal",
                            label="難易度",
                        ).props("outlined options-dense").classes("w-full")
                        timer_select = ui.select(
                            options={
                                "unlimited": "無制限",
                                "3": "3秒",
                                "10": "10秒",
                                "30": "30秒",
                                "60": "1分",
                                "180": "3分",
                            },
                            value="unlimited",
                            label="1手の制限時間",
                        ).props("outlined options-dense").classes("w-full")
                        create_error = ui.label("").classes(
                            "platform-muted"
                        ).props("role='alert' aria-live='assertive'")
                        creating = False

                        async def create_solo_game() -> None:
                            nonlocal creating
                            if creating:
                                return
                            creating = True
                            create_button.disable()
                            create_error.set_text("")
                            try:
                                current_principal = await principal_for(
                                    request
                                )
                                if current_principal is None:
                                    ui.navigate.to(
                                        "/login?next=/lobby"
                                    )
                                    return
                                if solo is None:
                                    raise RuntimeError(
                                        "solo service is unavailable"
                                    )
                                theme_key = theme_select.value
                                bot_count = bot_count_select.value
                                difficulty = difficulty_select.value
                                timer_value = timer_select.value
                                if not isinstance(theme_key, str):
                                    raise ValueError("theme is required")
                                if (
                                    type(bot_count) is not int
                                    or not 1 <= bot_count <= 7
                                ):
                                    raise ValueError("invalid bot count")
                                if difficulty not in {"normal", "hard"}:
                                    raise ValueError("invalid difficulty")
                                if timer_value == "unlimited":
                                    turn_seconds = None
                                elif (
                                    isinstance(timer_value, str)
                                    and timer_value.isdigit()
                                ):
                                    turn_seconds = int(timer_value)
                                    if not 3 <= turn_seconds <= 180:
                                        raise ValueError("invalid timer")
                                else:
                                    raise ValueError("invalid timer")
                                snapshot = await solo.create(
                                    current_principal.account.id,
                                    bot_count=bot_count,
                                    bot_difficulty=difficulty,
                                    theme_key=theme_key,
                                    turn_seconds=turn_seconds,
                                )
                            except (
                                TypeError,
                                ValueError,
                                KeyError,
                                RoomRuntimeCapabilityError,
                            ):
                                LOGGER.exception("invalid solo setup")
                                create_error.set_text(
                                    "設定を確認してください。"
                                )
                            except Exception:
                                LOGGER.exception(
                                    "failed to create solo game"
                                )
                                create_error.set_text(
                                    "Bot戦を開始できませんでした。"
                                )
                            else:
                                ui.navigate.to(
                                    f"/play/{snapshot.room_id}"
                                )
                            finally:
                                creating = False
                                create_button.enable()

                        create_button = ui.button(
                            "Bot戦を始める",
                            icon="play_arrow",
                            on_click=create_solo_game,
                        ).props("unelevated no-caps").classes("w-full")

    @ui.page("/room/{room_code}")
    async def waiting_room_page(room_code: str, request: Request):
        principal = await principal_for(request)
        if principal is None:
            next_path = f"/room/{room_code}"
            return RedirectResponse(
                f"/login?next={next_path}", status_code=303
            )
        if lobby is None or rooms is None:
            return RedirectResponse("/lobby", status_code=303)

        user_id = principal.account.id
        try:
            initial_room = await asyncio.to_thread(
                lobby.get_room, room_code
            )
        except (LobbyError, ValueError):
            return RedirectResponse("/lobby", status_code=303)
        if initial_room.member_for(user_id) is None:
            return RedirectResponse("/lobby", status_code=303)
        if initial_room.status is StoredRoomStatus.ACTIVE:
            try:
                game_id = await asyncio.to_thread(
                    lobby.active_game_id,
                    user_id,
                    initial_room.room_code,
                )
            except LobbyError:
                return RedirectResponse("/lobby", status_code=303)
            await rooms.recover_after_restart(game_id)
            return RedirectResponse(
                f"/play/{game_id}", status_code=303
            )

        _page_shell()
        current_room = initial_room
        refreshing = False

        def render_room(room) -> None:
            nonlocal current_room
            current_room = room
            room_name_label.set_text(room.name)
            room_code_label.set_text(
                f"参加コード: {room.room_code}"
            )
            theme = (
                solo.themes.get(room.theme_key).label
                if solo is not None
                else room.theme_key
            )
            timer_text = (
                "無制限"
                if room.turn_seconds is None
                else f"{room.turn_seconds}秒"
            )
            settings_label.set_text(
                f"テーマ: {theme}・制限時間: {timer_text}・"
                f"最大{room.max_players}人"
            )
            members_box.clear()
            with members_box:
                ui.label("対戦参加者").classes("aside-title")
                for index, member in enumerate(room.players, start=1):
                    name = (
                        "あなた"
                        if member.user_id == user_id
                        else f"参加者 {index}"
                    )
                    ready = "準備OK" if member.ready else "準備中"
                    with ui.row().classes(
                        "w-full items-center justify-between"
                    ):
                        ui.label(name)
                        ui.label(ready).classes("platform-muted")
                ui.label(
                    f"観戦者: {len(room.spectators)}人"
                ).classes("platform-muted")

            member = room.member_for(user_id)
            is_player = (
                member is not None
                and member.role is RoomRole.PLAYER
            )
            ready_button.set_visibility(is_player)
            if is_player:
                ready_button.enable()
                ready_button.set_text(
                    "準備を取り消す"
                    if member.ready
                    else "準備OKにする"
                )
            is_owner = room.owner_user_id == user_id
            start_button.set_visibility(is_owner)
            if is_owner and room.all_players_ready:
                start_button.enable()
                message_label.set_text(
                    "全員の準備が完了しました。"
                )
            elif is_owner:
                start_button.disable()
                message_label.set_text(
                    "2人以上が準備OKになると開始できます。"
                )
            elif is_player:
                message_label.set_text(
                    "部屋の作成者が開始するまでお待ちください。"
                )
            else:
                message_label.set_text(
                    "観戦者として参加しています。"
                )

        async def waiting_session_is_valid() -> bool:
            current_principal = await principal_for(request)
            if (
                current_principal is not None
                and current_principal.account.id == user_id
            ):
                return True
            poll_timer.deactivate()
            ui.navigate.to(
                f"/login?next=/room/{current_room.room_code}"
            )
            return False

        async def refresh_room() -> None:
            nonlocal refreshing
            if refreshing or not await waiting_session_is_valid():
                return
            refreshing = True
            try:
                room = await asyncio.to_thread(
                    lobby.get_room, room_code
                )
                if room.status is StoredRoomStatus.ACTIVE:
                    game_id = await asyncio.to_thread(
                        lobby.active_game_id,
                        user_id,
                        room.room_code,
                    )
                    poll_timer.deactivate()
                    ui.navigate.to(f"/play/{game_id}")
                    return
                if room.member_for(user_id) is None:
                    poll_timer.deactivate()
                    ui.navigate.to("/lobby")
                    return
                render_room(room)
            except (LobbyError, ValueError):
                poll_timer.deactivate()
                message_label.set_text(
                    "部屋が終了しました。ロビーへ戻ってください。"
                )
            except Exception:
                LOGGER.exception("failed to refresh waiting room")
            finally:
                refreshing = False

        async def toggle_ready() -> None:
            if not await waiting_session_is_valid():
                return
            member = current_room.member_for(user_id)
            if member is None or member.role is not RoomRole.PLAYER:
                return
            ready_button.disable()
            try:
                room = await asyncio.to_thread(
                    lobby.set_ready,
                    user_id,
                    current_room.room_code,
                    ready=not member.ready,
                )
            except LobbyError:
                message_label.set_text(
                    "準備状態を変更できませんでした。"
                )
            else:
                render_room(room)

        async def start_match() -> None:
            if not await waiting_session_is_valid():
                return
            start_button.disable()
            try:
                result = await asyncio.to_thread(
                    lobby.start,
                    user_id,
                    current_room.room_code,
                )
                await rooms.recover_after_restart(result.game_id)
            except (LobbyError, RoomError):
                message_label.set_text(
                    "全員の準備を確認してください。"
                )
                await refresh_room()
            else:
                poll_timer.deactivate()
                ui.navigate.to(f"/play/{result.game_id}")

        async def leave_room() -> None:
            if not await waiting_session_is_valid():
                return
            try:
                await asyncio.to_thread(
                    lobby.leave,
                    user_id,
                    current_room.room_code,
                )
            except LobbyError:
                message_label.set_text(
                    "現在は退出できません。"
                )
            else:
                poll_timer.deactivate()
                ui.navigate.to("/lobby")

        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("platform-wrap"):
                with ui.row().classes(
                    "w-full items-center justify-between gap-3"
                ):
                    with ui.column():
                        room_name_label = ui.label(
                            initial_room.name
                        ).classes("auth-title")
                        room_code_label = ui.label("").classes(
                            "aside-title"
                        )
                        settings_label = ui.label("").classes(
                            "platform-muted"
                        )
                    ui.link("ロビーへ", "/lobby").classes(
                        "platform-link"
                    )
                with ui.element("section").classes("dashboard-grid"):
                    with ui.column().classes("dashboard-card"):
                        members_box = ui.column().classes("w-full gap-2")
                    with ui.column().classes("dashboard-card"):
                        message_label = ui.label("").classes(
                            "platform-muted"
                        ).props("role='status' aria-live='polite'")
                        ready_button = ui.button(
                            "準備OKにする",
                            on_click=toggle_ready,
                        ).props("outline no-caps").classes("w-full")
                        start_button = ui.button(
                            "対戦を始める",
                            icon="play_arrow",
                            on_click=start_match,
                        ).props("unelevated no-caps").classes("w-full")
                        ui.button(
                            "部屋から退出",
                            on_click=leave_room,
                        ).props("flat no-caps").classes("w-full")

        render_room(initial_room)
        poll_timer = ui.timer(1.5, refresh_room)

    @ui.page("/play/{game_id}")
    async def solo_play_page(game_id: str, request: Request):
        principal = await principal_for(request)
        if principal is None:
            next_path = f"/play/{game_id}"
            return RedirectResponse(
                f"/login?next={next_path}", status_code=303
            )
        if solo is None or rooms is None or room_words is None:
            return RedirectResponse("/lobby", status_code=303)

        _page_shell()
        user_id = principal.account.id
        client = ui.context.client
        current_snapshot: RoomSnapshot | None = None
        pending_submission: tuple[str, int, str] | None = None
        attaching = False
        submitting = False
        polling = False

        def can_submit(snapshot: RoomSnapshot) -> bool:
            seat = snapshot.seat_for_user(user_id)
            return (
                snapshot.status is RoomStatus.ACTIVE
                and seat is not None
                and snapshot.current_turn == seat.index
                and seat.controller is SeatController.HUMAN
            )

        def render(snapshot: RoomSnapshot) -> None:
            nonlocal current_snapshot
            if (
                current_snapshot is not None
                and snapshot.state_version < current_snapshot.state_version
            ):
                return
            current_snapshot = snapshot
            try:
                theme_label = solo.themes.get(
                    snapshot.theme_key
                ).label
            except KeyError:
                theme_label = snapshot.theme_key
            theme_label_element.set_text(f"テーマ: {theme_label}")
            timer = (
                "無制限"
                if snapshot.turn_seconds is None
                else f"{snapshot.turn_seconds}秒"
            )
            if snapshot.mode.value == "solo_bot":
                game_title.set_text("1人でBot戦")
                settings_label.set_text(
                    f"Bot {len(snapshot.players) - 1}体・"
                    f"{snapshot.bot_difficulty}・{timer}"
                )
            else:
                game_title.set_text("オンライン対戦")
                settings_label.set_text(
                    f"{len(snapshot.players)}人対戦・{timer}"
                )
            status_names = {
                RoomStatus.ACTIVE: "対局中",
                RoomStatus.PAUSED: "中断中",
                RoomStatus.FINISHED: "終了",
            }
            status_label.set_text(status_names[snapshot.status])
            if (
                snapshot.status is RoomStatus.ACTIVE
                and snapshot.eliminated_seats
            ):
                status_label.set_text(
                    f"対局中・残り{len(snapshot.active_seat_indexes)}人"
                )
            expected_label.set_text(
                snapshot.expected_kana or "自由"
            )
            current_seat = snapshot.players[snapshot.current_turn]
            if current_seat.controller is SeatController.BOT:
                bot_label = (
                    "代行Bot"
                    if current_seat.owner_user_id == user_id
                    else f"Bot {current_seat.index + 1}"
                )
                turn_label.set_text(
                    f"{bot_label} の番です"
                )
            elif current_seat.owner_user_id == user_id:
                turn_label.set_text("あなたの番です")
            else:
                turn_label.set_text("相手の番です")
            if snapshot.deadline_at is None:
                deadline_label.set_text("残り時間: 無制限")
            else:
                remaining = max(
                    0,
                    int(
                        (
                            snapshot.deadline_at
                            - datetime.now(timezone.utc)
                        ).total_seconds()
                    ),
                )
                deadline_label.set_text(f"残り時間: 約{remaining}秒")

            history_box.clear()
            with history_box:
                if not snapshot.history:
                    ui.label(
                        "先攻は好きな辞書単語から始められます。"
                    ).classes("platform-muted")
                for index, record in enumerate(
                    snapshot.history, start=1
                ):
                    if record.by_bot:
                        actor = f"Bot {record.seat_index + 1}"
                    elif record.actor_user_id == user_id:
                        actor = "あなた"
                    else:
                        actor = "相手"
                    with ui.row().classes(
                        "w-full items-center justify-between gap-3"
                    ):
                        ui.label(
                            f"{index}. {record.surface}"
                        ).classes("aside-title")
                        ui.label(
                            f"{actor}・よみ: {record.reading}"
                        ).classes("platform-muted")

            allowed = can_submit(snapshot) and not submitting
            if allowed:
                word_input.enable()
                submit_button.enable()
            else:
                word_input.disable()
                submit_button.disable()

            if snapshot.status is RoomStatus.FINISHED:
                reasons = {
                    "ends_with_n": "「ん」で終わったため終了しました。",
                    "duplicate": "同じ読みを使ったため終了しました。",
                    "timeout": "時間切れで終了しました。",
                    "no_legal_move": "出せる単語がなく終了しました。",
                }
                feedback_label.set_text(
                    reasons.get(
                        snapshot.end_reason,
                        "対局が終了しました。",
                    )
                )
                winner_indexes = snapshot.active_seat_indexes
                if len(winner_indexes) == 1:
                    winner_index = winner_indexes[0]
                    own_seat = snapshot.seat_for_user(user_id)
                    feedback_label.set_text(
                        "あなたの勝ちです！"
                        if (
                            own_seat is not None
                            and own_seat.index == winner_index
                        )
                        else f"プレイヤー{winner_index + 1}の勝ちです。"
                    )
            elif (
                snapshot.role_for_user(user_id) is Role.SPECTATOR
                and snapshot.seat_for_user(user_id) is not None
            ):
                feedback_label.set_text("脱落しました。観戦中です。")
            elif allowed:
                feedback_label.set_text(
                    "辞書にある単語を入力してください。"
                )
            elif snapshot.eliminated_seats:
                latest = snapshot.eliminated_seats[-1]
                feedback_label.set_text(
                    f"プレイヤー{latest + 1}が脱落しました。"
                    f"残り{len(snapshot.active_seat_indexes)}人です。"
                )
            else:
                feedback_label.set_text(
                    (
                        "Botの手を待っています。"
                        if snapshot.mode.value == "solo_bot"
                        else "相手の手を待っています。"
                    )
                )

        async def on_room_event(event: RoomEvent) -> None:
            if (
                event.kind is not RoomEventKind.SNAPSHOT
                or event.snapshot is None
                or client.is_deleted
            ):
                return
            try:
                with client:
                    render(event.snapshot)
            except Exception:
                LOGGER.exception("failed to render room event")

        async def attach() -> None:
            nonlocal attaching
            if attaching:
                return
            attaching = True
            try:
                snapshot = await rooms.connect_client(
                    game_id,
                    user_id,
                    client.id,
                    on_room_event,
                )
            except (SoloGameAuthorizationError, RoomError):
                LOGGER.exception("failed to open solo game")
                feedback_label.set_text(
                    "この対局を開けません。"
                )
                word_input.disable()
                submit_button.disable()
            except Exception:
                LOGGER.exception("failed to connect solo game")
                feedback_label.set_text(
                    "対局へ接続できませんでした。"
                )
            else:
                render(snapshot)
            finally:
                attaching = False

        async def refresh_snapshot() -> None:
            """Keep clients in sync even across multiple server workers."""

            nonlocal polling
            if polling or client.is_deleted:
                return
            polling = True
            try:
                snapshot = await rooms.load_snapshot(game_id)
                if snapshot.role_for_user(user_id) is None:
                    feedback_label.set_text(
                        "この対局を開けません。"
                    )
                    word_input.disable()
                    submit_button.disable()
                    return
                render(snapshot)
            except RoomError:
                feedback_label.set_text(
                    "対局が終了しました。"
                )
                word_input.disable()
                submit_button.disable()
            except Exception:
                LOGGER.exception(
                    "failed to refresh game snapshot"
                )
            finally:
                polling = False

        async def detach() -> None:
            try:
                await rooms.disconnect_client(game_id, client.id)
            except Exception:
                LOGGER.exception("failed to disconnect solo client")

        async def perform_submission(
            surface: str,
            *,
            chosen_reading: str | None,
            expected_version: int,
            operation_id: str,
        ) -> None:
            nonlocal pending_submission, submitting
            if submitting:
                return
            submitting = True
            submit_button.disable()
            try:
                result = await room_words.submit_user_word(
                    game_id,
                    user_id,
                    surface,
                    chosen_reading=chosen_reading,
                    expected_version=expected_version,
                    operation_id=operation_id,
                )
            except RoomVersionConflict as error:
                pending_submission = None
                reading_dialog.close()
                if error.current_snapshot is not None:
                    render(error.current_snapshot)
                feedback_label.set_text(
                    "状態が更新されました。もう一度お試しください。"
                )
            except RoomError:
                LOGGER.exception("solo word submission failed")
                pending_submission = None
                reading_dialog.close()
                feedback_label.set_text(
                    "今はその単語を送信できません。"
                )
            except Exception:
                LOGGER.exception("unexpected solo submission failure")
                feedback_label.set_text(
                    "単語を確認できませんでした。"
                )
            else:
                if (
                    result.status
                    is WordSubmissionStatus.READING_REQUIRED
                ):
                    pending_submission = (
                        surface,
                        expected_version,
                        operation_id,
                    )
                    reading_choices.clear()
                    with reading_choices:
                        for reading in result.reading_choices:
                            ui.button(
                                reading,
                                on_click=lambda _event=None,
                                value=reading: choose_reading(value),
                            ).props("outline no-caps").classes("w-full")
                    feedback_label.set_text(result.message)
                    reading_dialog.open()
                elif (
                    result.status
                    is WordSubmissionStatus.COMMITTED
                ):
                    pending_submission = None
                    reading_dialog.close()
                    word_input.set_value("")
                    if (
                        result.outcome is not None
                        and result.outcome.snapshot is not None
                    ):
                        render(result.outcome.snapshot)
                    feedback_label.set_text(result.message)
                else:
                    pending_submission = None
                    reading_dialog.close()
                    feedback_label.set_text(result.message)
            finally:
                submitting = False
                if current_snapshot is not None:
                    render(current_snapshot)

        async def submit_word(
            _event: object | None = None,
        ) -> None:
            snapshot = current_snapshot
            if snapshot is None or not can_submit(snapshot):
                return
            await perform_submission(
                str(word_input.value or ""),
                chosen_reading=None,
                expected_version=snapshot.state_version,
                operation_id=uuid4().hex,
            )

        async def choose_reading(reading: str) -> None:
            pending = pending_submission
            if pending is None:
                reading_dialog.close()
                return
            surface, version, operation_id = pending
            await perform_submission(
                surface,
                chosen_reading=reading,
                expected_version=version,
                operation_id=operation_id,
            )

        def cancel_reading() -> None:
            nonlocal pending_submission
            pending_submission = None
            reading_dialog.close()
            feedback_label.set_text("読みの選択を取り消しました。")

        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("platform-wrap"):
                with ui.row().classes(
                    "w-full items-center justify-between gap-3"
                ):
                    with ui.column():
                        game_title = ui.label("対局").classes("auth-title")
                        theme_label_element = ui.label(
                            "テーマ: 読み込み中"
                        ).classes("platform-muted")
                        settings_label = ui.label("").classes(
                            "platform-muted"
                        )
                    ui.link("ロビーへ", "/lobby").classes(
                        "platform-link"
                    )
                with ui.element("section").classes("dashboard-grid"):
                    with ui.column().classes("dashboard-card"):
                        status_label = ui.label("接続中").classes(
                            "aside-title"
                        )
                        turn_label = ui.label("").classes("aside-title")
                        with ui.row().classes(
                            "w-full items-center gap-3"
                        ):
                            ui.label("次の文字")
                            expected_label = ui.label("自由").classes(
                                "aside-title"
                            )
                        deadline_label = ui.label("").classes(
                            "platform-muted"
                        )
                        word_input = ui.input(
                            label="次のことば",
                            placeholder="漢字・ひらがな・カタカナ",
                        ).props(
                            "outlined clearable maxlength=30 autocomplete=off"
                        ).classes("w-full")
                        submit_button = ui.button(
                            "つなぐ",
                            icon="arrow_forward",
                            on_click=submit_word,
                        ).props("unelevated no-caps").classes("w-full")
                        word_input.on("keydown.enter", submit_word)
                        feedback_label = ui.label(
                            "対局へ接続しています。"
                        ).classes("platform-muted").props(
                            "role='status' aria-live='polite'"
                        )
                    with ui.column().classes("dashboard-card"):
                        ui.label("ことばの履歴").classes("aside-title")
                        history_box = ui.column().classes(
                            "w-full gap-2"
                        )

                with ui.dialog() as reading_dialog, ui.card():
                    ui.label("読みを選んでください").classes(
                        "aside-title"
                    )
                    reading_choices = ui.column().classes("w-full gap-2")
                    ui.button(
                        "取り消す",
                        on_click=cancel_reading,
                    ).props("flat no-caps")

        word_input.disable()
        submit_button.disable()
        client.on_connect(attach)
        client.on_disconnect(detach)
        ui.timer(1.0, refresh_snapshot)

    @ui.page("/saved-games")
    async def saved_games_page(request: Request):
        principal = await principal_for(request)
        if principal is None:
            return RedirectResponse(
                "/login?next=/saved-games", status_code=303
            )
        if solo is None:
            saves = await asyncio.to_thread(
                games.list_solo_saves, principal.account.id
            )
        else:
            saves = await solo.list_paused(principal.account.id)
        _page_shell()
        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("platform-wrap"):
                ui.link("← ロビーへ", "/lobby").classes("platform-link")
                ui.label("保存したBot戦").classes("auth-title")
                if not saves:
                    ui.label("保存中のBot戦はありません。").classes(
                        "platform-muted"
                    )
                for save in saves:
                    with ui.column().classes("dashboard-card"):
                        if solo is None:
                            ui.label(save.slot_name).classes("aside-title")
                            ui.label(
                                f"状態バージョン: {save.saved_state_version}"
                            ).classes("platform-muted")
                        else:
                            ui.label(
                                f"{save.theme_key} / {save.bot_difficulty}"
                            ).classes("aside-title")
                            timer = (
                                "無制限"
                                if save.turn_seconds is None
                                else f"{save.turn_seconds}秒"
                            )
                            ui.label(
                                f"Bot {save.bot_count}体・{timer}・{save.move_count}手"
                            ).classes("platform-muted")
                            ui.label(
                                f"対局ID: {save.game_id}"
                            ).classes("platform-muted")
                            ui.link(
                                "この対局を再開", f"/play/{save.game_id}"
                            ).classes("platform-link")
                        ui.label(
                            f"保存日時: {save.updated_at.astimezone(timezone.utc).isoformat()}"
                        ).classes("platform-muted")

    @app.post("/auth/logout")
    async def logout_action(request: Request):
        form = await _read_form(request)
        token = request.cookies.get(settings.session_cookie_name, "")
        try:
            principal = await asyncio.to_thread(
                auth.authenticate_session, token
            )
        except InvalidSessionError:
            principal = None
        if (
            principal is None
            or not _same_origin(request)
            or not csrf.verify(
                form.get("csrf_token", ""), principal.session_id
            )
        ):
            raise HTTPException(status_code=403, detail="invalid logout")
        await asyncio.to_thread(auth.logout, token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(
            settings.session_cookie_name,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return response


__all__ = [
    "AuthWebServices",
    "CsrfProtector",
    "LoginAttemptLimiter",
    "PasswordWorkLimiter",
    "register_auth_pages",
    "session_principal_from_request",
]
