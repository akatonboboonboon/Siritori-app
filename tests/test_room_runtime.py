from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import unittest

from shiritori.bots import NormalBot, WordIndex, WordOption
from shiritori.room_runtime import (
    RoomRuntime,
    RoomRuntimeCapabilityError,
    RoomRuntimeClosed,
    _runtime_operation_id,
)
from shiritori.rooms import (
    CommandOutcome,
    InMemoryRoomRepository,
    PlayerSeat,
    RepositoryStatus,
    RoomCoordinator,
    RoomMode,
    RoomSnapshot,
    RoomStatus,
    RoomVersionConflict,
    SeatController,
)


NOW = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)


def operation_fingerprint(operation_id: str) -> str:
    return sha256(operation_id.encode("utf-8")).hexdigest()


class MutableClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class AdvancingSleep:
    def __init__(self, clock: MutableClock) -> None:
        self.clock = clock
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.clock.advance(seconds)
        await asyncio.sleep(0)


class GatedSleep:
    def __init__(self, clock: MutableClock) -> None:
        self.clock = clock
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.started.set()
        await self.release.wait()
        self.clock.advance(seconds)


class CountingCoordinator(RoomCoordinator):
    def __init__(
        self,
        repository: InMemoryRoomRepository,
        *,
        clock: MutableClock,
    ) -> None:
        super().__init__(repository, clock=clock)
        self.expire_calls = 0
        self.bot_calls = 0

    async def expire_turn(self, *args: object, **kwargs: object) -> CommandOutcome:
        self.expire_calls += 1
        return await super().expire_turn(*args, **kwargs)

    async def submit_bot_turn(
        self, *args: object, **kwargs: object
    ) -> CommandOutcome:
        self.bot_calls += 1
        return await super().submit_bot_turn(*args, **kwargs)


def human_room(
    room_id: str,
    *,
    deadline_at: datetime | None,
) -> RoomSnapshot:
    return RoomSnapshot(
        room_id=room_id,
        mode=RoomMode.PVP,
        status=RoomStatus.ACTIVE,
        players=(
            PlayerSeat(0, "alice", SeatController.HUMAN),
            PlayerSeat(1, "bob", SeatController.HUMAN),
        ),
        current_turn=0,
        expected_kana="り",
        turn_seconds=30 if deadline_at is not None else None,
        deadline_at=deadline_at,
    )


def bot_room(room_id: str = "bots") -> RoomSnapshot:
    return RoomSnapshot(
        room_id=room_id,
        mode=RoomMode.SOLO_BOT,
        status=RoomStatus.ACTIVE,
        players=(
            PlayerSeat(0, None, SeatController.BOT),
            PlayerSeat(1, None, SeatController.BOT),
        ),
        current_turn=0,
        expected_kana="り",
    )


def word_index() -> WordIndex:
    return WordIndex(
        (
            WordOption("りんご", "りんご", "りんご", rank=1),
            WordOption("ごりら", "ごりら", "ごりら", rank=1),
        )
    )


class RoomRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_internal_operation_ids_are_deterministic_and_bounded(self) -> None:
        first = _runtime_operation_id(
            "no-legal", "00000000-0000-0000-0000-000000000000", 10**20, 99
        )
        retried = _runtime_operation_id(
            "no-legal", "00000000-0000-0000-0000-000000000000", 10**20, 99
        )
        different = _runtime_operation_id(
            "no-legal", "00000000-0000-0000-0000-000000000000", 10**20 + 1, 99
        )

        self.assertEqual(first, retried)
        self.assertNotEqual(first, different)
        self.assertTrue(first.startswith("runtime:no-legal:"))
        self.assertLessEqual(len(first), 64)

    async def test_authoritative_deadline_expires_once(self) -> None:
        clock = MutableClock()
        sleep = AdvancingSleep(clock)
        repository = InMemoryRoomRepository(
            (human_room("timer", deadline_at=NOW + timedelta(seconds=3)),)
        )
        coordinator = CountingCoordinator(repository, clock=clock)
        runtime = RoomRuntime(
            coordinator,
            strategy_resolver=lambda _snapshot, _seat: NormalBot(seed=1),
            word_index_resolver=lambda _snapshot: word_index(),
            clock=clock,
            sleep=sleep,
        )

        await runtime.notify("timer")

        snapshot = await coordinator.load_snapshot("timer")
        self.assertEqual(snapshot.status, RoomStatus.FINISHED)
        self.assertEqual(snapshot.end_reason, "timeout")
        self.assertEqual(snapshot.timed_out_seat, 0)
        self.assertEqual(coordinator.expire_calls, 1)
        self.assertEqual(sleep.delays, [3.0])

    async def test_last_human_disconnect_pauses_before_bot_delay(self) -> None:
        clock = MutableClock()
        sleep = GatedSleep(clock)
        repository = InMemoryRoomRepository(
            (
                RoomSnapshot(
                    room_id="solo-pause",
                    mode=RoomMode.SOLO_BOT,
                    status=RoomStatus.ACTIVE,
                    players=(
                        PlayerSeat(0, "alice", SeatController.HUMAN),
                        PlayerSeat(1, None, SeatController.BOT),
                    ),
                    current_turn=1,
                    expected_kana="り",
                    turn_seconds=30,
                    deadline_at=NOW + timedelta(seconds=30),
                ),
            )
        )
        coordinator = CountingCoordinator(repository, clock=clock)
        runtime = RoomRuntime(
            coordinator,
            strategy_resolver=lambda _snapshot, _seat: NormalBot(seed=1),
            word_index_resolver=lambda _snapshot: word_index(),
            bot_delay_seconds=0.35,
            clock=clock,
            sleep=sleep,
        )
        coordinator.set_activity_notifier(runtime.notify)

        await coordinator.connect_client(
            "solo-pause", "alice", "alice-tab", now=NOW
        )
        await asyncio.wait_for(sleep.started.wait(), timeout=1)

        delayed_task = await coordinator.disconnect_client(
            "solo-pause", "alice-tab"
        )

        self.assertIsNone(delayed_task)
        supervisor = runtime.notify("solo-pause")
        await asyncio.wait_for(supervisor, timeout=1)
        snapshot = await coordinator.load_snapshot("solo-pause")
        self.assertEqual(snapshot.status, RoomStatus.PAUSED)
        self.assertEqual(snapshot.history, ())
        self.assertEqual(coordinator.bot_calls, 0)
        await runtime.close()

    async def test_bot_chain_runs_in_background_then_finishes_no_move(
        self,
    ) -> None:
        clock = MutableClock()
        sleep = AdvancingSleep(clock)
        repository = InMemoryRoomRepository((bot_room(),))
        coordinator = CountingCoordinator(repository, clock=clock)
        terminal_calls: list[tuple[str, int, int, str]] = []

        async def finish_no_legal_move(
            room_id: str,
            seat_index: int,
            *,
            expected_version: int,
            operation_id: str,
            now: datetime | None = None,
        ) -> CommandOutcome:
            current = await repository.load(room_id)
            assert current is not None
            if current.state_version != expected_version:
                raise RoomVersionConflict(current)
            self.assertEqual(current.current_turn, seat_index)
            self.assertEqual(current.players[seat_index].controller, SeatController.BOT)
            terminal_calls.append(
                (room_id, seat_index, expected_version, operation_id)
            )
            finished = replace(
                current,
                status=RoomStatus.FINISHED,
                deadline_at=None,
                losing_seat=seat_index,
                end_reason="no_legal_move",
                state_version=current.state_version + 1,
            )
            result = await repository.compare_and_swap(
                room_id,
                expected_version,
                operation_id,
                finished,
                command_fingerprint=operation_fingerprint(operation_id),
            )
            self.assertEqual(result.status, RepositoryStatus.APPLIED)
            return CommandOutcome(operation_id, finished)

        runtime = RoomRuntime(
            coordinator,
            strategy_resolver=lambda _snapshot, _seat: NormalBot(seed=7),
            word_index_resolver=lambda _snapshot: word_index(),
            bot_delay_seconds=0.1,
            clock=clock,
            sleep=sleep,
            no_legal_move_handler=finish_no_legal_move,
        )

        task = runtime.notify("bots")
        self.assertFalse(task.done(), "notify must not execute the Bot inline")
        await task

        snapshot = await coordinator.load_snapshot("bots")
        self.assertEqual(
            tuple(turn.surface for turn in snapshot.history),
            ("りんご", "ごりら"),
        )
        self.assertEqual(snapshot.status, RoomStatus.FINISHED)
        self.assertEqual(snapshot.end_reason, "no_legal_move")
        self.assertEqual(snapshot.losing_seat, 0)
        self.assertEqual(coordinator.bot_calls, 2)
        self.assertEqual(len(terminal_calls), 1)
        self.assertEqual(sleep.delays, [0.1, 0.1, 0.1])

    async def test_missing_no_move_capability_is_explicitly_recorded(
        self,
    ) -> None:
        clock = MutableClock()
        repository = InMemoryRoomRepository((bot_room("empty-index"),))
        coordinator = CountingCoordinator(repository, clock=clock)

        class CoordinatorWithoutTerminal:
            """Expose runtime commands except the optional terminal capability."""

            async def load_snapshot(self, room_id: str) -> RoomSnapshot:
                return await coordinator.load_snapshot(room_id)

            async def expire_turn(
                self, *args: object, **kwargs: object
            ) -> CommandOutcome:
                return await coordinator.expire_turn(*args, **kwargs)

            async def submit_bot_turn(
                self, *args: object, **kwargs: object
            ) -> CommandOutcome:
                return await coordinator.submit_bot_turn(*args, **kwargs)

        runtime = RoomRuntime(
            CoordinatorWithoutTerminal(),  # type: ignore[arg-type]
            strategy_resolver=lambda _snapshot, _seat: NormalBot(),
            word_index_resolver=lambda _snapshot: WordIndex(()),
            bot_delay_seconds=0,
            clock=clock,
            sleep=AdvancingSleep(clock),
        )

        await runtime.notify("empty-index")

        self.assertIsInstance(
            runtime.last_error("empty-index"), RoomRuntimeCapabilityError
        )
        snapshot = await coordinator.load_snapshot("empty-index")
        self.assertEqual(snapshot.status, RoomStatus.ACTIVE)

    async def test_timeout_race_reloads_newer_snapshot(self) -> None:
        clock = MutableClock(NOW + timedelta(seconds=3))
        initial = human_room(
            "race", deadline_at=NOW + timedelta(seconds=3)
        )
        repository = InMemoryRoomRepository((initial,))

        class RacingCoordinator(CountingCoordinator):
            async def expire_turn(
                self, *args: object, **kwargs: object
            ) -> CommandOutcome:
                self.expire_calls += 1
                current = await repository.load("race")
                assert current is not None
                moved = replace(
                    current,
                    current_turn=1,
                    state_version=current.state_version + 1,
                    turn_seconds=None,
                    deadline_at=None,
                )
                result = await repository.compare_and_swap(
                    "race",
                    current.state_version,
                    "simulated-human-commit",
                    moved,
                    command_fingerprint=operation_fingerprint(
                        "simulated-human-commit"
                    ),
                )
                if result.status is not RepositoryStatus.APPLIED:
                    raise AssertionError("simulated human commit was not applied")
                return await RoomCoordinator.expire_turn(
                    self, *args, **kwargs
                )

        coordinator = RacingCoordinator(repository, clock=clock)
        runtime = RoomRuntime(
            coordinator,
            strategy_resolver=lambda _snapshot, _seat: NormalBot(),
            word_index_resolver=lambda _snapshot: word_index(),
            clock=clock,
        )

        await runtime.notify("race")

        snapshot = await coordinator.load_snapshot("race")
        self.assertEqual(snapshot.status, RoomStatus.ACTIVE)
        self.assertEqual(snapshot.current_turn, 1)
        self.assertEqual(snapshot.state_version, 1)
        self.assertEqual(coordinator.expire_calls, 1)
        self.assertIsNone(runtime.last_error("race"))

    async def test_duplicate_notify_reuses_one_timer_task(self) -> None:
        clock = MutableClock()
        sleep = GatedSleep(clock)
        repository = InMemoryRoomRepository(
            (
                human_room(
                    "duplicate", deadline_at=NOW + timedelta(seconds=3)
                ),
            )
        )
        coordinator = CountingCoordinator(repository, clock=clock)
        runtime = RoomRuntime(
            coordinator,
            strategy_resolver=lambda _snapshot, _seat: NormalBot(),
            word_index_resolver=lambda _snapshot: word_index(),
            clock=clock,
            sleep=sleep,
        )

        first = runtime.notify("duplicate")
        second = runtime.notify("duplicate")
        self.assertIs(first, second)
        await sleep.started.wait()
        sleep.release.set()
        await first

        self.assertEqual(coordinator.expire_calls, 1)
        snapshot = await coordinator.load_snapshot("duplicate")
        self.assertEqual(snapshot.end_reason, "timeout")

    async def test_recover_deduplicates_supplied_room_ids(self) -> None:
        clock = MutableClock()
        repository = InMemoryRoomRepository(
            (
                human_room("recover-a", deadline_at=None),
                human_room("recover-b", deadline_at=None),
            )
        )
        coordinator = CountingCoordinator(repository, clock=clock)
        runtime = RoomRuntime(
            coordinator,
            strategy_resolver=lambda _snapshot, _seat: NormalBot(),
            word_index_resolver=lambda _snapshot: word_index(),
            clock=clock,
        )

        tasks = runtime.recover(("recover-a", "recover-b", "recover-a"))

        self.assertEqual(len(tasks), 2)
        await asyncio.gather(*tasks)
        self.assertEqual(runtime.active_room_ids, frozenset())
        self.assertIsNone(runtime.last_error("recover-a"))
        self.assertIsNone(runtime.last_error("recover-b"))

    async def test_cancel_and_close_stop_pending_work(self) -> None:
        clock = MutableClock()
        sleep = GatedSleep(clock)
        repository = InMemoryRoomRepository(
            (
                human_room(
                    "cancel-me", deadline_at=NOW + timedelta(seconds=30)
                ),
            )
        )
        coordinator = CountingCoordinator(repository, clock=clock)
        runtime = RoomRuntime(
            coordinator,
            strategy_resolver=lambda _snapshot, _seat: NormalBot(),
            word_index_resolver=lambda _snapshot: word_index(),
            clock=clock,
            sleep=sleep,
        )

        task = runtime.notify("cancel-me")
        await sleep.started.wait()
        self.assertIn("cancel-me", runtime.active_room_ids)
        await runtime.cancel("cancel-me")

        self.assertTrue(task.cancelled())
        self.assertNotIn("cancel-me", runtime.active_room_ids)
        snapshot = await coordinator.load_snapshot("cancel-me")
        self.assertEqual(snapshot.status, RoomStatus.ACTIVE)
        self.assertEqual(coordinator.expire_calls, 0)

        await runtime.close()
        await runtime.close()
        with self.assertRaises(RoomRuntimeClosed):
            runtime.notify("cancel-me")


if __name__ == "__main__":
    unittest.main()
