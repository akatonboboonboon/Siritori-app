from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from shiritori.lexicon import (
    LexiconCandidate,
    LexiconCode,
    LexiconResult,
)

from shiritori.rooms import (
    InMemoryRoomRepository,
    LexiconRoomService,
    PlayerSeat,
    Role,
    RoomAuthorizationError,
    RoomCoordinator,
    RoomEventKind,
    RoomMode,
    RoomNotFound,
    RoomOperationConflictError,
    RoomSnapshot,
    RoomStatus,
    RoomVersionConflict,
    SeatController,
    TurnDeadlineExpired,
    WordSubmissionStatus,
    create_room_snapshot,
)


NOW = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)


def pvp_room(
    *,
    version: int = 0,
    deadline_at: datetime | None = None,
) -> RoomSnapshot:
    return RoomSnapshot(
        room_id="pvp",
        mode=RoomMode.PVP,
        status=RoomStatus.ACTIVE,
        players=(
            PlayerSeat(0, "alice", SeatController.HUMAN),
            PlayerSeat(1, "bob", SeatController.HUMAN),
        ),
        spectators=("viewer",),
        current_turn=0,
        state_version=version,
        expected_kana="り",
        turn_seconds=30,
        deadline_at=deadline_at,
    )


def solo_room(*, deadline_at: datetime | None = None) -> RoomSnapshot:
    return RoomSnapshot(
        room_id="solo",
        mode=RoomMode.SOLO_BOT,
        status=RoomStatus.ACTIVE,
        players=(
            PlayerSeat(0, "alice", SeatController.HUMAN),
            PlayerSeat(1, None, SeatController.BOT),
        ),
        current_turn=0,
        expected_kana="り",
        turn_seconds=30,
        deadline_at=deadline_at,
    )

def lexicon_candidate(
    surface: str,
    reading: str,
    *,
    word_id: int = 1,
) -> LexiconCandidate:
    return LexiconCandidate(
        surface=surface,
        reading=reading,
        lemma=surface,
        normalized_form=surface,
        part_of_speech=("名詞", "普通名詞", "一般", "*", "*", "*"),
        dictionary_id=0,
        word_id=word_id,
        canonical_key=reading,
    )


class StubLexicon:
    def __init__(self, results: dict[str | None, LexiconResult]) -> None:
        self.results = results

    def validate(self, raw_surface: str | None) -> LexiconResult:
        return self.results[raw_surface]


class RoomFactoryTests(unittest.TestCase):
    def test_injected_picker_sets_random_first_seat_and_free_word(self) -> None:
        seen_counts = []

        def pick_last(seat_count: int) -> int:
            seen_counts.append(seat_count)
            return seat_count - 1

        snapshot = create_room_snapshot(
            "factory-pvp",
            ("alice", "bob"),
            mode=RoomMode.PVP,
            turn_seconds=3,
            now=NOW,
            seat_picker=pick_last,
        )

        self.assertEqual(seen_counts, [2])
        self.assertEqual(snapshot.current_turn, 1)
        self.assertIsNone(snapshot.expected_kana)
        self.assertEqual(snapshot.deadline_at, NOW + timedelta(seconds=3))

    def test_default_picker_uses_secure_randomness(self) -> None:
        with patch("shiritori.rooms.secrets.randbelow", return_value=0) as picker:
            snapshot = create_room_snapshot(
                "secure-pvp",
                ("alice", "bob"),
                mode=RoomMode.PVP,
                now=NOW,
            )

        picker.assert_called_once_with(2)
        self.assertEqual(snapshot.current_turn, 0)
        self.assertIsNone(snapshot.deadline_at)

    def test_solo_factory_supports_variable_permanent_bot_count(self) -> None:
        snapshot = create_room_snapshot(
            "many-bots",
            ("alice",),
            mode=RoomMode.SOLO_BOT,
            permanent_bot_count=3,
            now=NOW,
            seat_picker=lambda _: 2,
        )

        self.assertEqual(len(snapshot.players), 4)
        self.assertEqual(snapshot.current_turn, 2)
        self.assertEqual(
            [seat.controller for seat in snapshot.players],
            [
                SeatController.HUMAN,
                SeatController.BOT,
                SeatController.BOT,
                SeatController.BOT,
            ],
        )
        self.assertTrue(
            all(
                seat.owner_user_id is None
                for seat in snapshot.players[1:]
            )
        )

    def test_factory_validates_timer_range_and_picker(self) -> None:
        maximum = create_room_snapshot(
            "maximum-timer",
            ("alice", "bob"),
            mode=RoomMode.PVP,
            turn_seconds=180,
            now=NOW,
            seat_picker=lambda _: 0,
        )
        self.assertEqual(
            maximum.deadline_at,
            NOW + timedelta(seconds=180),
        )

        for seconds in (2, 181):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                create_room_snapshot(
                    "bad-timer",
                    ("alice", "bob"),
                    mode=RoomMode.PVP,
                    turn_seconds=seconds,
                    now=NOW,
                    seat_picker=lambda _: 0,
                )
        with self.assertRaises(ValueError):
            create_room_snapshot(
                "bad-picker",
                ("alice", "bob"),
                mode=RoomMode.PVP,
                now=NOW,
                seat_picker=lambda count: count,
            )

    def test_factory_persists_validated_runtime_settings(self) -> None:
        defaults = create_room_snapshot(
            "default-settings",
            ("alice", "bob"),
            mode=RoomMode.PVP,
            now=NOW,
            seat_picker=lambda _: 0,
        )
        configured = create_room_snapshot(
            "configured-settings",
            ("alice",),
            mode=RoomMode.SOLO_BOT,
            permanent_bot_count=1,
            theme_key="food_2026",
            bot_difficulty="hard",
            now=NOW,
            seat_picker=lambda _: 0,
        )

        self.assertEqual(defaults.theme_key, "all")
        self.assertEqual(defaults.bot_difficulty, "normal")
        self.assertEqual(configured.theme_key, "food_2026")
        self.assertEqual(configured.bot_difficulty, "hard")

        for difficulty in ("easy", "normal", "hard"):
            with self.subTest(difficulty=difficulty):
                snapshot = create_room_snapshot(
                    f"difficulty-{difficulty}",
                    ("alice",),
                    mode=RoomMode.SOLO_BOT,
                    permanent_bot_count=1,
                    bot_difficulty=difficulty,
                    now=NOW,
                    seat_picker=lambda _: 0,
                )
                self.assertEqual(snapshot.bot_difficulty, difficulty)

    def test_factory_rejects_noncanonical_runtime_settings(self) -> None:
        invalid_themes = (
            "",
            " Food",
            "FOOD",
            "-food",
            "food/path",
            "1food",
            "a" * 33,
            None,
        )
        for theme_key in invalid_themes:
            with self.subTest(theme_key=theme_key), self.assertRaises(ValueError):
                create_room_snapshot(
                    "bad-theme",
                    ("alice", "bob"),
                    mode=RoomMode.PVP,
                    theme_key=theme_key,
                    now=NOW,
                    seat_picker=lambda _: 0,
                )

        for difficulty in ("", "NORMAL", "expert", None, 3):
            with (
                self.subTest(difficulty=difficulty),
                self.assertRaises(ValueError),
            ):
                create_room_snapshot(
                    "bad-difficulty",
                    ("alice", "bob"),
                    mode=RoomMode.PVP,
                    bot_difficulty=difficulty,
                    now=NOW,
                    seat_picker=lambda _: 0,
                )

class LexiconRoomServiceTests(unittest.IsolatedAsyncioTestCase):
    def room(self) -> RoomSnapshot:
        return create_room_snapshot(
            "validated",
            ("alice", "bob"),
            mode=RoomMode.PVP,
            now=NOW,
            seat_picker=lambda _: 0,
        )

    async def test_ambiguous_reading_requires_explicit_dictionary_choice(self) -> None:
        candidates = (
            lexicon_candidate("日本", "にっぽん", word_id=1),
            lexicon_candidate("日本", "にほん", word_id=2),
        )
        validation = LexiconResult(
            LexiconCode.MULTIPLE_READINGS,
            "日本",
            "読みを選んでください。",
            candidates,
        )
        repository = InMemoryRoomRepository([self.room()])
        service = LexiconRoomService(
            RoomCoordinator(repository),
            StubLexicon({"日本": validation}),
        )

        pending = await service.submit_user_word(
            "validated",
            "alice",
            "日本",
            expected_version=0,
            operation_id="ambiguous-pending",
            now=NOW,
        )
        self.assertEqual(
            pending.status, WordSubmissionStatus.READING_REQUIRED
        )
        self.assertEqual(pending.reading_choices, ("にっぽん", "にほん"))
        unchanged = await repository.load("validated")
        assert unchanged is not None
        self.assertEqual(unchanged.state_version, 0)

        committed = await service.submit_user_word(
            "validated",
            "alice",
            "日本",
            chosen_reading="ニホン",
            expected_version=0,
            operation_id="ambiguous-chosen",
            now=NOW,
        )
        self.assertEqual(committed.status, WordSubmissionStatus.COMMITTED)
        self.assertEqual(committed.selected_reading, "にほん")
        assert committed.outcome is not None
        assert committed.outcome.snapshot is not None
        self.assertEqual(
            committed.outcome.snapshot.history[-1].canonical_key,
            "にほん",
        )

    async def test_spoofed_reading_is_rejected_without_state_change(self) -> None:
        validation = LexiconResult(
            LexiconCode.ACCEPTED,
            "林檎",
            "辞書にあります。",
            (lexicon_candidate("林檎", "りんご"),),
        )
        repository = InMemoryRoomRepository([self.room()])
        service = LexiconRoomService(
            RoomCoordinator(repository),
            StubLexicon({"林檎": validation}),
        )

        rejected = await service.submit_user_word(
            "validated",
            "alice",
            "林檎",
            chosen_reading="ごりら",
            expected_version=0,
            operation_id="spoofed-reading",
            now=NOW,
        )

        self.assertEqual(rejected.status, WordSubmissionStatus.REJECTED)
        snapshot = await repository.load("validated")
        assert snapshot is not None
        self.assertEqual(snapshot.state_version, 0)
        self.assertEqual(snapshot.history, ())

    async def test_public_adapter_does_not_accept_browser_canonical_key(self) -> None:
        validation = LexiconResult(
            LexiconCode.ACCEPTED,
            "林檎",
            "辞書にあります。",
            (lexicon_candidate("林檎", "りんご"),),
        )
        service = LexiconRoomService(
            RoomCoordinator(InMemoryRoomRepository([self.room()])),
            StubLexicon({"林檎": validation}),
        )

        with self.assertRaises(TypeError):
            await service.submit_user_word(  # type: ignore[call-arg]
                "validated",
                "alice",
                "林檎",
                canonical_key="browser-forgery",
                expected_version=0,
                operation_id="spoofed-key",
                now=NOW,
            )


class RoomCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_factory_allows_any_validated_free_first_word(self) -> None:
        room = create_room_snapshot(
            "free-first",
            ("alice", "bob"),
            mode=RoomMode.PVP,
            now=NOW,
            seat_picker=lambda _: 0,
        )
        coordinator = RoomCoordinator(InMemoryRoomRepository([room]))

        outcome = await coordinator.submit_user_turn(
            "free-first",
            "alice",
            surface="西瓜",
            reading="すいか",
            canonical_key="すいか",
            expected_version=0,
            operation_id="free-opening",
            now=NOW,
        )

        assert outcome.snapshot is not None
        self.assertEqual(outcome.snapshot.history[-1].reading, "すいか")
        self.assertEqual(outcome.snapshot.expected_kana, "か")

    async def test_roles_and_spectator_turn_authorization(self) -> None:
        repository = InMemoryRoomRepository([pvp_room()])
        coordinator = RoomCoordinator(repository, clock=lambda: NOW)

        snapshot = await coordinator.connect_client(
            "pvp", "viewer", "viewer-tab"
        )
        self.assertEqual(snapshot.role_for_user("viewer"), Role.SPECTATOR)

        with self.assertRaises(RoomAuthorizationError):
            await coordinator.submit_user_turn(
                "pvp",
                "viewer",
                surface="林檎",
                reading="りんご",
                canonical_key="りんご",
                expected_version=0,
                operation_id="viewer-move",
                now=NOW,
            )

    async def test_exact_retry_is_idempotent_and_ignores_server_now(self) -> None:
        repository = InMemoryRoomRepository([pvp_room()])
        coordinator = RoomCoordinator(repository, clock=lambda: NOW)

        first = await coordinator.submit_user_turn(
            "pvp",
            "alice",
            surface="林檎",
            reading="りんご",
            canonical_key="りんご",
            expected_version=0,
            operation_id="move-1",
            now=NOW,
        )
        retried = await coordinator.submit_user_turn(
            "pvp",
            "alice",
            surface="林檎",
            reading="りんご",
            canonical_key="りんご",
            expected_version=0,
            operation_id="move-1",
            now=NOW + timedelta(seconds=5),
        )

        self.assertFalse(first.duplicate)
        self.assertTrue(retried.duplicate)
        self.assertEqual(first.snapshot, retried.snapshot)
        assert retried.snapshot is not None
        self.assertEqual(len(retried.snapshot.history), 1)

    async def test_operation_id_reuse_rejects_payload_actor_action_and_version(self) -> None:
        async def committed() -> RoomCoordinator:
            repository = InMemoryRoomRepository([pvp_room()])
            coordinator = RoomCoordinator(repository, clock=lambda: NOW)
            await coordinator.submit_user_turn(
                "pvp",
                "alice",
                surface="林檎",
                reading="りんご",
                canonical_key="りんご",
                expected_version=0,
                operation_id="bound-operation",
                now=NOW,
            )
            return coordinator

        coordinator = await committed()
        with self.assertRaises(RoomOperationConflictError):
            await coordinator.submit_user_turn(
                "pvp",
                "alice",
                surface="栗鼠",
                reading="りす",
                canonical_key="りす",
                expected_version=0,
                operation_id="bound-operation",
                now=NOW,
            )

        coordinator = await committed()
        with self.assertRaises(RoomOperationConflictError):
            await coordinator.submit_user_turn(
                "pvp",
                "viewer",
                surface="林檎",
                reading="りんご",
                canonical_key="りんご",
                expected_version=0,
                operation_id="bound-operation",
                now=NOW,
            )

        coordinator = await committed()
        with self.assertRaises(RoomOperationConflictError):
            await coordinator.leave(
                "pvp",
                "alice",
                expected_version=0,
                operation_id="bound-operation",
                now=NOW,
            )

        coordinator = await committed()
        with self.assertRaises(RoomOperationConflictError):
            await coordinator.submit_user_turn(
                "pvp",
                "alice",
                surface="林檎",
                reading="りんご",
                canonical_key="りんご",
                expected_version=1,
                operation_id="bound-operation",
                now=NOW,
            )

    async def test_user_cannot_preclaim_future_runtime_timeout_operation(self) -> None:
        room = pvp_room(deadline_at=NOW + timedelta(seconds=3))
        repository = InMemoryRoomRepository([room])
        coordinator = RoomCoordinator(repository)
        operation_id = "runtime:timeout:pvp:0"

        with self.assertRaises(ValueError):
            await coordinator.submit_user_turn(
                "pvp",
                "alice",
                surface="林檎",
                reading="りんご",
                canonical_key="りんご",
                expected_version=0,
                operation_id=operation_id,
                now=NOW,
            )

        expired = await coordinator.expire_turn(
            "pvp",
            expected_version=0,
            operation_id=operation_id,
            now=NOW + timedelta(seconds=3),
        )
        assert expired.snapshot is not None
        self.assertEqual(expired.snapshot.status, RoomStatus.FINISHED)
        self.assertEqual(expired.snapshot.end_reason, "timeout")

    async def test_exact_concurrent_retry_applies_once(self) -> None:
        repository = InMemoryRoomRepository([pvp_room()])
        coordinator = RoomCoordinator(repository, clock=lambda: NOW)

        async def submit():
            return await coordinator.submit_user_turn(
                "pvp",
                "alice",
                surface="林檎",
                reading="りんご",
                canonical_key="りんご",
                expected_version=0,
                operation_id="same-concurrent-command",
                now=NOW,
            )

        results = await asyncio.gather(submit(), submit())
        self.assertEqual(sum(result.duplicate for result in results), 1)
        self.assertEqual(results[0].snapshot, results[1].snapshot)

    async def test_room_hub_broadcasts_committed_snapshot_to_subscribers(self) -> None:
        repository = InMemoryRoomRepository([pvp_room()])
        coordinator = RoomCoordinator(repository, clock=lambda: NOW)
        events = []
        await coordinator.connect_client(
            "pvp", "alice", "alice-tab", events.append
        )
        await coordinator.connect_client("pvp", "bob", "bob-tab")

        await coordinator.submit_user_turn(
            "pvp",
            "alice",
            surface="林檎",
            reading="りんご",
            canonical_key="りんご",
            expected_version=0,
            operation_id="broadcast-move",
            now=NOW,
        )

        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[-1].kind, RoomEventKind.SNAPSHOT)
        assert events[-1].snapshot is not None
        self.assertEqual(events[-1].snapshot.history[-1].surface, "林檎")

    async def test_concurrent_submissions_commit_only_one_move(self) -> None:
        repository = InMemoryRoomRepository([pvp_room()])
        coordinator = RoomCoordinator(repository, clock=lambda: NOW)

        async def submit(operation_id: str, reading: str):
            return await coordinator.submit_user_turn(
                "pvp",
                "alice",
                surface=reading,
                reading=reading,
                canonical_key=reading,
                expected_version=0,
                operation_id=operation_id,
                now=NOW,
            )

        results = await asyncio.gather(
            submit("race-a", "りんご"),
            submit("race-b", "りす"),
            return_exceptions=True,
        )

        self.assertEqual(
            sum(not isinstance(result, Exception) for result in results),
            1,
        )
        self.assertEqual(
            sum(isinstance(result, RoomVersionConflict) for result in results),
            1,
        )
        snapshot = await repository.load("pvp")
        assert snapshot is not None
        self.assertEqual(snapshot.state_version, 1)
        self.assertEqual(len(snapshot.history), 1)

    async def test_stale_version_is_rejected_before_mutation(self) -> None:
        repository = InMemoryRoomRepository([pvp_room(version=2)])
        coordinator = RoomCoordinator(repository)

        with self.assertRaises(RoomVersionConflict):
            await coordinator.submit_user_turn(
                "pvp",
                "alice",
                surface="林檎",
                reading="りんご",
                canonical_key="りんご",
                expected_version=1,
                operation_id="stale",
                now=NOW,
            )
        snapshot = await repository.load("pvp")
        assert snapshot is not None
        self.assertEqual(snapshot.history, ())

    async def test_duplicate_reading_ends_match_without_appending_it(self) -> None:
        repository = InMemoryRoomRepository([pvp_room()])
        coordinator = RoomCoordinator(repository)
        first = await coordinator.submit_user_turn(
            "pvp",
            "alice",
            surface="リス",
            reading="りす",
            canonical_key="りす",
            expected_version=0,
            operation_id="first",
            now=NOW,
        )
        assert first.snapshot is not None
        second = await coordinator.submit_user_turn(
            "pvp",
            "bob",
            surface="すり",
            reading="すり",
            canonical_key="すり",
            expected_version=1,
            operation_id="second",
            now=NOW,
        )
        assert second.snapshot is not None

        duplicate = await coordinator.submit_user_turn(
            "pvp",
            "alice",
            surface="栗鼠",
            reading="りす",
            canonical_key="りす",
            expected_version=2,
            operation_id="duplicate-word",
            now=NOW,
        )

        assert duplicate.snapshot is not None
        self.assertEqual(duplicate.snapshot.status, RoomStatus.FINISHED)
        self.assertEqual(duplicate.snapshot.end_reason, "duplicate")
        self.assertEqual(duplicate.snapshot.losing_seat, 0)
        self.assertEqual(len(duplicate.snapshot.history), 2)
        self.assertEqual(
            tuple(turn.canonical_key for turn in duplicate.snapshot.history),
            ("りす", "すり"),
        )

    async def test_one_of_multiple_tabs_disconnects_without_takeover(self) -> None:
        repository = InMemoryRoomRepository([pvp_room()])
        coordinator = RoomCoordinator(
            repository,
            disconnect_grace_seconds=0.01,
        )
        await coordinator.connect_client("pvp", "alice", "alice-1")
        await coordinator.connect_client("pvp", "alice", "alice-2")
        await coordinator.connect_client("pvp", "bob", "bob-1")

        task = await coordinator.disconnect_client("pvp", "alice-1")
        await asyncio.sleep(0.03)

        self.assertIsNone(task)
        snapshot = await repository.load("pvp")
        assert snapshot is not None
        self.assertEqual(
            snapshot.players[0].controller, SeatController.HUMAN
        )

    async def test_reconnect_inside_grace_cancels_takeover(self) -> None:
        repository = InMemoryRoomRepository([pvp_room()])
        coordinator = RoomCoordinator(
            repository,
            disconnect_grace_seconds=0.03,
        )
        await coordinator.connect_client("pvp", "alice", "alice-1")
        await coordinator.connect_client("pvp", "bob", "bob-1")
        task = await coordinator.disconnect_client("pvp", "alice-1")
        self.assertIsNotNone(task)

        await coordinator.connect_client("pvp", "alice", "alice-2")
        await asyncio.sleep(0.05)

        snapshot = await repository.load("pvp")
        assert snapshot is not None
        self.assertEqual(
            snapshot.players[0].controller, SeatController.HUMAN
        )
        self.assertEqual(snapshot.state_version, 0)

    async def test_last_connection_deletes_pvp_without_waiting_for_grace(
        self,
    ) -> None:
        repository = InMemoryRoomRepository([pvp_room()])
        sleep_delays: list[float] = []

        async def tracked_sleep(seconds: float) -> None:
            sleep_delays.append(seconds)
            await asyncio.sleep(0)

        coordinator = RoomCoordinator(
            repository,
            disconnect_grace_seconds=999,
            sleep=tracked_sleep,
            clock=lambda: NOW,
        )
        await coordinator.connect_client("pvp", "alice", "alice-only-tab")

        task = await coordinator.disconnect_client("pvp", "alice-only-tab")

        self.assertIsNone(task)
        self.assertEqual(sleep_delays, [])
        self.assertEqual(coordinator._disconnect_tasks, {})
        self.assertIsNone(await repository.load("pvp"))

    async def test_last_connection_cancels_existing_grace_before_delete(
        self,
    ) -> None:
        repository = InMemoryRoomRepository([pvp_room()])
        coordinator = RoomCoordinator(
            repository,
            disconnect_grace_seconds=999,
            clock=lambda: NOW,
        )
        await coordinator.connect_client("pvp", "alice", "alice-tab")
        await coordinator.connect_client("pvp", "bob", "bob-tab")

        alice_task = await coordinator.disconnect_client(
            "pvp", "alice-tab"
        )
        self.assertIsNotNone(alice_task)
        bob_task = await coordinator.disconnect_client("pvp", "bob-tab")

        self.assertIsNone(bob_task)
        assert alice_task is not None
        await asyncio.gather(alice_task, return_exceptions=True)
        self.assertTrue(alice_task.cancelled())
        self.assertEqual(coordinator._disconnect_tasks, {})
        self.assertIsNone(await repository.load("pvp"))

    async def test_last_connection_pauses_solo_before_grace_or_bot_work(
        self,
    ) -> None:
        active = replace(
            solo_room(deadline_at=NOW + timedelta(seconds=12)),
            current_turn=1,
        )
        repository = InMemoryRoomRepository([active])
        sleep_delays: list[float] = []

        async def tracked_sleep(seconds: float) -> None:
            sleep_delays.append(seconds)
            await asyncio.sleep(0)

        coordinator = RoomCoordinator(
            repository,
            disconnect_grace_seconds=999,
            sleep=tracked_sleep,
            clock=lambda: NOW,
        )
        await coordinator.connect_client("solo", "alice", "alice-tab")

        task = await coordinator.disconnect_client("solo", "alice-tab")

        self.assertIsNone(task)
        self.assertEqual(sleep_delays, [])
        snapshot = await coordinator.load_snapshot("solo")
        self.assertEqual(snapshot.status, RoomStatus.PAUSED)
        self.assertEqual(snapshot.current_turn, 1)
        self.assertEqual(snapshot.history, ())
        self.assertEqual(snapshot.paused_remaining_seconds, 12)
        self.assertIsNone(snapshot.deadline_at)

    async def test_disconnect_after_grace_enables_temporary_bot(self) -> None:
        repository = InMemoryRoomRepository([pvp_room()])
        coordinator = RoomCoordinator(
            repository,
            disconnect_grace_seconds=0,
        )
        await coordinator.connect_client("pvp", "alice", "alice-1")
        await coordinator.connect_client("pvp", "bob", "bob-1")

        task = await coordinator.disconnect_client("pvp", "alice-1")
        assert task is not None
        await task

        snapshot = await repository.load("pvp")
        assert snapshot is not None
        self.assertEqual(snapshot.players[0].controller, SeatController.BOT)

    async def test_return_during_own_bot_turn_hands_back_after_bot_move(self) -> None:
        disconnected = pvp_room()
        disconnected = RoomSnapshot(
            **{
                field: getattr(disconnected, field)
                for field in disconnected.__dataclass_fields__
                if field not in {"players", "state_version"}
            },
            players=(
                PlayerSeat(0, "alice", SeatController.BOT),
                PlayerSeat(1, "bob", SeatController.HUMAN),
            ),
            state_version=1,
        )
        repository = InMemoryRoomRepository([disconnected])
        coordinator = RoomCoordinator(repository, clock=lambda: NOW)

        reconnected = await coordinator.connect_client(
            "pvp", "alice", "alice-new"
        )
        self.assertTrue(reconnected.players[0].handback_pending)
        self.assertEqual(
            reconnected.players[0].controller, SeatController.BOT
        )

        moved = await coordinator.submit_bot_turn(
            "pvp",
            0,
            surface="林檎",
            reading="りんご",
            canonical_key="りんご",
            expected_version=2,
            operation_id="runtime:bot-boundary",
            now=NOW,
        )
        assert moved.snapshot is not None
        self.assertEqual(
            moved.snapshot.players[0].controller, SeatController.HUMAN
        )
        self.assertFalse(moved.snapshot.players[0].handback_pending)
        self.assertEqual(moved.snapshot.current_turn, 1)

    async def test_explicit_leave_takes_over_immediately(self) -> None:
        repository = InMemoryRoomRepository([pvp_room()])
        coordinator = RoomCoordinator(repository)
        await coordinator.connect_client("pvp", "alice", "alice-tab")
        await coordinator.connect_client("pvp", "bob", "bob-tab")

        outcome = await coordinator.leave(
            "pvp",
            "alice",
            expected_version=0,
            operation_id="leave-alice",
            now=NOW,
        )

        assert outcome.snapshot is not None
        self.assertEqual(
            outcome.snapshot.players[0].controller,
            SeatController.BOT,
        )

    async def test_leave_after_grace_has_a_durable_idempotency_receipt(self) -> None:
        repository = InMemoryRoomRepository([pvp_room()])
        coordinator = RoomCoordinator(
            repository,
            disconnect_grace_seconds=0,
        )
        await coordinator.connect_client("pvp", "alice", "alice-tab")
        await coordinator.connect_client("pvp", "bob", "bob-tab")
        task = await coordinator.disconnect_client("pvp", "alice-tab")
        assert task is not None
        await task

        first = await coordinator.leave(
            "pvp",
            "alice",
            expected_version=1,
            operation_id="leave-after-grace",
            now=NOW,
        )
        retried = await coordinator.leave(
            "pvp",
            "alice",
            expected_version=1,
            operation_id="leave-after-grace",
            now=NOW,
        )

        self.assertFalse(first.duplicate)
        self.assertTrue(retried.duplicate)
        self.assertEqual(first.snapshot, retried.snapshot)

    async def test_last_human_deletes_pvp_room_and_notifies_subscribers(self) -> None:
        repository = InMemoryRoomRepository([pvp_room()])
        coordinator = RoomCoordinator(repository)
        events = []
        await coordinator.connect_client(
            "pvp", "alice", "alice-tab", events.append
        )

        outcome = await coordinator.leave(
            "pvp",
            "alice",
            expected_version=0,
            operation_id="empty-pvp",
            now=NOW,
        )

        self.assertTrue(outcome.deleted)
        retried = await coordinator.leave(
            "pvp",
            "alice",
            expected_version=0,
            operation_id="empty-pvp",
            now=NOW + timedelta(seconds=30),
        )
        self.assertTrue(retried.duplicate)
        self.assertTrue(retried.deleted)
        self.assertIsNone(await repository.load("pvp"))
        # The leaving client's callback is removed first, as it would be in a
        # closed browser; RoomEventKind.CLOSED is still published to any tabs
        # that remain.
        self.assertTrue(
            all(event.kind is RoomEventKind.SNAPSHOT for event in events)
        )
        with self.assertRaises(RoomNotFound):
            await coordinator.load_snapshot("pvp")

    async def test_restart_recovery_rearms_absence_without_duplicate_tasks(self) -> None:
        repository = InMemoryRoomRepository([pvp_room()])
        coordinator = RoomCoordinator(
            repository,
            disconnect_grace_seconds=0,
        )

        recovered = await coordinator.recover_after_restart("pvp")
        again = await coordinator.recover_after_restart("pvp")
        self.assertEqual(recovered, again)
        self.assertEqual(len(coordinator._disconnect_tasks), 2)
        await asyncio.gather(
            *tuple(coordinator._disconnect_tasks.values()),
            return_exceptions=True,
        )
        self.assertIsNone(await repository.load("pvp"))

    async def test_restart_recovery_keeps_returning_user_and_bots_absent_seat(self) -> None:
        repository = InMemoryRoomRepository([pvp_room()])
        coordinator = RoomCoordinator(
            repository,
            disconnect_grace_seconds=0.01,
        )

        await coordinator.recover_after_restart("pvp")
        await coordinator.connect_client("pvp", "alice", "alice-returned")
        pending = tuple(coordinator._disconnect_tasks.values())
        await asyncio.gather(*pending, return_exceptions=True)

        snapshot = await coordinator.load_snapshot("pvp")
        self.assertEqual(snapshot.players[0].controller, SeatController.HUMAN)
        self.assertEqual(snapshot.players[1].controller, SeatController.BOT)

    async def test_restart_recovery_pauses_empty_solo_room(self) -> None:
        repository = InMemoryRoomRepository(
            [solo_room(deadline_at=NOW + timedelta(seconds=12))]
        )
        coordinator = RoomCoordinator(
            repository,
            disconnect_grace_seconds=0,
            clock=lambda: NOW,
        )

        await coordinator.recover_after_restart("solo")
        await asyncio.gather(
            *tuple(coordinator._disconnect_tasks.values()),
            return_exceptions=True,
        )

        snapshot = await coordinator.load_snapshot("solo")
        self.assertEqual(snapshot.status, RoomStatus.PAUSED)
        self.assertEqual(snapshot.paused_remaining_seconds, 12)

    async def test_last_human_pauses_solo_and_reconnect_resumes_snapshot(self) -> None:
        repository = InMemoryRoomRepository(
            [solo_room(deadline_at=NOW + timedelta(seconds=12))]
        )
        coordinator = RoomCoordinator(repository, clock=lambda: NOW)
        await coordinator.connect_client("solo", "alice", "alice-tab")

        paused = await coordinator.leave(
            "solo",
            "alice",
            expected_version=0,
            operation_id="pause-solo",
            now=NOW,
        )
        assert paused.snapshot is not None
        self.assertEqual(paused.snapshot.status, RoomStatus.PAUSED)
        self.assertEqual(paused.snapshot.paused_remaining_seconds, 12)
        self.assertIsNone(paused.snapshot.deadline_at)

        resumed = await coordinator.connect_client(
            "solo",
            "alice",
            "alice-returned",
            now=NOW + timedelta(minutes=5),
        )
        self.assertEqual(resumed.status, RoomStatus.ACTIVE)
        self.assertEqual(
            resumed.deadline_at,
            NOW + timedelta(minutes=5, seconds=12),
        )
        self.assertIsNone(resumed.paused_remaining_seconds)
        self.assertEqual(len(resumed.history), 0)

    async def test_server_utc_deadline_rejects_late_turn(self) -> None:
        repository = InMemoryRoomRepository(
            [pvp_room(deadline_at=NOW + timedelta(seconds=3))]
        )
        coordinator = RoomCoordinator(repository)

        with self.assertRaises(TurnDeadlineExpired):
            await coordinator.submit_user_turn(
                "pvp",
                "alice",
                surface="林檎",
                reading="りんご",
                canonical_key="りんご",
                expected_version=0,
                operation_id="late",
                now=NOW + timedelta(seconds=3),
            )


    async def test_current_bot_can_finish_when_no_legal_move_exists(self) -> None:
        snapshot = create_room_snapshot(
            "no-options",
            ["alice"],
            mode=RoomMode.SOLO_BOT,
            permanent_bot_count=1,
            now=NOW,
            seat_picker=lambda _count: 1,
        )
        repository = InMemoryRoomRepository([snapshot])
        coordinator = RoomCoordinator(repository, clock=lambda: NOW)

        first = await coordinator.finish_no_legal_move(
            "no-options",
            1,
            expected_version=0,
            operation_id="runtime:no-legal-1",
            now=NOW,
        )
        retried = await coordinator.finish_no_legal_move(
            "no-options",
            1,
            expected_version=0,
            operation_id="runtime:no-legal-1",
            now=NOW,
        )

        assert first.snapshot is not None
        self.assertEqual(first.snapshot.status, RoomStatus.FINISHED)
        self.assertEqual(first.snapshot.losing_seat, 1)
        self.assertEqual(first.snapshot.end_reason, "no_legal_move")
        self.assertTrue(retried.duplicate)
        self.assertEqual(retried.snapshot, first.snapshot)

    async def test_internal_bot_operations_require_runtime_prefix(self) -> None:
        snapshot = create_room_snapshot(
            "prefix-check",
            ["alice"],
            mode=RoomMode.SOLO_BOT,
            permanent_bot_count=1,
            now=NOW,
            seat_picker=lambda _count: 1,
        )
        coordinator = RoomCoordinator(InMemoryRoomRepository([snapshot]))

        with self.assertRaises(ValueError):
            await coordinator.finish_no_legal_move(
                "prefix-check",
                1,
                expected_version=0,
                operation_id="not-internal",
                now=NOW,
            )

    async def test_human_or_non_current_seat_cannot_claim_no_legal_move(self) -> None:
        snapshot = create_room_snapshot(
            "has-human",
            ["alice"],
            mode=RoomMode.SOLO_BOT,
            permanent_bot_count=1,
            now=NOW,
            seat_picker=lambda _count: 1,
        )
        repository = InMemoryRoomRepository([snapshot])
        coordinator = RoomCoordinator(repository, clock=lambda: NOW)

        with self.assertRaises(RoomAuthorizationError):
            await coordinator.finish_no_legal_move(
                "has-human",
                0,
                expected_version=0,
                operation_id="runtime:forged-no-legal",
                now=NOW,
            )
if __name__ == "__main__":
    unittest.main()
