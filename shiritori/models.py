"""SQLAlchemy models for accounts, rooms, games, and resumable solo play.

The models deliberately use portable SQL types so the production PostgreSQL
schema can also be exercised with SQLite in unit tests.  PostgreSQL remains
the authoritative store in production; SQLite is only a local test backend.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utc_now() -> datetime:
    """Return an aware UTC timestamp suitable for application-side defaults."""

    return datetime.now(timezone.utc)


def new_id() -> str:
    """Return a database-portable UUID string."""

    return str(uuid4())


class RoomStatus(StrEnum):
    WAITING = "waiting"
    ACTIVE = "active"
    CLOSED = "closed"


class RoomRole(StrEnum):
    PLAYER = "player"
    SPECTATOR = "spectator"


class PresenceState(StrEnum):
    CONNECTED = "connected"
    GRACE = "grace"
    OFFLINE = "offline"


class GameMode(StrEnum):
    MULTIPLAYER = "multiplayer"
    SOLO = "solo"


class StoredGameStatus(StrEnum):
    WAITING = "waiting"
    ACTIVE = "active"
    PAUSED = "paused"
    FINISHED = "finished"
    ABANDONED = "abandoned"


class ActorKind(StrEnum):
    USER = "user"
    BOT = "bot"
    SYSTEM = "system"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    username_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(40))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sessions: Mapped[list[LoginSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("length(username) >= 3", name="username_min_length"),
    )


class LoginSession(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="sessions")

    __table_args__ = (
        CheckConstraint("length(token_hash) = 64", name="token_hash_sha256"),
        Index("ix_sessions_user_expiry", "user_id", "expires_at"),
    )


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    room_code: Mapped[str] = mapped_column(String(12), nullable=False, unique=True)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RoomStatus.WAITING.value
    )
    max_players: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    allow_spectators: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    theme_key: Mapped[str] = mapped_column(
        String(32), nullable=False, default="all"
    )
    turn_seconds: Mapped[int | None] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list[RoomMembership]] = relationship(
        back_populates="room", cascade="all, delete-orphan"
    )
    games: Mapped[list[Game]] = relationship(back_populates="room")

    __table_args__ = (
        CheckConstraint(
            "status IN ('waiting', 'active', 'closed')", name="valid_status"
        ),
        CheckConstraint(
            "max_players >= 2 AND max_players <= 8", name="max_players_range"
        ),
        CheckConstraint(
            "turn_seconds IS NULL OR "
            "(turn_seconds >= 3 AND turn_seconds <= 180)",
            name="turn_seconds_range",
        ),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
        Index("ix_rooms_status_updated", "status", "updated_at"),
    )


class RoomMembership(Base):
    __tablename__ = "room_memberships"

    room_id: Mapped[str] = mapped_column(
        ForeignKey("rooms.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RoomRole.SPECTATOR.value
    )
    seat_index: Mapped[int | None] = mapped_column(Integer)
    presence: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PresenceState.OFFLINE.value
    )
    connected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_bot_substituting: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    presence_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    room: Mapped[Room] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship()

    __table_args__ = (
        UniqueConstraint("room_id", "seat_index", name="uq_room_memberships_seat"),
        CheckConstraint(
            "role IN ('player', 'spectator')", name="valid_role"
        ),
        CheckConstraint(
            "presence IN ('connected', 'grace', 'offline')",
            name="valid_presence",
        ),
        CheckConstraint("connected_count >= 0", name="connected_count_nonnegative"),
        CheckConstraint(
            "seat_index IS NULL OR (seat_index >= 0 AND seat_index < 8)",
            name="seat_index_range",
        ),
        CheckConstraint(
            "role = 'player' OR seat_index IS NULL", name="spectator_has_no_seat"
        ),
        CheckConstraint(
            "role = 'player' OR ready = false", name="spectator_not_ready"
        ),
        Index("ix_room_memberships_presence", "room_id", "presence"),
    )


class Game(Base):
    __tablename__ = "games"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    room_id: Mapped[str | None] = mapped_column(
        ForeignKey("rooms.id", ondelete="SET NULL")
    )
    created_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=StoredGameStatus.WAITING.value
    )
    theme_key: Mapped[str] = mapped_column(
        String(32), nullable=False, default="all"
    )
    turn_time_seconds: Mapped[int | None] = mapped_column(Integer)
    bot_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bot_difficulty: Mapped[str | None] = mapped_column(String(16))
    settings_json: Mapped[dict[str, Any]] = mapped_column(
        "settings", JSON, nullable=False, default=dict
    )
    state_json: Mapped[dict[str, Any]] = mapped_column(
        "state", JSON, nullable=False, default=dict
    )
    starting_seat_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    current_turn_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    current_word_surface: Mapped[str | None] = mapped_column(String(128))
    current_word_reading: Mapped[str | None] = mapped_column(String(128))
    expected_kana: Mapped[str | None] = mapped_column(String(4))
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_remaining_seconds: Mapped[int | None] = mapped_column(Integer)
    winner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    room: Mapped[Room | None] = relationship(back_populates="games")
    moves: Mapped[list[Move]] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
        order_by="Move.turn_number",
    )
    solo_save: Mapped[SoloGameSave | None] = relationship(
        back_populates="game", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        CheckConstraint("mode IN ('multiplayer', 'solo')", name="valid_mode"),
        CheckConstraint(
            "status IN ('waiting', 'active', 'paused', 'finished', 'abandoned')",
            name="valid_status",
        ),
        CheckConstraint(
            "turn_time_seconds IS NULL OR "
            "(turn_time_seconds >= 3 AND turn_time_seconds <= 180)",
            name="turn_time_range",
        ),
        CheckConstraint(
            "bot_count >= 0 AND bot_count <= 7", name="bot_count_range"
        ),
        CheckConstraint(
            "bot_difficulty IS NULL OR "
            "bot_difficulty IN ('easy', 'normal', 'hard')",
            name="valid_bot_difficulty",
        ),
        CheckConstraint("state_version >= 0", name="state_version_nonnegative"),
        CheckConstraint(
            "paused_remaining_seconds IS NULL OR paused_remaining_seconds >= 0",
            name="paused_remaining_nonnegative",
        ),
        Index("ix_games_room_status", "room_id", "status"),
        Index("ix_games_owner_mode_status", "created_by_user_id", "mode", "status"),
    )


class Move(Base):
    __tablename__ = "moves"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), nullable=False
    )
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_seat_index: Mapped[int | None] = mapped_column(Integer)
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    surface: Mapped[str] = mapped_column(String(128), nullable=False)
    reading: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_key: Mapped[str] = mapped_column(String(128), nullable=False)
    result_code: Mapped[str] = mapped_column(
        String(32), nullable=False, default="accepted"
    )
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    game: Mapped[Game] = relationship(back_populates="moves")
    actor_user: Mapped[User | None] = relationship()

    __table_args__ = (
        UniqueConstraint("game_id", "operation_id", name="uq_moves_operation"),
        UniqueConstraint("game_id", "turn_number", name="uq_moves_turn_number"),
        CheckConstraint(
            "actor_kind IN ('user', 'bot', 'system')", name="valid_actor_kind"
        ),
        CheckConstraint("turn_number >= 1", name="turn_number_positive"),
        CheckConstraint("state_version >= 1", name="state_version_positive"),
        Index("ix_moves_game_created", "game_id", "created_at"),
        Index("ix_moves_game_canonical", "game_id", "canonical_key"),
    )


class SoloGameSave(Base):
    __tablename__ = "solo_game_saves"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    game_id: Mapped[str] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    slot_name: Mapped[str] = mapped_column(
        String(32), nullable=False, default="autosave"
    )
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        "snapshot", JSON, nullable=False, default=dict
    )
    remaining_seconds: Mapped[int | None] = mapped_column(Integer)
    saved_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    game: Mapped[Game] = relationship(back_populates="solo_save")
    user: Mapped[User] = relationship()

    __table_args__ = (
        UniqueConstraint("user_id", "slot_name", name="uq_solo_saves_user_slot"),
        CheckConstraint(
            "remaining_seconds IS NULL OR remaining_seconds >= 0",
            name="remaining_seconds_nonnegative",
        ),
        CheckConstraint(
            "saved_state_version >= 0", name="saved_state_version_nonnegative"
        ),
        Index("ix_solo_saves_user_updated", "user_id", "updated_at"),
    )

class RoomCommandReceipt(Base):
    """Durable result of one coordinator command.

    Receipts intentionally do not reference ``games`` with a foreign key.
    A receipt must remain readable even if an administrator later physically
    removes an already logically deleted game.
    """

    __tablename__ = "room_command_receipts"

    room_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    command_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    command_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    result_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(
        "result_snapshot", JSON(none_as_null=True)
    )
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    __table_args__ = (
        CheckConstraint(
            "command_kind IN ('compare_and_swap', 'delete')",
            name="valid_command_kind",
        ),
        CheckConstraint(
            "length(command_fingerprint) = 64",
            name="command_fingerprint_sha256",
        ),
        CheckConstraint(
            "expected_version >= 0", name="expected_version_nonnegative"
        ),
        CheckConstraint(
            "(deleted = false AND result_snapshot IS NOT NULL) OR "
            "(deleted = true AND result_snapshot IS NULL)",
            name="receipt_result_shape",
        ),
        Index("ix_room_command_receipts_created", "created_at"),
    )

__all__ = [
    "ActorKind",
    "Base",
    "Game",
    "GameMode",
    "LoginSession",
    "Move",
    "PresenceState",
    "Room",
    "RoomCommandReceipt",
    "RoomMembership",
    "RoomRole",
    "RoomStatus",
    "SoloGameSave",
    "StoredGameStatus",
    "User",
    "new_id",
    "utc_now",
]
