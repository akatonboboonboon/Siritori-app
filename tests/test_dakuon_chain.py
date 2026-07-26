from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import unittest

from shiritori.bot_catalog import get_default_bot_catalog
from shiritori.bots import (
    BotContext,
    EasyBot,
    HardBot,
    NormalBot,
    WordIndex,
    WordOption,
    canonical_kana as bot_canonical_kana,
)
from shiritori.game import (
    GameState,
    TurnCode,
    canonical_kana as game_canonical_kana,
)
from shiritori.game_session import canonical_kana as session_canonical_kana
from shiritori.rooms import (
    InMemoryRoomRepository,
    RoomCoordinator,
    RoomMode,
    create_room_snapshot,
)


class DakuonConnectionRuleTests(unittest.TestCase):
    def test_connection_only_treats_ji_and_zu_spellings_as_equivalent(
        self,
    ) -> None:
        for canonicalize in (
            game_canonical_kana,
            session_canonical_kana,
            bot_canonical_kana,
        ):
            with self.subTest(module=canonicalize.__module__):
                self.assertEqual(canonicalize("ぢ"), "じ")
                self.assertEqual(canonicalize("じ"), "じ")
                self.assertEqual(canonicalize("づ"), "ず")
                self.assertEqual(canonicalize("ず"), "ず")

    def test_game_accepts_words_across_both_equivalent_connections(
        self,
    ) -> None:
        du_game = GameState("あいづ")
        du_result = du_game.submit("ずこう")
        self.assertEqual(du_result.code, TurnCode.ACCEPTED)
        self.assertEqual(du_game.history, ("あいづ", "ずこう"))

        di_game = GameState("はなぢ")
        di_result = di_game.submit("じしょ")
        self.assertEqual(di_result.code, TurnCode.ACCEPTED)
        self.assertEqual(di_game.history, ("はなぢ", "じしょ"))

        reverse_game = GameState("みず")
        reverse_result = reverse_game.submit("づら")
        self.assertEqual(reverse_result.code, TurnCode.ACCEPTED)
        self.assertEqual(reverse_game.history, ("みず", "づら"))

    def test_all_bot_difficulties_share_the_equivalent_bucket(self) -> None:
        index = WordIndex(
            [
                WordOption("図工", "ずこう", "ずこう", rank=1),
                WordOption("辞書", "じしょ", "じしょ", rank=1),
            ]
        )

        for expected, surface in (("づ", "図工"), ("ぢ", "辞書")):
            for strategy in (
                EasyBot(seed=1),
                NormalBot(seed=1),
                HardBot(seed=1),
            ):
                with self.subTest(
                    expected=expected,
                    strategy=type(strategy).__name__,
                ):
                    selected = strategy.choose(BotContext(expected), index)
                    self.assertIsNotNone(selected)
                    assert selected is not None
                    self.assertEqual(selected.surface, surface)

    def test_production_catalog_has_options_for_both_spellings(self) -> None:
        index = get_default_bot_catalog().index

        for rare, common in (("づ", "ず"), ("ぢ", "じ")):
            with self.subTest(rare=rare):
                self.assertEqual(
                    index.legal_options(rare),
                    index.legal_options(common),
                )
                self.assertTrue(index.legal_options(rare, avoid_n=True))


class PersistedRoomCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_raw_expected_kana_accepts_the_canonical_spelling(
        self,
    ) -> None:
        now = datetime(2026, 7, 26, tzinfo=timezone.utc)

        for stored, surface, reading in (
            ("づ", "図工", "ずこう"),
            ("ぢ", "辞書", "じしょ"),
        ):
            with self.subTest(stored=stored):
                room = create_room_snapshot(
                    f"legacy-{stored}",
                    ("alice", "bob"),
                    mode=RoomMode.PVP,
                    now=now,
                    seat_picker=lambda _: 0,
                )
                room = replace(room, expected_kana=stored)
                coordinator = RoomCoordinator(
                    InMemoryRoomRepository([room]),
                    clock=lambda: now,
                )

                outcome = await coordinator.submit_user_turn(
                    room.room_id,
                    "alice",
                    surface=surface,
                    reading=reading,
                    canonical_key=reading,
                    expected_version=0,
                    operation_id=f"legacy-{stored}-move",
                    now=now,
                )

                assert outcome.snapshot is not None
                self.assertEqual(
                    outcome.snapshot.history[-1].reading,
                    reading,
                )


if __name__ == "__main__":
    unittest.main()
