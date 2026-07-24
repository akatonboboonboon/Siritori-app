"""Background supervision for authoritative room timers and Bot turns.

``RoomCoordinator`` owns every state transition.  This module only decides
*when* to request an automatic transition and always reloads the persisted
snapshot before doing so.  Browser payloads are therefore never a source for
the active seat, deadline, reading, or canonical word key.

One lightweight task supervises each room.  Repeated notifications merely
wake that task, while deterministic operation IDs make a second process
harmless if two Render instances briefly reconcile the same state version.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import inspect
from typing import Protocol, runtime_checkable

from .bots import BotContext, BotStrategy, WordIndex, WordOption
from .rooms import (
    CommandOutcome,
    PlayerSeat,
    RoomAuthorizationError,
    RoomCoordinator,
    RoomInactive,
    RoomNotFound,
    RoomSnapshot,
    RoomStatus,
    RoomVersionConflict,
    SeatController,
    TurnDeadlineExpired,
    TurnDeadlineNotReached,
)


MAX_BOT_DELAY_SECONDS = 5.0

# WordIndex intentionally exposes only kana-bucket lookup.  Scanning this
# finite server-owned alphabet lets a Bot take a free opening turn without
# reaching into WordIndex internals.
_OPENING_KANA: tuple[str, ...] = tuple(
    "あいうえお"
    "かきくけこがぎぐげご"
    "さしすせそざじずぜぞ"
    "たちつてとだぢづでど"
    "なにぬねの"
    "はひふへほばびぶべぼぱぴぷぺぽ"
    "まみむめも"
    "やゆよ"
    "らりるれろ"
    "わゐゑを"
    "ゔ"
)


Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]
StrategyResolver = Callable[[RoomSnapshot, PlayerSeat], BotStrategy]
WordIndexResolver = Callable[[RoomSnapshot], WordIndex]
ErrorCallback = Callable[
    [str, BaseException], None | Awaitable[None]
]


@runtime_checkable
class NoLegalMoveHandler(Protocol):
    """CAS-protected terminal command required from the coordinator layer."""

    async def __call__(
        self,
        room_id: str,
        seat_index: int,
        *,
        expected_version: int,
        operation_id: str,
        now: datetime | None = None,
    ) -> CommandOutcome:
        """Finish the current Bot seat with ``end_reason='no_legal_move'``."""


class RoomRuntimeError(RuntimeError):
    """Base error recorded by a room supervisor."""


class RoomRuntimeClosed(RoomRuntimeError):
    """Raised when new supervision is requested after shutdown."""


class RoomRuntimeCapabilityError(RoomRuntimeError):
    """Raised when no safe coordinator terminal command is available."""


class RoomRuntime:
    """Supervise automatic room work without blocking request handlers.

    Call :meth:`notify` after each committed room event.  The method returns
    immediately with the room's existing or newly-created task.  The task
    always reloads the authoritative repository snapshot; callers supply only
    a room identifier.

    ``strategy_resolver`` and ``word_index_resolver`` may use persisted room
    settings (difficulty/theme) captured by their application service.  The
    returned ``WordOption`` is selected entirely server-side.
    """

    def __init__(
        self,
        coordinator: RoomCoordinator,
        *,
        strategy_resolver: StrategyResolver,
        word_index_resolver: WordIndexResolver,
        bot_delay_seconds: float = 0.35,
        clock: Clock | None = None,
        sleep: Sleep = asyncio.sleep,
        no_legal_move_handler: NoLegalMoveHandler | None = None,
        on_error: ErrorCallback | None = None,
        opening_kana: Sequence[str] = _OPENING_KANA,
    ) -> None:
        if not 0 <= bot_delay_seconds <= MAX_BOT_DELAY_SECONDS:
            raise ValueError(
                "bot_delay_seconds must be between 0 and "
                f"{MAX_BOT_DELAY_SECONDS:g}"
            )
        if not opening_kana or any(
            not isinstance(kana, str) or len(kana) != 1
            for kana in opening_kana
        ):
            raise ValueError("opening_kana must contain one-character strings")

        self.coordinator = coordinator
        self._strategy_resolver = strategy_resolver
        self._word_index_resolver = word_index_resolver
        self._bot_delay_seconds = float(bot_delay_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._no_legal_move_handler = (
            no_legal_move_handler
            or getattr(coordinator, "finish_no_legal_move", None)
        )
        self._on_error = on_error
        self._opening_kana = tuple(dict.fromkeys(opening_kana))

        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._signals: dict[str, asyncio.Event] = {}
        self._errors: dict[str, BaseException] = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def active_room_ids(self) -> frozenset[str]:
        """Room IDs which currently have a live supervisor task."""

        return frozenset(
            room_id
            for room_id, task in self._tasks.items()
            if not task.done()
        )

    def last_error(self, room_id: str) -> BaseException | None:
        """Return the last unexpected/capability error for ``room_id``."""

        return self._errors.get(room_id)

    def notify(self, room_id: str) -> asyncio.Task[None]:
        """Start or wake one room supervisor.

        Duplicate calls never create a second task.  No snapshot or turn data
        is accepted here, preventing stale/client-derived fields from entering
        automatic commands.
        """

        if self._closed:
            raise RoomRuntimeClosed("room runtime is closed")
        if not room_id:
            raise ValueError("room_id is required")

        existing = self._tasks.get(room_id)
        if existing is not None and not existing.done():
            self._signals[room_id].set()
            return existing

        signal = asyncio.Event()
        self._signals[room_id] = signal
        self._errors.pop(room_id, None)
        task = asyncio.create_task(
            self._supervise(room_id, signal),
            name=f"shiritori-room-runtime:{room_id}",
        )
        self._tasks[room_id] = task
        task.add_done_callback(
            lambda completed, target=room_id: self._task_finished(
                target, completed
            )
        )
        return task

    def recover(
        self, active_room_ids: Iterable[str]
    ) -> tuple[asyncio.Task[None], ...]:
        """Rebuild supervisors for persisted active room IDs after restart."""

        unique_ids = tuple(dict.fromkeys(active_room_ids))
        return tuple(self.notify(room_id) for room_id in unique_ids)

    async def cancel(self, room_id: str) -> None:
        """Cancel one room's scheduled timer/Bot work."""

        task = self._tasks.get(room_id)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._tasks.get(room_id) is task:
            self._tasks.pop(room_id, None)
            self._signals.pop(room_id, None)

    async def close(self) -> None:
        """Cancel all background work and reject future notifications."""

        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._signals.clear()

    async def __aenter__(self) -> "RoomRuntime":
        if self._closed:
            raise RoomRuntimeClosed("room runtime is closed")
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _task_finished(
        self, room_id: str, completed: asyncio.Task[None]
    ) -> None:
        if self._tasks.get(room_id) is completed:
            self._tasks.pop(room_id, None)
            self._signals.pop(room_id, None)

    async def _supervise(
        self, room_id: str, signal: asyncio.Event
    ) -> None:
        try:
            await self._run_room(room_id, signal)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self._errors[room_id] = error
            await self._report_error(room_id, error)

    async def _run_room(
        self, room_id: str, signal: asyncio.Event
    ) -> None:
        while True:
            # Clearing before the load avoids losing a commit notification:
            # if notify() runs during/after the load, the event remains set and
            # forces another authoritative read.
            signal.clear()
            try:
                snapshot = await self.coordinator.load_snapshot(room_id)
            except RoomNotFound:
                return

            if snapshot.status is not RoomStatus.ACTIVE:
                return

            now = self._now()
            if (
                snapshot.deadline_at is not None
                and now >= snapshot.deadline_at
            ):
                await self._expire(snapshot)
                continue

            seat = snapshot.players[snapshot.current_turn]
            if seat.controller is SeatController.BOT:
                woke_for_event = await self._wait_or_signal(
                    signal,
                    self._bounded_bot_wait(snapshot, now),
                )
                if woke_for_event:
                    continue
                await self._play_bot_turn(room_id)
                # The coordinator has committed and published the event.  Keep
                # looping so consecutive Bot seats progress automatically.
                continue

            if snapshot.deadline_at is None:
                # Unlimited human turns need no resident task.  A later commit
                # calls notify() and creates a fresh supervisor.
                return

            woke_for_event = await self._wait_or_signal(
                signal,
                max(0.0, (snapshot.deadline_at - now).total_seconds()),
            )
            if woke_for_event:
                continue
            # Reload on the next loop.  A racing human commit may have changed
            # the state_version or deadline while the timer was asleep.

    async def _expire(self, snapshot: RoomSnapshot) -> None:
        operation_id = _runtime_operation_id(
            "timeout", snapshot.room_id, snapshot.state_version
        )
        try:
            await self.coordinator.expire_turn(
                snapshot.room_id,
                expected_version=snapshot.state_version,
                operation_id=operation_id,
                now=self._now(),
            )
        except (
            RoomNotFound,
            RoomInactive,
            RoomVersionConflict,
            TurnDeadlineNotReached,
        ):
            # Another request/process won the race, or a newer snapshot moved
            # the deadline.  The next authoritative load determines the work.
            return

    async def _play_bot_turn(self, room_id: str) -> None:
        try:
            snapshot = await self.coordinator.load_snapshot(room_id)
        except RoomNotFound:
            return
        if snapshot.status is not RoomStatus.ACTIVE:
            return

        now = self._now()
        if snapshot.deadline_at is not None and now >= snapshot.deadline_at:
            await self._expire(snapshot)
            return

        seat = snapshot.players[snapshot.current_turn]
        if seat.controller is not SeatController.BOT:
            return

        strategy = self._strategy_resolver(snapshot, seat)
        index = self._word_index_resolver(snapshot)
        if not isinstance(strategy, BotStrategy):
            raise TypeError("strategy_resolver must return a BotStrategy")
        if not isinstance(index, WordIndex):
            raise TypeError("word_index_resolver must return a WordIndex")

        option = self._choose_option(snapshot, strategy, index)
        try:
            if option is None:
                await self._finish_no_legal_move(
                    snapshot,
                    operation_id=_runtime_operation_id(
                        "no-legal",
                        snapshot.room_id,
                        snapshot.state_version,
                        seat.index,
                    ),
                )
                return

            await self.coordinator.submit_bot_turn(
                snapshot.room_id,
                seat.index,
                surface=option.surface,
                reading=option.reading,
                canonical_key=option.canonical_key,
                expected_version=snapshot.state_version,
                operation_id=_runtime_operation_id(
                    "bot",
                    snapshot.room_id,
                    snapshot.state_version,
                    seat.index,
                ),
                now=self._now(),
            )
        except (
            RoomAuthorizationError,
            RoomInactive,
            RoomNotFound,
            RoomVersionConflict,
            TurnDeadlineExpired,
        ):
            # Human handback, another worker, or a deadline can win after word
            # selection.  No stale result is retried; the loop reloads state.
            return

    def _choose_option(
        self,
        snapshot: RoomSnapshot,
        strategy: BotStrategy,
        index: WordIndex,
    ) -> WordOption | None:
        used = snapshot.used_canonical_keys
        if snapshot.expected_kana is not None:
            context = BotContext(snapshot.expected_kana, used)
            option = strategy.choose(context, index)
            return self._require_server_legal(option, context, index)

        # A free opening word has no expected kana. Ask the injected strategy
        # for one candidate per server-owned bucket, then choose deterministically
        # across buckets. Prefer a move which does not immediately end in ん.
        candidates: list[WordOption] = []
        for kana in self._opening_kana:
            context = BotContext(kana, used)
            option = strategy.choose(context, index)
            checked = self._require_server_legal(option, context, index)
            if checked is not None:
                candidates.append(checked)
        if not candidates:
            return None
        safe = [option for option in candidates if not option.ends_with_n]
        pool = safe or candidates
        return min(
            pool,
            key=lambda option: (
                option.rank,
                option.reading,
                option.canonical_key,
                option.surface,
            ),
        )

    @staticmethod
    def _require_server_legal(
        option: WordOption | None,
        context: BotContext,
        index: WordIndex,
    ) -> WordOption | None:
        if option is None:
            return None
        if not isinstance(option, WordOption):
            raise TypeError("BotStrategy.choose must return WordOption or None")
        legal = index.legal_options(
            context.expected_kana,
            context.used_canonical_keys,
        )
        if option not in legal:
            raise RoomRuntimeError(
                "BotStrategy returned an option outside the server WordIndex"
            )
        return option

    async def _finish_no_legal_move(
        self,
        snapshot: RoomSnapshot,
        *,
        operation_id: str,
    ) -> CommandOutcome:
        handler = self._no_legal_move_handler
        if handler is None:
            raise RoomRuntimeCapabilityError(
                "RoomCoordinator.finish_no_legal_move is required to finish "
                "a Bot turn without a dictionary candidate"
            )
        return await handler(
            snapshot.room_id,
            snapshot.current_turn,
            expected_version=snapshot.state_version,
            operation_id=operation_id,
            now=self._now(),
        )

    def _bounded_bot_wait(
        self, snapshot: RoomSnapshot, now: datetime
    ) -> float:
        if snapshot.deadline_at is None:
            return self._bot_delay_seconds
        remaining = max(
            0.0, (snapshot.deadline_at - now).total_seconds()
        )
        return min(self._bot_delay_seconds, remaining)

    async def _wait_or_signal(
        self, signal: asyncio.Event, delay: float
    ) -> bool:
        """Return true when a commit notification won over the delay."""

        if signal.is_set():
            return True
        sleep_task = asyncio.create_task(self._sleep(max(0.0, delay)))
        signal_task = asyncio.create_task(signal.wait())
        try:
            done, _ = await asyncio.wait(
                (sleep_task, signal_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Prefer a queued authoritative-event wake when both complete in
            # the same event-loop turn.
            return signal_task in done and signal.is_set()
        finally:
            for task in (sleep_task, signal_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                sleep_task, signal_task, return_exceptions=True
            )

    async def _report_error(
        self, room_id: str, error: BaseException
    ) -> None:
        if self._on_error is None:
            return
        result = self._on_error(room_id, error)
        if inspect.isawaitable(result):
            await result

    def _now(self) -> datetime:
        value = self._clock()
        if (
            value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            raise ValueError("room runtime clock must return UTC")
        return value


def _runtime_operation_id(action: str, *semantic_parts: object) -> str:
    """Build a deterministic internal ID that always fits the 64-char DB column."""

    digest = sha256(repr(semantic_parts).encode("utf-8")).hexdigest()[:32]
    return f"runtime:{action}:{digest}"


__all__ = [
    "MAX_BOT_DELAY_SECONDS",
    "NoLegalMoveHandler",
    "RoomRuntime",
    "RoomRuntimeCapabilityError",
    "RoomRuntimeClosed",
    "RoomRuntimeError",
]
