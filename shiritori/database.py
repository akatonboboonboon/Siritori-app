"""Database setup and transactional repositories.

Runtime code reads ``DATABASE_URL`` (the Neon pooled URL).  Alembic is kept
separate and reads only ``DIRECT_DATABASE_URL``; see ``migrations/env.py``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import re
from typing import Any

from sqlalchemy import Engine, create_engine, event, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    ActorKind,
    Base,
    Game,
    GameMode,
    Move,
    PresenceState,
    Room,
    RoomMembership,
    RoomRole,
    RoomStatus,
    SoloGameSave,
    StoredGameStatus,
    utc_now,
)


class DatabaseConfigurationError(RuntimeError):
    """Raised when a required database setting is absent or unsupported."""


class GameNotFoundError(LookupError):
    """Raised when the requested game does not exist."""


class GameNotActiveError(RuntimeError):
    """Raised when a move targets a game that can no longer accept moves."""


class AuthoritativeGameStateError(RuntimeError):
    """Raised when a legacy mutation targets a coordinator-owned game row."""


class StaleGameStateError(RuntimeError):
    """Raised when the caller's state version is not the current version."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            f"game state changed: expected version {expected}, current version {actual}"
        )
        self.expected = expected
        self.actual = actual


class IdempotencyConflictError(RuntimeError):
    """Raised when one operation ID is reused for a different move."""


class SoloSaveNotFoundError(LookupError):
    """Raised when a resumable solo save cannot be found."""


class SoloSaveSlotOccupiedError(RuntimeError):
    """Raised when a named save slot already belongs to another game."""


def normalize_database_url(url: str) -> str:
    """Normalize common PostgreSQL URLs for SQLAlchemy's psycopg 3 driver."""

    value = url.strip()
    if not value:
        raise DatabaseConfigurationError("database URL must not be empty")
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value[len("postgres://") :]
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value[len("postgresql://") :]
    return value


def runtime_database_url(environ: Mapping[str, str] | None = None) -> str:
    """Return the runtime URL, intentionally ignoring migration credentials."""

    values = os.environ if environ is None else environ
    value = values.get("DATABASE_URL")
    if value is None:
        raise DatabaseConfigurationError("DATABASE_URL is not configured")
    return normalize_database_url(value)


def create_runtime_engine(url: str | None = None) -> Engine:
    """Create a small, health-checked engine suitable for Render and Neon."""

    resolved_url = normalize_database_url(url) if url else runtime_database_url()
    common: dict[str, Any] = {
        "future": True,
        "pool_pre_ping": True,
    }
    if resolved_url.startswith("sqlite"):
        common["connect_args"] = {"check_same_thread": False}
    else:
        # Keep the app-side pool intentionally small; Neon supplies the pooled
        # endpoint and the Render free service runs a single process.
        common.update(pool_size=3, max_overflow=2, pool_recycle=300)

    engine = create_engine(resolved_url, **common)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


class Database:
    """Own an engine and short-lived SQLAlchemy sessions."""

    def __init__(self, url: str | None = None, *, engine: Engine | None = None) -> None:
        if engine is not None and url is not None:
            raise ValueError("pass either url or engine, not both")
        self.engine = engine or create_runtime_engine(url)
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            autoflush=False,
            expire_on_commit=False,
        )

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """Yield one session whose changes commit atomically or roll back."""

        with self.session_factory() as session:
            with session.begin():
                yield session

    @contextmanager
    def read_session(self) -> Iterator[Session]:
        with self.session_factory() as session:
            yield session

    def create_schema_for_testing(self) -> None:
        """Create tables only for SQLite tests; production must use Alembic."""

        if self.engine.dialect.name != "sqlite":
            raise DatabaseConfigurationError(
                "create_schema_for_testing is restricted to SQLite"
            )
        Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()


@dataclass(frozen=True, slots=True)
class MembershipSnapshot:
    room_id: str
    user_id: str
    role: str
    seat_index: int | None
    presence: str
    connected_count: int
    is_bot_substituting: bool
    presence_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class RoomSnapshot:
    id: str
    room_code: str
    owner_user_id: str
    name: str
    status: str
    max_players: int
    allow_spectators: bool
    memberships: tuple[MembershipSnapshot, ...]


@dataclass(frozen=True, slots=True)
class MoveSnapshot:
    id: str
    operation_id: str
    actor_user_id: str | None
    actor_kind: str
    actor_seat_index: int | None
    turn_number: int
    surface: str
    reading: str
    canonical_key: str
    result_code: str
    state_version: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class GameSnapshot:
    id: str
    room_id: str | None
    created_by_user_id: str
    mode: str
    status: str
    theme_key: str
    turn_time_seconds: int | None
    bot_count: int
    bot_difficulty: str | None
    settings: dict[str, Any]
    state: dict[str, Any]
    starting_seat_index: int
    current_turn_index: int
    current_word_surface: str | None
    current_word_reading: str | None
    expected_kana: str | None
    state_version: int
    deadline_at: datetime | None
    paused_remaining_seconds: int | None
    winner_user_id: str | None
    finished_at: datetime | None
    moves: tuple[MoveSnapshot, ...]


@dataclass(frozen=True, slots=True)
class SoloSaveSnapshot:
    id: str
    game_id: str
    user_id: str
    slot_name: str
    snapshot: dict[str, Any]
    remaining_seconds: int | None
    saved_state_version: int
    updated_at: datetime
    resumed_at: datetime | None


@dataclass(frozen=True, slots=True)
class MoveCommand:
    """All persisted effects of one already-validated shiritori turn."""

    game_id: str
    operation_id: str
    expected_version: int
    actor_kind: str
    surface: str
    reading: str
    canonical_key: str
    next_turn_index: int
    expected_kana: str | None
    next_state: Mapping[str, Any]
    actor_user_id: str | None = None
    actor_seat_index: int | None = None
    result_code: str = "accepted"
    next_status: str = StoredGameStatus.ACTIVE.value
    deadline_at: datetime | None = None
    winner_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class MoveSubmission:
    snapshot: GameSnapshot
    move: MoveSnapshot
    replayed: bool


def _enum_value(value: str | Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _aware_utc(value: datetime | None) -> datetime | None:
    """Restore UTC awareness lost by SQLite's datetime adapter."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _move_snapshot(move: Move) -> MoveSnapshot:
    return MoveSnapshot(
        id=move.id,
        operation_id=move.operation_id,
        actor_user_id=move.actor_user_id,
        actor_kind=move.actor_kind,
        actor_seat_index=move.actor_seat_index,
        turn_number=move.turn_number,
        surface=move.surface,
        reading=move.reading,
        canonical_key=move.canonical_key,
        result_code=move.result_code,
        state_version=move.state_version,
        created_at=_aware_utc(move.created_at),
    )


_AUTHORITATIVE_ROOM_SCHEMA_KEY = "room_repository_schema"


def _reject_authoritative_game(game: Game) -> None:
    state = game.state_json
    if (
        isinstance(state, Mapping)
        and _AUTHORITATIVE_ROOM_SCHEMA_KEY in state
    ):
        raise AuthoritativeGameStateError(
            "game state is owned by RoomCoordinator; "
            "legacy GameRepository mutations are forbidden"
        )


class GameRepository:
    """Transactional access to rooms, game snapshots, moves, and solo saves."""

    _ROOM_CODE_PATTERN = re.compile(r"^[A-Z0-9]{4,12}$")

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_room(
        self,
        *,
        owner_user_id: str,
        room_code: str,
        name: str,
        max_players: int = 2,
        allow_spectators: bool = True,
    ) -> RoomSnapshot:
        code = room_code.strip().upper()
        clean_name = name.strip()
        if not self._ROOM_CODE_PATTERN.fullmatch(code):
            raise ValueError("room_code must be 4-12 uppercase letters or digits")
        if not clean_name or len(clean_name) > 64:
            raise ValueError("room name must contain 1-64 characters")

        with self.database.transaction() as session:
            room = Room(
                room_code=code,
                owner_user_id=owner_user_id,
                name=clean_name,
                status=RoomStatus.WAITING.value,
                max_players=max_players,
                allow_spectators=allow_spectators,
            )
            session.add(room)
            session.flush()
            owner_membership = RoomMembership(
                room_id=room.id,
                user_id=owner_user_id,
                role=RoomRole.PLAYER.value,
                seat_index=0,
                presence=PresenceState.CONNECTED.value,
                connected_count=1,
            )
            session.add(owner_membership)
            session.flush()
            return self._room_snapshot(session, room.id)

    def set_membership(
        self,
        *,
        room_id: str,
        user_id: str,
        role: str,
        seat_index: int | None,
        presence: str = PresenceState.CONNECTED.value,
        connected_count: int = 1,
        presence_expires_at: datetime | None = None,
        is_bot_substituting: bool = False,
    ) -> RoomSnapshot:
        role_value = _enum_value(role)
        presence_value = _enum_value(presence)
        with self.database.transaction() as session:
            room = session.get(Room, room_id)
            if room is None or room.deleted_at is not None:
                raise LookupError("room not found")
            membership = session.get(
                RoomMembership, {"room_id": room_id, "user_id": user_id}
            )
            if membership is None:
                membership = RoomMembership(room_id=room_id, user_id=user_id)
                session.add(membership)
            membership.role = role_value
            membership.seat_index = seat_index
            membership.presence = presence_value
            membership.connected_count = connected_count
            membership.presence_expires_at = presence_expires_at
            membership.is_bot_substituting = is_bot_substituting
            membership.last_seen_at = utc_now()
            membership.left_at = None
            room.updated_at = utc_now()
            session.flush()
            return self._room_snapshot(session, room_id)

    def get_room(self, room_id: str) -> RoomSnapshot:
        with self.database.read_session() as session:
            room = session.get(Room, room_id)
            if room is None:
                raise LookupError("room not found")
            return self._room_snapshot(session, room_id)

    def create_game(
        self,
        *,
        created_by_user_id: str,
        mode: str,
        room_id: str | None = None,
        status: str = StoredGameStatus.ACTIVE.value,
        theme_key: str = "all",
        turn_time_seconds: int | None = None,
        bot_count: int = 0,
        bot_difficulty: str | None = None,
        settings: Mapping[str, Any] | None = None,
        state: Mapping[str, Any] | None = None,
        starting_seat_index: int = 0,
        current_turn_index: int = 0,
        current_word_surface: str | None = None,
        current_word_reading: str | None = None,
        expected_kana: str | None = None,
        deadline_at: datetime | None = None,
    ) -> GameSnapshot:
        mode_value = _enum_value(mode)
        status_value = _enum_value(status)
        if mode_value == GameMode.SOLO.value:
            if room_id is not None:
                raise ValueError("solo games cannot belong to a room")
            if bot_count < 1:
                raise ValueError("solo games require at least one bot")
        elif mode_value == GameMode.MULTIPLAYER.value:
            if room_id is None:
                raise ValueError("multiplayer games require a room")
        else:
            raise ValueError("unsupported game mode")

        with self.database.transaction() as session:
            game = Game(
                room_id=room_id,
                created_by_user_id=created_by_user_id,
                mode=mode_value,
                status=status_value,
                theme_key=theme_key,
                turn_time_seconds=turn_time_seconds,
                bot_count=bot_count,
                bot_difficulty=bot_difficulty,
                settings_json=dict(settings or {}),
                state_json=dict(state or {}),
                starting_seat_index=starting_seat_index,
                current_turn_index=current_turn_index,
                current_word_surface=current_word_surface,
                current_word_reading=current_word_reading,
                expected_kana=expected_kana,
                deadline_at=deadline_at,
            )
            session.add(game)
            session.flush()
            return self._game_snapshot(session, game.id)

    def get_game_snapshot(self, game_id: str) -> GameSnapshot:
        with self.database.read_session() as session:
            return self._game_snapshot(session, game_id)

    def submit_move(self, command: MoveCommand) -> MoveSubmission:
        """Persist one move exactly once and advance the version atomically.

        PostgreSQL serializes contenders with ``FOR UPDATE``.  The conditional
        version update is an additional optimistic guard and is what keeps the
        semantics testable on SQLite, where ``FOR UPDATE`` is ignored.
        """

        self._validate_move_command(command)
        try:
            return self._submit_move_once(command)
        except IntegrityError as error:
            # A concurrent retry can lose the unique(operation_id) race after
            # its transaction rolls back.  Recover it as the same idempotent
            # result, but never hide a different integrity error.
            recovered = self._recover_idempotent_move(command)
            if recovered is not None:
                return recovered
            raise error

    def _submit_move_once(self, command: MoveCommand) -> MoveSubmission:
        with self.database.transaction() as session:
            game = session.scalar(
                select(Game)
                .where(Game.id == command.game_id)
                .with_for_update()
            )
            if game is None:
                raise GameNotFoundError(command.game_id)
            _reject_authoritative_game(game)

            existing = session.scalar(
                select(Move).where(
                    Move.game_id == command.game_id,
                    Move.operation_id == command.operation_id,
                )
            )
            if existing is not None:
                self._assert_same_operation(existing, command)
                return MoveSubmission(
                    snapshot=self._game_snapshot(session, game.id),
                    move=_move_snapshot(existing),
                    replayed=True,
                )

            if game.status != StoredGameStatus.ACTIVE.value:
                raise GameNotActiveError(f"game status is {game.status}")
            if game.state_version != command.expected_version:
                raise StaleGameStateError(
                    command.expected_version, game.state_version
                )

            latest_turn = session.scalar(
                select(func.max(Move.turn_number)).where(Move.game_id == game.id)
            )
            next_turn_number = int(latest_turn or 0) + 1
            next_version = command.expected_version + 1
            next_status = _enum_value(command.next_status)
            changed_at = utc_now()
            result = session.execute(
                update(Game)
                .where(
                    Game.id == game.id,
                    Game.state_version == command.expected_version,
                )
                .values(
                    status=next_status,
                    current_turn_index=command.next_turn_index,
                    current_word_surface=command.surface,
                    current_word_reading=command.reading,
                    expected_kana=command.expected_kana,
                    state_json=dict(command.next_state),
                    state_version=next_version,
                    deadline_at=command.deadline_at,
                    winner_user_id=command.winner_user_id,
                    finished_at=(
                        changed_at
                        if next_status == StoredGameStatus.FINISHED.value
                        else None
                    ),
                    updated_at=changed_at,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                current = session.scalar(
                    select(Game.state_version).where(Game.id == game.id)
                )
                raise StaleGameStateError(
                    command.expected_version,
                    int(current if current is not None else -1),
                )

            move = Move(
                game_id=game.id,
                operation_id=command.operation_id,
                actor_user_id=command.actor_user_id,
                actor_kind=_enum_value(command.actor_kind),
                actor_seat_index=command.actor_seat_index,
                turn_number=next_turn_number,
                surface=command.surface,
                reading=command.reading,
                canonical_key=command.canonical_key,
                result_code=command.result_code,
                state_version=next_version,
            )
            session.add(move)
            session.flush()
            session.expire(game)
            return MoveSubmission(
                snapshot=self._game_snapshot(session, game.id),
                move=_move_snapshot(move),
                replayed=False,
            )

    def _recover_idempotent_move(
        self, command: MoveCommand
    ) -> MoveSubmission | None:
        with self.database.read_session() as session:
            game = session.get(Game, command.game_id)
            if game is not None:
                _reject_authoritative_game(game)
            existing = session.scalar(
                select(Move).where(
                    Move.game_id == command.game_id,
                    Move.operation_id == command.operation_id,
                )
            )
            if existing is None:
                return None
            self._assert_same_operation(existing, command)
            return MoveSubmission(
                snapshot=self._game_snapshot(session, command.game_id),
                move=_move_snapshot(existing),
                replayed=True,
            )

    @staticmethod
    def _validate_move_command(command: MoveCommand) -> None:
        if not command.operation_id or len(command.operation_id) > 64:
            raise ValueError("operation_id must contain 1-64 characters")
        if command.expected_version < 0:
            raise ValueError("expected_version must not be negative")
        actor_kind = _enum_value(command.actor_kind)
        if actor_kind not in {item.value for item in ActorKind}:
            raise ValueError("unsupported actor kind")
        if actor_kind == ActorKind.USER.value and command.actor_user_id is None:
            raise ValueError("user moves require actor_user_id")
        if not command.surface or not command.reading or not command.canonical_key:
            raise ValueError("surface, reading, and canonical_key are required")

    @staticmethod
    def _assert_same_operation(existing: Move, command: MoveCommand) -> None:
        identity = (
            existing.actor_user_id,
            existing.actor_kind,
            existing.actor_seat_index,
            existing.surface,
            existing.reading,
            existing.canonical_key,
            existing.result_code,
        )
        requested = (
            command.actor_user_id,
            _enum_value(command.actor_kind),
            command.actor_seat_index,
            command.surface,
            command.reading,
            command.canonical_key,
            command.result_code,
        )
        if identity != requested:
            raise IdempotencyConflictError(
                "operation_id was already used for a different move"
            )

    def save_solo_game(
        self,
        *,
        user_id: str,
        game_id: str,
        expected_version: int,
        remaining_seconds: int | None,
        slot_name: str = "autosave",
        snapshot: Mapping[str, Any] | None = None,
    ) -> SoloSaveSnapshot:
        clean_slot = slot_name.strip()
        if not clean_slot or len(clean_slot) > 32:
            raise ValueError("slot_name must contain 1-32 characters")
        if remaining_seconds is not None and remaining_seconds < 0:
            raise ValueError("remaining_seconds must not be negative")

        with self.database.transaction() as session:
            game = session.scalar(
                select(Game).where(Game.id == game_id).with_for_update()
            )
            if game is None:
                raise GameNotFoundError(game_id)
            _reject_authoritative_game(game)
            if (
                game.mode != GameMode.SOLO.value
                or game.created_by_user_id != user_id
            ):
                raise PermissionError("only the solo game's owner can save it")
            if game.state_version != expected_version:
                raise StaleGameStateError(expected_version, game.state_version)

            slot_owner = session.scalar(
                select(SoloGameSave).where(
                    SoloGameSave.user_id == user_id,
                    SoloGameSave.slot_name == clean_slot,
                )
            )
            if slot_owner is not None and slot_owner.game_id != game_id:
                raise SoloSaveSlotOccupiedError(clean_slot)

            save = session.scalar(
                select(SoloGameSave).where(SoloGameSave.game_id == game_id)
            )
            now = utc_now()
            next_version = expected_version + 1
            result = session.execute(
                update(Game)
                .where(Game.id == game_id, Game.state_version == expected_version)
                .values(
                    status=StoredGameStatus.PAUSED.value,
                    state_version=next_version,
                    deadline_at=None,
                    paused_remaining_seconds=remaining_seconds,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                current = session.scalar(
                    select(Game.state_version).where(Game.id == game_id)
                )
                raise StaleGameStateError(
                    expected_version, int(current if current is not None else -1)
                )

            stored_snapshot = (
                dict(snapshot)
                if snapshot is not None
                else {
                    "state": dict(game.state_json),
                    "current_turn_index": game.current_turn_index,
                    "current_word_surface": game.current_word_surface,
                    "current_word_reading": game.current_word_reading,
                    "expected_kana": game.expected_kana,
                }
            )
            if save is None:
                save = SoloGameSave(
                    game_id=game_id,
                    user_id=user_id,
                    slot_name=clean_slot,
                    snapshot_json=stored_snapshot,
                    remaining_seconds=remaining_seconds,
                    saved_state_version=next_version,
                )
                session.add(save)
            else:
                save.slot_name = clean_slot
                save.snapshot_json = stored_snapshot
                save.remaining_seconds = remaining_seconds
                save.saved_state_version = next_version
                save.updated_at = now
                save.resumed_at = None
            session.flush()
            return self._solo_save_snapshot(save)

    def resume_solo_game(
        self,
        *,
        user_id: str,
        game_id: str,
        expected_version: int,
        now: datetime | None = None,
    ) -> GameSnapshot:
        resumed_at = now or utc_now()
        with self.database.transaction() as session:
            game = session.scalar(
                select(Game).where(Game.id == game_id).with_for_update()
            )
            if game is None:
                raise GameNotFoundError(game_id)
            _reject_authoritative_game(game)
            save = session.scalar(
                select(SoloGameSave).where(
                    SoloGameSave.game_id == game_id,
                    SoloGameSave.user_id == user_id,
                    SoloGameSave.resumed_at.is_(None),
                )
            )
            if save is None:
                raise SoloSaveNotFoundError(game_id)
            if game.created_by_user_id != user_id:
                raise PermissionError("only the solo game's owner can resume it")
            if game.state_version != expected_version:
                raise StaleGameStateError(expected_version, game.state_version)

            deadline = None
            if (
                game.turn_time_seconds is not None
                and save.remaining_seconds is not None
            ):
                deadline = resumed_at + timedelta(seconds=save.remaining_seconds)
            next_version = expected_version + 1
            result = session.execute(
                update(Game)
                .where(Game.id == game_id, Game.state_version == expected_version)
                .values(
                    status=StoredGameStatus.ACTIVE.value,
                    state_version=next_version,
                    deadline_at=deadline,
                    paused_remaining_seconds=None,
                    updated_at=resumed_at,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                current = session.scalar(
                    select(Game.state_version).where(Game.id == game_id)
                )
                raise StaleGameStateError(
                    expected_version, int(current if current is not None else -1)
                )
            save.resumed_at = resumed_at
            save.updated_at = resumed_at
            session.flush()
            session.expire(game)
            return self._game_snapshot(session, game_id)

    def list_solo_saves(
        self, user_id: str, *, active_only: bool = True
    ) -> tuple[SoloSaveSnapshot, ...]:
        with self.database.read_session() as session:
            statement = select(SoloGameSave).where(
                SoloGameSave.user_id == user_id
            )
            if active_only:
                statement = statement.where(SoloGameSave.resumed_at.is_(None))
            statement = statement.order_by(SoloGameSave.updated_at.desc())
            return tuple(
                self._solo_save_snapshot(save)
                for save in session.scalars(statement)
            )

    @staticmethod
    def _room_snapshot(session: Session, room_id: str) -> RoomSnapshot:
        room = session.get(Room, room_id)
        if room is None:
            raise LookupError("room not found")
        memberships = session.scalars(
            select(RoomMembership)
            .where(RoomMembership.room_id == room_id)
            .order_by(RoomMembership.seat_index, RoomMembership.joined_at)
        )
        return RoomSnapshot(
            id=room.id,
            room_code=room.room_code,
            owner_user_id=room.owner_user_id,
            name=room.name,
            status=room.status,
            max_players=room.max_players,
            allow_spectators=room.allow_spectators,
            memberships=tuple(
                MembershipSnapshot(
                    room_id=item.room_id,
                    user_id=item.user_id,
                    role=item.role,
                    seat_index=item.seat_index,
                    presence=item.presence,
                    connected_count=item.connected_count,
                    is_bot_substituting=item.is_bot_substituting,
                    presence_expires_at=_aware_utc(item.presence_expires_at),
                )
                for item in memberships
            ),
        )

    @staticmethod
    def _game_snapshot(session: Session, game_id: str) -> GameSnapshot:
        game = session.get(Game, game_id)
        if game is None:
            raise GameNotFoundError(game_id)
        moves = tuple(
            _move_snapshot(move)
            for move in session.scalars(
                select(Move)
                .where(Move.game_id == game_id)
                .order_by(Move.turn_number)
            )
        )
        return GameSnapshot(
            id=game.id,
            room_id=game.room_id,
            created_by_user_id=game.created_by_user_id,
            mode=game.mode,
            status=game.status,
            theme_key=game.theme_key,
            turn_time_seconds=game.turn_time_seconds,
            bot_count=game.bot_count,
            bot_difficulty=game.bot_difficulty,
            settings=dict(game.settings_json),
            state=dict(game.state_json),
            starting_seat_index=game.starting_seat_index,
            current_turn_index=game.current_turn_index,
            current_word_surface=game.current_word_surface,
            current_word_reading=game.current_word_reading,
            expected_kana=game.expected_kana,
            state_version=game.state_version,
            deadline_at=_aware_utc(game.deadline_at),
            paused_remaining_seconds=game.paused_remaining_seconds,
            winner_user_id=game.winner_user_id,
            finished_at=_aware_utc(game.finished_at),
            moves=moves,
        )

    @staticmethod
    def _solo_save_snapshot(save: SoloGameSave) -> SoloSaveSnapshot:
        return SoloSaveSnapshot(
            id=save.id,
            game_id=save.game_id,
            user_id=save.user_id,
            slot_name=save.slot_name,
            snapshot=dict(save.snapshot_json),
            remaining_seconds=save.remaining_seconds,
            saved_state_version=save.saved_state_version,
            updated_at=_aware_utc(save.updated_at),
            resumed_at=_aware_utc(save.resumed_at),
        )


__all__ = [
    "AuthoritativeGameStateError",
    "Database",
    "DatabaseConfigurationError",
    "GameNotActiveError",
    "GameNotFoundError",
    "GameRepository",
    "GameSnapshot",
    "IdempotencyConflictError",
    "MembershipSnapshot",
    "MoveCommand",
    "MoveSnapshot",
    "MoveSubmission",
    "RoomSnapshot",
    "SoloSaveNotFoundError",
    "SoloSaveSlotOccupiedError",
    "SoloSaveSnapshot",
    "StaleGameStateError",
    "create_runtime_engine",
    "normalize_database_url",
    "runtime_database_url",
]
