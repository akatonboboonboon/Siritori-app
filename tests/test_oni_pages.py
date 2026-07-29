"""Focused tests for the Oni shiritori NiceGUI integration."""

from __future__ import annotations

from dataclasses import replace
import inspect
from pathlib import Path
from types import SimpleNamespace
import unittest

from shiritori.oni_pages import (
    ONI_BOT_COUNT,
    ONI_BOT_DIFFICULTY,
    ONI_LIVES,
    ONI_TURN_SECONDS,
    register_oni_pages,
)
from shiritori.rooms import (
    RoomMode,
    RoomRuleSet,
    RoomStatus,
    create_room_snapshot,
)
from shiritori.web_auth import _oni_challenge_presentation


def oni_snapshot():
    return create_room_snapshot(
        "oni-game",
        ("user-1",),
        mode=RoomMode.SOLO_BOT,
        permanent_bot_count=1,
        rule_set=RoomRuleSet.ONI,
        bot_difficulty="hard",
        lives_per_player=3,
        turn_seconds=30,
        seat_picker=lambda _seat_count: 0,
    )


class RecordingOniRules:
    def __init__(self, challenge: object | None) -> None:
        self.challenge = challenge
        self.snapshots: list[object] = []

    def challenge_for(self, snapshot):
        self.snapshots.append(snapshot)
        return self.challenge


class OniPageIntegrationTests(unittest.TestCase):
    def test_fixed_setup_and_keyword_only_registration(self) -> None:
        self.assertEqual(ONI_BOT_COUNT, 1)
        self.assertEqual(ONI_BOT_DIFFICULTY, "hard")
        self.assertEqual(ONI_LIVES, 3)
        self.assertEqual(ONI_TURN_SECONDS, 30)
        parameters = inspect.signature(register_oni_pages).parameters
        self.assertEqual(tuple(parameters), ("auth", "settings", "solo"))
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for parameter in parameters.values()
            )
        )

    def test_presentation_exposes_commands_counts_but_not_answers(self) -> None:
        constraints = SimpleNamespace(
            descriptions=(
                "読みは4音",
                "「ぽ」を含む",
                "直近10手の末尾を封印中：り・ご",
            ),
            sealed_endings=("り", "ご", "り"),
        )
        challenge = SimpleNamespace(
            constraints=constraints,
            candidate_count=5,
            relaxed_seal_count=1,
            candidates=("秘密の答え",),
        )
        service = RecordingOniRules(challenge)
        snapshot = oni_snapshot()

        presentation = _oni_challenge_presentation(service, snapshot)

        self.assertIsNotNone(presentation)
        assert presentation is not None
        self.assertEqual(
            presentation.commands,
            ("読みは4音", "「ぽ」を含む"),
        )
        self.assertEqual(presentation.sealed_endings, ("り", "ご"))
        self.assertEqual(presentation.candidate_count, 5)
        self.assertEqual(presentation.relaxed_seal_count, 1)
        self.assertNotIn("秘密の答え", repr(presentation))
        self.assertEqual(service.snapshots, [snapshot])

    def test_standard_game_does_not_call_oni_service(self) -> None:
        snapshot = create_room_snapshot(
            "standard-game",
            ("user-1",),
            mode=RoomMode.SOLO_BOT,
            permanent_bot_count=1,
            seat_picker=lambda _seat_count: 0,
        )
        service = RecordingOniRules(None)

        self.assertIsNone(_oni_challenge_presentation(service, snapshot))
        self.assertEqual(service.snapshots, [])

    def test_finished_oni_game_does_not_show_a_future_command(self) -> None:
        snapshot = replace(
            oni_snapshot(),
            status=RoomStatus.FINISHED,
            end_reason="test_finished",
        )
        service = RecordingOniRules(None)

        self.assertIsNone(_oni_challenge_presentation(service, snapshot))
        self.assertEqual(service.snapshots, [])

    def test_page_and_main_keep_auth_redirect_and_old_url_redirect(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page_source = (root / "shiritori" / "oni_pages.py").read_text(
            encoding="utf-8"
        )
        web_source = (root / "shiritori" / "web_auth.py").read_text(
            encoding="utf-8"
        )
        main_source = (root / "main.py").read_text(encoding="utf-8")

        self.assertIn('@ui.page("/oni-shiritori")', page_source)
        self.assertIn('"/login?next=/oni-shiritori"', page_source)
        self.assertIn("rule_set=RoomRuleSet.ONI", page_source)
        self.assertIn("bot_difficulty=ONI_BOT_DIFFICULTY", page_source)
        self.assertIn('ui.label("登場する7種類の命令")', page_source)
        self.assertIn('"禁止かな：', page_source)
        self.assertIn(
            "既知の正解候補が5語以上", page_source
        )
        self.assertIn('"末尾封印：', page_source)
        self.assertIn('"鬼しりとり", "/oni-shiritori"', web_source)
        self.assertIn("register_oni_pages(", main_source)
        self.assertIn(
            'return RedirectResponse("/oni-shiritori", status_code=303)',
            main_source,
        )
        self.assertNotIn("register_daily_pages", main_source)


if __name__ == "__main__":
    unittest.main()
