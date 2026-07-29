from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from shiritori.bots import BotContext, HardBot, WordIndex, WordOption, final_kana
from shiritori.oni_room import (
    MAX_GENERATION_OPTIONS,
    OniRoomChallenge,
    OniRoomRuleService,
)
from shiritori.oni_rules import (
    GeneratedOniChallenge,
    OniConstraintSet,
)
from shiritori.room_runtime import RoomRuntime
from shiritori.rooms import (
    InMemoryRoomRepository,
    InvalidMove,
    LifeLossRecord,
    PlayerSeat,
    RoomCoordinator,
    RoomMode,
    RoomRuleSet,
    RoomSnapshot,
    RoomStatus,
    SeatController,
    TurnRecord,
    create_room_snapshot,
)


NOW = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)


def word(
    reading: str,
    *,
    key: str | None = None,
    rank: int = 1,
) -> WordOption:
    return WordOption(
        surface=reading,
        reading=reading,
        canonical_key=key or reading,
        rank=rank,
    )


def oni_room(
    room_id: str = "oni-room",
    *,
    current_turn: int = 0,
    expected_kana: str | None = "あ",
    history: tuple[TurnRecord, ...] = (),
    life_loss_events: tuple[LifeLossRecord, ...] = (),
) -> RoomSnapshot:
    return RoomSnapshot(
        room_id=room_id,
        mode=RoomMode.SOLO_BOT,
        rule_set=RoomRuleSet.ONI,
        status=RoomStatus.ACTIVE,
        players=(
            PlayerSeat(0, "alice", SeatController.HUMAN),
            PlayerSeat(1, None, SeatController.BOT),
        ),
        current_turn=current_turn,
        history=history,
        expected_kana=expected_kana,
        bot_difficulty="hard",
        turn_seconds=30,
        lives_per_player=3,
        remaining_lives=(
            3 - sum(event.seat_index == 0 for event in life_loss_events),
            3 - sum(event.seat_index == 1 for event in life_loss_events),
        ),
        life_loss_events=life_loss_events,
    )


def turn(reading: str, index: int) -> TurnRecord:
    return TurnRecord(
        surface=reading,
        reading=reading,
        canonical_key=f"{reading}:{index}",
        seat_index=index % 2,
        actor_user_id="alice" if index % 2 == 0 else None,
        by_bot=index % 2 == 1,
        submitted_at=NOW + timedelta(seconds=index),
    )


class OniRoomRuleServiceTests(unittest.TestCase):
    def test_opening_pool_is_bounded_and_presence_does_not_reroll(self) -> None:
        options = tuple(
            word("あかき", key=f"key-{index:04d}", rank=index)
            for index in range(MAX_GENERATION_OPTIONS + 80)
        )
        resolver_calls = 0

        def resolve(_snapshot: RoomSnapshot) -> WordIndex:
            nonlocal resolver_calls
            resolver_calls += 1
            return WordIndex(options)

        received_sizes: list[int] = []

        def fake_generate(
            candidates: tuple[WordOption, ...],
            **_kwargs: object,
        ) -> GeneratedOniChallenge:
            received_sizes.append(len(candidates))
            selected = tuple(candidates[:3])
            return GeneratedOniChallenge(
                constraints=OniConstraintSet(
                    forbidden_kana="さ",
                ),
                candidates=selected,
                minimum_candidates=3,
            )

        service = OniRoomRuleService(resolve)
        snapshot = oni_room(expected_kana=None)
        with patch(
            "shiritori.oni_room.generate_oni_challenge",
            side_effect=fake_generate,
        ):
            first = service.challenge_for(snapshot)
            presence_only = replace(
                snapshot,
                state_version=99,
                spectators=("viewer",),
            )
            second = service.challenge_for(presence_only)

        self.assertIs(first, second)
        self.assertEqual(received_sizes, [MAX_GENERATION_OPTIONS])
        self.assertEqual(resolver_calls, 1)

    def test_seal_uses_last_ten_successes_and_timeout_does_not_advance_it(
        self,
    ) -> None:
        readings = (
            "あか",
            "あき",
            "あく",
            "あけ",
            "あこ",
            "あさ",
            "あし",
            "あす",
            "あせ",
            "あそ",
            "あた",
        )
        history = tuple(
            turn(reading, index)
            for index, reading in enumerate(readings)
        )
        index = WordIndex(
            (
                word("たかき", key="one"),
                word("たさき", key="two"),
                word("たなき", key="three"),
            )
        )
        captured: list[dict[str, object]] = []

        def fake_generate_for_window(
            _snapshot: RoomSnapshot,
            _index: WordIndex,
            **kwargs: object,
        ) -> GeneratedOniChallenge:
            captured.append(kwargs)
            return GeneratedOniChallenge(
                constraints=OniConstraintSet(
                    forbidden_kana="ぬ",
                    required_kana="た",
                ),
                candidates=index.all_options(limit=3),
                minimum_candidates=3,
            )

        snapshot = oni_room(
            expected_kana="た",
            history=history,
            current_turn=1,
        )
        timeout = LifeLossRecord(
            seat_index=1,
            reason="timeout",
            surface=None,
            reading=None,
            remaining_lives=2,
            eliminated=False,
            occurred_at=NOW + timedelta(minutes=1),
        )
        service = OniRoomRuleService(lambda _snapshot: index)
        with patch.object(
            service,
            "_generate_for_window",
            side_effect=fake_generate_for_window,
        ):
            service.challenge_for(snapshot)
            service.challenge_for(
                replace(
                    snapshot,
                    current_turn=0,
                    life_loss_events=(timeout,),
                    remaining_lives=(3, 2),
                )
            )

        self.assertEqual(len(captured), 2)
        first_window = captured[0]["seal_window"]
        second_window = captured[1]["seal_window"]
        self.assertEqual(
            first_window.endings,
            tuple(final_kana(reading) for reading in readings[1:]),
        )
        self.assertEqual(second_window.endings, first_window.endings)
        self.assertEqual(captured[0]["turn_number"], 12)
        self.assertEqual(captured[1]["turn_number"], 12)
        self.assertEqual(captured[0]["extra_constraint_count"], 2)
        self.assertNotEqual(captured[0]["seed"], captured[1]["seed"])

    def test_exhausted_chain_returns_safe_empty_challenge(self) -> None:
        service = OniRoomRuleService(lambda _snapshot: WordIndex(()))

        challenge = service.challenge_for(oni_room())

        self.assertTrue(challenge.degraded)
        self.assertEqual(challenge.candidates, ())
        self.assertEqual(challenge.constraints, OniConstraintSet())

    def test_seal_filter_runs_before_the_ranked_generation_cap(self) -> None:
        sealed = tuple(
            word(
                "あかき",
                key=f"sealed-{index:03d}",
                rank=index,
            )
            for index in range(MAX_GENERATION_OPTIONS)
        )
        unsealed = tuple(
            word(
                "あかく",
                key=f"unsealed-{index}",
                rank=MAX_GENERATION_OPTIONS + index,
            )
            for index in range(3)
        )
        snapshot = oni_room(
            history=(turn("かき", 0),),
            expected_kana="あ",
        )
        service = OniRoomRuleService(
            lambda _snapshot: WordIndex((*sealed, *unsealed))
        )

        challenge = service.challenge_for(snapshot)

        self.assertEqual(challenge.relaxed_seal_count, 0)
        self.assertIn("き", challenge.constraints.sealed_endings)
        self.assertGreaterEqual(challenge.candidate_count, 3)
        self.assertTrue(
            all(option.last_kana == "く" for option in challenge.candidates)
        )

    def test_full_mora_group_finds_extra_command_below_rank_cap(self) -> None:
        common = tuple(
            word(
                "\u3042\u304b\u3044",
                key=f"common-{index:03d}",
                rank=index,
            )
            for index in range(MAX_GENERATION_OPTIONS)
        )
        rare = tuple(
            word(
                "\u3042\u305f\u3044",
                key=f"rare-{index}",
                rank=MAX_GENERATION_OPTIONS + index,
            )
            for index in range(3)
        )
        service = OniRoomRuleService(
            lambda _snapshot: WordIndex((*common, *rare))
        )

        challenge = service.challenge_for(
            oni_room(expected_kana="\u3042")
        )

        self.assertFalse(challenge.degraded)
        self.assertEqual(challenge.relaxed_seal_count, 0)
        self.assertEqual(
            challenge.constraints.forbidden_kana,
            "\u305f",
        )
        self.assertGreaterEqual(challenge.candidate_count, 3)

    def test_mora_failure_retries_after_dropping_the_oldest_seal(self) -> None:
        options = (
            word("あく", key="free-2", rank=1),
            word("あさく", key="free-3", rank=2),
            word("あたたく", key="free-4", rank=3),
            word("あかき", key="sealed-a", rank=4),
            word("あさき", key="sealed-b", rank=5),
            word("あたき", key="sealed-c", rank=6),
        )
        snapshot = oni_room(
            history=(turn("かき", 0),),
            expected_kana="あ",
        )
        service = OniRoomRuleService(lambda _snapshot: WordIndex(options))

        challenge = service.challenge_for(snapshot)

        self.assertEqual(challenge.relaxed_seal_count, 1)
        self.assertEqual(challenge.constraints.sealed_endings, ())
        self.assertEqual(challenge.constraints.mora_count_required, 3)
        self.assertGreaterEqual(challenge.candidate_count, 3)


class OniFixedSettingsTests(unittest.TestCase):
    def test_factory_accepts_only_the_fixed_oni_configuration(self) -> None:
        base: dict[str, object] = {
            "mode": RoomMode.SOLO_BOT,
            "permanent_bot_count": 1,
            "rule_set": RoomRuleSet.ONI,
            "turn_seconds": 30,
            "theme_key": "all",
            "bot_difficulty": "hard",
            "lives_per_player": 3,
            "now": NOW,
            "seat_picker": lambda _count: 0,
        }
        valid = create_room_snapshot(
            "fixed-oni",
            ("alice",),
            **base,
        )

        self.assertEqual(valid.rule_set, RoomRuleSet.ONI)
        self.assertEqual(valid.bot_difficulty, "hard")
        self.assertEqual(valid.lives_per_player, 3)
        self.assertEqual(valid.turn_seconds, 30)
        self.assertEqual(valid.theme_key, "all")
        self.assertEqual(len(valid.players), 2)

        invalid_overrides = (
            ("bot_count", {"permanent_bot_count": 2}),
            ("difficulty", {"bot_difficulty": "normal"}),
            ("lives", {"lives_per_player": 2}),
            ("timer", {"turn_seconds": 9}),
            ("theme", {"theme_key": "food"}),
        )
        for label, override in invalid_overrides:
            kwargs = {**base, **override}
            with (
                self.subTest(setting=label),
                self.assertRaises(ValueError),
            ):
                create_room_snapshot(
                    f"bad-oni-{label}",
                    ("alice",),
                    **kwargs,
                )


class HardBotCandidateTests(unittest.TestCase):
    def test_candidate_limit_is_applied_after_rank_sorting(self) -> None:
        readings = (
            "ああき",
            "あかき",
            "あさき",
            "あたき",
            "あなき",
            "あはき",
            "あまき",
            "あやき",
            "あらき",
            "あわき",
        )
        candidates = tuple(
            word(
                reading,
                key=f"candidate-{index}",
                rank=100 - index * 5 if index < 9 else 1,
            )
            for index, reading in enumerate(readings)
        )
        reply = word("きつね", key="reply", rank=1)
        index = WordIndex((*candidates, reply))

        selected = HardBot(seed=1).choose_from_candidates(
            BotContext("あ"),
            index,
            candidates,
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.rank, 1)
        self.assertEqual(selected, candidates[-1])


class OniCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_constraint_violation_keeps_state_lives_and_deadline(self) -> None:
        deadline = NOW + timedelta(seconds=30)
        snapshot = replace(
            oni_room(),
            turn_seconds=30,
            deadline_at=deadline,
        )
        repository = InMemoryRoomRepository((snapshot,))
        constraints = OniConstraintSet(
            mora_count_required=3,
            required_kana="か",
        )
        coordinator = RoomCoordinator(
            repository,
            oni_constraint_resolver=lambda _snapshot: constraints,
            clock=lambda: NOW,
        )

        with self.assertRaises(InvalidMove) as raised:
            await coordinator.submit_user_turn(
                snapshot.room_id,
                "alice",
                surface="あさき",
                reading="あさき",
                canonical_key="あさき",
                expected_version=0,
                operation_id="oni-invalid-command",
            )

        persisted = await coordinator.load_snapshot(snapshot.room_id)
        self.assertIn("鬼ルール違反", str(raised.exception))
        self.assertEqual(persisted, snapshot)
        self.assertEqual(persisted.remaining_lives, (3, 3))
        self.assertEqual(persisted.deadline_at, deadline)

    async def test_valid_word_is_committed_through_same_predicate(self) -> None:
        snapshot = oni_room()
        repository = InMemoryRoomRepository((snapshot,))
        constraints = OniConstraintSet(
            mora_count_required=3,
            required_kana="か",
        )
        coordinator = RoomCoordinator(
            repository,
            oni_constraint_resolver=lambda _snapshot: constraints,
            clock=lambda: NOW,
        )

        outcome = await coordinator.submit_user_turn(
            snapshot.room_id,
            "alice",
            surface="あかき",
            reading="あかき",
            canonical_key="あかき",
            expected_version=0,
            operation_id="oni-valid-command",
        )

        self.assertEqual(outcome.snapshot.state_version, 1)
        self.assertEqual(outcome.snapshot.history[-1].reading, "あかき")


class OniRuntimeTests(unittest.TestCase):
    def test_hard_bot_uses_only_command_candidates_but_full_index_ahead(
        self,
    ) -> None:
        forced_win = word("あかい", key="forced", rank=100)
        lower_rank_with_reply = word("あかき", key="replyable", rank=1)
        forbidden_best_rank = word("あさき", key="forbidden", rank=0)
        reply = word("きつね", key="reply", rank=1)
        index = WordIndex(
            (
                forced_win,
                lower_rank_with_reply,
                forbidden_best_rank,
                reply,
            )
        )
        challenge = OniRoomChallenge(
            constraints=OniConstraintSet(required_kana="か"),
            candidates=(lower_rank_with_reply, forced_win),
        )
        snapshot = replace(
            oni_room(current_turn=1),
            players=(
                PlayerSeat(0, "alice", SeatController.HUMAN),
                PlayerSeat(1, None, SeatController.BOT),
            ),
        )
        coordinator = RoomCoordinator(
            InMemoryRoomRepository((snapshot,)),
            oni_constraint_resolver=lambda _snapshot: challenge.constraints,
        )
        runtime = RoomRuntime(
            coordinator,
            strategy_resolver=lambda _snapshot, _seat: HardBot(seed=1),
            word_index_resolver=lambda _snapshot: index,
            oni_challenge_resolver=lambda _snapshot: challenge,
        )

        selected = runtime._choose_option(
            snapshot,
            HardBot(seed=1),
            index,
        )

        self.assertEqual(selected, forced_win)
        self.assertIn(selected, challenge.candidates)
        self.assertTrue(challenge.constraints.accepts_option(selected))
        self.assertNotEqual(selected, forbidden_best_rank)

    def test_zero_candidate_challenge_is_a_safe_no_move(self) -> None:
        index = WordIndex((word("あかき"),))
        challenge = OniRoomChallenge(
            constraints=OniConstraintSet(),
            candidates=(),
            degraded=True,
        )
        snapshot = replace(oni_room(current_turn=1))
        coordinator = RoomCoordinator(
            InMemoryRoomRepository((snapshot,)),
            oni_constraint_resolver=lambda _snapshot: challenge.constraints,
        )
        runtime = RoomRuntime(
            coordinator,
            strategy_resolver=lambda _snapshot, _seat: HardBot(seed=1),
            word_index_resolver=lambda _snapshot: index,
            oni_challenge_resolver=lambda _snapshot: challenge,
        )

        self.assertIsNone(
            runtime._choose_option(snapshot, HardBot(seed=1), index)
        )


if __name__ == "__main__":
    unittest.main()
