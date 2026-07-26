"""Authenticated NiceGUI page for the one-attempt daily challenge."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
import logging
from pathlib import Path

from fastapi import Request
from nicegui import ui
from starlette.responses import RedirectResponse

from .auth import AuthService
from .daily_challenge import DailyChallengeCondition, DailyChallengeSession
from .daily_challenge_persistence import (
    DailyChallengeLeaderboardEntry,
    DailyChallengePersistenceError,
    DailyChallengeRunFinishedError,
    DailyChallengeRunNotFoundError,
    DailyChallengeRunOwnershipError,
    DailyChallengeRunView,
    DailyChallengeUserUnavailableError,
    SQLAlchemyDailyChallengeService,
    StaleDailyChallengeStateError,
)
from .game_session import SessionCode
from .score_attack import ScoreAttackStatus
from .settings import Settings
from .web_auth import (
    _deadline_presentation,
    _page_shell,
    session_principal_from_request,
)


LOGGER = logging.getLogger(__name__)
_DAILY_CSS = (
    Path(__file__).parent.parent / "assets" / "daily_pages.css"
).read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class _InitialDailyState:
    """Read-only state gathered when the page is opened."""

    condition: DailyChallengeCondition
    run: DailyChallengeRunView | None


def _load_initial_daily_state(
    service: SQLAlchemyDailyChallengeService,
    user_id: str,
) -> _InitialDailyState:
    """Load the page without creating or consuming today's attempt."""

    today = service.today_condition()
    run = service.current(user_id)
    return _InitialDailyState(
        condition=run.condition if run is not None else today,
        run=run,
    )


def _restore_daily_session(
    run: DailyChallengeRunView,
    service: SQLAlchemyDailyChallengeService,
) -> DailyChallengeSession:
    """Rebuild trusted UI state from the authoritative saved snapshot."""

    return DailyChallengeSession.from_snapshot(
        run.snapshot,
        service.validator,
        expected_condition=run.condition,
    )


def _daily_date_label(challenge_date: date) -> str:
    return (
        f"{challenge_date.year}年{challenge_date.month}月"
        f"{challenge_date.day}日（日本時間）"
    )


def _daily_finish_reason(reason: str | None) -> str:
    return {
        "timeout": "3分が経過しました。",
        "ends_with_n": "「ん」で終わる単語を入力しました。",
        "duplicate": "同じ読みの単語を使いました。",
    }.get(reason, "チャレンジが終了しました。")


def register_daily_pages(
    *,
    auth: AuthService,
    settings: Settings,
    daily_challenge: SQLAlchemyDailyChallengeService,
) -> None:
    """Register the protected ``/daily-challenge`` NiceGUI page."""

    async def principal_for(request: Request):
        return await session_principal_from_request(
            request,
            auth,
            settings,
        )

    @ui.page("/daily-challenge")
    async def daily_challenge_page(request: Request):
        principal = await principal_for(request)
        if principal is None:
            return RedirectResponse(
                "/login?next=/daily-challenge",
                status_code=303,
            )

        user_id = principal.account.id
        try:
            initial = await asyncio.to_thread(
                _load_initial_daily_state,
                daily_challenge,
                user_id,
            )
        except DailyChallengeUserUnavailableError:
            return RedirectResponse(
                "/login?next=/daily-challenge",
                status_code=303,
            )
        except Exception:
            LOGGER.exception("failed to load daily challenge")
            return RedirectResponse("/lobby", status_code=303)

        initial_ranking: tuple[DailyChallengeLeaderboardEntry, ...] = ()
        if (
            initial.run is not None
            and initial.run.status == ScoreAttackStatus.FINISHED.value
        ):
            try:
                initial_ranking = await asyncio.to_thread(
                    daily_challenge.list_daily_leaderboard,
                    initial.run.challenge_date,
                    limit=50,
                )
            except Exception:
                LOGGER.exception("failed to load initial daily leaderboard")

        _page_shell()
        ui.add_css(_DAILY_CSS)

        busy = False
        current_run = initial.run
        page_timer = None

        with ui.element("main").classes(
            "platform-shell daily-page-shell"
        ):
            with ui.column().classes(
                "platform-wrap daily-page-wrap"
            ):
                with ui.row().classes(
                    "platform-nav daily-nav w-full items-center"
                ):
                    ui.link("← ロビー", "/lobby").classes("platform-link")
                    ui.link("遊び方", "/tutorial?next=/daily-challenge").classes(
                        "platform-link"
                    )
                    ui.link("自分の戦績", "/stats").classes("platform-link")

                ui.label("本日のチャレンジ").classes("auth-title")
                ui.label(
                    "全員が同じ条件で挑む、1日1回のスコアアタックです。"
                ).classes("platform-muted")

                with ui.column().classes(
                    "dashboard-card daily-rules-card w-full"
                ):
                    ui.label("今日の共通ルール").classes("aside-title")
                    condition_date_label = ui.label(
                        _daily_date_label(initial.condition.challenge_date)
                    ).classes("daily-condition-date")
                    ui.label(
                        "制限時間は3分。開始語と読みは全員共通です。"
                    ).classes("platform-muted")
                    with ui.row().classes(
                        "daily-seed w-full items-center justify-between"
                    ):
                        with ui.column().classes("gap-1 min-w-0"):
                            ui.label("開始語").classes("platform-muted")
                            seed_surface_label = ui.label(
                                initial.condition.start_surface
                            ).classes("daily-seed-surface")
                        with ui.column().classes("gap-1 min-w-0"):
                            ui.label("読み").classes("platform-muted")
                            seed_reading_label = ui.label(
                                initial.condition.start_reading
                            ).classes("daily-seed-reading")
                    ui.label(
                        "この画面を開いただけでは挑戦回数を消費しません。"
                        "「挑戦開始」を押した瞬間から計測します。"
                    ).classes("daily-start-notice")

                with ui.column().classes(
                    "dashboard-card daily-play-card w-full"
                ):
                    with ui.row().classes(
                        "daily-summary w-full items-center gap-3"
                    ):
                        with ui.column().classes("score-metric"):
                            score_label = ui.label("0").classes(
                                "score-value"
                            )
                            ui.label("スコア").classes("platform-muted")
                        with ui.column().classes("score-metric"):
                            count_label = ui.label("0").classes(
                                "stat-value"
                            )
                            ui.label("成功した単語").classes(
                                "platform-muted"
                            )
                        deadline_label = ui.label("開始前").classes(
                            "deadline-label deadline--normal"
                        )

                    expected_label = ui.label(
                        f"開始語「{initial.condition.start_reading}」の"
                        f"最後は「{initial.condition.expected_kana}」です。"
                    ).classes("aside-title")
                    feedback_label = ui.label("").classes(
                        "platform-muted daily-feedback"
                    ).props("role='status' aria-live='polite' aria-atomic=true")

                    with ui.column().classes(
                        "daily-before-panel w-full gap-3"
                    ) as before_panel:
                        ui.label(
                            "公式記録は1日につき1回だけです。"
                            "開始後は画面を閉じても時計が進みます。"
                        ).classes("platform-muted")
                        start_button = ui.button(
                            "挑戦開始",
                            icon="timer",
                        ).props(
                            "unelevated no-caps aria-label='本日の挑戦を開始'"
                        ).classes("daily-action daily-start-button w-full")

                    with ui.column().classes(
                        "daily-active-panel w-full gap-3"
                    ) as active_panel:
                        word_input = ui.input(
                            label="次の単語",
                            placeholder="漢字・ひらがな・カタカナ",
                        ).props(
                            "outlined maxlength=30 autocomplete=off "
                            "aria-label='次の単語'"
                        ).classes("w-full")
                        submit_button = ui.button(
                            "送信",
                            icon="send",
                        ).props(
                            "unelevated no-caps aria-label='単語を送信'"
                        ).classes("daily-action w-full")

                    with ui.column().classes(
                        "daily-finished-panel w-full gap-2"
                    ) as finished_panel:
                        finish_label = ui.label("").classes(
                            "daily-result-title"
                        )
                        result_detail_label = ui.label("").classes(
                            "platform-muted"
                        )
                        ui.label(
                            "本日の公式挑戦は完了です。"
                            "次の挑戦は日本時間の翌日です。"
                        ).classes("daily-complete-notice")

                    ui.separator()
                    ui.label("単語履歴").classes("aside-title")
                    history_box = ui.column().classes(
                        "game-history daily-history w-full gap-2"
                    ).props(
                        "role='log' aria-live='polite' "
                        "aria-relevant='additions'"
                    )

                with ui.column().classes(
                    "dashboard-card daily-ranking-card w-full"
                ) as ranking_panel:
                    ui.label("本日のランキング").classes("aside-title")
                    ui.label(
                        "ランキング公開をオンにした利用者だけを表示します。"
                    ).classes("platform-muted")
                    ranking_box = ui.column().classes(
                        "daily-ranking-list w-full gap-2"
                    )

                with ui.dialog() as reading_dialog, ui.card().classes(
                    "daily-reading-dialog"
                ):
                    ui.label("読みを選んでください").classes("aside-title")
                    ui.label(
                        "選んでいる間も3分の時計は進みます。"
                    ).classes("platform-muted")
                    reading_choices = ui.column().classes("w-full gap-2")
                    cancel_reading_button = ui.button(
                        "入力に戻る",
                        icon="arrow_back",
                    ).props(
                        "outline no-caps aria-label='読み選択を取り消す'"
                    ).classes("daily-action w-full")
                reading_dialog.props("persistent")

                def restored_session(
                    run: DailyChallengeRunView | None,
                ) -> DailyChallengeSession | None:
                    if run is None:
                        return None
                    try:
                        return _restore_daily_session(
                            run,
                            daily_challenge,
                        )
                    except ValueError:
                        LOGGER.exception(
                            "daily challenge snapshot failed UI validation"
                        )
                        return None

                def render_condition(
                    condition: DailyChallengeCondition,
                ) -> None:
                    condition_date_label.set_text(
                        _daily_date_label(condition.challenge_date)
                    )
                    seed_surface_label.set_text(condition.start_surface)
                    seed_reading_label.set_text(condition.start_reading)

                def render_history(
                    attack: DailyChallengeSession | None,
                ) -> None:
                    history_box.clear()
                    with history_box:
                        if attack is None or not attack.history:
                            ui.label(
                                "まだ入力した単語はありません。"
                            ).classes("platform-muted")
                            return
                        numbered = tuple(enumerate(attack.history, start=1))
                        for display_number, entry in reversed(numbered):
                            with ui.row().classes(
                                "game-history-row w-full items-center "
                                "justify-between gap-3"
                            ):
                                with ui.column().classes("min-w-0 gap-1"):
                                    ui.label(entry.surface).classes(
                                        "aside-title"
                                    )
                                    ui.label(entry.reading).classes(
                                        "platform-muted"
                                    )
                                result_text = (
                                    "終了語"
                                    if entry.result.value == "ends_with_n"
                                    else f"{display_number}語目"
                                )
                                ui.label(result_text).classes(
                                    "platform-muted"
                                )

                def render_ranking(
                    entries: tuple[
                        DailyChallengeLeaderboardEntry, ...
                    ],
                ) -> None:
                    ranking_box.clear()
                    with ranking_box:
                        if not entries:
                            ui.label(
                                "公開済みの結果はまだありません。"
                            ).classes("platform-muted")
                            return
                        for entry in entries:
                            with ui.row().classes(
                                "daily-ranking-row w-full items-center gap-3"
                            ):
                                ui.label(f"{entry.rank}位").classes(
                                    "daily-rank"
                                )
                                with ui.column().classes(
                                    "daily-ranking-player min-w-0 gap-1"
                                ):
                                    ui.label(entry.display_name).classes(
                                        "aside-title"
                                    )
                                    ui.label(
                                        f"{entry.accepted_count}語成功"
                                    ).classes("platform-muted")
                                ui.label(f"{entry.score}点").classes(
                                    "stat-value"
                                )

                def render_reading_choices(
                    attack: DailyChallengeSession | None,
                ) -> None:
                    reading_choices.clear()
                    pending = (
                        attack.pending_reading
                        if attack is not None
                        else None
                    )
                    if pending is None:
                        reading_dialog.close()
                        return
                    with reading_choices:
                        for reading in pending.readings:
                            ui.button(
                                reading,
                                on_click=lambda _event=None, selected=reading: (
                                    choose_reading(selected)
                                ),
                            ).props(
                                "outline no-caps "
                                f"aria-label='読み {reading} を選ぶ'"
                            ).classes("daily-action w-full")
                    reading_dialog.open()

                def update_deadline(
                    run: DailyChallengeRunView | None,
                ) -> bool:
                    if (
                        run is None
                        or run.status != ScoreAttackStatus.ACTIVE.value
                    ):
                        return False
                    deadline = _deadline_presentation(run.deadline_at)
                    deadline_label.set_text(deadline.text)
                    deadline_label.classes(
                        add=f"deadline--{deadline.level}",
                        remove=(
                            "deadline--normal deadline--warning "
                            "deadline--danger"
                        ),
                    )
                    return deadline.expired

                def render_run(
                    run: DailyChallengeRunView | None,
                    message: str | None = None,
                ) -> None:
                    nonlocal current_run
                    current_run = run
                    attack = restored_session(run)
                    condition = (
                        run.condition
                        if run is not None
                        else initial.condition
                    )
                    render_condition(condition)
                    score_label.set_text(str(run.score if run else 0))
                    count_label.set_text(
                        str(run.accepted_count if run else 0)
                    )
                    render_history(attack)
                    render_reading_choices(attack)

                    is_active = (
                        run is not None
                        and run.status == ScoreAttackStatus.ACTIVE.value
                        and attack is not None
                    )
                    before_panel.set_visibility(run is None)
                    active_panel.set_visibility(is_active)
                    finished_panel.set_visibility(
                        run is not None and not is_active
                    )
                    ranking_panel.set_visibility(
                        run is not None and not is_active
                    )

                    if run is None:
                        deadline_label.set_text("開始前")
                        deadline_label.classes(
                            add="deadline--normal",
                            remove=(
                                "deadline--warning deadline--danger"
                            ),
                        )
                        expected_label.set_text(
                            f"開始語「{condition.start_reading}」の"
                            f"最後は「{condition.expected_kana}」です。"
                        )
                        feedback_label.set_text(message or "")
                        return

                    if is_active and attack is not None:
                        update_deadline(run)
                        expected_label.set_text(
                            f"「{attack.expected_kana}」から"
                            "始めてください"
                            if attack.expected_kana is not None
                            else "次の単語を入力してください"
                        )
                        feedback_label.set_text(
                            message
                            or "時計はサーバー側で進んでいます。"
                        )
                        word_input.enable()
                        submit_button.enable()
                        if page_timer is not None:
                            page_timer.activate()
                        return

                    deadline_label.set_text("終了")
                    deadline_label.classes(
                        add="deadline--normal",
                        remove="deadline--warning deadline--danger",
                    )
                    reason = _daily_finish_reason(run.finish_reason)
                    finish_label.set_text(
                        f"{run.score}点・{run.accepted_count}語成功"
                    )
                    result_detail_label.set_text(reason)
                    expected_label.set_text("本日の挑戦は終了しました。")
                    feedback_label.set_text(message or reason)
                    word_input.disable()
                    submit_button.disable()
                    if page_timer is not None:
                        page_timer.deactivate()

                async def page_session_valid() -> bool:
                    current_principal = await principal_for(request)
                    if (
                        current_principal is not None
                        and current_principal.account.id == user_id
                    ):
                        return True
                    if page_timer is not None:
                        page_timer.deactivate()
                    reading_dialog.close()
                    ui.navigate.to("/login?next=/daily-challenge")
                    return False

                async def refresh_ranking(
                    run: DailyChallengeRunView,
                ) -> None:
                    try:
                        entries = await asyncio.to_thread(
                            daily_challenge.list_daily_leaderboard,
                            run.challenge_date,
                            limit=50,
                        )
                    except Exception:
                        LOGGER.exception(
                            "failed to refresh daily leaderboard"
                        )
                        render_ranking(())
                        return
                    render_ranking(entries)

                async def refresh_after_conflict() -> None:
                    nonlocal current_run
                    if current_run is None:
                        return
                    try:
                        latest = await asyncio.to_thread(
                            daily_challenge.get,
                            user_id,
                            current_run.id,
                        )
                    except (
                        DailyChallengeRunNotFoundError,
                        DailyChallengeRunOwnershipError,
                        DailyChallengeUserUnavailableError,
                    ):
                        ui.navigate.to("/daily-challenge")
                        return
                    except Exception:
                        LOGGER.exception(
                            "failed to refresh stale daily challenge"
                        )
                        ui.navigate.to("/daily-challenge")
                        return
                    render_run(
                        latest,
                        "別の画面で行われた操作を反映しました。",
                    )
                    if latest.status == ScoreAttackStatus.FINISHED.value:
                        await refresh_ranking(latest)

                async def start_run() -> None:
                    nonlocal busy
                    if busy or current_run is not None:
                        return
                    busy = True
                    start_button.disable()
                    try:
                        if not await page_session_valid():
                            return
                        started = await asyncio.to_thread(
                            daily_challenge.start_today,
                            user_id,
                        )
                        render_run(
                            started,
                            "開始しました。3分間でつないでください。",
                        )
                        if (
                            started.status
                            == ScoreAttackStatus.ACTIVE.value
                        ):
                            word_input.set_value("")
                            word_input.run_method("focus")
                        else:
                            await refresh_ranking(started)
                    except (
                        DailyChallengePersistenceError,
                        DailyChallengeUserUnavailableError,
                    ):
                        LOGGER.exception("failed to start daily challenge")
                        feedback_label.set_text(
                            "開始できませんでした。"
                            "再読み込みしてお試しください。"
                        )
                    except Exception:
                        LOGGER.exception(
                            "unexpected daily challenge start failure"
                        )
                        feedback_label.set_text(
                            "開始できませんでした。"
                            "再読み込みしてお試しください。"
                        )
                    finally:
                        busy = False
                        if current_run is None:
                            start_button.enable()

                async def submit_word() -> None:
                    nonlocal busy
                    if (
                        busy
                        or current_run is None
                        or current_run.status
                        != ScoreAttackStatus.ACTIVE.value
                    ):
                        return
                    busy = True
                    submit_button.disable()
                    try:
                        if not await page_session_valid():
                            return
                        outcome = await asyncio.to_thread(
                            daily_challenge.submit,
                            user_id=user_id,
                            run_id=current_run.id,
                            surface=str(word_input.value or ""),
                            expected_version=current_run.state_version,
                        )
                        result = outcome.result
                        render_run(
                            outcome.run,
                            result.message if result is not None else None,
                        )
                        if (
                            result is not None
                            and result.code
                            not in {
                                SessionCode.LEXICON_REJECTED,
                                SessionCode.NOT_CHAINED,
                                SessionCode.INVALID_LEXICON_RESULT,
                            }
                        ):
                            word_input.set_value("")
                        if (
                            outcome.run.status
                            == ScoreAttackStatus.ACTIVE.value
                            and outcome.run is current_run
                        ):
                            word_input.run_method("focus")
                        if (
                            outcome.run.status
                            == ScoreAttackStatus.FINISHED.value
                        ):
                            await refresh_ranking(outcome.run)
                    except StaleDailyChallengeStateError:
                        await refresh_after_conflict()
                    except DailyChallengeRunFinishedError:
                        await refresh_after_conflict()
                    except (
                        DailyChallengePersistenceError,
                        DailyChallengeRunNotFoundError,
                        DailyChallengeRunOwnershipError,
                        DailyChallengeUserUnavailableError,
                    ):
                        LOGGER.exception(
                            "daily challenge submission failed"
                        )
                        feedback_label.set_text(
                            "送信できませんでした。"
                            "再読み込みしてお試しください。"
                        )
                    finally:
                        busy = False
                        if (
                            current_run is not None
                            and current_run.status
                            == ScoreAttackStatus.ACTIVE.value
                        ):
                            submit_button.enable()

                async def choose_reading(reading: str) -> None:
                    nonlocal busy
                    if (
                        busy
                        or current_run is None
                        or current_run.status
                        != ScoreAttackStatus.ACTIVE.value
                    ):
                        return
                    busy = True
                    submit_button.disable()
                    cancel_reading_button.disable()
                    try:
                        if not await page_session_valid():
                            return
                        outcome = await asyncio.to_thread(
                            daily_challenge.resolve_reading,
                            user_id=user_id,
                            run_id=current_run.id,
                            reading=reading,
                            expected_version=current_run.state_version,
                        )
                        render_run(
                            outcome.run,
                            outcome.result.message
                            if outcome.result is not None
                            else None,
                        )
                        if (
                            outcome.run.status
                            == ScoreAttackStatus.FINISHED.value
                        ):
                            await refresh_ranking(outcome.run)
                    except (
                        StaleDailyChallengeStateError,
                        DailyChallengeRunFinishedError,
                    ):
                        await refresh_after_conflict()
                    except (
                        DailyChallengePersistenceError,
                        DailyChallengeRunNotFoundError,
                        DailyChallengeRunOwnershipError,
                        DailyChallengeUserUnavailableError,
                    ):
                        LOGGER.exception(
                            "daily challenge reading choice failed"
                        )
                        feedback_label.set_text(
                            "読みを確定できませんでした。"
                            "再読み込みしてください。"
                        )
                    finally:
                        busy = False
                        cancel_reading_button.enable()
                        if (
                            current_run is not None
                            and current_run.status
                            == ScoreAttackStatus.ACTIVE.value
                        ):
                            submit_button.enable()

                async def cancel_reading() -> None:
                    nonlocal busy
                    if (
                        busy
                        or current_run is None
                        or current_run.status
                        != ScoreAttackStatus.ACTIVE.value
                    ):
                        return
                    busy = True
                    cancel_reading_button.disable()
                    try:
                        if not await page_session_valid():
                            return
                        outcome = await asyncio.to_thread(
                            daily_challenge.cancel_reading_choice,
                            user_id=user_id,
                            run_id=current_run.id,
                            expected_version=current_run.state_version,
                        )
                        render_run(
                            outcome.run,
                            outcome.result.message
                            if outcome.result is not None
                            else None,
                        )
                        word_input.run_method("focus")
                    except (
                        StaleDailyChallengeStateError,
                        DailyChallengeRunFinishedError,
                    ):
                        await refresh_after_conflict()
                    except (
                        DailyChallengePersistenceError,
                        DailyChallengeRunNotFoundError,
                        DailyChallengeRunOwnershipError,
                        DailyChallengeUserUnavailableError,
                    ):
                        LOGGER.exception(
                            "daily challenge reading cancellation failed"
                        )
                        feedback_label.set_text(
                            "読み選択を取り消せませんでした。"
                            "再読み込みしてください。"
                        )
                    finally:
                        busy = False
                        cancel_reading_button.enable()

                async def tick_daily_challenge() -> None:
                    nonlocal busy
                    if (
                        busy
                        or current_run is None
                        or current_run.status
                        != ScoreAttackStatus.ACTIVE.value
                    ):
                        return
                    if not update_deadline(current_run):
                        return
                    busy = True
                    submit_button.disable()
                    try:
                        if not await page_session_valid():
                            return
                        outcome = await asyncio.to_thread(
                            daily_challenge.expire,
                            user_id=user_id,
                            run_id=current_run.id,
                            expected_version=current_run.state_version,
                        )
                        render_run(
                            outcome.run,
                            outcome.result.message
                            if outcome.result is not None
                            else None,
                        )
                        await refresh_ranking(outcome.run)
                    except (
                        StaleDailyChallengeStateError,
                        DailyChallengeRunFinishedError,
                    ):
                        await refresh_after_conflict()
                    except (
                        DailyChallengePersistenceError,
                        DailyChallengeRunNotFoundError,
                        DailyChallengeRunOwnershipError,
                        DailyChallengeUserUnavailableError,
                    ):
                        LOGGER.exception(
                            "daily challenge timeout failed"
                        )
                        feedback_label.set_text(
                            "終了状態を保存できませんでした。"
                            "再読み込みしてください。"
                        )
                    finally:
                        busy = False

                start_button.on("click", start_run)
                submit_button.on("click", submit_word)
                word_input.on("keydown.enter", submit_word)
                cancel_reading_button.on("click", cancel_reading)
                render_ranking(initial_ranking)
                render_run(current_run)
                page_timer = ui.timer(
                    0.5,
                    tick_daily_challenge,
                    active=(
                        current_run is not None
                        and current_run.status
                        == ScoreAttackStatus.ACTIVE.value
                    ),
                )


__all__ = ["register_daily_pages"]
