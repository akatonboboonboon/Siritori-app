"""NiceGUI page for the shiritori game."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

from .customize import (
    ACCENT_COLOR,
    APP_DESCRIPTION,
    APP_KICKER,
    APP_TITLE,
    PRIMARY_COLOR,
    REPOSITORY_URL,
    SECONDARY_COLOR,
    START_WORD,
)
from .game import GameState, TurnCode, TurnResult


_STYLES = (Path(__file__).parent.parent / "assets" / "styles.css").read_text(
    encoding="utf-8"
)
_FEEDBACK_TONES = (
    "feedback--neutral feedback--success feedback--error feedback--danger"
)
_STATUS_TONES = "status-pill--active status-pill--ended"


def register_pages() -> None:
    """Register application pages.

    This explicit function keeps importing ``shiritori.page`` side-effect free
    for tools that only need to inspect the module.
    """

    @ui.page("/")
    def game_page() -> None:
        game = GameState(START_WORD)
        ui.page_title(APP_TITLE)
        ui.colors(
            primary=PRIMARY_COLOR,
            secondary=SECONDARY_COLOR,
            accent=ACCENT_COLOR,
        )
        ui.add_head_html(
            "<meta name=\"description\" "
            f"content=\"{APP_DESCRIPTION}\">"
            "<style>:root {"
            f"--brand-primary: {PRIMARY_COLOR};"
            f"--brand-secondary: {SECONDARY_COLOR};"
            f"--brand-accent: {ACCENT_COLOR};"
            "}</style>"
        )
        ui.add_css(_STYLES)

        def draw_history() -> None:
            history_list.clear()
            with history_list:
                for index, word in enumerate(game.history):
                    with ui.element("li").classes("history-item"):
                        ui.label("START" if index == 0 else f"{index:02d}").classes(
                            "history-index"
                        )
                        ui.label(word).classes("history-word")
                        if index < len(game.history) - 1:
                            ui.icon("south").classes("history-arrow")

        def render(result: TurnResult | None = None) -> None:
            current_word_label.set_text(game.current_word)
            expected_kana_label.set_text(game.expected_kana)
            turn_count_label.set_text(str(game.turn_count))
            word_count_label.set_text(str(len(game.history)))
            draw_history()

            if game.is_over:
                status_label.set_text("ゲーム終了")
                status_label.classes(
                    remove=_STATUS_TONES, add="status-pill--ended"
                )
                word_input.disable()
                submit_button.disable()
            else:
                status_label.set_text("プレイ中")
                status_label.classes(
                    remove=_STATUS_TONES, add="status-pill--active"
                )
                word_input.enable()
                submit_button.enable()

            if result is None:
                symbol, tone = "💡", "neutral"
                message = (
                    f"最初のことばは「{game.current_word}」。"
                    f"「{game.expected_kana}」から始めましょう。"
                )
            elif result.code is TurnCode.ACCEPTED:
                symbol, tone, message = "✓", "success", result.message
            elif result.game_over:
                symbol, tone, message = "!", "danger", result.message
            else:
                symbol, tone, message = "!", "error", result.message

            feedback_symbol.set_text(symbol)
            feedback_text.set_text(message)
            feedback_box.classes(
                remove=_FEEDBACK_TONES, add=f"feedback--{tone}"
            )

        def submit_word(_event: object | None = None) -> None:
            result = game.submit(word_input.value)
            if result.accepted or result.game_over:
                word_input.set_value("")
            render(result)
            if not game.is_over:
                word_input.run_method("focus")

        def reset_game(_event: object | None = None) -> None:
            game.reset()
            word_input.set_value("")
            render()
            ui.notify(
                "最初から始めました。",
                type="positive",
                position="top",
                timeout=1600,
            )
            word_input.run_method("focus")

        with ui.element("main").classes("app-shell"):
            ui.element("div").classes("background-orb background-orb--one")
            ui.element("div").classes("background-orb background-orb--two")

            with ui.element("div").classes("page-wrap"):
                with ui.element("header").classes("topbar"):
                    with ui.column().classes("brand-block"):
                        ui.label("SHIRITORI").classes("brand-eyebrow")
                        ui.label(APP_TITLE).classes("brand-title")
                    ui.button(
                        "もう一度",
                        icon="replay",
                        on_click=reset_game,
                    ).props("flat no-caps aria-label='ゲームを最初からやり直す'").classes(
                        "reset-button"
                    )

                with ui.element("section").classes("intro"):
                    ui.label(APP_KICKER).classes("intro-title")
                    ui.label(APP_DESCRIPTION).classes("intro-copy")

                with ui.element("section").classes("content-grid"):
                    with ui.element("section").classes("game-card"):
                        with ui.row().classes("card-heading"):
                            ui.label("いまのことば").classes("section-label")
                            status_label = ui.label("プレイ中").classes(
                                "status-pill status-pill--active"
                            )

                        current_word_label = ui.label(game.current_word).classes(
                            "current-word"
                        )

                        with ui.row().classes("next-hint"):
                            ui.label("つぎは").classes("next-hint-label")
                            expected_kana_label = ui.label(
                                game.expected_kana
                            ).classes("expected-kana")
                            ui.label("から").classes("next-hint-label")

                        with ui.row().classes("input-row"):
                            word_input = (
                                ui.input(
                                    label="次のことば",
                                    placeholder="ひらがなで入力",
                                )
                                .props(
                                    "outlined clearable maxlength=32 "
                                    "autocomplete=off aria-label='次のことば'"
                                )
                                .classes("word-input")
                            )
                            submit_button = (
                                ui.button(
                                    "つなぐ",
                                    icon="arrow_forward",
                                    on_click=submit_word,
                                )
                                .props("unelevated no-caps")
                                .classes("submit-button")
                            )
                            word_input.on("keydown.enter", submit_word)

                        feedback_box = ui.row().classes(
                            "feedback feedback--neutral"
                        ).props("role='status' aria-live='polite'")
                        with feedback_box:
                            feedback_symbol = ui.label("💡").classes(
                                "feedback-symbol"
                            )
                            feedback_text = ui.label("").classes("feedback-text")

                        with ui.row().classes("stats-row"):
                            with ui.column().classes("stat"):
                                turn_count_label = ui.label("0").classes(
                                    "stat-value"
                                )
                                ui.label("つないだ回数").classes("stat-label")
                            ui.element("div").classes("stat-divider")
                            with ui.column().classes("stat"):
                                word_count_label = ui.label("1").classes(
                                    "stat-value"
                                )
                                ui.label("履歴のことば").classes("stat-label")

                    with ui.element("aside").classes("side-column"):
                        with ui.element("section").classes("history-card"):
                            with ui.row().classes("aside-heading"):
                                ui.icon("route").classes("aside-icon")
                                with ui.column().classes("aside-title-block"):
                                    ui.label("ことばの道").classes("aside-title")
                                    ui.label("これまでの履歴").classes(
                                        "aside-subtitle"
                                    )
                            history_list = ui.element("ol").classes("history-list")

                        with ui.element("section").classes("rules-card"):
                            with ui.row().classes("aside-heading"):
                                ui.icon("menu_book").classes("aside-icon")
                                with ui.column().classes("aside-title-block"):
                                    ui.label("あそびかた").classes("aside-title")
                                    ui.label("3つのルール").classes("aside-subtitle")
                            with ui.element("ul").classes("rules-list"):
                                for number, text in (
                                    ("1", "最後のひらがなで、ことばをつなぐ"),
                                    ("2", "「ん」で終わるとゲーム終了"),
                                    ("3", "一度使ったことばは使えない"),
                                ):
                                    with ui.element("li").classes("rule-item"):
                                        ui.label(number).classes("rule-number")
                                        ui.label(text).classes("rule-copy")

                with ui.element("footer").classes("page-footer"):
                    ui.label("Python + NiceGUI").classes("footer-tech")
                    ui.link(
                        "GitHubでコードを見る",
                        REPOSITORY_URL,
                        new_tab=True,
                    ).classes("footer-link")

        render()
