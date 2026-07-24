"""SQLAlchemy persistence for the synchronous waiting-room lobby service."""

from __future__ import annotations

from contextlib import AbstractContextManager
from threading import Lock, RLock
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import Database
from .lobby import (
    InviteCodeConflict,
    LobbyAuthorizationError,
    LobbyCapacityError,
    LobbyLeaveResult,
    LobbyMember,
    LobbyMemberError,
    LobbyNotReadyError,
    LobbyRevisionConflict,
    LobbyRoomNotFound,
    LobbyRoomSnapshot,
    LobbyStartResult,
    LobbyStateError,
    SpectatorsDisabledError,
    normalize_invite_code,
    validate_theme_key,
    validate_turn_seconds,
)
from .models import (
    Game,
    GameMode,
    PresenceState,
    Room,
    RoomMembership,
    RoomRole,
    RoomStatus as StoredRoomStatus,
    StoredGameStatus,
    utc_now,
)
from .room_persistence import (
    RoomSnapshotCorruptError,
    deserialize_room_snapshot,
    serialize_room_snapshot,
)
from .rooms import (
    RoomMode,
    RoomSnapshot as ActiveRoomSnapshot,
    RoomStatus as ActiveRoomStatus,
    SeatController,
)


_SQLITE_LOCKS_GUARD = Lock()
_SQLITE_LOCKS: dict[str, RLock] = {}


class SQLAlchemyLobbyRepository:
    """Atomic SQL implementation of :class:`shiritori.lobby.LobbyRepository`.

    PostgreSQL writes lock the ``rooms`` row with ``FOR UPDATE``. SQLite
    ignores that clause, so local tests use one process-wide lock per database
    URL. Production is expected to use PostgreSQL (Neon).
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self._sqlite_lock: RLock | None = None
        if database.engine.dialect.name == "sqlite":
            key = str(database.engine.url)
            with _SQLITE_LOCKS_GUARD:
                self._sqlite_lock = _SQLITE_LOCKS.setdefault(key, RLock())

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
        owner = _identifier(owner_user_id, "owner_user_id", 36)
        code = normalize_invite_code(room_code)
        clean_name = _room_name(name)
        if type(max_players) is not int or not 2 <= max_players <= 8:
            raise ValueError("max_players must be from 2 to 8")
        if type(allow_spectators) is not bool:
            raise ValueError("allow_spectators must be boolean")
        key = validate_theme_key(theme_key)
        seconds = validate_turn_seconds(turn_seconds)

        try:
            with self._guard(), self.database.transaction() as session:
                room = Room(
                    room_code=code,
                    owner_user_id=owner,
                    name=clean_name,
                    status=StoredRoomStatus.WAITING.value,
                    max_players=max_players,
                    allow_spectators=allow_spectators,
                    theme_key=key,
                    turn_seconds=seconds,
                    revision=0,
                )
                session.add(room)
                session.flush()
                session.add(
                    RoomMembership(
                        room_id=room.id,
                        user_id=owner,
                        role=RoomRole.PLAYER.value,
                        seat_index=0,
                        presence=PresenceState.CONNECTED.value,
                        connected_count=1,
                        is_bot_substituting=False,
                        ready=False,
                    )
                )
                session.flush()
                return _snapshot(session, room)
        except IntegrityError as error:
            if self._code_exists(code):
                raise InviteCodeConflict(code) from error
            raise

    def get_by_code(self, room_code: str) -> LobbyRoomSnapshot | None:
        code = normalize_invite_code(room_code)
        with self.database.read_session() as session:
            room = session.scalar(
                select(Room).where(
                    Room.room_code == code,
                    Room.deleted_at.is_(None),
                )
            )
            return _snapshot(session, room) if room is not None else None

    def find_active_game_id(
        self,
        *,
        room_code: str,
        user_id: str,
    ) -> str | None:
        """Resolve one exact, internally consistent active game for a member.

        All checks share a transaction. The Game row is locked before the Room
        row, matching coordinator deletion order and avoiding a cross-service
        deadlock. Invalid authorization and malformed or ambiguous persisted
        state fail closed without returning an identifier.
        """

        code = normalize_invite_code(room_code)
        member_id = _identifier(user_id, "user_id", 36)
        with self._guard(), self.database.transaction() as session:
            observed_room = session.scalar(
                select(Room).where(
                    Room.room_code == code,
                    Room.deleted_at.is_(None),
                )
            )
            if (
                observed_room is None
                or observed_room.room_code != code
                or observed_room.status != StoredRoomStatus.ACTIVE.value
            ):
                return None
            observed_member = session.get(
                RoomMembership,
                {"room_id": observed_room.id, "user_id": member_id},
            )
            if (
                observed_member is None
                or observed_member.left_at is not None
                or observed_member.role
                not in (RoomRole.PLAYER.value, RoomRole.SPECTATOR.value)
            ):
                return None

            games = tuple(
                session.scalars(
                    select(Game)
                    .where(Game.room_id == observed_room.id)
                    .order_by(Game.id)
                    .with_for_update()
                )
            )
            if len(games) != 1:
                return None
            game = games[0]
            room = session.scalar(
                select(Room)
                .where(Room.id == observed_room.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                room is None
                or room.room_code != code
                or room.deleted_at is not None
                or room.status != StoredRoomStatus.ACTIVE.value
            ):
                return None
            memberships = _active_memberships(session, room.id, lock=True)
            member = next(
                (
                    membership
                    for membership in memberships
                    if membership.user_id == member_id
                ),
                None,
            )
            if (
                member is None
                or member.role
                not in (RoomRole.PLAYER.value, RoomRole.SPECTATOR.value)
            ):
                return None
            try:
                active = deserialize_room_snapshot(game.state_json or {})
            except RoomSnapshotCorruptError:
                return None
            if not _active_game_matches(room, memberships, game, active):
                return None
            return game.id

    def join_waiting(
        self,
        *,
        room_id: str,
        user_id: str,
        role: RoomRole,
    ) -> LobbyRoomSnapshot:
        room_identifier = _identifier(room_id, "room_id", 36)
        member_id = _identifier(user_id, "user_id", 36)
        if not isinstance(role, RoomRole):
            raise ValueError("role must be a RoomRole")

        with self._guard(), self.database.transaction() as session:
            room = _waiting_room(session, room_identifier)
            memberships = _active_memberships(session, room.id, lock=True)
            existing = next(
                (
                    membership
                    for membership in memberships
                    if membership.user_id == member_id
                ),
                None,
            )
            if existing is not None:
                if existing.role == role.value:
                    return _snapshot(session, room)
                raise LobbyMemberError("member already has a different role")

            stored = session.get(
                RoomMembership,
                {"room_id": room.id, "user_id": member_id},
            )
            players = tuple(
                member
                for member in memberships
                if member.role == RoomRole.PLAYER.value
            )
            if role is RoomRole.PLAYER:
                if len(players) >= room.max_players:
                    raise LobbyCapacityError("the room has no open player seat")
                used_seats = {
                    member.seat_index
                    for member in players
                    if member.seat_index is not None
                }
                seat_index = next(
                    index
                    for index in range(room.max_players)
                    if index not in used_seats
                )
            else:
                if not room.allow_spectators:
                    raise SpectatorsDisabledError("spectators are disabled")
                seat_index = None

            now = utc_now()
            if stored is None:
                stored = RoomMembership(room_id=room.id, user_id=member_id)
                session.add(stored)
            else:
                stored.joined_at = now
            stored.role = role.value
            stored.seat_index = seat_index
            stored.presence = PresenceState.CONNECTED.value
            stored.connected_count = 1
            stored.is_bot_substituting = False
            stored.ready = False
            stored.last_seen_at = now
            stored.presence_expires_at = None
            stored.left_at = None
            room.revision += 1
            room.updated_at = now
            session.flush()
            return _snapshot(session, room)

    def set_ready(
        self,
        *,
        room_id: str,
        user_id: str,
        ready: bool,
    ) -> LobbyRoomSnapshot:
        room_identifier = _identifier(room_id, "room_id", 36)
        member_id = _identifier(user_id, "user_id", 36)
        if type(ready) is not bool:
            raise ValueError("ready must be boolean")

        with self._guard(), self.database.transaction() as session:
            room = _waiting_room(session, room_identifier)
            membership = session.get(
                RoomMembership,
                {"room_id": room.id, "user_id": member_id},
            )
            if membership is None or membership.left_at is not None:
                raise LobbyMemberError("user is not a room member")
            if membership.role != RoomRole.PLAYER.value:
                raise LobbyAuthorizationError("spectators cannot become ready")
            if membership.ready is ready:
                return _snapshot(session, room)

            membership.ready = ready
            membership.last_seen_at = utc_now()
            room.revision += 1
            room.updated_at = utc_now()
            session.flush()
            return _snapshot(session, room)

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
        room_identifier = _identifier(room_id, "room_id", 36)
        owner_id = _identifier(
            requesting_owner_user_id,
            "requesting_owner_user_id",
            36,
        )
        game_identifier = _identifier(game_id, "game_id", 36)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        key = validate_theme_key(theme_key)
        seconds = validate_turn_seconds(turn_seconds)
        document = serialize_room_snapshot(active_room)

        try:
            with self._guard(), self.database.transaction() as session:
                room = _waiting_room(session, room_identifier)
                current = _snapshot(session, room)
                if room.revision != expected_revision:
                    raise LobbyRevisionConflict(current)
                if room.owner_user_id != owner_id:
                    raise LobbyAuthorizationError("only the room owner can start")
                if not current.all_players_ready:
                    raise LobbyNotReadyError("all players must be ready")
                _validate_active_snapshot(
                    current,
                    active_room,
                    game_identifier,
                    key,
                    seconds,
                )
                if session.get(Game, game_identifier) is not None:
                    raise LobbyStateError("game ID already exists")

                game = Game(
                    id=game_identifier,
                    room_id=room.id,
                    created_by_user_id=owner_id,
                    mode=GameMode.MULTIPLAYER.value,
                    status=StoredGameStatus.ACTIVE.value,
                    theme_key=key,
                    turn_time_seconds=seconds,
                    bot_count=0,
                    bot_difficulty=active_room.bot_difficulty,
                    settings_json={
                        "room_code": room.room_code,
                        "max_players": room.max_players,
                        "allow_spectators": room.allow_spectators,
                    },
                    state_json=document,
                    starting_seat_index=active_room.current_turn,
                    current_turn_index=active_room.current_turn,
                    current_word_surface=None,
                    current_word_reading=None,
                    expected_kana=None,
                    state_version=active_room.state_version,
                    deadline_at=active_room.deadline_at,
                    paused_remaining_seconds=None,
                    winner_user_id=None,
                    finished_at=None,
                )
                session.add(game)
                room.status = StoredRoomStatus.ACTIVE.value
                room.revision += 1
                room.updated_at = utc_now()
                session.flush()
                active_lobby = _snapshot(session, room)
                return LobbyStartResult(
                    lobby=active_lobby,
                    game_id=game_identifier,
                    active_room=active_room,
                )
        except IntegrityError as error:
            if self._game_exists(game_identifier):
                raise LobbyStateError("game ID already exists") from error
            raise

    def leave_waiting(
        self,
        *,
        room_id: str,
        user_id: str,
    ) -> LobbyLeaveResult:
        room_identifier = _identifier(room_id, "room_id", 36)
        member_id = _identifier(user_id, "user_id", 36)

        with self._guard(), self.database.transaction() as session:
            room = _waiting_room(session, room_identifier)
            membership = session.get(
                RoomMembership,
                {"room_id": room.id, "user_id": member_id},
            )
            if membership is None or membership.left_at is not None:
                raise LobbyMemberError("user is not a room member")

            now = utc_now()
            membership.seat_index = None
            membership.ready = False
            membership.presence = PresenceState.OFFLINE.value
            membership.connected_count = 0
            membership.is_bot_substituting = False
            membership.presence_expires_at = None
            membership.last_seen_at = now
            membership.left_at = now
            session.flush()

            remaining = _active_memberships(session, room.id, lock=True)
            players = sorted(
                (
                    candidate
                    for candidate in remaining
                    if candidate.role == RoomRole.PLAYER.value
                ),
                key=lambda candidate: int(candidate.seat_index),
            )
            room.revision += 1
            room.updated_at = now
            if not players:
                room.status = StoredRoomStatus.CLOSED.value
                room.deleted_at = now
                session.flush()
                return LobbyLeaveResult(room=None, deleted=True)

            if room.owner_user_id == member_id:
                room.owner_user_id = players[0].user_id

            # Avoid transient unique-seat conflicts while compacting indexes.
            for player in players:
                player.seat_index = None
            session.flush()
            for index, player in enumerate(players):
                player.seat_index = index
            session.flush()
            return LobbyLeaveResult(room=_snapshot(session, room), deleted=False)

    def _code_exists(self, room_code: str) -> bool:
        with self.database.read_session() as session:
            return (
                session.scalar(
                    select(Room.id).where(Room.room_code == room_code)
                )
                is not None
            )

    def _game_exists(self, game_id: str) -> bool:
        with self.database.read_session() as session:
            return session.get(Game, game_id) is not None

    def _guard(self) -> AbstractContextManager[Any]:
        return self._sqlite_lock or _NullLock()


class _NullLock:
    def __enter__(self) -> _NullLock:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _waiting_room(session: Session, room_id: str) -> Room:
    room = session.scalar(
        select(Room).where(Room.id == room_id).with_for_update()
    )
    if room is None or room.deleted_at is not None:
        raise LobbyRoomNotFound("room not found")
    if room.status != StoredRoomStatus.WAITING.value:
        raise LobbyStateError("room is no longer waiting")
    return room


def _active_memberships(
    session: Session,
    room_id: str,
    *,
    lock: bool = False,
) -> tuple[RoomMembership, ...]:
    statement = select(RoomMembership).where(
        RoomMembership.room_id == room_id,
        RoomMembership.left_at.is_(None),
    )
    if lock:
        statement = statement.with_for_update()
    return tuple(session.scalars(statement))


def _snapshot(session: Session, room: Room) -> LobbyRoomSnapshot:
    memberships = _active_memberships(session, room.id)
    players = sorted(
        (
            membership
            for membership in memberships
            if membership.role == RoomRole.PLAYER.value
        ),
        key=lambda membership: int(membership.seat_index),
    )
    spectators = sorted(
        (
            membership
            for membership in memberships
            if membership.role == RoomRole.SPECTATOR.value
        ),
        # User IDs remain stable across PostgreSQL/SQLite restarts; timestamps
        # can lose timezone metadata in SQLite's adapter.
        key=lambda membership: membership.user_id,
    )
    members = tuple(
        LobbyMember(
            user_id=membership.user_id,
            role=RoomRole(membership.role),
            seat_index=membership.seat_index,
            ready=membership.ready,
        )
        for membership in (*players, *spectators)
    )
    return LobbyRoomSnapshot(
        id=room.id,
        room_code=room.room_code,
        owner_user_id=room.owner_user_id,
        name=room.name,
        status=StoredRoomStatus(room.status),
        max_players=room.max_players,
        allow_spectators=room.allow_spectators,
        theme_key=room.theme_key,
        turn_seconds=room.turn_seconds,
        revision=room.revision,
        members=members,
    )


def _active_game_matches(
    room: Room,
    memberships: tuple[RoomMembership, ...],
    game: Game,
    active: ActiveRoomSnapshot,
) -> bool:
    if any(
        membership.role not in (RoomRole.PLAYER.value, RoomRole.SPECTATOR.value)
        for membership in memberships
    ):
        return False
    player_rows = tuple(
        membership
        for membership in memberships
        if membership.role == RoomRole.PLAYER.value
    )
    if any(
        type(membership.seat_index) is not int
        for membership in player_rows
    ):
        return False
    players = tuple(
        sorted(player_rows, key=lambda membership: int(membership.seat_index))
    )
    if tuple(player.seat_index for player in players) != tuple(
        range(len(players))
    ):
        return False
    spectators = tuple(
        sorted(
            (
                membership
                for membership in memberships
                if membership.role == RoomRole.SPECTATOR.value
            ),
            key=lambda membership: membership.user_id,
        )
    )
    if any(spectator.seat_index is not None for spectator in spectators):
        return False

    last_turn = active.history[-1] if active.history else None
    return (
        game.room_id == room.id
        and game.mode == GameMode.MULTIPLAYER.value
        and game.status == StoredGameStatus.ACTIVE.value
        and game.id == active.room_id
        and active.mode is RoomMode.PVP
        and active.status is ActiveRoomStatus.ACTIVE
        and game.state_version == active.state_version
        and game.current_turn_index == active.current_turn
        and game.theme_key == active.theme_key == room.theme_key
        and game.turn_time_seconds == active.turn_seconds == room.turn_seconds
        and game.bot_difficulty == active.bot_difficulty
        and game.bot_count == 0
        and game.current_word_surface
        == (last_turn.surface if last_turn is not None else None)
        and game.current_word_reading
        == (last_turn.reading if last_turn is not None else None)
        and game.expected_kana == active.expected_kana
        and game.finished_at is None
        and tuple(seat.owner_user_id for seat in active.players)
        == tuple(player.user_id for player in players)
        and active.spectators
        == tuple(spectator.user_id for spectator in spectators)
    )


def _validate_active_snapshot(
    lobby: LobbyRoomSnapshot,
    active: ActiveRoomSnapshot,
    game_id: str,
    theme_key: str,
    turn_seconds: int | None,
) -> None:
    expected_players = tuple(player.user_id for player in lobby.players)
    actual_players = tuple(seat.owner_user_id for seat in active.players)
    expected_spectators = tuple(
        spectator.user_id for spectator in lobby.spectators
    )
    human_controllers = all(
        seat.controller is SeatController.HUMAN
        and not seat.handback_pending
        for seat in active.players
    )
    initial_shape = (
        active.mode is RoomMode.PVP
        and active.status is ActiveRoomStatus.ACTIVE
        and active.state_version == 0
        and active.history == ()
        and active.expected_kana is None
        and active.paused_remaining_seconds is None
        and active.timed_out_seat is None
        and active.losing_seat is None
        and active.end_reason is None
    )
    deadline_shape = (
        active.deadline_at is None
        if turn_seconds is None
        else active.deadline_at is not None
    )
    if (
        active.room_id != game_id
        or actual_players != expected_players
        or active.spectators != expected_spectators
        or active.turn_seconds != turn_seconds
        or active.theme_key != lobby.theme_key
        or lobby.theme_key != theme_key
        or active.bot_difficulty != "normal"
        or not human_controllers
        or not initial_shape
        or not deadline_shape
    ):
        raise LobbyRevisionConflict(lobby)


def _identifier(value: str, name: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.isspace()
        or len(value) > maximum
    ):
        raise ValueError(f"{name} must contain 1-{maximum} characters")
    return value


def _room_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("room name must be text")
    result = value.strip()
    if not 1 <= len(result) <= 64:
        raise ValueError("room name must contain 1-64 characters")
    return result


__all__ = ["SQLAlchemyLobbyRepository"]
