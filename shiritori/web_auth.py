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
from pathlib import Path
import secrets
from threading import Lock
import time
from typing import TypeVar
from urllib.parse import parse_qs, urlencode, urlsplit

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
from .settings import Settings
from .solo import SoloGameService


_PLATFORM_CSS = (
    Path(__file__).parent.parent / "assets" / "platform.css"
).read_text(encoding="utf-8")
_MAX_FORM_BYTES = 8_192
_PasswordResult = TypeVar("_PasswordResult")


@dataclass(frozen=True, slots=True)
class AuthWebServices:
    auth: AuthService
    games: GameRepository
    settings: Settings
    solo: SoloGameService | None = None


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
                        ui.label(
                            "部屋UIは、認証済みサービスAPIへ接続して実装します。"
                        ).classes("platform-muted")
                    with ui.column().classes("dashboard-card"):
                        ui.label("保存したBot戦").classes("aside-title")
                        ui.link("保存一覧を開く", "/saved-games").classes(
                            "platform-link"
                        )
                    with ui.column().classes("dashboard-card"):
                        ui.label("1人で練習").classes("aside-title")
                        ui.link("辞書対応ゲームを開く", "/").classes(
                            "platform-link"
                        )

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
