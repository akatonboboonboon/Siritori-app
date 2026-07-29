"""Multiplayer room coordination independent from NiceGUI and PostgreSQL.

The database implementation is represented by :class:`RoomRepository`.
``RoomCoordinator`` serializes work per room inside one process, while every
write still uses a state-version compare-and-swap and an idempotency key.  The
latter remains necessary if a request is retried or the app is later run in
more than one process.

This is a post-validation coordination layer. Values passed to the turn
methods must come from a server-side ``LexiconValidator`` result (including an
explicitly selected reading), never from hidden fields or other browser-supplied
``reading``/``canonical_key`` values.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
import inspect
import json
import re
import secrets
from typing import Final, Protocol, runtime_checkable
from uuid import uuid4

from .bots import canonical_kana, final_kana, first_kana
from .lexicon import LexiconResult, get_default_validator
from .oni_rules import OniConstraintSet
from .themes import ALL_THEME_ID, ThemeCatalog


DISCONNECT_GRACE_SECONDS = 15.0
_THEME_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_BOT_DIFFICULTIES = frozenset({"easy", "normal", "hard"})
_COMMAND_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RESERVED_OPERATION_PREFIXES = ("runtime:", "system:")
_COMMAND_FINGERPRINT_VERSION = 1
SUPPORTED_REACTIONS: Final[tuple[str, ...]] = ("👍", "👏", "😮", "😂", "🔥")
REACTION_COOLDOWN_SECONDS: Final = 1.0
REACTION_RATE_LIMIT_CAPACITY: Final = 2048
REACTION_DELIVERY_TIMEOUT_SECONDS: Final = 0.25
ONI_BOT_COUNT: Final = 1
ONI_BOT_DIFFICULTY: Final = "hard"
ONI_LIVES: Final = 3
ONI_TURN_SECONDS: Final = 30

OniConstraintResolver = Callable[["RoomSnapshot"], OniConstraintSet]


class Role(str, Enum):
    PLAYER = "player"
    SPECTATOR = "spectator"


class RoomMode(str, Enum):
    PVP = "pvp"
    SOLO_BOT = "solo_bot"


class RoomRuleSet(str, Enum):
    STANDARD = "standard"
    ONI = "oni"


class RoomStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    FINISHED = "finished"


class SeatController(str, Enum):
    HUMAN = "human"
    BOT = "bot"


class RoomEventKind(str, Enum):
    SNAPSHOT = "snapshot"
    CLOSED = "closed"
    REACTION = "reaction"


@dataclass(frozen=True, slots=True)
class PlayerSeat:
    """One turn-order seat.

    ``owner_user_id`` remains set during temporary Bot takeover, allowing the
    authenticated owner to reclaim the seat later.  A permanent Bot has no
    owner.
    """

    index: int
    owner_user_id: str | None
    controller: SeatController
    handback_pending: bool = False

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("seat index must be non-negative")
        if self.owner_user_id is None and self.controller is not SeatController.BOT:
            raise ValueError("a permanent Bot seat must be controlled by a Bot")
        if self.handback_pending and (
            self.owner_user_id is None
            or self.controller is not SeatController.BOT
        ):
            raise ValueError("only a temporary Bot seat can await handback")


@dataclass(frozen=True, slots=True)
class TurnRecord:
    surface: str
    reading: str
    canonical_key: str
    seat_index: int
    actor_user_id: str | None
    by_bot: bool
    submitted_at: datetime

    def __post_init__(self) -> None:
        if not self.surface or not self.reading or not self.canonical_key:
            raise ValueError("turn word fields must not be empty")
        _require_utc(self.submitted_at, "submitted_at")


@dataclass(frozen=True, slots=True)
class LifeLossRecord:
    """One authoritative failed-turn or surrender event."""

    seat_index: int
    reason: str
    surface: str | None
    reading: str | None
    remaining_lives: int
    eliminated: bool
    occurred_at: datetime

    def __post_init__(self) -> None:
        if type(self.seat_index) is not int or self.seat_index < 0:
            raise ValueError("life-loss seat index must be non-negative")
        if type(self.reason) is not str or not self.reason:
            raise ValueError("life-loss reason is required")
        if self.surface is not None and type(self.surface) is not str:
            raise ValueError("life-loss surface must be a string or None")
        if self.reading is not None and type(self.reading) is not str:
            raise ValueError("life-loss reading must be a string or None")
        if (self.surface is None) != (self.reading is None):
            raise ValueError(
                "life-loss surface and reading must both be set or both be absent"
            )
        if self.surface is not None and (
            self.surface == "" or self.reading == ""
        ):
            raise ValueError("life-loss word fields must not be empty")
        if type(self.remaining_lives) is not int or self.remaining_lives < 0:
            raise ValueError("remaining_lives must be a non-negative integer")
        if type(self.eliminated) is not bool:
            raise ValueError("eliminated must be boolean")
        if self.eliminated != (self.remaining_lives == 0):
            raise ValueError("an eliminated life-loss event must have zero lives")
        _require_utc(self.occurred_at, "occurred_at")


@dataclass(frozen=True, slots=True)
class RoomSnapshot:
    """Complete state required to rebuild a room after a process restart."""

    room_id: str
    mode: RoomMode
    status: RoomStatus
    players: tuple[PlayerSeat, ...]
    current_turn: int
    state_version: int = 0
    rule_set: RoomRuleSet = RoomRuleSet.STANDARD
    theme_key: str = "all"
    bot_difficulty: str = "normal"
    spectators: tuple[str, ...] = ()
    eliminated_seats: tuple[int, ...] = ()
    lives_per_player: int = 1
    remaining_lives: tuple[int, ...] = ()
    life_loss_events: tuple[LifeLossRecord, ...] = ()
    history: tuple[TurnRecord, ...] = ()
    expected_kana: str | None = None
    turn_seconds: int | None = None
    deadline_at: datetime | None = None
    paused_remaining_seconds: float | None = None
    timed_out_seat: int | None = None
    losing_seat: int | None = None
    end_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.room_id:
            raise ValueError("room_id is required")
        if len(self.players) < 2:
            raise ValueError("a room needs at least two seats")
        if tuple(seat.index for seat in self.players) != tuple(
            range(len(self.players))
        ):
            raise ValueError("seat indexes must be contiguous and ordered")
        if not 0 <= self.current_turn < len(self.players):
            raise ValueError("current_turn is outside the player list")
        if self.state_version < 0:
            raise ValueError("state_version must be non-negative")
        if not isinstance(self.rule_set, RoomRuleSet):
            raise ValueError("rule_set must be 'standard' or 'oni'")
        if (
            type(self.theme_key) is not str
            or not _THEME_KEY_PATTERN.fullmatch(self.theme_key)
        ):
            raise ValueError(
                "theme_key must start with a lowercase letter and contain "
                "1-32 lowercase letters, numbers, '_' or '-'"
            )
        if (
            type(self.bot_difficulty) is not str
            or self.bot_difficulty not in _BOT_DIFFICULTIES
        ):
            raise ValueError(
                "bot_difficulty must be 'easy', 'normal', or 'hard'"
            )
        if self.turn_seconds is not None and not 3 <= self.turn_seconds <= 180:
            raise ValueError("turn_seconds must be between 3 and 180")
        if self.deadline_at is not None:
            _require_utc(self.deadline_at, "deadline_at")
        if self.status is RoomStatus.PAUSED and self.deadline_at is not None:
            raise ValueError("a paused room cannot have a running deadline")
        if (
            self.paused_remaining_seconds is not None
            and self.paused_remaining_seconds < 0
        ):
            raise ValueError("paused remaining time cannot be negative")

        owners = [
            seat.owner_user_id
            for seat in self.players
            if seat.owner_user_id is not None
        ]
        if len(owners) != len(set(owners)):
            raise ValueError("one user cannot own multiple seats")
        if set(owners).intersection(self.spectators):
            raise ValueError("a player cannot also be a spectator")
        if (
            len(self.eliminated_seats) != len(set(self.eliminated_seats))
            or any(
                type(index) is not int
                or not 0 <= index < len(self.players)
                for index in self.eliminated_seats
            )
        ):
            raise ValueError("eliminated seat indexes must be unique and valid")
        if (
            type(self.lives_per_player) is not int
            or not 1 <= self.lives_per_player <= 5
        ):
            raise ValueError("lives_per_player must be from 1 to 5")
        if self.rule_set is RoomRuleSet.ONI:
            permanent_bot_count = sum(
                seat.owner_user_id is None for seat in self.players
            )
            if (
                self.mode is not RoomMode.SOLO_BOT
                or len(self.players) != ONI_BOT_COUNT + 1
                or permanent_bot_count != ONI_BOT_COUNT
                or self.bot_difficulty != ONI_BOT_DIFFICULTY
                or self.lives_per_player != ONI_LIVES
                or self.turn_seconds != ONI_TURN_SECONDS
                or self.theme_key != ALL_THEME_ID
            ):
                raise ValueError(
                    "Oni rooms require one Hard Bot, three lives, "
                    "a 30-second turn, and the complete word catalog"
                )
        if not self.remaining_lives:
            object.__setattr__(
                self,
                "remaining_lives",
                tuple(
                    0 if seat.index in self.eliminated_seats
                    else self.lives_per_player
                    for seat in self.players
                ),
            )
        if (
            len(self.remaining_lives) != len(self.players)
            or any(
                type(lives) is not int
                or not 0 <= lives <= self.lives_per_player
                for lives in self.remaining_lives
            )
        ):
            raise ValueError(
                "remaining_lives must contain one valid count per player"
            )
        zero_life_seats = {
            index
            for index, lives in enumerate(self.remaining_lives)
            if lives == 0
        }
        if zero_life_seats != set(self.eliminated_seats):
            raise ValueError(
                "zero-life seats must exactly match eliminated_seats"
            )
        for event in self.life_loss_events:
            if (
                not isinstance(event, LifeLossRecord)
                or event.seat_index >= len(self.players)
                or event.remaining_lives > self.lives_per_player
            ):
                raise ValueError("life_loss_events contain an invalid event")
        active_seat_count = len(self.players) - len(self.eliminated_seats)
        if (
            self.status in {RoomStatus.ACTIVE, RoomStatus.PAUSED}
            and self.current_turn in self.eliminated_seats
        ):
            raise ValueError("an eliminated seat cannot hold the current turn")
        if (
            self.status in {RoomStatus.ACTIVE, RoomStatus.PAUSED}
            and active_seat_count < 2
        ):
            raise ValueError(
                "an active or paused room needs at least two active seats"
            )
        if (
            self.status is RoomStatus.FINISHED
            and self.eliminated_seats
            and active_seat_count != 1
        ):
            raise ValueError(
                "a finished elimination room must have exactly one survivor"
            )

    @property
    def used_canonical_keys(self) -> frozenset[str]:
        return frozenset(turn.canonical_key for turn in self.history)

    @property
    def active_seat_indexes(self) -> tuple[int, ...]:
        eliminated = frozenset(self.eliminated_seats)
        return tuple(
            seat.index for seat in self.players
            if seat.index not in eliminated
        )

    def seat_for_user(self, user_id: str) -> PlayerSeat | None:
        return next(
            (
                seat
                for seat in self.players
                if seat.owner_user_id == user_id
            ),
            None,
        )

    def role_for_user(self, user_id: str) -> Role | None:
        seat = self.seat_for_user(user_id)
        if seat is not None and seat.index in self.eliminated_seats:
            return Role.SPECTATOR
        if seat is not None:
            return Role.PLAYER
        if user_id in self.spectators:
            return Role.SPECTATOR
        return None


@dataclass(frozen=True, slots=True)
class RoomReaction:
    emoji: str
    sender_user_id: str
    sender_role: Role
    sent_at: datetime

    def __post_init__(self) -> None:
        if self.emoji not in SUPPORTED_REACTIONS:
            raise ValueError("reaction emoji is not supported")
        if not self.sender_user_id:
            raise ValueError("reaction sender_user_id is required")
        _require_utc(self.sent_at, "reaction sent_at")


@dataclass(frozen=True, slots=True)
class RoomEvent:
    kind: RoomEventKind
    room_id: str
    snapshot: RoomSnapshot | None
    reason: str | None = None
    reaction: RoomReaction | None = None


RoomCallback = Callable[[RoomEvent], None | Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ClientConnection:
    room_id: str
    user_id: str
    client_id: str
    role: Role
    callback: RoomCallback | None = None


class RoomHub:
    """In-process subscriptions and multi-tab presence.

    No game state is stored here.  Losing the hub on a Render restart is safe
    because every reconnect obtains a fresh repository snapshot.
    """

    def __init__(self) -> None:
        self._connections: dict[str, dict[str, ClientConnection]] = {}

    def subscribe(
        self,
        room_id: str,
        user_id: str,
        client_id: str,
        role: Role,
        callback: RoomCallback | None = None,
    ) -> None:
        if not client_id:
            raise ValueError("client_id is required")
        room = self._connections.setdefault(room_id, {})
        previous = room.get(client_id)
        if previous is not None and previous.user_id != user_id:
            raise ValueError("client_id is already used by another user")
        room[client_id] = ClientConnection(
            room_id=room_id,
            user_id=user_id,
            client_id=client_id,
            role=role,
            callback=callback,
        )

    def unsubscribe(
        self, room_id: str, client_id: str
    ) -> ClientConnection | None:
        room = self._connections.get(room_id)
        if room is None:
            return None
        connection = room.pop(client_id, None)
        if not room:
            self._connections.pop(room_id, None)
        return connection

    def remove_user(
        self, room_id: str, user_id: str
    ) -> tuple[ClientConnection, ...]:
        room = self._connections.get(room_id)
        if room is None:
            return ()
        removed = tuple(
            connection
            for connection in room.values()
            if connection.user_id == user_id
        )
        for connection in removed:
            room.pop(connection.client_id, None)
        if not room:
            self._connections.pop(room_id, None)
        return removed

    def has_user(self, room_id: str, user_id: str) -> bool:
        return any(
            connection.user_id == user_id
            for connection in self._connections.get(room_id, {}).values()
        )

    def client_count(self, room_id: str, user_id: str | None = None) -> int:
        connections = self._connections.get(room_id, {}).values()
        if user_id is None:
            return len(tuple(connections))
        return sum(
            connection.user_id == user_id for connection in connections
        )

    def connected_user_count(self, room_id: str) -> int:
        return len(
            {
                connection.user_id
                for connection in self._connections.get(room_id, {}).values()
            }
        )

    async def publish(self, event: RoomEvent) -> None:
        callbacks = tuple(
            connection.callback
            for connection in self._connections.get(
                event.room_id, {}
            ).values()
            if connection.callback is not None
        )
        await _deliver_callbacks(callbacks, event)

    async def publish_ephemeral(
        self,
        event: RoomEvent,
        *,
        timeout_seconds: float,
    ) -> None:
        """Deliver an ephemeral event without trusting callbacks to finish."""

        if timeout_seconds <= 0:
            raise ValueError("ephemeral delivery timeout must be positive")
        callbacks = tuple(
            connection.callback
            for connection in self._connections.get(
                event.room_id, {}
            ).values()
            if connection.callback is not None
        )
        try:
            await asyncio.wait_for(
                _deliver_callbacks(callbacks, event),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            # wait_for cancels the callback gather, so repeated reactions
            # cannot leave one additional pending task per delivery.
            return


class RepositoryStatus(str, Enum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    NOT_FOUND = "not_found"
    VERSION_CONFLICT = "version_conflict"


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    operation_id: str
    snapshot: RoomSnapshot | None
    command_kind: str
    fingerprint: str
    expected_version: int
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class RepositoryResult:
    status: RepositoryStatus
    receipt: CommandReceipt | None = None
    current_snapshot: RoomSnapshot | None = None


@runtime_checkable
class RoomRepository(Protocol):
    """Persistence boundary for an atomic PostgreSQL implementation."""

    async def load(self, room_id: str) -> RoomSnapshot | None:
        """Load the authoritative room snapshot."""

    async def find_operation(
        self, room_id: str, operation_id: str
    ) -> CommandReceipt | None:
        """Return a previous command result for retry deduplication."""

    async def compare_and_swap(
        self,
        room_id: str,
        expected_version: int,
        operation_id: str,
        next_snapshot: RoomSnapshot,
        *,
        command_fingerprint: str,
    ) -> RepositoryResult:
        """Atomically store ``next_snapshot`` when the version still matches."""

    async def delete_if_version(
        self,
        room_id: str,
        expected_version: int,
        operation_id: str,
        *,
        command_fingerprint: str,
    ) -> RepositoryResult:
        """Atomically delete an empty PvP room."""


class InMemoryRoomRepository:
    """A reference repository for tests and local domain experiments."""

    def __init__(self, snapshots: Iterable[RoomSnapshot] = ()) -> None:
        self._rooms = {snapshot.room_id: snapshot for snapshot in snapshots}
        self._operations: dict[tuple[str, str], CommandReceipt] = {}
        self._lock = asyncio.Lock()

    async def create(self, snapshot: RoomSnapshot) -> None:
        async with self._lock:
            if snapshot.room_id in self._rooms:
                raise ValueError(f"room {snapshot.room_id!r} already exists")
            self._rooms[snapshot.room_id] = snapshot

    async def load(self, room_id: str) -> RoomSnapshot | None:
        async with self._lock:
            return self._rooms.get(room_id)

    async def find_operation(
        self, room_id: str, operation_id: str
    ) -> CommandReceipt | None:
        async with self._lock:
            return self._operations.get((room_id, operation_id))

    async def compare_and_swap(
        self,
        room_id: str,
        expected_version: int,
        operation_id: str,
        next_snapshot: RoomSnapshot,
        *,
        command_fingerprint: str,
    ) -> RepositoryResult:
        _validate_repository_command(
            room_id,
            expected_version,
            operation_id,
            command_fingerprint,
        )
        async with self._lock:
            operation_key = (room_id, operation_id)
            previous = self._operations.get(operation_key)
            if previous is not None:
                _require_receipt_match(
                    previous,
                    command_kind="compare_and_swap",
                    fingerprint=command_fingerprint,
                    expected_version=expected_version,
                    room_id=room_id,
                )
                return RepositoryResult(
                    RepositoryStatus.DUPLICATE,
                    receipt=previous,
                    current_snapshot=self._rooms.get(room_id),
                )

            current = self._rooms.get(room_id)
            if current is None:
                return RepositoryResult(RepositoryStatus.NOT_FOUND)
            if current.state_version != expected_version:
                return RepositoryResult(
                    RepositoryStatus.VERSION_CONFLICT,
                    current_snapshot=current,
                )
            if next_snapshot.room_id != room_id:
                raise ValueError("next snapshot belongs to another room")
            if next_snapshot.state_version != expected_version + 1:
                raise ValueError("next snapshot must increment state_version")

            self._rooms[room_id] = next_snapshot
            receipt = CommandReceipt(
                operation_id=operation_id,
                snapshot=next_snapshot,
                command_kind="compare_and_swap",
                fingerprint=command_fingerprint,
                expected_version=expected_version,
            )
            self._operations[operation_key] = receipt
            return RepositoryResult(
                RepositoryStatus.APPLIED,
                receipt=receipt,
                current_snapshot=next_snapshot,
            )

    async def delete_if_version(
        self,
        room_id: str,
        expected_version: int,
        operation_id: str,
        *,
        command_fingerprint: str,
    ) -> RepositoryResult:
        _validate_repository_command(
            room_id,
            expected_version,
            operation_id,
            command_fingerprint,
        )
        async with self._lock:
            operation_key = (room_id, operation_id)
            previous = self._operations.get(operation_key)
            if previous is not None:
                _require_receipt_match(
                    previous,
                    command_kind="delete",
                    fingerprint=command_fingerprint,
                    expected_version=expected_version,
                    room_id=room_id,
                )
                return RepositoryResult(
                    RepositoryStatus.DUPLICATE,
                    receipt=previous,
                    current_snapshot=self._rooms.get(room_id),
                )

            current = self._rooms.get(room_id)
            if current is None:
                return RepositoryResult(RepositoryStatus.NOT_FOUND)
            if current.state_version != expected_version:
                return RepositoryResult(
                    RepositoryStatus.VERSION_CONFLICT,
                    current_snapshot=current,
                )

            del self._rooms[room_id]
            receipt = CommandReceipt(
                operation_id=operation_id,
                snapshot=None,
                command_kind="delete",
                fingerprint=command_fingerprint,
                expected_version=expected_version,
                deleted=True,
            )
            self._operations[operation_key] = receipt
            return RepositoryResult(
                RepositoryStatus.APPLIED,
                receipt=receipt,
            )


class RoomError(RuntimeError):
    pass


class RoomOperationConflictError(RoomError):
    """An operation ID was reused for a non-identical semantic command."""

    def __init__(self, room_id: str, operation_id: str) -> None:
        super().__init__(
            f"operation {operation_id!r} for room {room_id!r} "
            "was already used by a different command"
        )
        self.room_id = room_id
        self.operation_id = operation_id


class RoomNotFound(RoomError):
    pass


class RoomAuthorizationError(RoomError):
    pass


class UnsupportedReactionError(RoomError):
    """Only the server-defined emoji allowlist may be broadcast."""


class ReactionRateLimitError(RoomError):
    """The same room member sent another reaction before cooldown expiry."""

    def __init__(self, retry_after_seconds: float) -> None:
        retry_after_seconds = max(0.0, retry_after_seconds)
        super().__init__(
            "reaction cooldown is active; "
            f"retry after {retry_after_seconds:.3f} seconds"
        )
        self.retry_after_seconds = retry_after_seconds


class ReactionCapacityError(ReactionRateLimitError):
    """The bounded limiter is full of cooldowns that have not expired."""

    def __init__(self, retry_after_seconds: float) -> None:
        retry_after_seconds = max(0.0, retry_after_seconds)
        RoomError.__init__(
            self,
            "reaction limiter is at capacity; "
            f"retry after {retry_after_seconds:.3f} seconds",
        )
        self.retry_after_seconds = retry_after_seconds


class RoomVersionConflict(RoomError):
    def __init__(self, current_snapshot: RoomSnapshot | None) -> None:
        super().__init__("room state version is stale")
        self.current_snapshot = current_snapshot


class RoomInactive(RoomError):
    pass


class InvalidMove(RoomError):
    pass


class TurnDeadlineExpired(RoomError):
    pass


class TurnDeadlineNotReached(RoomError):
    pass


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    operation_id: str
    snapshot: RoomSnapshot | None
    duplicate: bool = False
    deleted: bool = False

SeatPicker = Callable[[int], int]


def create_room_snapshot(
    room_id: str,
    human_player_ids: Iterable[str],
    *,
    mode: RoomMode,
    permanent_bot_count: int = 0,
    spectators: Iterable[str] = (),
    rule_set: RoomRuleSet = RoomRuleSet.STANDARD,
    turn_seconds: int | None = None,
    theme_key: str = "all",
    bot_difficulty: str = "normal",
    lives_per_player: int = 1,
    now: datetime | None = None,
    seat_picker: SeatPicker | None = None,
) -> RoomSnapshot:
    """Create an active match with a secure random first seat.

    ``seat_picker`` receives the total seat count and is injectable for
    deterministic tests. Production callers should leave it unset so
    :func:`secrets.randbelow` is used. ``expected_kana=None`` gives the
    randomly selected first participant a free opening word.
    """

    humans = tuple(human_player_ids)
    if not humans or any(not user_id for user_id in humans):
        raise ValueError("at least one non-empty human player id is required")
    if len(humans) != len(set(humans)):
        raise ValueError("human player ids must be unique")
    if permanent_bot_count < 0:
        raise ValueError("permanent_bot_count must be non-negative")
    if (
        type(lives_per_player) is not int or not 1 <= lives_per_player <= 5
    ):
        raise ValueError("lives_per_player must be from 1 to 5")
    if turn_seconds is not None and not 3 <= turn_seconds <= 180:
        raise ValueError("turn_seconds must be between 3 and 180")
    if mode is RoomMode.SOLO_BOT:
        if len(humans) != 1 or permanent_bot_count < 1:
            raise ValueError(
                "solo Bot mode needs one human and at least one Bot"
            )
    elif len(humans) + permanent_bot_count < 2:
        raise ValueError(
            "PvP mode needs at least two human or Bot seats"
        )

    seats = tuple(
        PlayerSeat(index, user_id, SeatController.HUMAN)
        for index, user_id in enumerate(humans)
    ) + tuple(
        PlayerSeat(index, None, SeatController.BOT)
        for index in range(
            len(humans), len(humans) + permanent_bot_count
        )
    )
    picker = seat_picker or secrets.randbelow
    starting_seat = picker(len(seats))
    if type(starting_seat) is not int or not 0 <= starting_seat < len(seats):
        raise ValueError("seat_picker returned an invalid seat index")

    server_now = now or datetime.now(timezone.utc)
    _require_utc(server_now, "server time")
    deadline = (
        server_now + timedelta(seconds=turn_seconds)
        if turn_seconds is not None
        else None
    )
    return RoomSnapshot(
        room_id=room_id,
        mode=mode,
        status=RoomStatus.ACTIVE,
        players=seats,
        rule_set=rule_set,
        spectators=tuple(spectators),
        lives_per_player=lives_per_player,
        remaining_lives=(lives_per_player,) * len(seats),
        current_turn=starting_seat,
        expected_kana=None,
        turn_seconds=turn_seconds,
        theme_key=theme_key,
        bot_difficulty=bot_difficulty,
        deadline_at=deadline,
    )


class WordSubmissionStatus(str, Enum):
    COMMITTED = "committed"
    READING_REQUIRED = "reading_required"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class WordSubmissionResult:
    status: WordSubmissionStatus
    surface: str
    message: str
    reading_choices: tuple[str, ...] = ()
    selected_reading: str | None = None
    outcome: CommandOutcome | None = None

    @property
    def requires_reading_choice(self) -> bool:
        return self.status is WordSubmissionStatus.READING_REQUIRED


@runtime_checkable
class WordLexicon(Protocol):
    def validate(self, raw_surface: str | None) -> LexiconResult:
        """Resolve raw input exclusively from the server-side dictionary."""


class LexiconRoomService:
    """Validate raw input, then delegate only dictionary-derived values.

    The public method has no ``canonical_key`` argument. A browser may choose
    one of the readings returned by the dictionary, but cannot supply a new
    reading or canonical key.
    """

    def __init__(
        self,
        coordinator: "RoomCoordinator",
        validator: WordLexicon | None = None,
        *,
        themes: ThemeCatalog | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.validator = (
            validator
            if validator is not None
            else get_default_validator()
        )
        self.themes = themes or ThemeCatalog()

    async def submit_user_word(
        self,
        room_id: str,
        user_id: str,
        raw_surface: str | None,
        *,
        chosen_reading: str | None = None,
        expected_version: int,
        operation_id: str,
        now: datetime | None = None,
    ) -> WordSubmissionResult:
        snapshot = await self.coordinator.load_snapshot(room_id)
        if snapshot.role_for_user(user_id) is not Role.PLAYER:
            raise RoomAuthorizationError("only a player can submit words")
        validation = self.validator.validate(raw_surface)
        filtered = self.themes.filter(
            ALL_THEME_ID,
            validation,
            selected_reading=chosen_reading,
        )

        if filtered.requires_reading_choice:
            return WordSubmissionResult(
                WordSubmissionStatus.READING_REQUIRED,
                validation.surface,
                filtered.message,
                reading_choices=filtered.allowed_readings,
            )
        if not filtered.accepted or filtered.selected_candidate is None:
            return WordSubmissionResult(
                WordSubmissionStatus.REJECTED,
                validation.surface,
                filtered.message,
                reading_choices=filtered.allowed_readings,
            )

        candidate = filtered.selected_candidate
        try:
            outcome = await self.coordinator.submit_user_turn(
                room_id,
                user_id,
                surface=candidate.surface,
                reading=candidate.reading,
                canonical_key=candidate.canonical_key,
                expected_version=expected_version,
                operation_id=operation_id,
                now=now,
            )
        except InvalidMove as error:
            return WordSubmissionResult(
                WordSubmissionStatus.REJECTED,
                validation.surface,
                str(error),
                reading_choices=validation.readings,
            )
        return WordSubmissionResult(
            WordSubmissionStatus.COMMITTED,
            validation.surface,
            validation.message,
            reading_choices=filtered.allowed_readings,
            selected_reading=candidate.reading,
            outcome=outcome,
        )


class RoomCoordinator:
    """Coordinate presence, turns, takeover, pause, and room cleanup."""

    def __init__(
        self,
        repository: RoomRepository,
        *,
        hub: RoomHub | None = None,
        disconnect_grace_seconds: float = DISCONNECT_GRACE_SECONDS,
        reaction_cooldown_seconds: float = REACTION_COOLDOWN_SECONDS,
        reaction_rate_limit_capacity: int = REACTION_RATE_LIMIT_CAPACITY,
        reaction_delivery_timeout_seconds: float = REACTION_DELIVERY_TIMEOUT_SECONDS,
        oni_constraint_resolver: OniConstraintResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if disconnect_grace_seconds < 0:
            raise ValueError("disconnect grace must be non-negative")
        if reaction_cooldown_seconds <= 0:
            raise ValueError("reaction cooldown must be positive")
        if (
            type(reaction_rate_limit_capacity) is not int
            or reaction_rate_limit_capacity < 1
        ):
            raise ValueError("reaction rate-limit capacity must be positive")
        if reaction_delivery_timeout_seconds <= 0:
            raise ValueError("reaction delivery timeout must be positive")
        if oni_constraint_resolver is not None and not callable(
            oni_constraint_resolver
        ):
            raise TypeError("oni_constraint_resolver must be callable")
        self.repository = repository
        self.hub = hub or RoomHub()
        self.disconnect_grace_seconds = disconnect_grace_seconds
        self.reaction_cooldown_seconds = reaction_cooldown_seconds
        self.reaction_rate_limit_capacity = reaction_rate_limit_capacity
        self.reaction_delivery_timeout_seconds = reaction_delivery_timeout_seconds
        self._oni_constraint_resolver = oni_constraint_resolver
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._locks: dict[str, asyncio.Lock] = {}
        self._disconnect_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._reaction_sent_at: dict[tuple[str, str], datetime] = {}
        self._activity_notifier: Callable[[str], object] | None = None

    def set_activity_notifier(
        self, notifier: Callable[[str], object] | None
    ) -> None:
        """Set the process-local hook used to wake automatic room work."""

        self._activity_notifier = notifier

    def _notify_activity(self, room_id: str) -> None:
        if self._activity_notifier is not None:
            self._activity_notifier(room_id)

    def _lock_for(self, room_id: str) -> asyncio.Lock:
        # There is no await between lookup and insertion, so this is atomic
        # within an asyncio event loop.
        return self._locks.setdefault(room_id, asyncio.Lock())

    def _now(self, supplied: datetime | None = None) -> datetime:
        value = supplied if supplied is not None else self._clock()
        _require_utc(value, "server time")
        return value

    async def load_snapshot(self, room_id: str) -> RoomSnapshot:
        snapshot = await self.repository.load(room_id)
        if snapshot is None:
            raise RoomNotFound(f"room {room_id!r} does not exist")
        return snapshot

    async def send_reaction(
        self,
        room_id: str,
        user_id: str,
        emoji: str,
        *,
        now: datetime | None = None,
    ) -> RoomReaction:
        """Broadcast one allowlisted, ephemeral room reaction.

        Authorization is checked against the freshly loaded repository
        snapshot. Reactions never update that snapshot, its history, or its
        state version, and deliberately do not wake automatic Bot work.
        """

        if type(emoji) is not str or emoji not in SUPPORTED_REACTIONS:
            raise UnsupportedReactionError(
                "reaction must be one of: " + " ".join(SUPPORTED_REACTIONS)
            )
        async with self._lock_for(room_id):
            snapshot = await self.load_snapshot(room_id)
            role = snapshot.role_for_user(user_id)
            if role is None:
                raise RoomAuthorizationError("user is not a room member")

            sent_at = self._now(now)
            self._prune_reaction_rate_limits(sent_at)
            key = (room_id, user_id)
            previous = self._reaction_sent_at.get(key)
            if previous is not None:
                elapsed = (sent_at - previous).total_seconds()
                if elapsed < self.reaction_cooldown_seconds:
                    raise ReactionRateLimitError(
                        self.reaction_cooldown_seconds - elapsed
                    )

            if (
                key not in self._reaction_sent_at
                and len(self._reaction_sent_at)
                >= self.reaction_rate_limit_capacity
            ):
                retry_after = min(
                    self.reaction_cooldown_seconds
                    - (sent_at - previous_sent_at).total_seconds()
                    for previous_sent_at in self._reaction_sent_at.values()
                )
                raise ReactionCapacityError(retry_after)

            self._reaction_sent_at[key] = sent_at
            reaction = RoomReaction(
                emoji=emoji,
                sender_user_id=user_id,
                sender_role=role,
                sent_at=sent_at,
            )
            event = RoomEvent(
                RoomEventKind.REACTION,
                room_id,
                None,
                reaction=reaction,
            )

        await self.hub.publish_ephemeral(
            event,
            timeout_seconds=self.reaction_delivery_timeout_seconds,
        )
        return reaction

    def _prune_reaction_rate_limits(
        self,
        now: datetime,
    ) -> None:
        """Expire cooldowns and keep the process-local cache strictly bounded."""

        cutoff = now - timedelta(seconds=self.reaction_cooldown_seconds)
        for key, sent_at in tuple(self._reaction_sent_at.items()):
            if sent_at <= cutoff:
                self._reaction_sent_at.pop(key, None)

    async def connect_client(
        self,
        room_id: str,
        user_id: str,
        client_id: str,
        callback: RoomCallback | None = None,
        *,
        now: datetime | None = None,
    ) -> RoomSnapshot:
        """Register a tab and return a freshly loaded authoritative snapshot."""

        event: RoomEvent
        subscribed = False
        async with self._lock_for(room_id):
            snapshot = await self.load_snapshot(room_id)
            role = snapshot.role_for_user(user_id)
            if role is None:
                raise RoomAuthorizationError("user is not a room member")

            self._cancel_disconnect(room_id, user_id)
            self.hub.subscribe(
                room_id,
                user_id,
                client_id,
                role,
                callback,
            )
            subscribed = True
            try:
                changed = self._state_after_reconnect(
                    snapshot,
                    user_id,
                    self._now(now),
                )
                if changed != snapshot:
                    operation_id = f"system:reconnect:{uuid4().hex}"
                    _validate_internal_operation_id(operation_id, "system:")
                    fingerprint = _command_fingerprint(
                        action="reconnect_client",
                        room_id=room_id,
                        expected_version=snapshot.state_version,
                        actor={"kind": "user", "user_id": user_id},
                        inputs={},
                    )
                    outcome = await self._store_snapshot(
                        snapshot,
                        changed,
                        operation_id,
                        fingerprint,
                    )
                    assert outcome.snapshot is not None
                    snapshot = outcome.snapshot
            except BaseException:
                if subscribed:
                    self.hub.unsubscribe(room_id, client_id)
                raise
            event = RoomEvent(RoomEventKind.SNAPSHOT, room_id, snapshot)

        await self.hub.publish(event)
        self._notify_activity(room_id)
        return snapshot

    async def disconnect_client(
        self, room_id: str, client_id: str
    ) -> asyncio.Task[None] | None:
        """Remove one tab and apply the appropriate absence policy.

        A user receives the reconnect grace only while somebody else is still
        connected to the room. Once the final connection disappears there is
        nobody to keep a PvP room alive, and no human who should let a solo Bot
        match continue, so cleanup/pause is committed before this method
        returns. Restart recovery deliberately remains grace-based because a
        freshly restarted process cannot distinguish reconnecting clients from
        a genuinely empty room yet.
        """

        outcome: CommandOutcome | None = None
        async with self._lock_for(room_id):
            connection = self.hub.unsubscribe(room_id, client_id)
            if connection is None:
                return None
            if self.hub.has_user(room_id, connection.user_id):
                return None
            self._cancel_disconnect(room_id, connection.user_id)

            if self.hub.connected_user_count(room_id) == 0:
                self._cancel_room_disconnects(room_id)
                snapshot = await self.repository.load(room_id)
                if (
                    snapshot is None
                    or snapshot.role_for_user(connection.user_id) is None
                ):
                    return None
                outcome = await self._apply_disconnect_absence(
                    snapshot,
                    connection.user_id,
                    action="disconnect_empty_room",
                )
            else:
                task = asyncio.create_task(
                    self._disconnect_after_grace(room_id, connection.user_id)
                )
                self._disconnect_tasks[(room_id, connection.user_id)] = task
                return task

        assert outcome is not None
        await self._publish_outcome(room_id, outcome)
        return None

    async def recover_after_restart(self, room_id: str) -> RoomSnapshot:
        """Re-arm absence grace for human owners after process restart.

        A restarted process has an empty in-memory hub. Each still-absent human
        receives the normal disconnect grace exactly once: an empty PvP room is
        deleted (including a finished PvP room whose lobby was still open), an
        empty active solo room is paused, and an active room with one
        reconnecting human hands the other seats to Bots.
        """

        async with self._lock_for(room_id):
            snapshot = await self.load_snapshot(room_id)
            recoverable = (
                snapshot.status is RoomStatus.ACTIVE
                or (
                    snapshot.mode is RoomMode.PVP
                    and snapshot.status is RoomStatus.FINISHED
                )
            )
            if not recoverable:
                return snapshot
            for seat in snapshot.players:
                user_id = seat.owner_user_id
                if user_id is None or self.hub.has_user(room_id, user_id):
                    continue
                key = (room_id, user_id)
                existing = self._disconnect_tasks.get(key)
                if existing is not None and not existing.done():
                    continue
                task = asyncio.create_task(
                    self._disconnect_after_grace(room_id, user_id)
                )
                self._disconnect_tasks[key] = task
            return snapshot

    async def leave(
        self,
        room_id: str,
        user_id: str,
        *,
        expected_version: int,
        operation_id: str,
        now: datetime | None = None,
    ) -> CommandOutcome:
        """Explicit leave: no grace; takeover or empty-room cleanup is immediate."""

        _validate_user_operation_id(operation_id)
        fingerprint = _command_fingerprint(
            action="leave",
            room_id=room_id,
            expected_version=expected_version,
            actor={"kind": "user", "user_id": user_id},
            inputs={},
        )
        async with self._lock_for(room_id):
            duplicate = await self._duplicate_outcome(
                room_id,
                operation_id,
                fingerprint,
                expected_version,
                allowed_command_kinds=("compare_and_swap", "delete"),
            )
            if duplicate is not None:
                return duplicate
            snapshot = await self.load_snapshot(room_id)
            self._require_version(snapshot, expected_version)
            if snapshot.role_for_user(user_id) is None:
                raise RoomAuthorizationError("user is not a room member")

            self._cancel_disconnect(room_id, user_id)
            self.hub.remove_user(room_id, user_id)
            outcome = await self._apply_absence(
                snapshot,
                user_id,
                operation_id,
                fingerprint,
                self._now(now),
                remove_spectator=True,
            )

        await self._publish_outcome(room_id, outcome)
        return outcome

    async def surrender(
        self,
        room_id: str,
        user_id: str,
        *,
        expected_version: int,
        operation_id: str,
        now: datetime | None = None,
    ) -> CommandOutcome:
        """Eliminate an active human-owned seat by explicit surrender.

        Surrender is distinct from disconnecting: the owner permanently leaves
        the active player order and becomes a spectator for the rest of the
        match. A temporary Bot takeover does not prevent the authenticated
        owner from surrendering their seat.
        """

        _validate_user_operation_id(operation_id)
        fingerprint = _command_fingerprint(
            action="surrender",
            room_id=room_id,
            expected_version=expected_version,
            actor={"kind": "user", "user_id": user_id},
            inputs={},
        )
        async with self._lock_for(room_id):
            duplicate = await self._duplicate_outcome(
                room_id,
                operation_id,
                fingerprint,
                expected_version,
            )
            if duplicate is not None:
                return duplicate
            snapshot = await self.load_snapshot(room_id)
            self._require_version(snapshot, expected_version)
            if snapshot.status is not RoomStatus.ACTIVE:
                raise RoomInactive("room is not active")

            seat = snapshot.seat_for_user(user_id)
            if (
                seat is None
                or seat.owner_user_id is None
                or seat.index in snapshot.eliminated_seats
            ):
                raise RoomAuthorizationError(
                    "only an active human seat owner can surrender"
                )

            current_time = self._now(now)
            self._cancel_disconnect(room_id, user_id)
            surrender_event = LifeLossRecord(
                seat_index=seat.index,
                reason="surrender",
                surface=None,
                reading=None,
                remaining_lives=0,
                eliminated=True,
                occurred_at=current_time,
            )
            if snapshot.mode is RoomMode.SOLO_BOT:
                active = frozenset(snapshot.active_seat_indexes)
                permanent_bots = tuple(
                    candidate.index
                    for candidate in snapshot.players
                    if candidate.owner_user_id is None
                    and candidate.index in active
                )
                if not permanent_bots:
                    raise RoomAuthorizationError(
                        "solo surrender needs an active permanent Bot"
                    )
                survivor = next(
                    candidate
                    for offset in range(1, len(snapshot.players) + 1)
                    if (
                        candidate
                        := (seat.index + offset) % len(snapshot.players)
                    ) in permanent_bots
                )
                next_snapshot = replace(
                    snapshot,
                    status=RoomStatus.FINISHED,
                    current_turn=survivor,
                    eliminated_seats=tuple(
                        candidate.index
                        for candidate in snapshot.players
                        if candidate.index != survivor
                    ),
                    remaining_lives=tuple(
                        (
                            snapshot.remaining_lives[candidate.index]
                            if candidate.index == survivor
                            else 0
                        )
                        for candidate in snapshot.players
                    ),
                    life_loss_events=(*snapshot.life_loss_events, surrender_event),
                    expected_kana=None,
                    deadline_at=None,
                    paused_remaining_seconds=None,
                    timed_out_seat=None,
                    losing_seat=seat.index,
                    end_reason="surrender",
                    state_version=snapshot.state_version + 1,
                )
            else:
                eliminated = (*snapshot.eliminated_seats, seat.index)
                remaining = len(snapshot.players) - len(eliminated)
                finished = remaining == 1
                surrendered_current_turn = (
                    snapshot.current_turn == seat.index
                )
                next_turn = (
                    self._next_active_turn(
                        snapshot,
                        after=seat.index,
                        eliminated_seats=eliminated,
                    )
                    if surrendered_current_turn or finished
                    else snapshot.current_turn
                )
                deadline = snapshot.deadline_at
                if finished:
                    deadline = None
                elif surrendered_current_turn:
                    deadline = (
                        current_time
                        + timedelta(seconds=snapshot.turn_seconds)
                        if snapshot.turn_seconds is not None
                        else None
                    )
                next_snapshot = replace(
                    snapshot,
                    status=(
                        RoomStatus.FINISHED
                        if finished
                        else RoomStatus.ACTIVE
                    ),
                    current_turn=next_turn,
                    eliminated_seats=eliminated,
                    remaining_lives=tuple(
                        (
                            0
                            if index == seat.index
                            else lives
                        )
                        for index, lives in enumerate(snapshot.remaining_lives)
                    ),
                    life_loss_events=(*snapshot.life_loss_events, surrender_event),
                    expected_kana=(
                        None if finished else snapshot.expected_kana
                    ),
                    deadline_at=deadline,
                    paused_remaining_seconds=None,
                    timed_out_seat=None,
                    losing_seat=seat.index,
                    end_reason="surrender",
                    state_version=snapshot.state_version + 1,
                )

            outcome = await self._store_snapshot(
                snapshot,
                next_snapshot,
                operation_id,
                fingerprint,
            )

        await self._publish_outcome(room_id, outcome)
        return outcome

    async def submit_user_turn(
        self,
        room_id: str,
        user_id: str,
        *,
        surface: str,
        reading: str,
        canonical_key: str,
        expected_version: int,
        operation_id: str,
        now: datetime | None = None,
    ) -> CommandOutcome:
        """Commit a server-validated word for the authenticated seat owner."""

        return await self._submit_turn(
            room_id,
            user_id=user_id,
            bot_seat=None,
            surface=surface,
            reading=reading,
            canonical_key=canonical_key,
            expected_version=expected_version,
            operation_id=operation_id,
            now=now,
        )

    async def submit_bot_turn(
        self,
        room_id: str,
        seat_index: int,
        *,
        surface: str,
        reading: str,
        canonical_key: str,
        expected_version: int,
        operation_id: str,
        now: datetime | None = None,
    ) -> CommandOutcome:
        """Commit a dictionary-indexed word chosen for the active Bot seat."""

        return await self._submit_turn(
            room_id,
            user_id=None,
            bot_seat=seat_index,
            surface=surface,
            reading=reading,
            canonical_key=canonical_key,
            expected_version=expected_version,
            operation_id=operation_id,
            now=now,
        )

    async def expire_turn(
        self,
        room_id: str,
        *,
        expected_version: int,
        operation_id: str,
        now: datetime | None = None,
    ) -> CommandOutcome:
        """Apply a life loss only when the persisted UTC deadline is due."""

        _validate_internal_operation_id(operation_id, "runtime:")
        fingerprint = _command_fingerprint(
            action="expire_turn",
            room_id=room_id,
            expected_version=expected_version,
            actor={"kind": "runtime"},
            inputs={},
        )
        async with self._lock_for(room_id):
            duplicate = await self._duplicate_outcome(
                room_id,
                operation_id,
                fingerprint,
                expected_version,
            )
            if duplicate is not None:
                return duplicate
            snapshot = await self.load_snapshot(room_id)
            self._require_version(snapshot, expected_version)
            if snapshot.status is not RoomStatus.ACTIVE:
                raise RoomInactive("room is not active")
            current_time = self._now(now)
            if (
                snapshot.deadline_at is None
                or current_time < snapshot.deadline_at
            ):
                raise TurnDeadlineNotReached("turn deadline is not due")

            next_snapshot = self._apply_current_seat_life_loss(
                snapshot,
                players=self._complete_pending_handbacks(snapshot.players),
                reason="timeout",
                now=current_time,
                expected_kana=snapshot.expected_kana,
                timed_out=True,
            )
            outcome = await self._store_snapshot(
                snapshot,
                next_snapshot,
                operation_id,
                fingerprint,
            )

        await self._publish_outcome(room_id, outcome)
        return outcome

    async def finish_no_legal_move(
        self,
        room_id: str,
        seat_index: int,
        *,
        expected_version: int,
        operation_id: str,
        now: datetime | None = None,
    ) -> CommandOutcome:
        """Apply a life loss when the active Bot has no legal candidate."""

        _validate_internal_operation_id(operation_id, "runtime:")
        fingerprint = _command_fingerprint(
            action="finish_no_legal_move",
            room_id=room_id,
            expected_version=expected_version,
            actor={"kind": "bot", "seat_index": seat_index},
            inputs={},
        )
        async with self._lock_for(room_id):
            duplicate = await self._duplicate_outcome(
                room_id,
                operation_id,
                fingerprint,
                expected_version,
            )
            if duplicate is not None:
                return duplicate
            snapshot = await self.load_snapshot(room_id)
            self._require_version(snapshot, expected_version)
            if snapshot.status is not RoomStatus.ACTIVE:
                raise RoomInactive("room is not active")
            current_time = self._now(now)
            if (
                snapshot.deadline_at is not None
                and current_time >= snapshot.deadline_at
            ):
                raise TurnDeadlineExpired("turn deadline has expired")
            seat = snapshot.players[snapshot.current_turn]
            if (
                seat.index != seat_index
                or seat.controller is not SeatController.BOT
            ):
                raise RoomAuthorizationError(
                    "only the current Bot seat can have no legal move"
                )
            next_snapshot = self._apply_current_seat_life_loss(
                snapshot,
                players=self._complete_pending_handbacks(snapshot.players),
                reason="no_legal_move",
                now=current_time,
                expected_kana=snapshot.expected_kana,
            )
            outcome = await self._store_snapshot(
                snapshot,
                next_snapshot,
                operation_id,
                fingerprint,
            )

        await self._publish_outcome(room_id, outcome)
        return outcome

    async def _submit_turn(
        self,
        room_id: str,
        *,
        user_id: str | None,
        bot_seat: int | None,
        surface: str,
        reading: str,
        canonical_key: str,
        expected_version: int,
        operation_id: str,
        now: datetime | None,
    ) -> CommandOutcome:
        if bot_seat is None:
            _validate_user_operation_id(operation_id)
            action = "submit_user_turn"
            actor: object = {"kind": "user", "user_id": user_id}
        else:
            _validate_internal_operation_id(operation_id, "runtime:")
            action = "submit_bot_turn"
            actor = {"kind": "bot", "seat_index": bot_seat}
        fingerprint = _command_fingerprint(
            action=action,
            room_id=room_id,
            expected_version=expected_version,
            actor=actor,
            inputs={
                "surface": surface,
                "reading": reading,
                "canonical_key": canonical_key,
            },
        )
        async with self._lock_for(room_id):
            duplicate = await self._duplicate_outcome(
                room_id,
                operation_id,
                fingerprint,
                expected_version,
            )
            if duplicate is not None:
                return duplicate
            snapshot = await self.load_snapshot(room_id)
            self._require_version(snapshot, expected_version)
            if snapshot.status is not RoomStatus.ACTIVE:
                raise RoomInactive("room is not active")

            current_time = self._now(now)
            if (
                snapshot.deadline_at is not None
                and current_time >= snapshot.deadline_at
            ):
                raise TurnDeadlineExpired("turn deadline has expired")

            seat = snapshot.players[snapshot.current_turn]
            by_bot = bot_seat is not None
            if by_bot:
                if bot_seat != seat.index:
                    raise RoomAuthorizationError("it is not this Bot's turn")
                if seat.controller is not SeatController.BOT:
                    raise RoomAuthorizationError("seat is controlled by its user")
            else:
                if seat.owner_user_id != user_id:
                    raise RoomAuthorizationError("it is not this user's turn")
                if seat.controller is not SeatController.HUMAN:
                    raise RoomAuthorizationError("seat is temporarily Bot-controlled")

            if not surface or not reading or not canonical_key:
                raise InvalidMove("word fields must not be empty")
            required_kana = (
                canonical_kana(snapshot.expected_kana)
                if snapshot.expected_kana is not None
                else None
            )
            if (
                required_kana is not None
                and first_kana(reading) != required_kana
            ):
                raise InvalidMove(
                    f"word must begin with {required_kana!r}"
                )
            if snapshot.rule_set is RoomRuleSet.ONI:
                if self._oni_constraint_resolver is None:
                    raise InvalidMove(
                        "鬼しりとりのルール判定を利用できません"
                    )
                constraints = self._oni_constraint_resolver(snapshot)
                if not isinstance(constraints, OniConstraintSet):
                    raise TypeError(
                        "oni_constraint_resolver must return OniConstraintSet"
                    )
                violations = constraints.violations(reading)
                if violations:
                    details = " / ".join(
                        violation.message for violation in violations
                    )
                    raise InvalidMove(f"鬼ルール違反: {details}")

            next_players = self._complete_pending_handbacks(snapshot.players)
            if canonical_key in snapshot.used_canonical_keys:
                # Repeating a normalized reading costs one life but is
                # deliberately not appended to the accepted-word history.
                next_snapshot = self._apply_current_seat_life_loss(
                    snapshot,
                    players=next_players,
                    reason="duplicate",
                    now=current_time,
                    expected_kana=required_kana,
                    surface=surface,
                    reading=reading,
                )
            else:
                last_kana = final_kana(reading)
                record = TurnRecord(
                    surface=surface,
                    reading=reading,
                    canonical_key=canonical_key,
                    seat_index=seat.index,
                    actor_user_id=user_id,
                    by_bot=by_bot,
                    submitted_at=current_time,
                )
                if last_kana == "ん":
                    next_snapshot = self._apply_current_seat_life_loss(
                        snapshot,
                        players=next_players,
                        history=(*snapshot.history, record),
                        reason="ends_with_n",
                        now=current_time,
                        surface=surface,
                        reading=reading,
                        # In an elimination match the next survivor inherits
                        # the kana this player had to answer with. The losing
                        # word itself cannot continue because it ends in ん.
                        expected_kana=required_kana,
                    )
                else:
                    next_turn = self._next_active_turn(
                        snapshot,
                        after=snapshot.current_turn,
                    )
                    deadline = (
                        current_time + timedelta(seconds=snapshot.turn_seconds)
                        if snapshot.turn_seconds is not None
                        else None
                    )
                    next_snapshot = replace(
                        snapshot,
                        players=next_players,
                        current_turn=next_turn,
                        history=(*snapshot.history, record),
                        expected_kana=last_kana,
                        deadline_at=deadline,
                        state_version=snapshot.state_version + 1,
                    )
            outcome = await self._store_snapshot(
                snapshot,
                next_snapshot,
                operation_id,
                fingerprint,
            )

        await self._publish_outcome(room_id, outcome)
        return outcome

    @staticmethod
    def _next_active_turn(
        snapshot: RoomSnapshot,
        *,
        after: int,
        eliminated_seats: tuple[int, ...] | None = None,
    ) -> int:
        eliminated = frozenset(
            snapshot.eliminated_seats
            if eliminated_seats is None
            else eliminated_seats
        )
        for offset in range(1, len(snapshot.players) + 1):
            candidate = (after + offset) % len(snapshot.players)
            if candidate not in eliminated:
                return candidate
        raise ValueError("a room must retain at least one active seat")

    def _apply_current_seat_life_loss(
        self,
        snapshot: RoomSnapshot,
        *,
        reason: str,
        now: datetime,
        expected_kana: str | None,
        players: tuple[PlayerSeat, ...] | None = None,
        history: tuple[TurnRecord, ...] | None = None,
        timed_out: bool = False,
        surface: str | None = None,
        reading: str | None = None,
    ) -> RoomSnapshot:
        losing_seat = snapshot.current_turn
        remaining_lives = list(snapshot.remaining_lives)
        remaining_lives[losing_seat] -= 1
        eliminated_now = remaining_lives[losing_seat] == 0
        eliminated = (
            (*snapshot.eliminated_seats, losing_seat)
            if eliminated_now
            else snapshot.eliminated_seats
        )
        next_turn = self._next_active_turn(
            snapshot,
            after=losing_seat,
            eliminated_seats=eliminated,
        )
        active_seat_count = len(snapshot.players) - len(eliminated)
        finished = eliminated_now and active_seat_count == 1
        deadline = (
            now + timedelta(seconds=snapshot.turn_seconds)
            if not finished and snapshot.turn_seconds is not None
            else None
        )
        event = LifeLossRecord(
            seat_index=losing_seat,
            reason=reason,
            surface=surface,
            reading=reading,
            remaining_lives=remaining_lives[losing_seat],
            eliminated=eliminated_now,
            occurred_at=now,
        )
        return replace(
            snapshot,
            status=RoomStatus.FINISHED if finished else RoomStatus.ACTIVE,
            players=snapshot.players if players is None else players,
            current_turn=next_turn,
            eliminated_seats=eliminated,
            remaining_lives=tuple(remaining_lives),
            life_loss_events=(*snapshot.life_loss_events, event),
            history=snapshot.history if history is None else history,
            expected_kana=None if finished else expected_kana,
            deadline_at=deadline,
            timed_out_seat=losing_seat if timed_out else None,
            losing_seat=losing_seat,
            end_reason=reason,
            state_version=snapshot.state_version + 1,
        )

    async def _disconnect_after_grace(
        self, room_id: str, user_id: str
    ) -> None:
        key = (room_id, user_id)
        try:
            await self._sleep(self.disconnect_grace_seconds)
            async with self._lock_for(room_id):
                if self.hub.has_user(room_id, user_id):
                    return
                snapshot = await self.repository.load(room_id)
                if snapshot is None or snapshot.role_for_user(user_id) is None:
                    return
                outcome = await self._apply_disconnect_absence(
                    snapshot,
                    user_id,
                    action="disconnect_after_grace",
                )
            await self._publish_outcome(room_id, outcome)
        finally:
            if self._disconnect_tasks.get(key) is asyncio.current_task():
                self._disconnect_tasks.pop(key, None)

    async def _apply_disconnect_absence(
        self,
        snapshot: RoomSnapshot,
        user_id: str,
        *,
        action: str,
    ) -> CommandOutcome:
        """Commit one server-owned disconnect transition under the room lock."""

        operation_id = f"system:disconnect:{uuid4().hex}"
        _validate_internal_operation_id(operation_id, "system:")
        fingerprint = _command_fingerprint(
            action=action,
            room_id=snapshot.room_id,
            expected_version=snapshot.state_version,
            actor={"kind": "user", "user_id": user_id},
            inputs={},
        )
        return await self._apply_absence(
            snapshot,
            user_id,
            operation_id,
            fingerprint,
            self._now(),
            remove_spectator=False,
        )

    async def _apply_absence(
        self,
        snapshot: RoomSnapshot,
        user_id: str,
        operation_id: str,
        fingerprint: str,
        now: datetime,
        *,
        remove_spectator: bool,
    ) -> CommandOutcome:
        if self.hub.connected_user_count(snapshot.room_id) == 0:
            if snapshot.mode is RoomMode.PVP:
                result = await self.repository.delete_if_version(
                    snapshot.room_id,
                    snapshot.state_version,
                    operation_id,
                    command_fingerprint=fingerprint,
                )
                return self._outcome_from_repository(
                    result,
                    operation_id,
                    room_id=snapshot.room_id,
                    command_kind="delete",
                    fingerprint=fingerprint,
                    expected_version=snapshot.state_version,
                )

            if snapshot.status is RoomStatus.ACTIVE:
                remaining = (
                    max(0.0, (snapshot.deadline_at - now).total_seconds())
                    if snapshot.deadline_at is not None
                    else None
                )
                paused = replace(
                    snapshot,
                    status=RoomStatus.PAUSED,
                    deadline_at=None,
                    paused_remaining_seconds=remaining,
                    state_version=snapshot.state_version + 1,
                )
                return await self._store_snapshot(
                    snapshot, paused, operation_id, fingerprint
                )

            # A finished solo match remains finished. An explicit leave still
            # stores a receipt/version so a retried command is idempotent.
            if remove_spectator:
                unchanged = replace(
                    snapshot,
                    state_version=snapshot.state_version + 1,
                )
                return await self._store_snapshot(
                    snapshot, unchanged, operation_id, fingerprint
                )
            return CommandOutcome(operation_id, snapshot)

        # A connected participant may still be reading the final result. The
        # finished state needs no temporary Bot takeover; the last subsequent
        # disconnect will use the empty-PvP branch above and close the room.
        if (
            snapshot.mode is RoomMode.PVP
            and snapshot.status is RoomStatus.FINISHED
        ):
            return CommandOutcome(operation_id, snapshot)

        seat = snapshot.seat_for_user(user_id)
        if (
            seat is not None
            and seat.index not in snapshot.eliminated_seats
            and seat.controller is not SeatController.BOT
        ):
            players = tuple(
                replace(
                    candidate,
                    controller=SeatController.BOT,
                    handback_pending=False,
                )
                if candidate.index == seat.index
                else candidate
                for candidate in snapshot.players
            )
            changed = replace(
                snapshot,
                players=players,
                state_version=snapshot.state_version + 1,
            )
            return await self._store_snapshot(
                snapshot, changed, operation_id, fingerprint
            )

        if remove_spectator and user_id in snapshot.spectators:
            changed = replace(
                snapshot,
                spectators=tuple(
                    spectator
                    for spectator in snapshot.spectators
                    if spectator != user_id
                ),
                state_version=snapshot.state_version + 1,
            )
            return await self._store_snapshot(
                snapshot, changed, operation_id, fingerprint
            )

        if remove_spectator:
            # The state already has the desired controller (for example, grace
            # enabled the Bot first). Persist a version-only change so retries
            # still find a durable operation receipt.
            changed = replace(
                snapshot,
                state_version=snapshot.state_version + 1,
            )
            return await self._store_snapshot(
                snapshot, changed, operation_id, fingerprint
            )

        return CommandOutcome(operation_id, snapshot)

    def _state_after_reconnect(
        self,
        snapshot: RoomSnapshot,
        user_id: str,
        now: datetime,
    ) -> RoomSnapshot:
        seat = snapshot.seat_for_user(user_id)
        changed = snapshot

        if (
            snapshot.mode is RoomMode.SOLO_BOT
            and snapshot.status is RoomStatus.PAUSED
            and seat is not None
        ):
            deadline = (
                now
                + timedelta(
                    seconds=snapshot.paused_remaining_seconds
                    if snapshot.paused_remaining_seconds is not None
                    else snapshot.turn_seconds
                )
                if snapshot.turn_seconds is not None
                else None
            )
            players = tuple(
                replace(
                    candidate,
                    controller=SeatController.HUMAN,
                    handback_pending=False,
                )
                if candidate.index == seat.index
                else candidate
                for candidate in snapshot.players
            )
            changed = replace(
                snapshot,
                status=RoomStatus.ACTIVE,
                players=players,
                deadline_at=deadline,
                paused_remaining_seconds=None,
                state_version=snapshot.state_version + 1,
            )
            return changed

        if seat is None or seat.controller is SeatController.HUMAN:
            return changed

        # If the Bot is already eligible to act in this exact turn, changing
        # controllers immediately can race an in-flight Bot job.  Mark it and
        # perform the handback after that committed move instead.
        pending = (
            snapshot.status is RoomStatus.ACTIVE
            and snapshot.current_turn == seat.index
        )
        players = tuple(
            replace(
                candidate,
                controller=(
                    SeatController.BOT if pending else SeatController.HUMAN
                ),
                handback_pending=pending,
            )
            if candidate.index == seat.index
            else candidate
            for candidate in snapshot.players
        )
        return replace(
            snapshot,
            players=players,
            state_version=snapshot.state_version + 1,
        )

    @staticmethod
    def _complete_pending_handbacks(
        players: tuple[PlayerSeat, ...],
    ) -> tuple[PlayerSeat, ...]:
        return tuple(
            replace(
                seat,
                controller=SeatController.HUMAN,
                handback_pending=False,
            )
            if seat.handback_pending
            else seat
            for seat in players
        )

    async def _duplicate_outcome(
        self,
        room_id: str,
        operation_id: str,
        fingerprint: str,
        expected_version: int,
        *,
        allowed_command_kinds: tuple[str, ...] = ("compare_and_swap",),
    ) -> CommandOutcome | None:
        receipt = await self.repository.find_operation(room_id, operation_id)
        if receipt is None:
            return None
        if receipt.command_kind not in allowed_command_kinds:
            raise RoomOperationConflictError(room_id, operation_id)
        _require_receipt_match(
            receipt,
            command_kind=receipt.command_kind,
            fingerprint=fingerprint,
            expected_version=expected_version,
            room_id=room_id,
        )
        return CommandOutcome(
            operation_id,
            receipt.snapshot,
            duplicate=True,
            deleted=receipt.deleted,
        )

    async def _store_snapshot(
        self,
        previous: RoomSnapshot,
        next_snapshot: RoomSnapshot,
        operation_id: str,
        fingerprint: str,
    ) -> CommandOutcome:
        result = await self.repository.compare_and_swap(
            previous.room_id,
            previous.state_version,
            operation_id,
            next_snapshot,
            command_fingerprint=fingerprint,
        )
        return self._outcome_from_repository(
            result,
            operation_id,
            room_id=previous.room_id,
            command_kind="compare_and_swap",
            fingerprint=fingerprint,
            expected_version=previous.state_version,
        )

    @staticmethod
    def _outcome_from_repository(
        result: RepositoryResult,
        operation_id: str,
        *,
        room_id: str,
        command_kind: str,
        fingerprint: str,
        expected_version: int,
    ) -> CommandOutcome:
        if result.status is RepositoryStatus.NOT_FOUND:
            raise RoomNotFound("room no longer exists")
        if result.status is RepositoryStatus.VERSION_CONFLICT:
            raise RoomVersionConflict(result.current_snapshot)
        if result.receipt is None:
            raise RuntimeError("repository returned no command receipt")
        _require_receipt_match(
            result.receipt,
            command_kind=command_kind,
            fingerprint=fingerprint,
            expected_version=expected_version,
            room_id=room_id,
        )
        return CommandOutcome(
            operation_id,
            result.receipt.snapshot,
            duplicate=result.status is RepositoryStatus.DUPLICATE,
            deleted=result.receipt.deleted,
        )

    @staticmethod
    def _require_version(
        snapshot: RoomSnapshot, expected_version: int
    ) -> None:
        if snapshot.state_version != expected_version:
            raise RoomVersionConflict(snapshot)

    def _cancel_disconnect(self, room_id: str, user_id: str) -> None:
        task = self._disconnect_tasks.pop((room_id, user_id), None)
        if task is not None and not task.done():
            task.cancel()

    def _cancel_room_disconnects(self, room_id: str) -> None:
        """Cancel stale per-user grace jobs before empty-room finalization."""

        for key, task in tuple(self._disconnect_tasks.items()):
            if key[0] != room_id:
                continue
            self._disconnect_tasks.pop(key, None)
            if task is not asyncio.current_task() and not task.done():
                task.cancel()

    async def _publish_outcome(
        self, room_id: str, outcome: CommandOutcome
    ) -> None:
        if outcome.deleted:
            event = RoomEvent(
                RoomEventKind.CLOSED,
                room_id,
                None,
                reason="empty",
            )
        elif outcome.snapshot is not None:
            event = RoomEvent(
                RoomEventKind.SNAPSHOT,
                room_id,
                outcome.snapshot,
            )
        else:
            return
        await self.hub.publish(event)
        self._notify_activity(room_id)


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _validate_operation_id(operation_id: str) -> None:
    if (
        not isinstance(operation_id, str)
        or not operation_id
        or len(operation_id) > 64
    ):
        raise ValueError("operation_id must contain 1-64 characters")


def _validate_user_operation_id(operation_id: str) -> None:
    _validate_operation_id(operation_id)
    if operation_id.startswith(_RESERVED_OPERATION_PREFIXES):
        raise ValueError("operation_id uses a reserved internal prefix")


def _validate_internal_operation_id(
    operation_id: str, required_prefix: str
) -> None:
    _validate_operation_id(operation_id)
    if required_prefix not in _RESERVED_OPERATION_PREFIXES:
        raise ValueError("invalid internal operation prefix")
    if not operation_id.startswith(required_prefix):
        raise ValueError(
            f"internal operation_id must start with {required_prefix!r}"
        )


def _command_fingerprint(
    *,
    action: str,
    room_id: str,
    expected_version: int,
    actor: object,
    inputs: object,
) -> str:
    if not isinstance(action, str) or not action:
        raise ValueError("command action is required")
    if not isinstance(room_id, str) or not room_id:
        raise ValueError("room_id is required")
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("expected_version must be a non-negative integer")
    try:
        raw = json.dumps(
            {
                "v": _COMMAND_FINGERPRINT_VERSION,
                "action": action,
                "room": room_id,
                "expected_version": expected_version,
                "actor": actor,
                "inputs": inputs,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("command fingerprint inputs must be JSON-safe") from error
    return sha256(raw).hexdigest()


def _validate_repository_command(
    room_id: str,
    expected_version: int,
    operation_id: str,
    command_fingerprint: str,
) -> None:
    if not isinstance(room_id, str) or not room_id:
        raise ValueError("room_id is required")
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("expected_version must be a non-negative integer")
    _validate_operation_id(operation_id)
    if (
        not isinstance(command_fingerprint, str)
        or not _COMMAND_FINGERPRINT_PATTERN.fullmatch(command_fingerprint)
    ):
        raise ValueError("command_fingerprint must be a lowercase SHA-256 hex digest")


def _require_receipt_match(
    receipt: CommandReceipt,
    *,
    command_kind: str,
    fingerprint: str,
    expected_version: int,
    room_id: str,
) -> None:
    if (
        receipt.command_kind != command_kind
        or receipt.fingerprint != fingerprint
        or receipt.expected_version != expected_version
        or (receipt.deleted and receipt.command_kind != "delete")
    ):
        raise RoomOperationConflictError(room_id, receipt.operation_id)


async def _deliver_callbacks(
    callbacks: tuple[RoomCallback, ...], event: RoomEvent
) -> None:
    pending: list[Awaitable[None]] = []
    for callback in callbacks:
        try:
            result = callback(event)
        except Exception:
            # A closed browser callback must not block other clients.
            continue
        if inspect.isawaitable(result):
            pending.append(result)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
