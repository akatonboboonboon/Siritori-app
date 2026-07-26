"""Focused tests for the standalone daily challenge NiceGUI page."""

from __future__ import annotations

from datetime import date
import inspect
from pathlib import Path
from types import SimpleNamespace
import unittest

from shiritori.daily_challenge import (
    DailyChallengeCondition,
    DailyChallengeSession,
)
from shiritori.daily_pages import (
    _daily_date_label,
    _daily_finish_reason,
    _load_initial_daily_state,
    _restore_daily_session,
    register_daily_pages,
)
from shiritori.score_attack import ScoreAttackStatus
from tests.test_score_attack import FakeLexicon, ManualClock, accepted


DAY = date(2026, 7, 26)
CONDITION = DailyChallengeCondition.create(DAY, "林檎", "りんご")
WORDS = {
    "林檎": accepted("林檎", "りんご"),
    "語尾": accepted("語尾", "ごり"),
}


class RecordingDailyService:
    """Minimal service double which rejects accidental attempt creation."""

    def __init__(self, run=None) -> None:
        self.run = run
        self.calls: list[tuple[str, object]] = []

    def today_condition(self) -> DailyChallengeCondition:
        self.calls.append(("today_condition", None))
        return CONDITION

    def current(self, user_id: str):
        self.calls.append(("current", user_id))
        return self.run

    def start_today(self, user_id: str):
        raise AssertionError(
            f"opening the page consumed the attempt for {user_id}"
        )


class DailyPageHelperTests(unittest.TestCase):
    def test_initial_load_never_starts_or_consumes_attempt(self) -> None:
        service = RecordingDailyService()

        state = _load_initial_daily_state(service, "user-1")  # type: ignore[arg-type]

        self.assertIsNone(state.run)
        self.assertEqual(state.condition, CONDITION)
        self.assertEqual(
            service.calls,
            [
                ("today_condition", None),
                ("current", "user-1"),
            ],
        )

    def test_active_run_condition_wins_over_new_jst_day(self) -> None:
        older = DailyChallengeCondition.create(
            date(2026, 7, 25),
            "寿司",
            "すし",
        )
        run = SimpleNamespace(condition=older)
        service = RecordingDailyService(run)

        state = _load_initial_daily_state(service, "user-1")  # type: ignore[arg-type]

        self.assertIs(state.run, run)
        self.assertEqual(state.condition, older)

    def test_snapshot_restore_keeps_seed_out_of_visible_history(self) -> None:
        lexicon = FakeLexicon(WORDS)
        clock = ManualClock()
        challenge = DailyChallengeSession(
            CONDITION,
            lexicon,  # type: ignore[arg-type]
            clock=clock,
        ).start()
        run = SimpleNamespace(
            snapshot=challenge.to_snapshot(),
            condition=CONDITION,
        )
        service = SimpleNamespace(validator=lexicon)

        restored = _restore_daily_session(
            run,  # type: ignore[arg-type]
            service,  # type: ignore[arg-type]
        )

        self.assertIs(restored.status, ScoreAttackStatus.ACTIVE)
        self.assertEqual(restored.expected_kana, "ご")
        self.assertEqual(restored.history, ())
        self.assertEqual(restored.score, 0)

    def test_date_and_finish_text_are_public_and_japanese(self) -> None:
        self.assertEqual(
            _daily_date_label(DAY),
            "2026年7月26日（日本時間）",
        )
        self.assertEqual(
            _daily_finish_reason("timeout"),
            "3分が経過しました。",
        )
        self.assertEqual(
            _daily_finish_reason("ends_with_n"),
            "「ん」で終わる単語を入力しました。",
        )
        self.assertEqual(
            _daily_finish_reason("duplicate"),
            "同じ読みの単語を使いました。",
        )
        self.assertNotIn("id", _daily_finish_reason(None).casefold())

    def test_registration_api_is_keyword_only(self) -> None:
        parameters = inspect.signature(
            register_daily_pages
        ).parameters

        self.assertEqual(
            tuple(parameters),
            ("auth", "settings", "daily_challenge"),
        )
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for parameter in parameters.values()
            )
        )

    def test_reading_choice_ignores_nicegui_event_argument(self) -> None:
        source = inspect.getsource(register_daily_pages)

        self.assertIn(
            "lambda _event=None, selected=reading",
            source,
        )

    def test_daily_css_has_mobile_layout_and_44px_targets(self) -> None:
        css = Path("assets/daily_pages.css").read_text(encoding="utf-8")

        self.assertIn("min-height: 44px", css)
        self.assertIn("@media (max-width: 640px)", css)
        self.assertIn(".daily-reading-dialog", css)


if __name__ == "__main__":
    unittest.main()
