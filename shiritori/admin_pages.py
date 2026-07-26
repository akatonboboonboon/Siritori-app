"""NiceGUI pages reserved for server-configured administrators."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from nicegui import ui
from starlette.responses import RedirectResponse

from .auth import AuthService, SessionPrincipal
from .settings import Settings
from .web_auth import _page_shell, session_principal_from_request
from .word_review import (
    PendingSuggestionGroup,
    ReviewDecision,
    ReviewedSuggestionView,
    WordReviewAuthorizationError,
    WordReviewConflictError,
    WordReviewError,
    WordReviewNotFoundError,
    WordReviewService,
    WordReviewValidationError,
)


LOGGER = logging.getLogger(__name__)
_ADMIN_CSS = (
    Path(__file__).parent.parent / "assets" / "admin_pages.css"
).read_text(encoding="utf-8")
_ADMIN_PATH = "/admin/word-suggestions"
_LOGIN_REDIRECT = f"/login?next={_ADMIN_PATH}"
_JST = ZoneInfo("Asia/Tokyo")


def _admin_datetime(value: datetime) -> str:
    """Format a service-owned aware timestamp without exposing internals."""

    if value.tzinfo is None:
        # SQLite drops timezone metadata but stores our UTC wall-clock value.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_JST).strftime("%Y/%m/%d %H:%M")


def _decision_label(status: str) -> str:
    return "承認" if status == ReviewDecision.APPROVED.value else "却下"


def _review_error_message(error: Exception) -> str:
    """Return UI-safe Japanese feedback for known service failures."""

    if isinstance(error, WordReviewConflictError):
        return "別の審査結果がすでに保存されています。一覧を更新しました。"
    if isinstance(error, WordReviewNotFoundError):
        return "対象の申請はすでに処理されたようです。一覧を更新しました。"
    if isinstance(error, WordReviewValidationError):
        if error.field == "review_note":
            return "審査メモは200文字以内で、改行なしで入力してください。"
        return "審査対象を確認できませんでした。一覧を更新してください。"
    if isinstance(error, WordReviewError):
        return "審査を完了できませんでした。画面を更新してお試しください。"
    return "一時的な問題で審査を完了できませんでした。"


async def _fresh_admin_principal(
    request: Request,
    *,
    auth: AuthService,
    settings: Settings,
    word_review: WordReviewService,
) -> SessionPrincipal | None:
    """Revalidate both the login session and administrator allowlist."""

    principal = await session_principal_from_request(request, auth, settings)
    if principal is None:
        return None
    try:
        allowed = await asyncio.to_thread(
            word_review.is_admin,
            principal.account.id,
        )
    except Exception:
        LOGGER.exception("failed to verify administrator authorization")
        return None
    return principal if allowed else None


def register_admin_pages(
    *,
    auth: AuthService,
    settings: Settings,
    word_review: WordReviewService,
) -> None:
    """Register the private grouped word-suggestion review screen."""

    @ui.page(_ADMIN_PATH)
    async def word_suggestion_review_page(request: Request):
        principal = await session_principal_from_request(
            request,
            auth,
            settings,
        )
        if principal is None:
            return RedirectResponse(_LOGIN_REDIRECT, status_code=303)

        try:
            administrator = await asyncio.to_thread(
                word_review.is_admin,
                principal.account.id,
            )
        except Exception:
            LOGGER.exception("failed to verify administrator authorization")
            return RedirectResponse("/lobby", status_code=303)
        if not administrator:
            return RedirectResponse("/lobby", status_code=303)

        try:
            pending, recent = await asyncio.gather(
                asyncio.to_thread(
                    word_review.list_pending_groups,
                    principal.account.id,
                ),
                asyncio.to_thread(
                    word_review.list_recent_reviews,
                    principal.account.id,
                ),
            )
        except WordReviewAuthorizationError:
            return RedirectResponse("/lobby", status_code=303)
        except Exception:
            LOGGER.exception("failed to load the word review page")
            pending = ()
            recent = ()
            initial_error = (
                "審査一覧を読み込めませんでした。"
                "「一覧を更新」でもう一度お試しください。"
            )
        else:
            initial_error = ""

        _page_shell()
        ui.add_css(_ADMIN_CSS)
        busy_groups: set[tuple[str, str]] = set()

        with ui.element("main").classes("platform-shell"):
            with ui.column().classes(
                "platform-wrap admin-review-shell"
            ):
                with ui.column().classes("admin-review-header"):
                    ui.link("← ロビーへ", "/lobby").classes(
                        "platform-link admin-review-back"
                    )
                    ui.label("単語申請の審査").classes("auth-title")
                    ui.label(
                        "同じ表記と読みの申請をまとめて承認・却下します。"
                        "承認した単語は、人が入力したときの辞書判定に反映されます。"
                    ).classes("platform-muted")

                feedback = ui.label(initial_error).classes(
                    "admin-review-feedback "
                    + ("auth-error" if initial_error else "platform-muted")
                ).props(
                    "role='status' aria-live='polite' aria-atomic='true'"
                )

                with ui.row().classes("admin-review-toolbar"):
                    pending_summary = ui.label("").classes("aside-title")
                    reload_button = ui.button(
                        "一覧を更新",
                        icon="refresh",
                    ).props(
                        "outline no-caps aria-label='審査一覧を更新する'"
                    ).classes("admin-review-reload")

                with ui.element("div").classes("admin-review-layout"):
                    pending_container = ui.column().classes(
                        "admin-review-section"
                    )
                    recent_container = ui.column().classes(
                        "admin-review-section"
                    )

        current_pending = tuple(pending)
        current_recent = tuple(recent)

        async def authorize_action() -> SessionPrincipal | None:
            fresh = await _fresh_admin_principal(
                request,
                auth=auth,
                settings=settings,
                word_review=word_review,
            )
            if (
                fresh is None
                or fresh.account.id != principal.account.id
            ):
                return None
            return fresh

        def render_recent(
            rows: tuple[ReviewedSuggestionView, ...],
        ) -> None:
            recent_container.clear()
            with recent_container:
                ui.label("最近の審査").classes("aside-title")
                if not rows:
                    ui.label("まだ審査履歴はありません。").classes(
                        "platform-muted admin-review-empty"
                    )
                    return
                with ui.column().classes("dashboard-card w-full"):
                    for row in rows:
                        with ui.column().classes(
                            "admin-review-recent-row"
                        ):
                            ui.label(
                                f"{_decision_label(row.status)}："
                                f"{row.surface}（{row.reading}）"
                            ).classes("admin-review-person")
                            ui.label(
                                f"申請者: {row.submitter_display_name}"
                            ).classes(
                                "platform-muted admin-review-person"
                            )
                            ui.label(
                                f"審査者: {row.reviewer_display_name}・"
                                f"{_admin_datetime(row.reviewed_at)}"
                            ).classes(
                                "platform-muted admin-review-person"
                            )
                            if row.review_note:
                                ui.label(
                                    f"審査メモ: {row.review_note}"
                                ).classes(
                                    "platform-muted admin-review-note"
                                )

        async def review(
            group: PendingSuggestionGroup,
            decision: ReviewDecision,
            review_note: str | None,
            controls: tuple,
        ) -> None:
            key = (group.surface, group.reading)
            if key in busy_groups:
                return
            busy_groups.add(key)
            for control in controls:
                control.disable()
            should_refresh = False
            try:
                fresh = await authorize_action()
                if fresh is None:
                    ui.navigate.to("/lobby")
                    return
                result = await asyncio.to_thread(
                    word_review.review_group,
                    fresh.account.id,
                    group.surface,
                    group.reading,
                    decision,
                    review_note,
                )
                action = (
                    "承認"
                    if result.decision is ReviewDecision.APPROVED
                    else "却下"
                )
                feedback.text = (
                    f"「{result.surface}」を{action}しました"
                    f"（{result.reviewed_count}件）。"
                )
                feedback.classes(
                    remove="auth-error", add="platform-muted"
                )
                should_refresh = True
            except WordReviewAuthorizationError:
                ui.navigate.to("/lobby")
                return
            except (
                WordReviewConflictError,
                WordReviewNotFoundError,
                WordReviewValidationError,
                WordReviewError,
            ) as error:
                feedback.text = _review_error_message(error)
                feedback.classes(
                    remove="platform-muted", add="auth-error"
                )
                should_refresh = isinstance(
                    error,
                    (
                        WordReviewConflictError,
                        WordReviewNotFoundError,
                    ),
                )
            except Exception as error:
                LOGGER.exception("failed to review a word suggestion group")
                feedback.text = _review_error_message(error)
                feedback.classes(
                    remove="platform-muted", add="auth-error"
                )
            finally:
                busy_groups.discard(key)
                if not should_refresh:
                    for control in controls:
                        control.enable()

            if should_refresh:
                await refresh_lists(require_fresh_admin=True)

        def render_pending(
            groups: tuple[PendingSuggestionGroup, ...],
        ) -> None:
            pending_container.clear()
            pending_summary.text = f"未審査 {len(groups)}グループ"
            with pending_container:
                ui.label("未審査の申請").classes("aside-title")
                if not groups:
                    ui.label(
                        "現在、審査待ちの申請はありません。"
                    ).classes("platform-muted admin-review-empty").props(
                        "role='status'"
                    )
                    return

                for group in groups:
                    with ui.column().classes(
                        "admin-review-card"
                    ).props("role='group'"):
                        ui.label(group.surface).classes(
                            "admin-review-word"
                        )
                        ui.label(f"よみ: {group.reading}").classes(
                            "platform-muted admin-review-reading"
                        )
                        ui.label(
                            f"{group.submission_count}件の申請・"
                            f"最終申請 {_admin_datetime(group.last_submitted_at)}"
                        ).classes("platform-muted")

                        with ui.column().classes(
                            "admin-review-submissions"
                        ):
                            for submission in group.submissions:
                                with ui.column().classes(
                                    "admin-review-submission"
                                ):
                                    ui.label(
                                        submission.submitter_display_name
                                    ).classes(
                                        "admin-review-person"
                                    )
                                    ui.label(
                                        _admin_datetime(
                                            submission.created_at
                                        )
                                    ).classes("platform-muted")
                                    ui.label(
                                        submission.note
                                        or "申請メモなし"
                                    ).classes(
                                        "platform-muted admin-review-note"
                                    )

                        note_input = ui.textarea(
                            label="審査メモ（任意）",
                            placeholder="判断理由など（200文字以内）",
                        ).props(
                            "outlined autogrow maxlength=200 counter "
                            "aria-label='審査メモ（任意、200文字以内）'"
                        ).classes("w-full")

                        controls: list = []

                        async def approve(
                            _event=None,
                            *,
                            item=group,
                            note=note_input,
                            buttons=controls,
                        ) -> None:
                            await review(
                                item,
                                ReviewDecision.APPROVED,
                                note.value,
                                tuple(buttons),
                            )

                        async def reject(
                            _event=None,
                            *,
                            item=group,
                            note=note_input,
                            buttons=controls,
                        ) -> None:
                            await review(
                                item,
                                ReviewDecision.REJECTED,
                                note.value,
                                tuple(buttons),
                            )

                        with ui.element("div").classes(
                            "admin-review-actions"
                        ):
                            controls.append(
                                ui.button(
                                    "承認",
                                    icon="check",
                                    on_click=approve,
                                ).props(
                                    "unelevated no-caps "
                                    "aria-label='この単語申請を承認する'"
                                ).classes("admin-review-action")
                            )
                            controls.append(
                                ui.button(
                                    "却下",
                                    icon="close",
                                    on_click=reject,
                                ).props(
                                    "outline no-caps color=negative "
                                    "aria-label='この単語申請を却下する'"
                                ).classes("admin-review-action")
                            )

        async def refresh_lists(
            *,
            require_fresh_admin: bool,
        ) -> None:
            nonlocal current_pending, current_recent
            reload_button.disable()
            try:
                current = principal
                if require_fresh_admin:
                    current = await authorize_action()
                    if current is None:
                        ui.navigate.to("/lobby")
                        return
                current_pending, current_recent = await asyncio.gather(
                    asyncio.to_thread(
                        word_review.list_pending_groups,
                        current.account.id,
                    ),
                    asyncio.to_thread(
                        word_review.list_recent_reviews,
                        current.account.id,
                    ),
                )
            except WordReviewAuthorizationError:
                ui.navigate.to("/lobby")
                return
            except Exception:
                LOGGER.exception("failed to refresh word review lists")
                feedback.text = (
                    "審査一覧を更新できませんでした。"
                    "少し待ってからもう一度お試しください。"
                )
                feedback.classes(
                    remove="platform-muted", add="auth-error"
                )
                return
            finally:
                reload_button.enable()
            render_pending(current_pending)
            render_recent(current_recent)

        async def manual_refresh(_event=None) -> None:
            feedback.text = ""
            feedback.classes(remove="auth-error", add="platform-muted")
            await refresh_lists(require_fresh_admin=True)

        reload_button.on_click(manual_refresh)
        render_pending(current_pending)
        render_recent(current_recent)


__all__ = ["register_admin_pages"]
