"""Durable SQLAlchemy adapter for :mod:`shiritori.rooms`.

The coordinator ``room_id`` is the corresponding ``Game.id``. Complete state
is kept in versioned JSON; selected Game columns are synchronized projections.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
import json
import math
from threading import Lock, RLock
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .database import Database
from .models import (
    Game,
    GameMode,
    PresenceState,
    Room,
    RoomCommandReceipt,
    RoomMembership,
    RoomRole,
    RoomStatus as StoredRoomStatus,
    StoredGameStatus,
    utc_now,
)
from .rooms import (
    CommandReceipt,
    PlayerSeat,
    RepositoryResult,
    RepositoryStatus,
    RoomMode,
    RoomOperationConflictError,
    RoomSnapshot,
    RoomStatus,
    SeatController,
    TurnRecord,
)


SNAPSHOT_SCHEMA_VERSION = 3
_LEGACY_SNAPSHOT_SCHEMA_VERSION = 2
_SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS = frozenset({2, 3})
_SQLITE_LOCKS_GUARD = Lock()
_SQLITE_LOCKS: dict[str, RLock] = {}
_SCHEMA_KEY = "room_repository_schema"
_ROOT_KEYS = {_SCHEMA_KEY, "deleted", "snapshot"}
_SNAPSHOT_KEYS = {
    "room_id", "mode", "status", "players", "current_turn",
    "state_version", "theme_key", "bot_difficulty", "spectators",
    "eliminated_seats", "history", "expected_kana",
    "turn_seconds", "deadline_at", "paused_remaining_seconds",
    "timed_out_seat", "losing_seat", "end_reason",
}
_LEGACY_SNAPSHOT_KEYS = _SNAPSHOT_KEYS - {"eliminated_seats"}
_PLAYER_KEYS = {"index", "owner_user_id", "controller", "handback_pending"}
_TURN_KEYS = {
    "surface", "reading", "canonical_key", "seat_index", "actor_user_id",
    "by_bot", "submitted_at",
}


class RoomPersistenceError(RuntimeError):
    """Base error for durable room persistence."""


class RoomPersistenceNotFound(RoomPersistenceError):
    """Initialization targeted a missing Game."""


class RoomAlreadyInitialized(RoomPersistenceError):
    """A Game already contains different coordinator state."""



class RoomSnapshotCorruptError(RoomPersistenceError):
    """Stored JSON does not conform to the supported schema."""


def serialize_room_snapshot(snapshot: RoomSnapshot) -> dict[str, Any]:
    """Convert a snapshot to a strict, JSON-safe, versioned document."""

    payload: dict[str, Any] = {
        "room_id": snapshot.room_id,
        "mode": snapshot.mode.value,
        "status": snapshot.status.value,
        "players": [
            {
                "index": seat.index,
                "owner_user_id": seat.owner_user_id,
                "controller": seat.controller.value,
                "handback_pending": seat.handback_pending,
            }
            for seat in snapshot.players
        ],
        "current_turn": snapshot.current_turn,
        "state_version": snapshot.state_version,
        "theme_key": snapshot.theme_key,
        "bot_difficulty": snapshot.bot_difficulty,
        "spectators": list(snapshot.spectators),
        "eliminated_seats": list(snapshot.eliminated_seats),
        "history": [
            {
                "surface": turn.surface,
                "reading": turn.reading,
                "canonical_key": turn.canonical_key,
                "seat_index": turn.seat_index,
                "actor_user_id": turn.actor_user_id,
                "by_bot": turn.by_bot,
                "submitted_at": _datetime_to_json(turn.submitted_at),
            }
            for turn in snapshot.history
        ],
        "expected_kana": snapshot.expected_kana,
        "turn_seconds": snapshot.turn_seconds,
        "deadline_at": _nullable_datetime_to_json(snapshot.deadline_at),
        "paused_remaining_seconds": snapshot.paused_remaining_seconds,
        "timed_out_seat": snapshot.timed_out_seat,
        "losing_seat": snapshot.losing_seat,
        "end_reason": snapshot.end_reason,
    }
    document = {_SCHEMA_KEY: SNAPSHOT_SCHEMA_VERSION, "deleted": False,
                "snapshot": payload}
    try:
        json.dumps(document, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("room snapshot is not JSON-safe") from error
    return document


def deserialize_room_snapshot(document: Mapping[str, Any]) -> RoomSnapshot:
    """Rebuild and validate a snapshot from schema-versioned JSON."""

    root = _mapping(document, "snapshot document")
    _exact_keys(root, _ROOT_KEYS, "snapshot document")
    schema_version = _integer(root[_SCHEMA_KEY], _SCHEMA_KEY)
    if schema_version not in _SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS:
        raise RoomSnapshotCorruptError("unsupported room snapshot schema")
    if _boolean(root["deleted"], "deleted"):
        raise RoomSnapshotCorruptError("room snapshot is marked deleted")
    payload = _mapping(root["snapshot"], "snapshot")
    if schema_version == _LEGACY_SNAPSHOT_SCHEMA_VERSION:
        _exact_keys(payload, _LEGACY_SNAPSHOT_KEYS, "snapshot")
        eliminated_seats: tuple[int, ...] = ()
    else:
        _exact_keys(payload, _SNAPSHOT_KEYS, "snapshot")
        eliminated_seats = tuple(
            _integer(value, f"eliminated_seats[{index}]")
            for index, value in enumerate(
                _array(payload["eliminated_seats"], "eliminated_seats")
            )
        )

    players = []
    for index, raw in enumerate(_array(payload["players"], "players")):
        value = _mapping(raw, f"players[{index}]")
        _exact_keys(value, _PLAYER_KEYS, f"players[{index}]")
        players.append(PlayerSeat(
            index=_integer(value["index"], f"players[{index}].index"),
            owner_user_id=_nullable_string(
                value["owner_user_id"], f"players[{index}].owner_user_id"
            ),
            controller=_enum(
                SeatController, value["controller"], f"players[{index}].controller"
            ),
            handback_pending=_boolean(
                value["handback_pending"], f"players[{index}].handback_pending"
            ),
        ))

    history = []
    for index, raw in enumerate(_array(payload["history"], "history")):
        value = _mapping(raw, f"history[{index}]")
        _exact_keys(value, _TURN_KEYS, f"history[{index}]")
        history.append(TurnRecord(
            surface=_string(value["surface"], f"history[{index}].surface"),
            reading=_string(value["reading"], f"history[{index}].reading"),
            canonical_key=_string(
                value["canonical_key"], f"history[{index}].canonical_key"
            ),
            seat_index=_integer(
                value["seat_index"], f"history[{index}].seat_index"
            ),
            actor_user_id=_nullable_string(
                value["actor_user_id"], f"history[{index}].actor_user_id"
            ),
            by_bot=_boolean(value["by_bot"], f"history[{index}].by_bot"),
            submitted_at=_datetime_from_json(
                value["submitted_at"], f"history[{index}].submitted_at"
            ),
        ))
    spectators = tuple(
        _text(value, f"spectators[{index}]")
        for index, value in enumerate(_array(payload["spectators"], "spectators"))
    )
    try:
        return RoomSnapshot(
            room_id=_string(payload["room_id"], "room_id"),
            mode=_enum(RoomMode, payload["mode"], "mode"),
            status=_enum(RoomStatus, payload["status"], "status"),
            players=tuple(players),
            current_turn=_integer(payload["current_turn"], "current_turn"),
            state_version=_integer(payload["state_version"], "state_version"),
            theme_key=_string(payload["theme_key"], "theme_key"),
            bot_difficulty=_string(
                payload["bot_difficulty"], "bot_difficulty"
            ),
            spectators=spectators,
            eliminated_seats=eliminated_seats,
            history=tuple(history),
            expected_kana=_nullable_string(payload["expected_kana"], "expected_kana"),
            turn_seconds=_nullable_integer(payload["turn_seconds"], "turn_seconds"),
            deadline_at=_nullable_datetime(payload["deadline_at"], "deadline_at"),
            paused_remaining_seconds=_nullable_number(
                payload["paused_remaining_seconds"], "paused_remaining_seconds"
            ),
            timed_out_seat=_nullable_integer(
                payload["timed_out_seat"], "timed_out_seat"
            ),
            losing_seat=_nullable_integer(payload["losing_seat"], "losing_seat"),
            end_reason=_nullable_string(payload["end_reason"], "end_reason"),
        )
    except (TypeError, ValueError) as error:
        raise RoomSnapshotCorruptError(
            f"stored room snapshot violates domain rules: {error}"
        ) from error

class SQLAlchemyRoomRepository:
    """SQL-backed optimistic and idempotent ``RoomRepository`` adapter."""

    def __init__(self, database: Database) -> None:
        self.database = database
        # SQLite ignores FOR UPDATE. Share one lock per database URL so even
        # separate adapter/engine instances in this process serialize writes.
        self._sqlite_lock: RLock | None = None
        if database.engine.dialect.name == "sqlite":
            key = str(database.engine.url)
            with _SQLITE_LOCKS_GUARD:
                self._sqlite_lock = _SQLITE_LOCKS.setdefault(key, RLock())

    async def initialize(self, snapshot: RoomSnapshot) -> RoomSnapshot:
        """Atomically attach initial state to an already-created Game.

        Repeating the exact initialization is a no-op. A different snapshot
        cannot replace a live match after a process restart.
        """
        return await asyncio.to_thread(self._initialize_sync, snapshot)

    async def load(self, room_id: str) -> RoomSnapshot | None:
        return await asyncio.to_thread(self._load_sync, room_id)

    async def list_active_room_ids(self) -> tuple[str, ...]:
        """Return validated active coordinator IDs for startup recovery."""
        return await asyncio.to_thread(self._list_active_room_ids_sync)

    async def find_operation(
        self, room_id: str, operation_id: str
    ) -> CommandReceipt | None:
        _identifier(room_id, "room_id", 36)
        _identifier(operation_id, "operation_id", 64)
        return await asyncio.to_thread(
            self._find_operation_sync, room_id, operation_id
        )

    async def compare_and_swap(
        self,
        room_id: str,
        expected_version: int,
        operation_id: str,
        next_snapshot: RoomSnapshot,
        *,
        command_fingerprint: str,
    ) -> RepositoryResult:
        _validate_command(
            room_id,
            expected_version,
            operation_id,
            next_snapshot,
            command_fingerprint,
        )
        return await asyncio.to_thread(
            self._compare_and_swap_sync,
            room_id,
            expected_version,
            operation_id,
            next_snapshot,
            command_fingerprint,
        )

    async def delete_if_version(
        self,
        room_id: str,
        expected_version: int,
        operation_id: str,
        *,
        command_fingerprint: str,
    ) -> RepositoryResult:
        _identifier(room_id, "room_id", 36)
        _identifier(operation_id, "operation_id", 64)
        _version(expected_version)
        _command_fingerprint(command_fingerprint)
        return await asyncio.to_thread(
            self._delete_if_version_sync,
            room_id,
            expected_version,
            operation_id,
            command_fingerprint,
        )

    def _initialize_sync(self, snapshot: RoomSnapshot) -> RoomSnapshot:
        _identifier(snapshot.room_id, "room_id", 36)
        document = serialize_room_snapshot(snapshot)
        with self._guard(), self.database.transaction() as session:
            game = session.scalar(
                select(Game).where(Game.id == snapshot.room_id).with_for_update()
            )
            if game is None:
                raise RoomPersistenceNotFound(
                    f"game {snapshot.room_id!r} does not exist"
                )
            existing_state = game.state_json or {}
            if _repository_document(existing_state):
                if _deleted_document(existing_state):
                    raise RoomAlreadyInitialized("room was already logically deleted")
                existing = deserialize_room_snapshot(existing_state)
                _projection_matches(game, existing)
                if existing == snapshot:
                    return existing
                raise RoomAlreadyInitialized(
                    "game already contains different coordinator state"
                )
            if game.state_version != snapshot.state_version:
                raise RoomAlreadyInitialized(
                    "game and initial snapshot versions differ"
                )
            for key, value in _projection_values(snapshot, document).items():
                setattr(game, key, value)
            game.starting_seat_index = snapshot.current_turn
            session.flush()
            return snapshot

    def _load_sync(self, room_id: str) -> RoomSnapshot | None:
        _identifier(room_id, "room_id", 36)
        with self.database.read_session() as session:
            game = session.get(Game, room_id)
            if game is None or _game_deleted(game):
                return None
            state = game.state_json or {}
            if not _repository_document(state):
                return None
            snapshot = deserialize_room_snapshot(state)
            _projection_matches(game, snapshot)
            return snapshot

    def _list_active_room_ids_sync(self) -> tuple[str, ...]:
        with self.database.read_session() as session:
            games = tuple(session.scalars(
                select(Game)
                .where(Game.status == StoredGameStatus.ACTIVE.value)
                .order_by(Game.id)
            ))
            room_ids: list[str] = []
            for game in games:
                state = game.state_json or {}
                # Active Games belonging to another subsystem have no marker
                # and are outside this repository. Once a marker is present,
                # unsupported or malformed state is an operational error.
                if not isinstance(state, Mapping) or _SCHEMA_KEY not in state:
                    continue
                snapshot = _snapshot_from_game(game)
                if snapshot.status is not RoomStatus.ACTIVE:
                    raise RoomSnapshotCorruptError(
                        f"game {game.id!r} is active but its room snapshot is not"
                    )
                room_ids.append(game.id)
            return tuple(room_ids)

    def _find_operation_sync(
        self, room_id: str, operation_id: str
    ) -> CommandReceipt | None:
        with self.database.read_session() as session:
            stored = session.get(
                RoomCommandReceipt,
                {"room_id": room_id, "operation_id": operation_id},
            )
            return _receipt(stored) if stored is not None else None

    def _compare_and_swap_sync(
        self,
        room_id: str,
        expected_version: int,
        operation_id: str,
        next_snapshot: RoomSnapshot,
        fingerprint: str,
    ) -> RepositoryResult:
        document = serialize_room_snapshot(next_snapshot)
        try:
            with self._guard():
                return self._compare_and_swap_once(
                    room_id,
                    expected_version,
                    operation_id,
                    next_snapshot,
                    document,
                    fingerprint,
                )
        except IntegrityError as error:
            recovered = self._recover_duplicate(
                room_id,
                operation_id,
                command_kind="compare_and_swap",
                fingerprint=fingerprint,
                expected_version=expected_version,
            )
            if recovered is not None:
                return recovered
            raise error

    def _compare_and_swap_once(
        self,
        room_id: str,
        expected_version: int,
        operation_id: str,
        next_snapshot: RoomSnapshot,
        document: dict[str, Any],
        fingerprint: str,
    ) -> RepositoryResult:
        with self.database.transaction() as session:
            stored = session.get(
                RoomCommandReceipt,
                {"room_id": room_id, "operation_id": operation_id},
            )
            if stored is not None:
                return self._duplicate_result(
                    session,
                    stored,
                    command_kind="compare_and_swap",
                    fingerprint=fingerprint,
                    expected_version=expected_version,
                )
            game = session.scalar(
                select(Game).where(Game.id == room_id).with_for_update()
            )
            # The first receipt lookup can race while waiting for the Game row
            # lock. Recheck after acquiring it so concurrent retries replay.
            stored = session.get(
                RoomCommandReceipt,
                {"room_id": room_id, "operation_id": operation_id},
            )
            if stored is not None:
                return self._duplicate_result(
                    session,
                    stored,
                    command_kind="compare_and_swap",
                    fingerprint=fingerprint,
                    expected_version=expected_version,
                )
            if game is None or _game_deleted(game):
                return RepositoryResult(RepositoryStatus.NOT_FOUND)
            current = _snapshot_from_game(game)
            if current.state_version != expected_version:
                return RepositoryResult(
                    RepositoryStatus.VERSION_CONFLICT,
                    current_snapshot=current,
                )
            changed = session.execute(
                update(Game)
                .where(
                    Game.id == room_id,
                    Game.state_version == expected_version,
                    Game.status != StoredGameStatus.ABANDONED.value,
                )
                .values(**_projection_values(next_snapshot, document))
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                return self._lost_update_result(
                    session,
                    room_id,
                    operation_id,
                    command_kind="compare_and_swap",
                    fingerprint=fingerprint,
                    expected_version=expected_version,
                )
            _reconcile_departed_spectators(
                session,
                game,
                current,
                next_snapshot,
            )
            stored = RoomCommandReceipt(
                room_id=room_id,
                operation_id=operation_id,
                command_kind="compare_and_swap",
                command_fingerprint=fingerprint,
                expected_version=expected_version,
                result_snapshot_json=document,
                deleted=False,
            )
            session.add(stored)
            session.flush()
            receipt = CommandReceipt(
                operation_id=operation_id,
                snapshot=next_snapshot,
                command_kind="compare_and_swap",
                fingerprint=fingerprint,
                expected_version=expected_version,
            )
            return RepositoryResult(
                RepositoryStatus.APPLIED,
                receipt=receipt,
                current_snapshot=next_snapshot,
            )

    def _delete_if_version_sync(
        self,
        room_id: str,
        expected_version: int,
        operation_id: str,
        fingerprint: str,
    ) -> RepositoryResult:
        try:
            with self._guard():
                return self._delete_once(
                    room_id, expected_version, operation_id, fingerprint
                )
        except IntegrityError as error:
            recovered = self._recover_duplicate(
                room_id,
                operation_id,
                command_kind="delete",
                fingerprint=fingerprint,
                expected_version=expected_version,
            )
            if recovered is not None:
                return recovered
            raise error

    def _delete_once(
        self,
        room_id: str,
        expected_version: int,
        operation_id: str,
        fingerprint: str,
    ) -> RepositoryResult:
        with self.database.transaction() as session:
            stored = session.get(
                RoomCommandReceipt,
                {"room_id": room_id, "operation_id": operation_id},
            )
            if stored is not None:
                return self._duplicate_result(
                    session,
                    stored,
                    command_kind="delete",
                    fingerprint=fingerprint,
                    expected_version=expected_version,
                )
            game = session.scalar(
                select(Game).where(Game.id == room_id).with_for_update()
            )
            # The first receipt lookup can race while waiting for the Game row
            # lock. Recheck after acquiring it so concurrent retries replay.
            stored = session.get(
                RoomCommandReceipt,
                {"room_id": room_id, "operation_id": operation_id},
            )
            if stored is not None:
                return self._duplicate_result(
                    session,
                    stored,
                    command_kind="delete",
                    fingerprint=fingerprint,
                    expected_version=expected_version,
                )
            if game is None or _game_deleted(game):
                return RepositoryResult(RepositoryStatus.NOT_FOUND)
            current = _snapshot_from_game(game)
            if current.state_version != expected_version:
                return RepositoryResult(
                    RepositoryStatus.VERSION_CONFLICT,
                    current_snapshot=current,
                )
            now = utc_now()
            deleted_state = {
                _SCHEMA_KEY: SNAPSHOT_SCHEMA_VERSION,
                "deleted": True,
                "snapshot": None,
                "deleted_state_version": expected_version + 1,
            }
            changed = session.execute(
                update(Game)
                .where(
                    Game.id == room_id,
                    Game.state_version == expected_version,
                    Game.status != StoredGameStatus.ABANDONED.value,
                )
                .values(
                    state_json=deleted_state,
                    state_version=expected_version + 1,
                    status=StoredGameStatus.ABANDONED.value,
                    deadline_at=None,
                    updated_at=now,
                    finished_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                return self._lost_update_result(
                    session,
                    room_id,
                    operation_id,
                    command_kind="delete",
                    fingerprint=fingerprint,
                    expected_version=expected_version,
                )
            if game.room_id is not None:
                session.execute(
                    update(Room)
                    .where(Room.id == game.room_id)
                    .values(
                        status=StoredRoomStatus.CLOSED.value,
                        deleted_at=now,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
            session.add(RoomCommandReceipt(
                room_id=room_id,
                operation_id=operation_id,
                command_kind="delete",
                command_fingerprint=fingerprint,
                expected_version=expected_version,
                result_snapshot_json=None,
                deleted=True,
            ))
            session.flush()
            receipt = CommandReceipt(
                operation_id=operation_id,
                snapshot=None,
                command_kind="delete",
                fingerprint=fingerprint,
                expected_version=expected_version,
                deleted=True,
            )
            return RepositoryResult(RepositoryStatus.APPLIED, receipt=receipt)

    def _lost_update_result(
        self,
        session: Any,
        room_id: str,
        operation_id: str,
        *,
        command_kind: str,
        fingerprint: str,
        expected_version: int,
    ) -> RepositoryResult:
        # Discard the pre-update identity-map value before reading the winner.
        session.expire_all()
        stored = session.get(
            RoomCommandReceipt,
            {"room_id": room_id, "operation_id": operation_id},
        )
        if stored is not None:
            return self._duplicate_result(
                session,
                stored,
                command_kind=command_kind,
                fingerprint=fingerprint,
                expected_version=expected_version,
            )
        latest = session.scalar(
            select(Game).where(Game.id == room_id).execution_options(
                populate_existing=True
            )
        )
        if latest is None or _game_deleted(latest):
            return RepositoryResult(RepositoryStatus.NOT_FOUND)
        return RepositoryResult(
            RepositoryStatus.VERSION_CONFLICT,
            current_snapshot=_snapshot_from_game(latest),
        )

    def _duplicate_result(
        self,
        session: Any,
        stored: RoomCommandReceipt,
        *,
        command_kind: str,
        fingerprint: str,
        expected_version: int,
    ) -> RepositoryResult:
        if (
            stored.command_kind != command_kind
            or stored.command_fingerprint != fingerprint
            or stored.expected_version != expected_version
        ):
            raise RoomOperationConflictError(stored.room_id, stored.operation_id)
        game = session.get(Game, stored.room_id)
        current = (
            _snapshot_from_game(game)
            if game is not None and not _game_deleted(game)
            else None
        )
        return RepositoryResult(
            RepositoryStatus.DUPLICATE,
            receipt=_receipt(stored),
            current_snapshot=current,
        )

    def _recover_duplicate(
        self,
        room_id: str,
        operation_id: str,
        *,
        command_kind: str,
        fingerprint: str,
        expected_version: int,
    ) -> RepositoryResult | None:
        with self.database.read_session() as session:
            stored = session.get(
                RoomCommandReceipt,
                {"room_id": room_id, "operation_id": operation_id},
            )
            if stored is None:
                return None
            return self._duplicate_result(
                session,
                stored,
                command_kind=command_kind,
                fingerprint=fingerprint,
                expected_version=expected_version,
            )
    def _guard(self) -> RLock | _NullLock:
        return self._sqlite_lock or _NullLock()


class _NullLock:
    def __enter__(self) -> _NullLock:
        return self

    def __exit__(self, *_: object) -> None:
        return None

def _projection_values(
    snapshot: RoomSnapshot, document: dict[str, Any]
) -> dict[str, Any]:
    last_turn = snapshot.history[-1] if snapshot.history else None
    paused = snapshot.paused_remaining_seconds
    return {
        "mode": (
            GameMode.MULTIPLAYER.value
            if snapshot.mode is RoomMode.PVP else GameMode.SOLO.value
        ),
        "status": _stored_status(snapshot.status),
        "turn_time_seconds": snapshot.turn_seconds,
        "theme_key": snapshot.theme_key,
        "bot_difficulty": snapshot.bot_difficulty,
        "bot_count": sum(
            seat.owner_user_id is None and seat.controller is SeatController.BOT
            for seat in snapshot.players
        ),
        "state_json": document,
        "current_turn_index": snapshot.current_turn,
        "current_word_surface": last_turn.surface if last_turn else None,
        "current_word_reading": last_turn.reading if last_turn else None,
        "expected_kana": snapshot.expected_kana,
        "state_version": snapshot.state_version,
        "deadline_at": snapshot.deadline_at,
        # The projection is integer by schema; exact fractional state remains JSON.
        "paused_remaining_seconds": math.ceil(paused) if paused is not None else None,
        "winner_user_id": _winner(snapshot),
        "updated_at": utc_now(),
        "finished_at": utc_now() if snapshot.status is RoomStatus.FINISHED else None,
    }


def _reconcile_departed_spectators(
    session: Any,
    game: Game,
    current: RoomSnapshot,
    next_snapshot: RoomSnapshot,
) -> None:
    """Keep active-lobby authorization aligned with spectator departures.

    Active rooms cannot accept new spectators, so a coordinator CAS may only
    preserve or remove the existing ordered spectator set. When spectators
    leave, their lobby membership is closed in the same transaction as the
    authoritative Game snapshot. Any missing or differently-shaped membership
    aborts the transaction rather than persisting a state that active-game
    lookup must reject.
    """

    if current.mode is not RoomMode.PVP:
        return
    if next_snapshot.mode is not RoomMode.PVP:
        raise RoomSnapshotCorruptError("an active PvP room cannot change mode")

    current_spectators = tuple(current.spectators)
    next_spectators = tuple(next_snapshot.spectators)
    departed = set(current_spectators).difference(next_spectators)
    expected_remaining = tuple(
        user_id
        for user_id in current_spectators
        if user_id not in departed
    )
    if next_spectators != expected_remaining:
        raise RoomSnapshotCorruptError(
            "an active room may only remove existing spectators"
        )
    if not departed:
        return
    if game.room_id is None:
        raise RoomSnapshotCorruptError(
            "a PvP spectator departure requires a lobby room"
        )

    memberships = tuple(
        session.scalars(
            select(RoomMembership)
            .where(
                RoomMembership.room_id == game.room_id,
                RoomMembership.user_id.in_(departed),
                RoomMembership.left_at.is_(None),
            )
            .with_for_update()
        )
    )
    if (
        {membership.user_id for membership in memberships} != departed
        or any(
            membership.role != RoomRole.SPECTATOR.value
            or membership.seat_index is not None
            for membership in memberships
        )
    ):
        raise RoomSnapshotCorruptError(
            "departed spectators disagree with active lobby memberships"
        )

    now = utc_now()
    for membership in memberships:
        membership.presence = PresenceState.OFFLINE.value
        membership.connected_count = 0
        membership.presence_expires_at = None
        membership.is_bot_substituting = False
        membership.ready = False
        membership.last_seen_at = now
        membership.left_at = now


def _stored_status(status: RoomStatus) -> str:
    if status is RoomStatus.ACTIVE:
        return StoredGameStatus.ACTIVE.value
    if status is RoomStatus.PAUSED:
        return StoredGameStatus.PAUSED.value
    return StoredGameStatus.FINISHED.value


def _winner(snapshot: RoomSnapshot) -> str | None:
    if snapshot.status is not RoomStatus.FINISHED:
        return None
    active = tuple(
        seat
        for seat in snapshot.players
        if seat.index not in snapshot.eliminated_seats
    )
    if len(active) == 1:
        return active[0].owner_user_id
    # Backward compatibility for finished two-player schema-v2 snapshots.
    if snapshot.losing_seat is not None and len(snapshot.players) == 2:
        return snapshot.players[1 - snapshot.losing_seat].owner_user_id
    return None


def _snapshot_from_game(game: Game) -> RoomSnapshot:
    state = game.state_json or {}
    if not _repository_document(state) or _deleted_document(state):
        raise RoomSnapshotCorruptError(
            f"game {game.id!r} has no active coordinator snapshot"
        )
    snapshot = deserialize_room_snapshot(state)
    _projection_matches(game, snapshot)
    return snapshot


def _projection_matches(game: Game, snapshot: RoomSnapshot) -> None:
    expected_mode = (
        GameMode.MULTIPLAYER.value
        if snapshot.mode is RoomMode.PVP else GameMode.SOLO.value
    )
    if (
        game.id != snapshot.room_id
        or game.state_version != snapshot.state_version
        or game.mode != expected_mode
        or game.status != _stored_status(snapshot.status)
        or game.current_turn_index != snapshot.current_turn
        or game.theme_key != snapshot.theme_key
        or game.bot_difficulty != snapshot.bot_difficulty
    ):
        raise RoomSnapshotCorruptError(
            f"game {game.id!r} projection disagrees with its room snapshot"
        )


def _receipt(stored: RoomCommandReceipt) -> CommandReceipt:
    if stored.command_kind not in {"compare_and_swap", "delete"}:
        raise RoomSnapshotCorruptError("invalid command receipt kind")
    if type(stored.expected_version) is not int or stored.expected_version < 0:
        raise RoomSnapshotCorruptError("invalid receipt expected_version")
    try:
        _command_fingerprint(stored.command_fingerprint)
    except ValueError as error:
        raise RoomSnapshotCorruptError(
            "invalid receipt command_fingerprint"
        ) from error
    if stored.deleted != (stored.command_kind == "delete"):
        raise RoomSnapshotCorruptError(
            "command receipt kind disagrees with deleted flag"
        )
    if stored.deleted:
        if stored.result_snapshot_json is not None:
            raise RoomSnapshotCorruptError(
                "deleted command receipt unexpectedly contains a snapshot"
            )
        snapshot = None
    else:
        if stored.result_snapshot_json is None:
            raise RoomSnapshotCorruptError(
                "non-deleted command receipt has no result snapshot"
            )
        snapshot = deserialize_room_snapshot(stored.result_snapshot_json)
    return CommandReceipt(
        operation_id=stored.operation_id,
        snapshot=snapshot,
        command_kind=stored.command_kind,
        fingerprint=stored.command_fingerprint,
        expected_version=stored.expected_version,
        deleted=stored.deleted,
    )


def _validate_command(
    room_id: str,
    expected_version: int,
    operation_id: str,
    next_snapshot: RoomSnapshot,
    command_fingerprint: str,
) -> None:
    _identifier(room_id, "room_id", 36)
    _identifier(operation_id, "operation_id", 64)
    _version(expected_version)
    _command_fingerprint(command_fingerprint)
    if next_snapshot.room_id != room_id:
        raise ValueError("next snapshot belongs to another room")
    if next_snapshot.state_version != expected_version + 1:
        raise ValueError("next snapshot must increment state_version")


def _command_fingerprint(value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(
            "command_fingerprint must be a lowercase SHA-256 hex digest"
        )

def _identifier(value: str, name: str, maximum: int) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{name} must contain 1-{maximum} characters")


def _version(value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("expected_version must be a non-negative integer")


def _repository_document(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get(_SCHEMA_KEY) in _SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS
        and isinstance(value.get("deleted"), bool)
    )


def _deleted_document(value: Mapping[str, Any]) -> bool:
    return value.get("deleted") is True


def _game_deleted(game: Game) -> bool:
    state = game.state_json or {}
    return (
        game.status == StoredGameStatus.ABANDONED.value
        or (_repository_document(state) and _deleted_document(state))
    )


def _datetime_to_json(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _nullable_datetime_to_json(value: datetime | None) -> str | None:
    return _datetime_to_json(value) if value is not None else None


def _datetime_from_json(value: object, name: str) -> datetime:
    text = _string(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise RoomSnapshotCorruptError(f"{name} is not ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RoomSnapshotCorruptError(f"{name} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _nullable_datetime(value: object, name: str) -> datetime | None:
    return None if value is None else _datetime_from_json(value, name)


def _enum(enum_type: Any, value: object, name: str) -> Any:
    text = _string(value, name)
    try:
        return enum_type(text)
    except ValueError as error:
        raise RoomSnapshotCorruptError(f"{name} is unsupported") from error


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RoomSnapshotCorruptError(f"{name} must be an object")
    return value


def _array(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise RoomSnapshotCorruptError(f"{name} must be an array")
    return value


def _exact_keys(value: Mapping[str, Any], keys: set[str], name: str) -> None:
    if set(value) != keys:
        raise RoomSnapshotCorruptError(f"{name} has unexpected fields")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RoomSnapshotCorruptError(f"{name} must be a non-empty string")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise RoomSnapshotCorruptError(f"{name} must be a string")
    return value


def _nullable_string(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _integer(value: object, name: str) -> int:
    if type(value) is not int:
        raise RoomSnapshotCorruptError(f"{name} must be an integer")
    return value


def _nullable_integer(value: object, name: str) -> int | None:
    return None if value is None else _integer(value, name)


def _nullable_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float):
        raise RoomSnapshotCorruptError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise RoomSnapshotCorruptError(f"{name} must be finite")
    return result


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise RoomSnapshotCorruptError(f"{name} must be a boolean")
    return value


__all__ = [
    "RoomAlreadyInitialized",
    "RoomOperationConflictError",
    "RoomPersistenceError",
    "RoomPersistenceNotFound",
    "RoomSnapshotCorruptError",
    "SNAPSHOT_SCHEMA_VERSION",
    "SQLAlchemyRoomRepository",
    "deserialize_room_snapshot",
    "serialize_room_snapshot",
]