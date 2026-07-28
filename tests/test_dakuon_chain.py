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
    final_kana as bot_final_kana,
    first_kana as bot_first_kana,
)
from shiritori.game import (
    GameState,
    TurnCode,
    canonical_kana as game_canonical_kana,
)
from shiritori.game_session import (
    canonical_kana as session_canonical_kana,
    ending_chain_kana as session_ending_chain_kana,
    first_chain_kana as session_first_chain_kana,
)
from shiritori.rooms import (
    InMemoryRoomRepository,
    RoomCoordinator,
    RoomMode,
    create_room_snapshot,
)


class DakuonConnectionRuleTests(unittest.TestCase):
    def test_connection_canonicalizes_supported_equivalent_spellings(
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
                self.assertEqual(canonicalize("ゔ"), "ぶ")
                self.assertEqual(canonicalize("ぶ"), "ぶ")
                for vu_mora, b_mora in (
                    ("ゔぁ", "ば"),
                    ("ゔぃ", "び"),
                    ("ゔぇ", "べ"),
                    ("ゔぉ", "ぼ"),
                ):
                    self.assertEqual(canonicalize(vu_mora), b_mora)

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

    def test_vu_and_b_morae_connect_in_both_directions(self) -> None:
        for vu_mora, b_mora in (
            ("ゔ", "ぶ"),
            ("ゔぁ", "ば"),
            ("ゔぃ", "び"),
            ("ゔぇ", "べ"),
            ("ゔぉ", "ぼ"),
        ):
            with self.subTest(vu_mora=vu_mora, direction="vu-to-b"):
                game = GameState(f"あ{vu_mora}")
                result = game.submit(f"{b_mora}ら")
                self.assertEqual(result.code, TurnCode.ACCEPTED)

            with self.subTest(vu_mora=vu_mora, direction="b-to-vu"):
                game = GameState(f"あ{b_mora}")
                result = game.submit(f"{vu_mora}ら")
                self.assertEqual(result.code, TurnCode.ACCEPTED)

            self.assertEqual(session_first_chain_kana(f"{vu_mora}ら"), b_mora)
            self.assertEqual(session_ending_chain_kana(f"あ{vu_mora}"), b_mora)
            self.assertEqual(bot_first_kana(f"{vu_mora}ら"), b_mora)
            self.assertEqual(bot_final_kana(f"あ{vu_mora}"), b_mora)

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

    def test_all_bot_difficulties_share_vu_and_b_mora_buckets(self) -> None:
        for vu_mora, b_mora in (
            ("ゔ", "ぶ"),
            ("ゔぁ", "ば"),
            ("ゔぃ", "び"),
            ("ゔぇ", "べ"),
            ("ゔぉ", "ぼ"),
        ):
            for candidate_mora, expected_mora in (
                (vu_mora, b_mora),
                (b_mora, bot_final_kana(f"あ{vu_mora}")),
            ):
                index = WordIndex(
                    [
                        WordOption(
                            f"{candidate_mora}候補",
                            f"{candidate_mora}ら",
                            f"{candidate_mora}ら",
                            rank=1,
                        )
                    ]
                )
                for strategy in (
                    EasyBot(seed=1),
                    NormalBot(seed=1),
                    HardBot(seed=1),
                ):
                    with self.subTest(
                        candidate_mora=candidate_mora,
                        expected_mora=expected_mora,
                        strategy=type(strategy).__name__,
                    ):
                        selected = strategy.choose(
                            BotContext(expected_mora),
                            index,
                        )
                        self.assertIsNotNone(selected)

    def test_hard_bot_counts_b_reply_to_vu_mora_as_legal(self) -> None:
        index = WordIndex(
            [
                WordOption("ヴァ終わり", "こゔぁ", "こゔぁ", rank=1),
                WordOption("キ終わり", "こき", "こき", rank=2),
                WordOption("薔薇", "ばら", "ばら", rank=1),
            ]
        )

        selected = HardBot(seed=1).choose(BotContext("こ"), index)

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.surface, "キ終わり")

    def test_production_catalog_has_options_for_both_spellings(self) -> None:
        index = get_default_bot_catalog().index

        for rare, common in (("づ", "ず"), ("ぢ", "じ"), ("ゔ", "ぶ")):
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
            ("ゔ", "豚", "ぶた"),
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

    async def test_room_connects_vu_compound_mora_to_b_mora(self) -> None:
        now = datetime(2026, 7, 26, tzinfo=timezone.utc)
        room = create_room_snapshot(
            "vu-compound",
            ("alice", "bob"),
            mode=RoomMode.PVP,
            now=now,
            seat_picker=lambda _: 0,
        )
        coordinator = RoomCoordinator(
            InMemoryRoomRepository([room]),
            clock=lambda: now,
        )

        first = await coordinator.submit_user_turn(
            room.room_id,
            "alice",
            surface="ラヴァ",
            reading="らゔぁ",
            canonical_key="らゔぁ",
            expected_version=0,
            operation_id="vu-compound-first",
            now=now,
        )
        assert first.snapshot is not None
        self.assertEqual(first.snapshot.expected_kana, "ば")

        second = await coordinator.submit_user_turn(
            room.room_id,
            "bob",
            surface="薔薇",
            reading="ばら",
            canonical_key="ばら",
            expected_version=1,
            operation_id="vu-compound-second",
            now=now,
        )
        assert second.snapshot is not None
        self.assertEqual(second.snapshot.history[-1].reading, "ばら")

    async def test_room_accepts_vu_compound_for_b_expected_kana(self) -> None:
        now = datetime(2026, 7, 26, tzinfo=timezone.utc)
        room = create_room_snapshot(
            "b-to-vu-compound",
            ("alice", "bob"),
            mode=RoomMode.PVP,
            now=now,
            seat_picker=lambda _: 0,
        )
        room = replace(room, expected_kana="ば")
        coordinator = RoomCoordinator(
            InMemoryRoomRepository([room]),
            clock=lambda: now,
        )
        outcome = await coordinator.submit_user_turn(
            room.room_id,
            "alice",
            surface="ヴァニラ",
            reading="ゔぁにら",
            canonical_key="ゔぁにら",
            expected_version=0,
            operation_id="b-to-vu-compound",
            now=now,
        )
        assert outcome.snapshot is not None
        self.assertEqual(outcome.snapshot.history[-1].reading, "ゔぁにら")


if __name__ == "__main__":
    unittest.main()
