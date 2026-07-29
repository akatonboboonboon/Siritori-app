"""Authenticated entry page for the high-difficulty Oni mode."""

from __future__ import annotations

import logging

from fastapi import Request
from nicegui import ui
from starlette.responses import RedirectResponse

from .auth import AuthService
from .room_runtime import RoomRuntimeCapabilityError
from .rooms import (
    ONI_BOT_COUNT as REQUIRED_ONI_BOT_COUNT,
    ONI_BOT_DIFFICULTY as REQUIRED_ONI_BOT_DIFFICULTY,
    ONI_LIVES as REQUIRED_ONI_LIVES,
    ONI_TURN_SECONDS as REQUIRED_ONI_TURN_SECONDS,
    RoomRuleSet,
)
from .settings import Settings
from .solo import SoloGameService
from .web_auth import _page_shell, session_principal_from_request


LOGGER = logging.getLogger(__name__)
ONI_BOT_COUNT = REQUIRED_ONI_BOT_COUNT
ONI_BOT_DIFFICULTY = REQUIRED_ONI_BOT_DIFFICULTY
ONI_LIVES = REQUIRED_ONI_LIVES
ONI_TURN_SECONDS = REQUIRED_ONI_TURN_SECONDS


def register_oni_pages(
    *,
    auth: AuthService,
    settings: Settings,
    solo: SoloGameService,
) -> None:
    """Register the protected setup page for Oni shiritori."""

    async def principal_for(request: Request):
        return await session_principal_from_request(request, auth, settings)

    @ui.page("/oni-shiritori")
    async def oni_shiritori_page(request: Request):
        principal = await principal_for(request)
        if principal is None:
            return RedirectResponse(
                "/login?next=/oni-shiritori",
                status_code=303,
            )

        try:
            paused_games = tuple(
                saved
                for saved in await solo.list_paused(principal.account.id)
                if saved.rule_set is RoomRuleSet.ONI
            )
        except Exception:
            LOGGER.exception("failed to load paused Oni games")
            paused_games = ()

        _page_shell()
        starting = False

        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("platform-wrap"):
                with ui.row().classes(
                    "platform-nav w-full items-center gap-3"
                ):
                    ui.link("← ロビー", "/lobby").classes("platform-link")
                    ui.link(
                        "遊び方",
                        "/tutorial?next=/oni-shiritori",
                    ).classes("platform-link")
                    ui.link("ランキング", "/rankings").classes(
                        "platform-link"
                    )

                ui.label("鬼しりとり").classes("auth-title")
                ui.label(
                    "Hard Botにも物足りない人向け。毎ターン変わる"
                    "鬼命令を守りながら、しりとりを続けます。"
                ).classes("platform-muted")

                with ui.column().classes("dashboard-card w-full gap-3"):
                    ui.label("固定ルール").classes("aside-title")
                    ui.label(
                        "Hard Bot 1体・ライフ3・1手30秒。"
                        "読みの音数制限と、直近10語の末尾封印は"
                        "すべての手番に適用されます。"
                    ).classes("platform-muted")
                    ui.label(
                        "1〜4手目は追加の鬼命令が1個、"
                        "5手目以降は最大2個まで同時に適用されます。"
                    ).classes("platform-muted")
                    ui.label(
                        "既知の正解候補が5語以上残る組み合わせだけを出題。"
                        "足りない場合は古い末尾封印から自動で解除します。"
                    ).classes("platform-muted")

                with ui.column().classes("dashboard-card w-full gap-2"):
                    ui.label("登場する7種類の命令").classes("aside-title")
                    commands = (
                        "禁止かな：指定されたかなを含めない",
                        "指定かな：指定されたかなを含める",
                        "指定語尾：指定されたかなで終える",
                        "文字継承：前の単語から指定かなを引き継ぐ",
                        "音の種類：濁音・半濁音・拗音などを含める",
                        "重複禁止：同じかなを単語内で繰り返さない",
                        "末尾封印：直近10語と同じ末尾を使わない",
                    )
                    for command in commands:
                        ui.label(f"・{command}").classes(
                            "platform-muted w-full"
                        )
                    ui.label(
                        "その手番の命令・封印中の末尾・既知の候補数は、"
                        "対局画面に表示されます。答えの単語は表示されません。"
                    ).classes("platform-muted")

                feedback = ui.label("").classes("platform-muted").props(
                    "role='alert' aria-live='assertive'"
                )

                async def start_oni_game() -> None:
                    nonlocal starting
                    if starting:
                        return
                    starting = True
                    start_button.disable()
                    feedback.set_text("")
                    try:
                        current_principal = await principal_for(request)
                        if current_principal is None:
                            ui.navigate.to(
                                "/login?next=/oni-shiritori"
                            )
                            return
                        snapshot = await solo.create(
                            current_principal.account.id,
                            bot_count=ONI_BOT_COUNT,
                            bot_difficulty=ONI_BOT_DIFFICULTY,
                            lives_per_player=ONI_LIVES,
                            turn_seconds=ONI_TURN_SECONDS,
                            rule_set=RoomRuleSet.ONI,
                        )
                    except (
                        TypeError,
                        ValueError,
                        KeyError,
                        RoomRuntimeCapabilityError,
                    ):
                        LOGGER.exception("invalid Oni game setup")
                        feedback.set_text(
                            "鬼しりとりの設定を準備できませんでした。"
                        )
                    except Exception:
                        LOGGER.exception("failed to create Oni game")
                        feedback.set_text(
                            "鬼しりとりを開始できませんでした。"
                            "少し待ってからもう一度お試しください。"
                        )
                    else:
                        ui.navigate.to(f"/play/{snapshot.room_id}")
                        return
                    finally:
                        starting = False
                        start_button.enable()

                start_button = ui.button(
                    "鬼しりとりに挑戦",
                    icon="local_fire_department",
                    on_click=start_oni_game,
                ).props("unelevated no-caps color=negative").classes(
                    "w-full"
                )

                if paused_games:
                    with ui.column().classes(
                        "dashboard-card w-full gap-2"
                    ):
                        ui.label("中断中の鬼しりとり").classes(
                            "aside-title"
                        )
                        for saved in paused_games:
                            timer = (
                                "無制限"
                                if saved.turn_seconds is None
                                else f"{saved.turn_seconds}秒"
                            )
                            with ui.row().classes(
                                "w-full items-center justify-between gap-3"
                            ):
                                ui.label(
                                    f"{saved.move_count}手・{timer}・"
                                    f"ライフ{saved.lives_per_player}"
                                ).classes("platform-muted")
                                ui.link(
                                    "再開",
                                    f"/play/{saved.game_id}",
                                ).classes("platform-link")


__all__ = [
    "ONI_BOT_COUNT",
    "ONI_BOT_DIFFICULTY",
    "ONI_LIVES",
    "ONI_TURN_SECONDS",
    "register_oni_pages",
]
