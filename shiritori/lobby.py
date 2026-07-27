"""Synchronous lobby use cases and their persistence boundary.

The web layer supplies an authenticated user ID, but this module deliberately
does not know how authentication works.  Persistence implementations must make
``join_waiting``, ``join_active_spectator``, ``update_waiting_settings``,
``activate_waiting``, and ``leave_waiting`` atomic: the service performs the
same checks for useful errors, while the repository repeats them while holding
its database lock.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import hashlib
import re
import secrets
from threading import RLock
from typing import Protocol, runtime_checkable
import unicodedata
from uuid import uuid4

from .models import RoomRole, RoomStatus as StoredRoomStatus
from .rooms import (
    RoomMode,
    RoomSnapshot as ActiveRoomSnapshot,
    RoomStatus as ActiveRoomStatus,
    SeatController,
    SeatPicker,
    create_room_snapshot,
)
from .themes import ALL_THEME_ID


INVITE_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DEFAULT_INVITE_CODE_LENGTH = 10
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


class LobbyNameConflict(LobbyError):
    """Another non-closed room already uses the normalized room name."""


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


def normalize_room_name(raw_name: str) -> str:
    """Return the canonical display name used for storage and presentation."""

    if not isinstance(raw_name, str):
        raise ValueError("room name must be text")
    name = " ".join(unicodedata.normalize("NFKC", raw_name).split())
    if not 1 <= len(name) <= 64:
        raise ValueError("room name must contain 1-64 characters")
    return name


def room_name_key(raw_name: str) -> str:
    """Return the case-insensitive identity key for a room display name."""

    canonical_identity = normalize_room_name(raw_name).casefold()
    return hashlib.sha256(canonical_identity.encode("utf-8")).hexdigest()


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


WaitingGameplaySettings = tuple[int, int | None, bool]


def validate_waiting_gameplay_settings(
    settings: WaitingGameplaySettings | None,
) -> WaitingGameplaySettings | None:
    """Validate the gameplay settings a waiting client last displayed."""

    if settings is None:
        return None
    if type(settings) is not tuple or len(settings) != 3:
        raise ValueError("expected_gameplay_settings must be a three-item tuple")
    max_players, turn_seconds, bot_fill = settings
    if type(max_players) is not int or not 2 <= max_players <= 8:
        raise ValueError("expected max_players must be from 2 to 8")
    seconds = validate_turn_seconds(turn_seconds)
    if type(bot_fill) is not bool:
        raise ValueError("expected bot fill must be boolean")
    return (max_players, seconds, bot_fill)


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
    is_public: bool = False
    fill_empty_seats_with_bots: bool = False

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("room id is required")
        normalized_code = normalize_invite_code(self.room_code)
        if normalized_code != self.room_code:
            raise ValueError("room_code must already be canonical")
        _require_user_id(self.owner_user_id)
        if normalize_room_name(self.name) != self.name:
            raise ValueError("room name must already be canonical")
        if not isinstance(self.status, StoredRoomStatus):
            raise ValueError("status must be a stored RoomStatus")
        if type(self.max_players) is not int or not 2 <= self.max_players <= 8:
            raise ValueError("max_players must be from 2 to 8")
        validate_theme_key(self.theme_key)
        validate_turn_seconds(self.turn_seconds)
        if type(self.is_public) is not bool:
            raise ValueError("is_public must be boolean")
        if type(self.fill_empty_seats_with_bots) is not bool:
            raise ValueError("fill_empty_seats_with_bots must be boolean")
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
        minimum_players = 1 if self.fill_empty_seats_with_bots else 2
        return len(self.players) >= minimum_players and all(
            player.ready for player in self.players
        )

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
        is_public: bool = False,
        fill_empty_seats_with_bots: bool = False,
    ) -> LobbyRoomSnapshot:
        """Create the room and owner membership in one transaction."""

    def get_by_code(self, room_code: str) -> LobbyRoomSnapshot | None:
        """Look up exactly one canonical, unique invite code."""

    def list_public_waiting(
        self,
        *,
        limit: int = 50,
    ) -> tuple[LobbyRoomSnapshot, ...]:
        """List public waiting rooms in stable creation order."""

    def list_public_rooms(
        self,
        *,
        limit: int = 50,
    ) -> tuple[LobbyRoomSnapshot, ...]:
        """List public waiting rooms and active rooms open to spectators."""

    def find_active_game_id(
        self,
        *,
        room_code: str,
        user_id: str,
    ) -> str | None:
        """Return a validated active game only to one of its room members."""

    def find_open_room_by_game_id(
        self,
        *,
        historical_game_id: str,
        user_id: str,
    ) -> LobbyRoomSnapshot | None:
        """Resolve a finished PvP game's current open room for a member."""

    def join_waiting(
        self,
        *,
        room_id: str,
        user_id: str,
        role: RoomRole,
    ) -> LobbyRoomSnapshot:
        """Atomically check state/permissions/capacity and add the member."""

    def join_active_spectator(
        self,
        *,
        room_id: str,
        user_id: str,
    ) -> LobbyRoomSnapshot:
        """Atomically add a spectator to a public active lobby and game."""

    def set_ready(
        self,
        *,
        room_id: str,
        user_id: str,
        ready: bool,
        expected_gameplay_settings: WaitingGameplaySettings | None = None,
    ) -> LobbyRoomSnapshot:
        """Atomically change readiness for an existing player."""

    def update_waiting_settings(
        self,
        *,
        room_id: str,
        requesting_owner_user_id: str,
        expected_revision: int,
        max_players: int,
        allow_spectators: bool,
        turn_seconds: int | None,
        is_public: bool,
        fill_empty_seats_with_bots: bool,
    ) -> LobbyRoomSnapshot:
        """Atomically update owner-controlled settings of a waiting room."""

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

    def return_finished_to_waiting(
        self,
        *,
        requesting_user_id: str,
        finished_game_id: str,
    ) -> LobbyRoomSnapshot:
        """Atomically return a finished current round to the waiting lobby.

        Exact retries for the same finished round must be idempotent.
        """

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
        self.max_code_attempts = max_code_attempts

    def create_pvp_room(
        self,
        owner_user_id: str,
        *,
        name: str,
        max_players: int = 2,
        allow_spectators: bool = True,
        theme_key: str | None = None,
        turn_seconds: int | None = None,
        is_public: bool = False,
        fill_empty_seats_with_bots: bool = False,
    ) -> LobbyRoomSnapshot:
        """Create an unrestricted room; legacy theme arguments are ignored."""
        owner = _require_user_id(owner_user_id)
        clean_name = normalize_room_name(name)
        if type(max_players) is not int or not 2 <= max_players <= 8:
            raise ValueError("max_players must be from 2 to 8")
        if type(allow_spectators) is not bool:
            raise ValueError("allow_spectators must be boolean")
        if type(is_public) is not bool:
            raise ValueError("is_public must be boolean")
        if type(fill_empty_seats_with_bots) is not bool:
            raise ValueError("fill_empty_seats_with_bots must be boolean")
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
                    theme_key=ALL_THEME_ID,
                    turn_seconds=seconds,
                    is_public=is_public,
                    fill_empty_seats_with_bots=fill_empty_seats_with_bots,
                )
            except InviteCodeConflict:
                continue
        raise InviteCodeGenerationError(
            "could not allocate a unique invite code; retry later"
        )

    def list_public_waiting(
        self,
        *,
        limit: int = 50,
    ) -> tuple[LobbyRoomSnapshot, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        rooms = self.repository.list_public_waiting(limit=limit)
        if len(rooms) > limit or any(
            not room.is_public
            or room.status is not StoredRoomStatus.WAITING
            for room in rooms
        ):
            raise RuntimeError("repository returned an invalid public room listing")
        return rooms

    def list_public_rooms(
        self,
        *,
        limit: int = 50,
    ) -> tuple[LobbyRoomSnapshot, ...]:
        """Return public rooms that can accept a player or spectator."""

        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        rooms = self.repository.list_public_rooms(limit=limit)
        if len(rooms) > limit or any(
            not room.is_public
            or (
                room.status is not StoredRoomStatus.WAITING
                and not (
                    room.status is StoredRoomStatus.ACTIVE
                    and room.allow_spectators
                )
            )
            for room in rooms
        ):
            raise RuntimeError("repository returned an invalid public room listing")
        return rooms

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

    def open_room_for_game(
        self,
        user_id: str,
        historical_game_id: str,
    ) -> LobbyRoomSnapshot:
        """Resolve the continuing room for an authorized historical round.

        Missing games, closed rooms, and non-members deliberately share the
        same public error so an opaque game ID cannot disclose room membership.
        """

        member_id = _require_user_id(user_id)
        game_id = _require_user_id(historical_game_id)
        if len(game_id) > 36:
            raise ValueError("historical_game_id is too long")
        room = self.repository.find_open_room_by_game_id(
            historical_game_id=game_id,
            user_id=member_id,
        )
        if room is None:
            raise LobbyRoomNotFound("room not found")
        if (
            room.status
            not in (StoredRoomStatus.WAITING, StoredRoomStatus.ACTIVE)
            or room.member_for(member_id) is None
        ):
            raise RuntimeError("repository returned an unauthorized open room")
        return room

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
        existing = room.member_for(member_id)
        if existing is not None:
            if existing.role is role:
                return room
            raise LobbyMemberError("a member cannot change roles by joining again")
        if room.status is StoredRoomStatus.ACTIVE:
            if role is RoomRole.PLAYER:
                raise LobbyStateError(
                    "players cannot join after the match has started"
                )
            if not room.allow_spectators:
                raise SpectatorsDisabledError("spectators are disabled")
            return self.repository.join_active_spectator(
                room_id=room.id,
                user_id=member_id,
            )
        if room.status is not StoredRoomStatus.WAITING:
            raise LobbyStateError("new members can join only while waiting")
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
        expected_gameplay_settings: WaitingGameplaySettings | None = None,
    ) -> LobbyRoomSnapshot:
        member_id = _require_user_id(user_id)
        if type(ready) is not bool:
            raise ValueError("ready must be boolean")
        expected_settings = validate_waiting_gameplay_settings(
            expected_gameplay_settings
        )
        room = self.get_room(raw_room_code)
        if room.status is not StoredRoomStatus.WAITING:
            raise LobbyStateError("readiness changes are allowed only while waiting")
        member = room.member_for(member_id)
        if member is None:
            raise LobbyMemberError("user is not a room member")
        if member.role is not RoomRole.PLAYER:
            raise LobbyAuthorizationError("spectators cannot become ready")
        if expected_settings is not None and expected_settings != (
            room.max_players,
            room.turn_seconds,
            room.fill_empty_seats_with_bots,
        ):
            raise LobbyRevisionConflict(room)
        if member.ready is ready:
            return room
        return self.repository.set_ready(
            room_id=room.id,
            user_id=member_id,
            ready=ready,
            expected_gameplay_settings=expected_settings,
        )

    def update_settings(
        self,
        owner_user_id: str,
        raw_room_code: str,
        *,
        expected_revision: int,
        max_players: int,
        allow_spectators: bool,
        turn_seconds: int | None,
        is_public: bool,
        fill_empty_seats_with_bots: bool,
    ) -> LobbyRoomSnapshot:
        """Update settings while waiting without silently overwriting changes."""

        owner = _require_user_id(owner_user_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        if type(max_players) is not int or not 2 <= max_players <= 8:
            raise ValueError("max_players must be from 2 to 8")
        if type(allow_spectators) is not bool:
            raise ValueError("allow_spectators must be boolean")
        if type(is_public) is not bool:
            raise ValueError("is_public must be boolean")
        if type(fill_empty_seats_with_bots) is not bool:
            raise ValueError("fill_empty_seats_with_bots must be boolean")
        seconds = validate_turn_seconds(turn_seconds)

        room = self.get_room(raw_room_code)
        if room.status is not StoredRoomStatus.WAITING:
            raise LobbyStateError("settings can change only while waiting")
        if room.owner_user_id != owner:
            raise LobbyAuthorizationError(
                "only the room owner can change settings"
            )
        if max_players < len(room.players):
            raise LobbyCapacityError(
                "max_players cannot be lower than the current player count"
            )
        return self.repository.update_waiting_settings(
            room_id=room.id,
            requesting_owner_user_id=owner,
            expected_revision=expected_revision,
            max_players=max_players,
            allow_spectators=allow_spectators,
            turn_seconds=seconds,
            is_public=is_public,
            fill_empty_seats_with_bots=fill_empty_seats_with_bots,
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
        if not room.fill_empty_seats_with_bots and len(room.players) < 2:
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
        permanent_bot_count = (
            room.max_players - len(room.players)
            if room.fill_empty_seats_with_bots
            else 0
        )
        active_room = create_room_snapshot(
            game_id,
            (player.user_id for player in room.players),
            mode=RoomMode.PVP,
            permanent_bot_count=permanent_bot_count,
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

    def return_to_waiting(
        self,
        user_id: str,
        finished_game_id: str,
    ) -> LobbyRoomSnapshot:
        """Return one finished current round to its persistent waiting lobby.

        Persistence resolves the room from the opaque game ID and repeats the
        membership/current-round checks under one lock. This method never
        rewrites the finished game; the next ``start`` creates a new game ID.
        """

        member_id = _require_user_id(user_id)
        game_id = _require_user_id(finished_game_id)
        if len(game_id) > 36:
            raise ValueError("finished_game_id is too long")
        room = self.repository.return_finished_to_waiting(
            requesting_user_id=member_id,
            finished_game_id=game_id,
        )
        if room.status is not StoredRoomStatus.WAITING:
            raise RuntimeError("repository returned a non-waiting rematch lobby")
        if room.member_for(member_id) is None:
            raise RuntimeError("repository returned a lobby without the requester")
        return room

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
        self._room_id_by_name_key: dict[str, str] = {}
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
        is_public: bool = False,
        fill_empty_seats_with_bots: bool = False,
    ) -> LobbyRoomSnapshot:
        with self._lock:
            if room_code in self._room_id_by_code:
                raise InviteCodeConflict(room_code)
            name_key = room_name_key(name)
            existing_id = self._room_id_by_name_key.get(name_key)
            existing = (
                self._rooms_by_id.get(existing_id)
                if existing_id is not None
                else None
            )
            if existing is not None and existing.status is not StoredRoomStatus.CLOSED:
                raise LobbyNameConflict(name)
            if existing_id is not None:
                self._room_id_by_name_key.pop(name_key, None)
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
                is_public=is_public,
                fill_empty_seats_with_bots=fill_empty_seats_with_bots,
            )
            self._rooms_by_id[room.id] = room
            self._room_id_by_code[room.room_code] = room.id
            self._room_id_by_name_key[name_key] = room.id
            return room

    def get_by_code(self, room_code: str) -> LobbyRoomSnapshot | None:
        with self._lock:
            room_id = self._room_id_by_code.get(room_code)
            return self._rooms_by_id.get(room_id) if room_id is not None else None

    def list_public_waiting(
        self,
        *,
        limit: int = 50,
    ) -> tuple[LobbyRoomSnapshot, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        with self._lock:
            return tuple(
                room
                for room in self._rooms_by_id.values()
                if room.is_public and room.status is StoredRoomStatus.WAITING
            )[:limit]

    def list_public_rooms(
        self,
        *,
        limit: int = 50,
    ) -> tuple[LobbyRoomSnapshot, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        with self._lock:
            return tuple(
                room
                for room in self._rooms_by_id.values()
                if room.is_public
                and self._is_public_room_joinable(room)
            )[:limit]

    def _is_public_room_joinable(
        self,
        room: LobbyRoomSnapshot,
    ) -> bool:
        if room.status is StoredRoomStatus.WAITING:
            return True
        if (
            room.status is not StoredRoomStatus.ACTIVE
            or not room.allow_spectators
        ):
            return False
        game_id = self._game_id_by_room_id.get(room.id)
        started = self._starts_by_game_id.get(game_id) if game_id else None
        return started is not None and _active_start_matches(room, started)

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
            started = (
                self._starts_by_game_id.get(game_id)
                if game_id is not None
                else None
            )
            if (
                game_id is None
                or started is None
                or not _active_start_matches(room, started)
            ):
                return None
            return game_id

    def find_open_room_by_game_id(
        self,
        *,
        historical_game_id: str,
        user_id: str,
    ) -> LobbyRoomSnapshot | None:
        with self._lock:
            started = self._starts_by_game_id.get(historical_game_id)
            if (
                started is None
                or started.game_id != historical_game_id
                or started.active_room.status
                is not ActiveRoomStatus.FINISHED
            ):
                return None
            room = self._rooms_by_id.get(started.lobby.id)
            if (
                room is None
                or room.status
                not in (StoredRoomStatus.WAITING, StoredRoomStatus.ACTIVE)
                or room.member_for(user_id) is None
            ):
                return None
            return room

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

    def join_active_spectator(
        self,
        *,
        room_id: str,
        user_id: str,
    ) -> LobbyRoomSnapshot:
        with self._lock:
            room = self._rooms_by_id.get(room_id)
            if room is None or room.status is StoredRoomStatus.CLOSED:
                raise LobbyRoomNotFound("room not found")
            if room.status is not StoredRoomStatus.ACTIVE:
                raise LobbyStateError("room is not active")
            existing = room.member_for(user_id)
            if existing is not None:
                if existing.role is RoomRole.SPECTATOR:
                    return room
                raise LobbyMemberError("member already has a different role")
            if not room.allow_spectators:
                raise SpectatorsDisabledError("spectators are disabled")

            game_id = self._game_id_by_room_id.get(room.id)
            started = (
                self._starts_by_game_id.get(game_id)
                if game_id is not None
                else None
            )
            if started is None or not _active_start_matches(room, started):
                raise LobbyStateError("active game is unavailable")

            member = LobbyMember(
                user_id,
                RoomRole.SPECTATOR,
                seat_index=None,
            )
            updated_room = replace(
                room,
                members=(*room.members, member),
                revision=room.revision + 1,
            )
            updated_active = replace(
                started.active_room,
                spectators=(*started.active_room.spectators, user_id),
                state_version=started.active_room.state_version + 1,
            )
            updated_start = LobbyStartResult(
                updated_room,
                started.game_id,
                updated_active,
            )
            self._rooms_by_id[room.id] = updated_room
            self._starts_by_game_id[started.game_id] = updated_start
            return updated_room

    def set_ready(
        self,
        *,
        room_id: str,
        user_id: str,
        ready: bool,
        expected_gameplay_settings: WaitingGameplaySettings | None = None,
    ) -> LobbyRoomSnapshot:
        expected_settings = validate_waiting_gameplay_settings(
            expected_gameplay_settings
        )
        with self._lock:
            room = self._waiting_room(room_id)
            if expected_settings is not None and expected_settings != (
                room.max_players,
                room.turn_seconds,
                room.fill_empty_seats_with_bots,
            ):
                raise LobbyRevisionConflict(room)
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

    def update_waiting_settings(
        self,
        *,
        room_id: str,
        requesting_owner_user_id: str,
        expected_revision: int,
        max_players: int,
        allow_spectators: bool,
        turn_seconds: int | None,
        is_public: bool,
        fill_empty_seats_with_bots: bool,
    ) -> LobbyRoomSnapshot:
        with self._lock:
            room = self._waiting_room(room_id)
            if room.owner_user_id != requesting_owner_user_id:
                raise LobbyAuthorizationError(
                    "only the room owner can change settings"
                )
            if room.revision != expected_revision:
                raise LobbyRevisionConflict(room)
            if max_players < len(room.players):
                raise LobbyCapacityError(
                    "max_players cannot be lower than the current player count"
                )
            unchanged = (
                room.max_players == max_players
                and room.allow_spectators is allow_spectators
                and room.turn_seconds == turn_seconds
                and room.is_public is is_public
                and room.fill_empty_seats_with_bots
                is fill_empty_seats_with_bots
            )
            if unchanged:
                return room

            gameplay_changed = (
                room.max_players != max_players
                or room.turn_seconds != turn_seconds
                or room.fill_empty_seats_with_bots
                is not fill_empty_seats_with_bots
            )
            members = (
                tuple(
                    replace(member, ready=False)
                    if member.role is RoomRole.PLAYER and member.ready
                    else member
                    for member in room.members
                )
                if gameplay_changed
                else room.members
            )
            updated = replace(
                room,
                max_players=max_players,
                allow_spectators=allow_spectators,
                turn_seconds=turn_seconds,
                is_public=is_public,
                fill_empty_seats_with_bots=fill_empty_seats_with_bots,
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
            expected_bot_count = (
                room.max_players - len(room.players)
                if room.fill_empty_seats_with_bots
                else 0
            )
            if (
                active_room.room_id != game_id
                or not _active_player_layout_matches(
                    room,
                    active_room,
                    expected_bot_count=expected_bot_count,
                )
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

    def record_finished_game(
        self,
        finished_room: ActiveRoomSnapshot,
    ) -> None:
        """Record an external coordinator finish in this reference store.

        Production persists room turns through ``SQLAlchemyRoomRepository``.
        The in-memory lobby has no separate coordinator store, so tests and
        local prototypes use this hook before returning a room to waiting.
        """

        if (
            finished_room.mode is not RoomMode.PVP
            or finished_room.status is not ActiveRoomStatus.FINISHED
        ):
            raise ValueError("finished_room must be a finished PvP snapshot")
        with self._lock:
            game_id = finished_room.room_id
            started = self._starts_by_game_id.get(game_id)
            if started is None:
                raise LobbyRoomNotFound("game not found")
            if self._game_id_by_room_id.get(started.lobby.id) != game_id:
                raise LobbyStateError("game is not the room's current round")
            if finished_room.state_version < started.active_room.state_version:
                raise LobbyRevisionConflict(self._rooms_by_id[started.lobby.id])
            self._starts_by_game_id[game_id] = LobbyStartResult(
                lobby=started.lobby,
                game_id=game_id,
                active_room=finished_room,
            )

    def return_finished_to_waiting(
        self,
        *,
        requesting_user_id: str,
        finished_game_id: str,
    ) -> LobbyRoomSnapshot:
        with self._lock:
            started = self._starts_by_game_id.get(finished_game_id)
            if started is None:
                raise LobbyRoomNotFound("finished game not found")
            room = self._rooms_by_id.get(started.lobby.id)
            if room is None or room.status is StoredRoomStatus.CLOSED:
                raise LobbyRoomNotFound("room not found")
            if room.member_for(requesting_user_id) is None:
                raise LobbyAuthorizationError(
                    "only a current room member can request a rematch"
                )
            if self._game_id_by_room_id.get(room.id) != finished_game_id:
                raise LobbyStateError("game is not the room's current round")
            if started.active_room.status is not ActiveRoomStatus.FINISHED:
                raise LobbyStateError("current game has not finished")
            if room.status is StoredRoomStatus.WAITING:
                return room
            if room.status is not StoredRoomStatus.ACTIVE:
                raise LobbyStateError("room cannot return to waiting")

            members = tuple(
                replace(member, ready=False)
                if member.role is RoomRole.PLAYER and member.ready
                else member
                for member in room.members
            )
            waiting = replace(
                room,
                status=StoredRoomStatus.WAITING,
                members=members,
                revision=room.revision + 1,
            )
            self._rooms_by_id[room.id] = waiting
            return waiting

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
                name_key = room_name_key(room.name)
                if self._room_id_by_name_key.get(name_key) == room.id:
                    self._room_id_by_name_key.pop(name_key, None)
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
        and _active_player_layout_matches(
            room,
            active,
            expected_bot_count=(
                room.max_players - len(room.players)
                if room.fill_empty_seats_with_bots
                else 0
            ),
        )
        and active.spectators
        == tuple(member.user_id for member in room.spectators)
        and active.theme_key == room.theme_key
        and active.bot_difficulty == "normal"
        and active.turn_seconds == room.turn_seconds
    )


def _active_player_layout_matches(
    room: LobbyRoomSnapshot,
    active: ActiveRoomSnapshot,
    *,
    expected_bot_count: int,
) -> bool:
    humans = room.players
    if len(active.players) != len(humans) + expected_bot_count:
        return False
    human_seats = active.players[: len(humans)]
    bot_seats = active.players[len(humans) :]
    return (
        tuple(seat.owner_user_id for seat in human_seats)
        == tuple(player.user_id for player in humans)
        and all(seat.controller is SeatController.HUMAN for seat in human_seats)
        and all(
            seat.owner_user_id is None
            and seat.controller is SeatController.BOT
            and not seat.handback_pending
            for seat in bot_seats
        )
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
    "LobbyNameConflict",
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
    "normalize_room_name",
    "room_name_key",
    "validate_theme_key",
    "validate_turn_seconds",
]
