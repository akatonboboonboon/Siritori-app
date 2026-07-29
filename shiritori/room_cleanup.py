"""Periodic cleanup for multiplayer rooms with no durable activity.

Waiting-room activity is tracked by ``rooms.updated_at`` while an active PvP
round is tracked by its current ``games.updated_at`` and state version.  The
repositories perform the database-side cutoff checks; this service owns only
the schedule and delegates active deletion to :class:`RoomCoordinator` so it
uses the same CAS and event path as every other room transition.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Final, Protocol

from .rooms import RoomCoordinator


INACTIVE_ROOM_TTL: Final = timedelta(minutes=30)
INACTIVE_ROOM_SWEEP_SECONDS: Final = 60.0
INACTIVE_ROOM_BATCH_LIMIT: Final = 100

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]

LOGGER = logging.getLogger(__name__)


class WaitingRoomCleanupRepository(Protocol):
    def expire_inactive_waiting_rooms(
        self,
        inactive_before: datetime,
        *,
        now: datetime | None = None,
        limit: int = INACTIVE_ROOM_BATCH_LIMIT,
    ) -> tuple[str, ...]:
        """Logically delete waiting rooms inactive through the cutoff."""


class ActiveRoomCleanupRepository(Protocol):
    async def list_inactive_pvp_rooms(
        self,
        inactive_before: datetime,
        *,
        limit: int = INACTIVE_ROOM_BATCH_LIMIT,
    ) -> tuple[tuple[str, int], ...]:
        """Return current inactive PvP game IDs and observed state versions."""


@dataclass(frozen=True, slots=True)
class RoomCleanupResult:
    """Summary of one bounded cleanup sweep."""

    inactive_before: datetime
    waiting_room_ids: tuple[str, ...]
    pvp_candidate_count: int
    pvp_room_ids: tuple[str, ...]


class RoomCleanupClosed(RuntimeError):
    """Raised when a closed cleanup service is started again."""


class RoomCleanupService:
    """Run one immediate sweep, then repeat it once per minute."""

    def __init__(
        self,
        waiting_repository: WaitingRoomCleanupRepository,
        active_repository: ActiveRoomCleanupRepository,
        coordinator: RoomCoordinator,
        *,
        inactive_ttl: timedelta = INACTIVE_ROOM_TTL,
        sweep_seconds: float = INACTIVE_ROOM_SWEEP_SECONDS,
        batch_limit: int = INACTIVE_ROOM_BATCH_LIMIT,
        clock: Clock | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not isinstance(inactive_ttl, timedelta) or inactive_ttl <= timedelta():
            raise ValueError("inactive_ttl must be a positive timedelta")
        if (
            not isinstance(sweep_seconds, (int, float))
            or isinstance(sweep_seconds, bool)
            or sweep_seconds <= 0
        ):
            raise ValueError("sweep_seconds must be positive")
        if type(batch_limit) is not int or not 1 <= batch_limit <= 100:
            raise ValueError("batch_limit must be an integer from 1 to 100")
        if not callable(sleep):
            raise TypeError("sleep must be callable")

        self.waiting_repository = waiting_repository
        self.active_repository = active_repository
        self.coordinator = coordinator
        self.inactive_ttl = inactive_ttl
        self.sweep_seconds = float(sweep_seconds)
        self.batch_limit = batch_limit
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._sweep_lock = asyncio.Lock()
        self._closed = False
        self._last_error: BaseException | None = None

    @property
    def running(self) -> bool:
        task = self._task
        return task is not None and not task.done()

    @property
    def last_error(self) -> BaseException | None:
        return self._last_error

    async def run_once(self) -> RoomCleanupResult:
        """Run one bounded sweep using one exact UTC cutoff."""

        async with self._sweep_lock:
            now = self._now()
            inactive_before = now - self.inactive_ttl
            waiting_room_ids = await asyncio.to_thread(
                self.waiting_repository.expire_inactive_waiting_rooms,
                inactive_before,
                now=now,
                limit=self.batch_limit,
            )
            candidates = await self.active_repository.list_inactive_pvp_rooms(
                inactive_before,
                limit=self.batch_limit,
            )

            expired_pvp: list[str] = []
            for room_id, state_version in candidates:
                outcome = await self.coordinator.expire_inactive_pvp_room(
                    room_id,
                    state_version,
                )
                if outcome is not None and outcome.deleted:
                    expired_pvp.append(room_id)

            self._last_error = None
            return RoomCleanupResult(
                inactive_before=inactive_before,
                waiting_room_ids=tuple(waiting_room_ids),
                pvp_candidate_count=len(candidates),
                pvp_room_ids=tuple(expired_pvp),
            )

    async def start(self) -> asyncio.Task[None]:
        """Sweep immediately and idempotently start periodic cleanup."""

        async with self._lifecycle_lock:
            if self._closed:
                raise RoomCleanupClosed("room cleanup service is closed")
            if self.running:
                assert self._task is not None
                return self._task
            await self._await_sweep_completion()
            task = asyncio.create_task(
                self._run_periodically(),
                name="shiritori-room-cleanup",
            )
            self._task = task
            return task

    async def close(self) -> None:
        """Cancel and await periodic work before database disposal."""

        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            task = self._task
            self._task = None
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)

    async def _run_periodically(self) -> None:
        while True:
            await self._sleep(self.sweep_seconds)
            await self._await_sweep_completion()

    async def _await_sweep_completion(self) -> None:
        # asyncio.to_thread cannot stop its worker when the awaiting task is
        # cancelled. Shield each startup/periodic sweep and wait for it before
        # propagating cancellation, so the database pool is never disposed
        # while a cleanup transaction is still using it.
        sweep = asyncio.create_task(self._run_safely())
        try:
            await asyncio.shield(sweep)
        except asyncio.CancelledError:
            await asyncio.gather(sweep, return_exceptions=True)
            raise

    async def _run_safely(self) -> None:
        try:
            await self.run_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._last_error = error
            LOGGER.exception("inactive room cleanup sweep failed")

    def _now(self) -> datetime:
        now = self._clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() != timedelta(0)
        ):
            raise ValueError("cleanup clock must return timezone-aware UTC")
        return now


__all__ = [
    "INACTIVE_ROOM_BATCH_LIMIT",
    "INACTIVE_ROOM_SWEEP_SECONDS",
    "INACTIVE_ROOM_TTL",
    "RoomCleanupClosed",
    "RoomCleanupResult",
    "RoomCleanupService",
]
