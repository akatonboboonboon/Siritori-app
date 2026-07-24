"""Synchronous lobby use cases and their persistence boundary.

The web layer supplies an authenticated user ID, but this module deliberately
does not know how authentication works.  Persistence implementations must make
``join_waiting``, ``activate_waiting``, and ``leave_waiting`` atomic: the
service performs the same checks for useful errors, while the repository
repeats them while holding its database lock.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import re
import secrets
from threading import RLock
from typing import Protocol, runtime_checkable
from uuid import uuid4

from .models import RoomRole, RoomStatus as StoredRoomStatus
from .rooms import (
    RoomMode,
    RoomSnapshot as ActiveRoomSnapshot,
    RoomStatus as ActiveRoomStatus,
    SeatPicker,
    create_room_snapshot,
)


INVITE_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DEFAULT_INVITE_CODE_LENGTH = 6
MAX_INVITE_CODE_ATTEMPTS = 12
_INVITE_CODE_PATTERN = re.compile(r"^[A-Z0-9]{4,12}$")
_THEME_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class LobbyError(RuntimeError):
    """Base class for expected lobby failures."""


class LobbyRoomNotFound(LobbyError):
    """The exact invite code or room ID does not identify an open room."""


class InviteCodeConflict(LobbyError):
    """A newly generated invite code already exists."""


class InviteCodeGenerationError(LobbyError):
    """Unique invite-code generation exhausted its bounded retry budget."""


class LobbyCapacityError(LobbyError):
    """A player cannot join because every seat is occupied."""


class SpectatorsDisabledError(LobbyError):
    """The room owner disabled spectators."""


class LobbyAuthorizationError(LobbyError):
    """The authenticated user is not allowed to perform the operation."""


class LobbyStateError(LobbyError):
    """The requested operation is invalid in the room's current state."""


class LobbyNotReadyError(LobbyStateError):
    """A PvP match cannot start until all player conditions are satisfied."""


class LobbyMemberError(LobbyError):
    """The user is absent, duplicated, or has an incompatible role."""


class LobbyRevisionConflict(LobbyError):
    """The room changed between the service check and atomic persistence."""

    def __init__(self, current_room: LobbyRoomSnapshot | None) -> None:
        super().__init__("the lobby changed; reload its latest state")
        self.current_room = current_room


def _require_user_id(user_id: str) -> str:
    if not isinstance(user_id, str) or not user_id or user_id.isspace():
        raise ValueError("user_id must be a non-empty opaque string")
    if len(user_id) > 128:
        raise ValueError("user_id is too long")
    return user_id


def normalize_invite_code(raw_code: str) -> str:
    """Return one exact canonical code; partial/fuzzy lookup is never allowed."""

    if not isinstance(raw_code, str):
        raise ValueError("invite code must be text")
    code = raw_code.strip().upper()
    if not _INVITE_CODE_PATTERN.fullmatch(code):
        raise ValueError("invite code must be 4-12 ASCII letters or digits")
    return code


def generate_invite_code(
    length: int = DEFAULT_INVITE_CODE_LENGTH,
    *,
    chooser: Callable[[str], str] = secrets.choice,
) -> str:
    """Generate a cryptographically random, human-friendly invite code."""

    if type(length) is not int or not 4 <= length <= 12:
        raise ValueError("invite code length must be between 4 and 12")
    code = "".join(chooser(INVITE_CODE_ALPHABET) for _ in range(length))
    # A custom chooser is accepted for deterministic tests, so validate it.
    if len(code) != length or any(char not in INVITE_CODE_ALPHABET for char in code):
        raise ValueError("invite-code chooser returned an unsupported character")
    return code


def validate_turn_seconds(turn_seconds: int | None) -> int | None:
    """Validate the agreed unlimited or 3-180 second turn setting."""

    if turn_seconds is None:
        return None
    if type(turn_seconds) is not int or not 3 <= turn_seconds <= 180:
        raise ValueError("turn_seconds must be None or an integer from 3 to 180")
    return turn_seconds


def validate_theme_key(theme_key: str) -> str:
    if not isinstance(theme_key, str):
        raise ValueError("theme_key must be text")
    key = theme_key.strip().lower()
    if not _THEME_KEY_PATTERN.fullmatch(key):
        raise ValueError(
            "theme_key must start with a-z and contain only a-z, 0-9, "
            "'_' or '-' (maximum 32 characters)"
        )
    return key


@dataclass(frozen=True, slots=True)
class LobbyMember:
    user_id: str
    role: RoomRole
    seat_index: int | None
    ready: bool = False

    def __post_init__(self) -> None:
        _require_user_id(self.user_id)
        if not isinstance(self.role, RoomRole):
            raise ValueError("role must be a RoomRole")
        if self.role is RoomRole.PLAYER:
            if type(self.seat_index) is not int or self.seat_index < 0:
                raise ValueError("a player needs a non-negative seat index")
        elif self.seat_index is not None:
            raise ValueError("a spectator cannot occupy a player seat")
        if self.role is RoomRole.SPECTATOR and self.ready:
            raise ValueError("a spectator cannot be ready")


@dataclass(frozen=True, slots=True)
class LobbyRoomSnapshot:
    id: str
    room_code: str
    owner_user_id: str
    name: str
    status: StoredRoomStatus
    max_players: int
    allow_spectators: bool
    theme_key: str
    turn_seconds: int | None
    revision: int
    members: tuple[LobbyMember, ...]

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("room id is required")
        normalized_code = normalize_invite_code(self.room_code)
        if normalized_code != self.room_code:
            raise ValueError("room_code must already be canonical")
        _require_user_id(self.owner_user_id)
        if not isinstance(self.name, str) or not 1 <= len(self.name) <= 64:
            raise ValueError("room name must contain 1-64 characters")
        if not isinstance(self.status, StoredRoomStatus):
            raise ValueError("status must be a stored RoomStatus")
        if type(self.max_players) is not int or not 2 <= self.max_players <= 8:
            raise ValueError("max_players must be from 2 to 8")
        validate_theme_key(self.theme_key)
        validate_turn_seconds(self.turn_seconds)
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")

        user_ids = tuple(member.user_id for member in self.members)
        if len(user_ids) != len(set(user_ids)):
            raise ValueError("one user cannot have two lobby memberships")
        players = self.players
        if len(players) > self.max_players:
            raise ValueError("player count exceeds max_players")
        if tuple(player.seat_index for player in players) != tuple(
            range(len(players))
        ):
            raise ValueError("player seats must be contiguous and ordered")
        if self.status is not StoredRoomStatus.CLOSED:
            owner = self.member_for(self.owner_user_id)
            if owner is None or owner.role is not RoomRole.PLAYER:
                raise ValueError("an open room owner must be a player")

    @property
    def players(self) -> tuple[LobbyMember, ...]:
        return tuple(
            sorted(
                (
                    member
                    for member in self.members
                    if member.role is RoomRole.PLAYER
                ),
                key=lambda member: int(member.seat_index),
            )
        )

    @property
    def spectators(self) -> tuple[LobbyMember, ...]:
        return tuple(
            member
            for member in self.members
            if member.role is RoomRole.SPECTATOR
        )

    @property
    def all_players_ready(self) -> bool:
        return len(self.players) >= 2 and all(player.ready for player in self.players)

    def member_for(self, user_id: str) -> LobbyMember | None:
        return next(
            (member for member in self.members if member.user_id == user_id),
            None,
        )


@dataclass(frozen=True, slots=True)
class LobbyStartResult:
    lobby: LobbyRoomSnapshot
    game_id: str
    active_room: ActiveRoomSnapshot

    def __post_init__(self) -> None:
        if self.lobby.status is not StoredRoomStatus.ACTIVE:
            raise ValueError("a started lobby must be active")
        if not self.game_id or self.active_room.room_id != self.game_id:
            raise ValueError("game_id must identify the active room snapshot")


@dataclass(frozen=True, slots=True)
class LobbyLeaveResult:
    room: LobbyRoomSnapshot | None
    deleted: bool

    def __post_init__(self) -> None:
        if self.deleted == (self.room is not None):
            raise ValueError("deleted leave result cannot contain a room")


@runtime_checkable
class LobbyRepository(Protocol):
    """Atomic persistence operations required by :class:`LobbyService`."""

    def create_waiting_room(
        self,
        *,
        owner_user_id: str,
        room_code: str,
        name: str,
        max_players: int,
        allow_spectators: bool,
        theme_key: str,
        turn_seconds: int | None,
    ) -> LobbyRoomSnapshot:
        """Create the room and owner membership in one transaction."""

    def get_by_code(self, room_code: str) -> LobbyRoomSnapshot | None:
        """Look up exactly one canonical, unique invite code."""

    def find_active_game_id(
        self,
        *,
        room_code: str,
        user_id: str,
    ) -> str | None:
        """Return a validated active game only to one of its room members."""

    def join_waiting(
        self,
        *,
        room_id: str,
        user_id: str,
        role: RoomRole,
    ) -> LobbyRoomSnapshot:
        """Atomically check state/permissions/capacity and add the member."""

    def set_ready(
        self,
        *,
        room_id: str,
        user_id: str,
        ready: bool,
    ) -> LobbyRoomSnapshot:
        """Atomically change readiness for an existing player."""

    def activate_waiting(
        self,
        *,
        room_id: str,
        requesting_owner_user_id: str,
        expected_revision: int,
        game_id: str,
        active_room: ActiveRoomSnapshot,
        theme_key: str,
        turn_seconds: int | None,
    ) -> LobbyStartResult:
        """Re-check owner/readiness/revision and persist room plus game."""

    def leave_waiting(
        self,
        *,
        room_id: str,
        user_id: str,
    ) -> LobbyLeaveResult:
        """Atomically leave, transfer owner if needed, or delete last-player room."""


class LobbyService:
    """Validated lobby workflows suitable for ``asyncio.to_thread``."""

    def __init__(
        self,
        repository: LobbyRepository,
        *,
        code_factory: Callable[[], str] | None = None,
        game_id_factory: Callable[[], str] | None = None,
        seat_picker: SeatPicker | None = None,
        theme_resolver: Callable[[str], object] | None = None,
        max_code_attempts: int = MAX_INVITE_CODE_ATTEMPTS,
    ) -> None:
        if type(max_code_attempts) is not int or max_code_attempts < 1:
            raise ValueError("max_code_attempts must be a positive integer")
        self.repository = repository
        self.code_factory = code_factory or generate_invite_code
        self.game_id_factory = game_id_factory or (lambda: str(uuid4()))
        self.seat_picker = seat_picker
        self.theme_resolver = theme_resolver
        self.max_code_attempts = max_code_attempts

    def create_pvp_room(
        self,
        owner_user_id: str,
        *,
        name: str,
        max_players: int = 2,
        allow_spectators: bool = True,
        theme_key: str = "all",
        turn_seconds: int | None = None,
    ) -> LobbyRoomSnapshot:
        owner = _require_user_id(owner_user_id)
        if not isinstance(name, str):
            raise ValueError("room name must be text")
        clean_name = name.strip()
        if not 1 <= len(clean_name) <= 64:
            raise ValueError("room name must contain 1-64 characters")
        if type(max_players) is not int or not 2 <= max_players <= 8:
            raise ValueError("max_players must be from 2 to 8")
        if type(allow_spectators) is not bool:
            raise ValueError("allow_spectators must be boolean")
        key = validate_theme_key(theme_key)
        if self.theme_resolver is not None:
            try:
                self.theme_resolver(key)
            except KeyError as error:
                raise ValueError(f"unknown theme: {key}") from error
        seconds = validate_turn_seconds(turn_seconds)

        for _ in range(self.max_code_attempts):
            code = normalize_invite_code(self.code_factory())
            try:
                return self.repository.create_waiting_room(
                    owner_user_id=owner,
                    room_code=code,
                    name=clean_name,
                    max_players=max_players,
                    allow_spectators=allow_spectators,
                    theme_key=key,
                    turn_seconds=seconds,
                )
            except InviteCodeConflict:
                continue
        raise InviteCodeGenerationError(
            "could not allocate a unique invite code; retry later"
        )

    def get_room(self, raw_room_code: str) -> LobbyRoomSnapshot:
        code = normalize_invite_code(raw_room_code)
        room = self.repository.get_by_code(code)
        if room is None or room.status is StoredRoomStatus.CLOSED:
            raise LobbyRoomNotFound("room not found")
        # A persistence implementation must never return a fuzzy/partial hit.
        if room.room_code != code:
            raise RuntimeError("repository returned a different invite code")
        return room

    def active_game_id(self, user_id: str, raw_room_code: str) -> str:
        """Resolve a started lobby without exposing its game to outsiders.

        The repository performs membership, room-state, and coordinator-state
        checks in one persistence boundary. Unavailable, unauthorized,
        ambiguous, and corrupt cases intentionally share one response.
        """

        member_id = _require_user_id(user_id)
        code = normalize_invite_code(raw_room_code)
        game_id = self.repository.find_active_game_id(
            room_code=code,
            user_id=member_id,
        )
        if game_id is None:
            raise LobbyRoomNotFound("active game not found")
        if (
            not isinstance(game_id, str)
            or not game_id
            or game_id.isspace()
            or len(game_id) > 36
        ):
            raise RuntimeError("repository returned an invalid game ID")
        return game_id

    def join_as_player(
        self, user_id: str, raw_room_code: str
    ) -> LobbyRoomSnapshot:
        return self._join(user_id, raw_room_code, RoomRole.PLAYER)

    def join_as_spectator(
        self, user_id: str, raw_room_code: str
    ) -> LobbyRoomSnapshot:
        return self._join(user_id, raw_room_code, RoomRole.SPECTATOR)

    def _join(
        self,
        user_id: str,
        raw_room_code: str,
        role: RoomRole,
    ) -> LobbyRoomSnapshot:
        member_id = _require_user_id(user_id)
        room = self.get_room(raw_room_code)
        if room.status is not StoredRoomStatus.WAITING:
            raise LobbyStateError("new members can join only while waiting")
        existing = room.member_for(member_id)
        if existing is not None:
            if existing.role is role:
                return room
            raise LobbyMemberError("a member cannot change roles by joining again")
        if role is RoomRole.PLAYER and len(room.players) >= room.max_players:
            raise LobbyCapacityError("the room has no open player seat")
        if role is RoomRole.SPECTATOR and not room.allow_spectators:
            raise SpectatorsDisabledError("spectators are disabled")
        return self.repository.join_waiting(
            room_id=room.id,
            user_id=member_id,
            role=role,
        )

    def set_ready(
        self,
        user_id: str,
        raw_room_code: str,
        *,
        ready: bool,
    ) -> LobbyRoomSnapshot:
        member_id = _require_user_id(user_id)
        if type(ready) is not bool:
            raise ValueError("ready must be boolean")
        room = self.get_room(raw_room_code)
        if room.status is not StoredRoomStatus.WAITING:
            raise LobbyStateError("readiness changes are allowed only while waiting")
        member = room.member_for(member_id)
        if member is None:
            raise LobbyMemberError("user is not a room member")
        if member.role is not RoomRole.PLAYER:
            raise LobbyAuthorizationError("spectators cannot become ready")
        if member.ready is ready:
            return room
        return self.repository.set_ready(
            room_id=room.id,
            user_id=member_id,
            ready=ready,
        )

    def start(
        self,
        owner_user_id: str,
        raw_room_code: str,
    ) -> LobbyStartResult:
        owner = _require_user_id(owner_user_id)
        room = self.get_room(raw_room_code)
        if room.status is not StoredRoomStatus.WAITING:
            raise LobbyStateError("only a waiting room can start")
        if room.owner_user_id != owner:
            raise LobbyAuthorizationError("only the room owner can start")
        if len(room.players) < 2:
            raise LobbyNotReadyError("PvP needs at least two players")
        if not room.all_players_ready:
            raise LobbyNotReadyError("all players must be ready")

        game_id = self.game_id_factory()
        if not isinstance(game_id, str) or not game_id or len(game_id) > 36:
            raise ValueError("game_id_factory returned an invalid ID")
        picker_arguments = (
            {"seat_picker": self.seat_picker}
            if self.seat_picker is not None
            else {}
        )
        active_room = create_room_snapshot(
            game_id,
            (player.user_id for player in room.players),
            mode=RoomMode.PVP,
            spectators=(member.user_id for member in room.spectators),
            turn_seconds=room.turn_seconds,
            theme_key=room.theme_key,
            bot_difficulty="normal",
            **picker_arguments,
        )
        # create_room_snapshot deliberately leaves expected_kana unset, so the
        # cryptographically selected first participant may choose any word.
        return self.repository.activate_waiting(
            room_id=room.id,
            requesting_owner_user_id=owner,
            expected_revision=room.revision,
            game_id=game_id,
            active_room=active_room,
            theme_key=room.theme_key,
            turn_seconds=room.turn_seconds,
        )

    def leave(
        self,
        user_id: str,
        raw_room_code: str,
    ) -> LobbyLeaveResult:
        member_id = _require_user_id(user_id)
        room = self.get_room(raw_room_code)
        if room.status is not StoredRoomStatus.WAITING:
            raise LobbyStateError("this leave operation is only for waiting rooms")
        if room.member_for(member_id) is None:
            raise LobbyMemberError("user is not a room member")
        return self.repository.leave_waiting(
            room_id=room.id,
            user_id=member_id,
        )


class InMemoryLobbyRepository:
    """Thread-safe reference implementation for tests and local prototypes.

    Production must use a database implementation.  Methods intentionally
    repeat all security-sensitive checks inside the lock to document the
    transaction semantics that implementation must preserve.
    """

    def __init__(self) -> None:
        self._rooms_by_id: dict[str, LobbyRoomSnapshot] = {}
        self._room_id_by_code: dict[str, str] = {}
        self._starts_by_game_id: dict[str, LobbyStartResult] = {}
        self._game_id_by_room_id: dict[str, str] = {}
        self._lock = RLock()

    def create_waiting_room(
        self,
        *,
        owner_user_id: str,
        room_code: str,
        name: str,
        max_players: int,
        allow_spectators: bool,
        theme_key: str,
        turn_seconds: int | None,
    ) -> LobbyRoomSnapshot:
        with self._lock:
            if room_code in self._room_id_by_code:
                raise InviteCodeConflict(room_code)
            room = LobbyRoomSnapshot(
                id=str(uuid4()),
                room_code=room_code,
                owner_user_id=owner_user_id,
                name=name,
                status=StoredRoomStatus.WAITING,
                max_players=max_players,
                allow_spectators=allow_spectators,
                theme_key=theme_key,
                turn_seconds=turn_seconds,
                revision=0,
                members=(
                    LobbyMember(
                        owner_user_id,
                        RoomRole.PLAYER,
                        seat_index=0,
                    ),
                ),
            )
            self._rooms_by_id[room.id] = room
            self._room_id_by_code[room.room_code] = room.id
            return room

    def get_by_code(self, room_code: str) -> LobbyRoomSnapshot | None:
        with self._lock:
            room_id = self._room_id_by_code.get(room_code)
            return self._rooms_by_id.get(room_id) if room_id is not None else None

    def find_active_game_id(
        self,
        *,
        room_code: str,
        user_id: str,
    ) -> str | None:
        with self._lock:
            room_id = self._room_id_by_code.get(room_code)
            room = (
                self._rooms_by_id.get(room_id)
                if room_id is not None
                else None
            )
            if (
                room is None
                or room.room_code != room_code
                or room.status is not StoredRoomStatus.ACTIVE
                or room.member_for(user_id) is None
            ):
                return None

            game_id = self._game_id_by_room_id.get(room.id)
            matching = tuple(
                result
                for result in self._starts_by_game_id.values()
                if result.lobby.id == room.id
            )
            if (
                game_id is None
                or len(matching) != 1
                or matching[0].game_id != game_id
                or not _active_start_matches(room, matching[0])
            ):
                return None
            return game_id

    def join_waiting(
        self,
        *,
        room_id: str,
        user_id: str,
        role: RoomRole,
    ) -> LobbyRoomSnapshot:
        with self._lock:
            room = self._waiting_room(room_id)
            existing = room.member_for(user_id)
            if existing is not None:
                if existing.role is role:
                    return room
                raise LobbyMemberError("member already has a different role")
            if role is RoomRole.PLAYER:
                if len(room.players) >= room.max_players:
                    raise LobbyCapacityError("the room has no open player seat")
                member = LobbyMember(
                    user_id,
                    role,
                    seat_index=len(room.players),
                )
            else:
                if not room.allow_spectators:
                    raise SpectatorsDisabledError("spectators are disabled")
                member = LobbyMember(user_id, role, seat_index=None)
            updated = replace(
                room,
                members=(*room.members, member),
                revision=room.revision + 1,
            )
            self._rooms_by_id[room_id] = updated
            return updated

    def set_ready(
        self,
        *,
        room_id: str,
        user_id: str,
        ready: bool,
    ) -> LobbyRoomSnapshot:
        with self._lock:
            room = self._waiting_room(room_id)
            member = room.member_for(user_id)
            if member is None:
                raise LobbyMemberError("user is not a member")
            if member.role is not RoomRole.PLAYER:
                raise LobbyAuthorizationError("spectators cannot become ready")
            if member.ready is ready:
                return room
            members = tuple(
                replace(candidate, ready=ready)
                if candidate.user_id == user_id
                else candidate
                for candidate in room.members
            )
            updated = replace(
                room,
                members=members,
                revision=room.revision + 1,
            )
            self._rooms_by_id[room_id] = updated
            return updated

    def activate_waiting(
        self,
        *,
        room_id: str,
        requesting_owner_user_id: str,
        expected_revision: int,
        game_id: str,
        active_room: ActiveRoomSnapshot,
        theme_key: str,
        turn_seconds: int | None,
    ) -> LobbyStartResult:
        with self._lock:
            room = self._waiting_room(room_id)
            if room.revision != expected_revision:
                raise LobbyRevisionConflict(room)
            if room.owner_user_id != requesting_owner_user_id:
                raise LobbyAuthorizationError("only the room owner can start")
            if not room.all_players_ready:
                raise LobbyNotReadyError("all players must be ready")
            expected_users = tuple(player.user_id for player in room.players)
            actual_users = tuple(
                seat.owner_user_id for seat in active_room.players
            )
            if (
                active_room.room_id != game_id
                or actual_users != expected_users
                or active_room.turn_seconds != turn_seconds
                or active_room.theme_key != room.theme_key
                or active_room.bot_difficulty != "normal"
                or theme_key != room.theme_key
            ):
                raise LobbyRevisionConflict(room)
            if game_id in self._starts_by_game_id:
                raise LobbyStateError("game ID already exists")

            active_lobby = replace(
                room,
                status=StoredRoomStatus.ACTIVE,
                revision=room.revision + 1,
            )
            result = LobbyStartResult(active_lobby, game_id, active_room)
            self._rooms_by_id[room_id] = active_lobby
            self._starts_by_game_id[game_id] = result
            self._game_id_by_room_id[room_id] = game_id
            return result

    def leave_waiting(
        self,
        *,
        room_id: str,
        user_id: str,
    ) -> LobbyLeaveResult:
        with self._lock:
            room = self._waiting_room(room_id)
            member = room.member_for(user_id)
            if member is None:
                raise LobbyMemberError("user is not a member")

            remaining = tuple(
                candidate
                for candidate in room.members
                if candidate.user_id != user_id
            )
            remaining_players = tuple(
                candidate
                for candidate in remaining
                if candidate.role is RoomRole.PLAYER
            )
            if not remaining_players:
                self._rooms_by_id.pop(room.id, None)
                self._room_id_by_code.pop(room.room_code, None)
                return LobbyLeaveResult(None, deleted=True)

            players_by_old_seat = sorted(
                remaining_players,
                key=lambda member: int(member.seat_index),
            )
            reseated = {
                member.user_id: replace(member, seat_index=index)
                for index, member in enumerate(players_by_old_seat)
            }
            members = tuple(
                reseated.get(member.user_id, member)
                for member in remaining
            )
            owner_user_id = (
                players_by_old_seat[0].user_id
                if room.owner_user_id == user_id
                else room.owner_user_id
            )
            updated = replace(
                room,
                owner_user_id=owner_user_id,
                members=members,
                revision=room.revision + 1,
            )
            self._rooms_by_id[room_id] = updated
            return LobbyLeaveResult(updated, deleted=False)

    def _waiting_room(self, room_id: str) -> LobbyRoomSnapshot:
        room = self._rooms_by_id.get(room_id)
        if room is None or room.status is StoredRoomStatus.CLOSED:
            raise LobbyRoomNotFound("room not found")
        if room.status is not StoredRoomStatus.WAITING:
            raise LobbyStateError("room is no longer waiting")
        return room


def _active_start_matches(
    room: LobbyRoomSnapshot,
    result: LobbyStartResult,
) -> bool:
    active = result.active_room
    return (
        result.lobby == room
        and active.room_id == result.game_id
        and active.mode is RoomMode.PVP
        and active.status is ActiveRoomStatus.ACTIVE
        and tuple(seat.owner_user_id for seat in active.players)
        == tuple(player.user_id for player in room.players)
        and active.spectators
        == tuple(member.user_id for member in room.spectators)
        and active.theme_key == room.theme_key
        and active.bot_difficulty == "normal"
        and active.turn_seconds == room.turn_seconds
    )


__all__ = [
    "DEFAULT_INVITE_CODE_LENGTH",
    "INVITE_CODE_ALPHABET",
    "InMemoryLobbyRepository",
    "InviteCodeConflict",
    "InviteCodeGenerationError",
    "LobbyAuthorizationError",
    "LobbyCapacityError",
    "LobbyError",
    "LobbyLeaveResult",
    "LobbyMember",
    "LobbyMemberError",
    "LobbyNotReadyError",
    "LobbyRepository",
    "LobbyRevisionConflict",
    "LobbyRoomNotFound",
    "LobbyRoomSnapshot",
    "LobbyService",
    "LobbyStartResult",
    "LobbyStateError",
    "SpectatorsDisabledError",
    "generate_invite_code",
    "normalize_invite_code",
    "validate_theme_key",
    "validate_turn_seconds",
]
