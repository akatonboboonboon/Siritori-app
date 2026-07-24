"""NiceGUI page for the dictionary-backed shiritori game."""

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
)
from .game_session import GameSession, SessionCode, SessionResult

_STYLES = (Path(__file__).parent.parent / "assets" / "styles.css").read_text(encoding="utf-8")
_FEEDBACK_TONES = "feedback--neutral feedback--success feedback--error feedback--danger"
_STATUS_TONES = "status-pill--active status-pill--ended"


def register_pages() -> None:
    """Register the public dictionary-backed game page."""

    @ui.page("/")
    def game_page() -> None:
        game = GameSession()
        ui.page_title(APP_TITLE)
        ui.colors(primary=PRIMARY_COLOR, secondary=SECONDARY_COLOR, accent=ACCENT_COLOR)
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
                if not game.history:
                    ui.label("最初のことばを自由に入力してください。").classes("history-empty")
                for index, entry in enumerate(game.history, start=1):
                    with ui.element("li").classes("history-item"):
                        ui.label("START" if index == 1 else f"{index:02d}").classes("history-index")
                        with ui.column().classes("history-word-block"):
                            ui.label(entry.surface).classes("history-word")
                            ui.label(f"よみ: {entry.reading}").classes("history-reading")
                        if index < len(game.history):
                            ui.icon("south").classes("history-arrow")

        def render(result: SessionResult | None = None) -> None:
            current = game.current_entry
            current_word_label.set_text(current.surface if current else "—")
            current_reading_label.set_text(
                f"よみ: {current.reading}" if current else "最初のことばは自由です"
            )
            expected_kana_label.set_text(game.expected_kana or "自由")
            turn_count_label.set_text(str(game.turn_count))
            word_count_label.set_text(str(len(game.history)))
            draw_history()

            if game.is_over:
                status_label.set_text("ゲーム終了")
                status_label.classes(remove=_STATUS_TONES, add="status-pill--ended")
                word_input.disable()
                submit_button.disable()
            else:
                status_label.set_text("プレイ中")
                status_label.classes(remove=_STATUS_TONES, add="status-pill--active")
                word_input.enable()
                submit_button.enable()

            if result is None:
                symbol, tone, message = "💡", "neutral", "先攻は、辞書にある好きなことばから始められます。"
            elif result.code is SessionCode.ACCEPTED:
                symbol, tone, message = "✓", "success", result.message
            elif result.code is SessionCode.READING_CHOICE_REQUIRED:
                symbol, tone, message = "?", "neutral", result.message
            elif result.game_over:
                symbol, tone, message = "!", "danger", f"{result.message} ゲーム終了です。"
            else:
                symbol, tone, message = "!", "error", result.message

            feedback_symbol.set_text(symbol)
            feedback_text.set_text(message)
            feedback_box.classes(remove=_FEEDBACK_TONES, add=f"feedback--{tone}")

        def choose_reading(reading: str) -> None:
            result = game.resolve_reading(reading)
            render(result)
            if result.accepted or result.game_over:
                word_input.set_value("")
                reading_dialog.close()
            if not game.is_over:
                word_input.run_method("focus")

        def cancel_reading() -> None:
            result = game.cancel_reading_choice()
            reading_dialog.close()
            render(result)
            word_input.run_method("focus")

        def show_reading_choices(result: SessionResult) -> None:
            reading_choices.clear()
            with reading_choices:
                for reading in result.reading_choices:
                    ui.button(
                        reading,
                        on_click=lambda _event=None, value=reading: choose_reading(value),
                    ).props("outline no-caps").classes("reading-choice-button")
            reading_dialog.open()

        def submit_word(_event: object | None = None) -> None:
            result = game.submit(word_input.value)
            if result.code is SessionCode.READING_CHOICE_REQUIRED:
                show_reading_choices(result)
            if result.accepted or result.game_over:
                word_input.set_value("")
            render(result)
            if not game.is_over:
                word_input.run_method("focus")

        def reset_game(_event: object | None = None) -> None:
            game.reset()
            reading_dialog.close()
            word_input.set_value("")
            render()
            ui.notify("最初から始めました。", type="positive", position="top", timeout=1600)
            word_input.run_method("focus")

        with ui.element("main").classes("app-shell"):
            ui.element("div").classes("background-orb background-orb--one")
            ui.element("div").classes("background-orb background-orb--two")
            with ui.element("div").classes("page-wrap"):
                with ui.element("header").classes("topbar"):
                    with ui.column().classes("brand-block"):
                        ui.label("SHIRITORI").classes("brand-eyebrow")
                        ui.label(APP_TITLE).classes("brand-title")
                    with ui.row().classes("topbar-actions"):
                        ui.link("ログイン", "/login").classes("login-link")
                        ui.button("もう一度", icon="replay", on_click=reset_game).props(
                            "flat no-caps aria-label='ゲームを最初からやり直す'"
                        ).classes("reset-button")

                with ui.element("section").classes("intro"):
                    ui.label(APP_KICKER).classes("intro-title")
                    ui.label(APP_DESCRIPTION).classes("intro-copy")

                with ui.element("section").classes("content-grid"):
                    with ui.element("section").classes("game-card"):
                        with ui.row().classes("card-heading"):
                            ui.label("いまのことば").classes("section-label")
                            status_label = ui.label("プレイ中").classes("status-pill status-pill--active")
                        current_word_label = ui.label("—").classes("current-word")
                        current_reading_label = ui.label("").classes("current-reading")
                        with ui.row().classes("next-hint"):
                            ui.label("つぎは").classes("next-hint-label")
                            expected_kana_label = ui.label("自由").classes("expected-kana")
                            ui.label("から").classes("next-hint-label")

                        with ui.row().classes("input-row"):
                            word_input = ui.input(
                                label="次のことば", placeholder="漢字・ひらがな・カタカナ"
                            ).props(
                                "outlined clearable maxlength=30 autocomplete=off aria-label='次のことば'"
                            ).classes("word-input")
                            submit_button = ui.button(
                                "つなぐ", icon="arrow_forward", on_click=submit_word
                            ).props("unelevated no-caps").classes("submit-button")
                            word_input.on("keydown.enter", submit_word)

                        feedback_box = ui.row().classes("feedback feedback--neutral").props(
                            "role='status' aria-live='polite'"
                        )
                        with feedback_box:
                            feedback_symbol = ui.label("💡").classes("feedback-symbol")
                            feedback_text = ui.label("").classes("feedback-text")

                        with ui.row().classes("stats-row"):
                            with ui.column().classes("stat"):
                                turn_count_label = ui.label("0").classes("stat-value")
                                ui.label("確定した手数").classes("stat-label")
                            ui.element("div").classes("stat-divider")
                            with ui.column().classes("stat"):
                                word_count_label = ui.label("0").classes("stat-value")
                                ui.label("履歴のことば").classes("stat-label")

                        with ui.dialog() as reading_dialog, ui.card().classes("reading-dialog-card"):
                            ui.label("読みを選んでください").classes("reading-dialog-title")
                            ui.label("選んだ読みで接続と重複を判定します。").classes("reading-dialog-copy")
                            reading_choices = ui.column().classes("reading-choice-list")
                            ui.button("取り消す", icon="close", on_click=cancel_reading).props(
                                "flat no-caps"
                            ).classes("reading-cancel-button")

                    with ui.element("aside").classes("side-column"):
                        with ui.element("section").classes("history-card"):
                            with ui.row().classes("aside-heading"):
                                ui.icon("route").classes("aside-icon")
                                with ui.column().classes("aside-title-block"):
                                    ui.label("ことばの道").classes("aside-title")
                                    ui.label("表記と読みの履歴").classes("aside-subtitle")
                            history_list = ui.element("ol").classes("history-list")

                        with ui.element("section").classes("rules-card"):
                            with ui.row().classes("aside-heading"):
                                ui.icon("menu_book").classes("aside-icon")
                                with ui.column().classes("aside-title-block"):
                                    ui.label("あそびかた").classes("aside-title")
                                    ui.label("辞書で確かめるルール").classes("aside-subtitle")
                            with ui.element("ul").classes("rules-list"):
                                for number, text in (
                                    ("1", "先攻は辞書にある好きな単語から開始"),
                                    ("2", "読みの最後から次の単語をつなぐ"),
                                    ("3", "『ん』と同じ読みの再使用で終了"),
                                ):
                                    with ui.element("li").classes("rule-item"):
                                        ui.label(number).classes("rule-number")
                                        ui.label(text).classes("rule-copy")

                with ui.element("footer").classes("page-footer"):
                    ui.label("Python + NiceGUI + Sudachi").classes("footer-tech")
                    ui.link("GitHubでコードを見る", REPOSITORY_URL, new_tab=True).classes("footer-link")

        render()