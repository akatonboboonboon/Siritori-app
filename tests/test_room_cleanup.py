from __future__ import annotations

import asyncio
from collections.abc import Callable
from collections import deque
from datetime import datetime, timedelta, timezone
from threading import Event
import unittest
from unittest.mock import patch

from shiritori.room_cleanup import (
    INACTIVE_ROOM_BATCH_LIMIT,
    INACTIVE_ROOM_SWEEP_SECONDS,
    INACTIVE_ROOM_TTL,
    RoomCleanupClosed,
    RoomCleanupService,
)
from shiritori.rooms import CommandOutcome


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


class StubWaitingRepository:
    def __init__(self, actions: tuple[object, ...] = ((),)) -> None:
        self.actions = deque(actions)
        self.calls: list[tuple[datetime, datetime | None, int]] = []

    def expire_inactive_waiting_rooms(
        self,
        inactive_before: datetime,
        *,
        now: datetime | None = None,
        limit: int = INACTIVE_ROOM_BATCH_LIMIT,
    ) -> tuple[str, ...]:
        self.calls.append((inactive_before, now, limit))
        action = self.actions.popleft() if self.actions else ()
        if isinstance(action, BaseException):
            raise action
        return tuple(action)


class BlockingWaitingRepository:
    def __init__(self) -> None:
        self.calls = 0
        self.entered = Event()
        self.release = Event()

    def expire_inactive_waiting_rooms(
        self,
        inactive_before: datetime,
        *,
        now: datetime | None = None,
        limit: int = INACTIVE_ROOM_BATCH_LIMIT,
    ) -> tuple[str, ...]:
        del inactive_before, now, limit
        self.calls += 1
        if self.calls == 1:
            return ()
        self.entered.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("test did not release cleanup transaction")
        return ()


class StubActiveRepository:
    def __init__(
        self,
        candidates: tuple[tuple[str, int], ...] = (),
    ) -> None:
        self.candidates = candidates
        self.calls: list[tuple[datetime, int]] = []

    async def list_inactive_pvp_rooms(
        self,
        inactive_before: datetime,
        *,
        limit: int = INACTIVE_ROOM_BATCH_LIMIT,
    ) -> tuple[tuple[str, int], ...]:
        self.calls.append((inactive_before, limit))
        return self.candidates


class StubCoordinator:
    def __init__(self, stale_room_ids: tuple[str, ...] = ()) -> None:
        self.stale_room_ids = frozenset(stale_room_ids)
        self.calls: list[tuple[str, int]] = []

    async def expire_inactive_pvp_room(
        self,
        room_id: str,
        state_version: int,
    ) -> CommandOutcome | None:
        self.calls.append((room_id, state_version))
        if room_id in self.stale_room_ids:
            return None
        return CommandOutcome(
            operation_id=f"cleanup-{room_id}",
            snapshot=None,
            deleted=True,
        )


class ControlledSleep:
    """Let a test release one periodic sleep at a time."""

    def __init__(self) -> None:
        self.entered: asyncio.Queue[float] = asyncio.Queue()
        self.permits: asyncio.Queue[None] = asyncio.Queue()

    async def __call__(self, seconds: float) -> None:
        await self.entered.put(seconds)
        await self.permits.get()

    async def release_next(self) -> float:
        seconds = await asyncio.wait_for(self.entered.get(), timeout=1)
        self.permits.put_nowait(None)
        await asyncio.sleep(0)
        return seconds


async def wait_for_call_count(calls: list[object], count: int) -> None:
    for _ in range(100):
        if len(calls) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected {count} calls, got {len(calls)}")


async def wait_for_condition(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


class RoomCleanupServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_once_uses_exact_cutoff_and_bounded_batches(self) -> None:
        waiting = StubWaitingRepository((("waiting-old",),))
        active = StubActiveRepository(
            (("game-old", 7), ("game-became-active", 8))
        )
        coordinator = StubCoordinator(("game-became-active",))
        service = RoomCleanupService(
            waiting,
            active,
            coordinator,  # type: ignore[arg-type]
            clock=lambda: NOW,
        )

        result = await service.run_once()

        cutoff = NOW - timedelta(minutes=30)
        self.assertEqual(INACTIVE_ROOM_TTL, timedelta(minutes=30))
        self.assertEqual(INACTIVE_ROOM_SWEEP_SECONDS, 60.0)
        self.assertEqual(INACTIVE_ROOM_BATCH_LIMIT, 100)
        self.assertEqual(
            waiting.calls,
            [(cutoff, NOW, INACTIVE_ROOM_BATCH_LIMIT)],
        )
        self.assertEqual(
            active.calls,
            [(cutoff, INACTIVE_ROOM_BATCH_LIMIT)],
        )
        self.assertEqual(
            coordinator.calls,
            [("game-old", 7), ("game-became-active", 8)],
        )
        self.assertEqual(result.inactive_before, cutoff)
        self.assertEqual(result.waiting_room_ids, ("waiting-old",))
        self.assertEqual(result.pvp_candidate_count, 2)
        self.assertEqual(result.pvp_room_ids, ("game-old",))

    async def test_start_runs_immediately_once_and_is_idempotent(self) -> None:
        waiting = StubWaitingRepository()
        active = StubActiveRepository()
        coordinator = StubCoordinator()
        controlled_sleep = ControlledSleep()
        service = RoomCleanupService(
            waiting,
            active,
            coordinator,  # type: ignore[arg-type]
            clock=lambda: NOW,
            sleep=controlled_sleep,
        )
        try:
            first, second = await asyncio.gather(
                service.start(),
                service.start(),
            )

            self.assertIs(first, second)
            self.assertTrue(service.running)
            self.assertEqual(len(waiting.calls), 1)
            self.assertEqual(len(active.calls), 1)
            seconds = await controlled_sleep.release_next()
            self.assertEqual(seconds, 60.0)
            await wait_for_call_count(waiting.calls, 2)
        finally:
            await service.close()

        self.assertFalse(service.running)
        await service.close()
        with self.assertRaises(RoomCleanupClosed):
            await service.start()

    async def test_periodic_failure_is_recorded_and_later_sweeps_continue(
        self,
    ) -> None:
        failure = RuntimeError("temporary database outage")
        waiting = StubWaitingRepository(((), failure, ("recovered",)))
        active = StubActiveRepository()
        coordinator = StubCoordinator()
        controlled_sleep = ControlledSleep()
        service = RoomCleanupService(
            waiting,
            active,
            coordinator,  # type: ignore[arg-type]
            clock=lambda: NOW,
            sleep=controlled_sleep,
        )
        try:
            await service.start()
            self.assertIsNone(service.last_error)

            with patch("shiritori.room_cleanup.LOGGER.exception") as logged:
                await controlled_sleep.release_next()
                await wait_for_call_count(waiting.calls, 2)
                await wait_for_condition(lambda: service.last_error is failure)
            logged.assert_called_once()
            self.assertTrue(service.running)

            await controlled_sleep.release_next()
            await wait_for_call_count(waiting.calls, 3)
            await wait_for_condition(lambda: service.last_error is None)
            self.assertTrue(service.running)
        finally:
            await service.close()

    async def test_initial_failure_does_not_prevent_periodic_recovery(self) -> None:
        failure = RuntimeError("startup database outage")
        waiting = StubWaitingRepository((failure, ("recovered",)))
        controlled_sleep = ControlledSleep()
        service = RoomCleanupService(
            waiting,
            StubActiveRepository(),
            StubCoordinator(),  # type: ignore[arg-type]
            clock=lambda: NOW,
            sleep=controlled_sleep,
        )
        try:
            with patch("shiritori.room_cleanup.LOGGER.exception") as logged:
                await service.start()
            logged.assert_called_once()
            self.assertIs(service.last_error, failure)
            self.assertTrue(service.running)

            await controlled_sleep.release_next()
            await wait_for_call_count(waiting.calls, 2)
            await wait_for_condition(lambda: service.last_error is None)
        finally:
            await service.close()

    async def test_close_waits_for_inflight_database_sweep(self) -> None:
        waiting = BlockingWaitingRepository()
        controlled_sleep = ControlledSleep()
        service = RoomCleanupService(
            waiting,
            StubActiveRepository(),
            StubCoordinator(),  # type: ignore[arg-type]
            clock=lambda: NOW,
            sleep=controlled_sleep,
        )
        await service.start()
        await controlled_sleep.release_next()
        entered = await asyncio.to_thread(waiting.entered.wait, 1)
        self.assertTrue(entered)

        close_task = asyncio.create_task(service.close())
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(close_task.done())

        waiting.release.set()
        await asyncio.wait_for(close_task, timeout=1)
        self.assertFalse(service.running)

    async def test_rejects_non_utc_clock_and_invalid_settings(self) -> None:
        waiting = StubWaitingRepository()
        active = StubActiveRepository()
        coordinator = StubCoordinator()
        service = RoomCleanupService(
            waiting,
            active,
            coordinator,  # type: ignore[arg-type]
            clock=lambda: datetime(2026, 7, 29, 12, 0),
        )
        with self.assertRaises(ValueError):
            await service.run_once()

        for kwargs in (
            {"inactive_ttl": timedelta()},
            {"sweep_seconds": 0},
            {"sweep_seconds": True},
            {"batch_limit": 0},
            {"batch_limit": 101},
            {"batch_limit": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    RoomCleanupService(
                        waiting,
                        active,
                        coordinator,  # type: ignore[arg-type]
                        **kwargs,
                    )


if __name__ == "__main__":
    unittest.main()
