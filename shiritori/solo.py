"""Authoritative creation and discovery of resumable solo Bot matches."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from .bots import BotStrategy
from .database import Database
from .models import Game, GameMode, StoredGameStatus
from .room_persistence import (
    RoomSnapshotCorruptError,
    deserialize_room_snapshot,
    serialize_room_snapshot,
)
from .room_runtime import RoomRuntime
from .rooms import (
    RoomCallback,
    RoomCoordinator,
    RoomMode,
    RoomSnapshot,
    RoomStatus,
    SeatController,
    create_room_snapshot,
)
from .themes import ThemeCatalog


StrategyResolver = Callable[[str], BotStrategy]


class SoloGameError(RuntimeError):
    """Base error for solo lifecycle commands."""


class SoloGameNotFound(SoloGameError):
    pass


class SoloGameAuthorizationError(SoloGameError):
    pass


@dataclass(frozen=True, slots=True)
class PausedSoloGame:
    """Small server-derived row suitable for a saved-games list."""

    game_id: str
    theme_key: str
    bot_difficulty: str
    bot_count: int
    turn_seconds: int | None
    state_version: int
    move_count: int
    paused_remaining_seconds: int | None
    updated_at: datetime


class SoloGameService:
    """Create, reconnect, and list strict RoomSnapshot-based solo games."""

    def __init__(
        self,
        database: Database,
        coordinator: RoomCoordinator,
        runtime: RoomRuntime,
        themes: ThemeCatalog,
        *,
        strategy_resolver: StrategyResolver,
    ) -> None:
        self.database = database
        self.coordinator = coordinator
        self.runtime = runtime
        self.themes = themes
        self._strategy_resolver = strategy_resolver

    async def create(
        self,
        user_id: str,
        *,
        bot_count: int = 1,
        bot_difficulty: str = "normal",
        theme_key: str = "all",
        turn_seconds: int | None = None,
        now: datetime | None = None,
    ) -> RoomSnapshot:
        """Atomically persist a new solo game, then start its supervisor."""

        owner = _identifier(user_id, "user_id")
        if type(bot_count) is not int or not 1 <= bot_count <= 7:
            raise ValueError("bot_count must be from 1 to 7")
        difficulty = str(bot_difficulty).strip().lower()
        # Resolve before writing so an unregistered user-owned EasyBot fails
        # closed instead of leaving an unplayable Game row.
        self._strategy_resolver(difficulty)
        theme = self.themes.get(theme_key)
        created_at = now or datetime.now(timezone.utc)
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        created_at = created_at.astimezone(timezone.utc)
        game_id = str(uuid4())
        snapshot = create_room_snapshot(
            game_id,
            (owner,),
            mode=RoomMode.SOLO_BOT,
            permanent_bot_count=bot_count,
            turn_seconds=turn_seconds,
            theme_key=theme.theme_id,
            bot_difficulty=difficulty,
            now=created_at,
        )
        await asyncio.to_thread(
            self._insert_snapshot,
            owner,
            snapshot,
            created_at,
        )
        self.runtime.notify(game_id)
        return snapshot

    async def connect(
        self,
        user_id: str,
        game_id: str,
        client_id: str,
        callback: RoomCallback | None = None,
        *,
        now: datetime | None = None,
    ) -> RoomSnapshot:
        """Reconnect the owner; paused time resumes inside the coordinator."""

        owner = _identifier(user_id, "user_id")
        identifier = _identifier(game_id, "game_id")
        loaded = await self.coordinator.load_snapshot(identifier)
        if loaded.mode is not RoomMode.SOLO_BOT:
            raise SoloGameAuthorizationError("game is not a solo Bot match")
        if loaded.seat_for_user(owner) is None:
            raise SoloGameAuthorizationError("user does not own this solo game")
        snapshot = await self.coordinator.connect_client(
            identifier,
            owner,
            _identifier(client_id, "client_id", maximum=128),
            callback,
            now=now,
        )
        self.runtime.notify(snapshot.room_id)
        return snapshot

    async def list_paused(self, user_id: str) -> tuple[PausedSoloGame, ...]:
        """List authoritative paused solo snapshots owned by ``user_id``."""

        owner = _identifier(user_id, "user_id")
        return await asyncio.to_thread(self._list_paused_sync, owner)

    def _insert_snapshot(
        self,
        owner_user_id: str,
        snapshot: RoomSnapshot,
        created_at: datetime,
    ) -> None:
        document = serialize_room_snapshot(snapshot)
        with self.database.transaction() as session:
            session.add(
                Game(
                    id=snapshot.room_id,
                    room_id=None,
                    created_by_user_id=owner_user_id,
                    mode=GameMode.SOLO.value,
                    status=StoredGameStatus.ACTIVE.value,
                    theme_key=snapshot.theme_key,
                    turn_time_seconds=snapshot.turn_seconds,
                    bot_count=sum(
                        seat.owner_user_id is None
                        for seat in snapshot.players
                    ),
                    bot_difficulty=snapshot.bot_difficulty,
                    settings_json={},
                    state_json=document,
                    starting_seat_index=snapshot.current_turn,
                    current_turn_index=snapshot.current_turn,
                    current_word_surface=None,
                    current_word_reading=None,
                    expected_kana=None,
                    state_version=snapshot.state_version,
                    deadline_at=snapshot.deadline_at,
                    paused_remaining_seconds=None,
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
            session.flush()

    def _list_paused_sync(
        self, owner_user_id: str
    ) -> tuple[PausedSoloGame, ...]:
        with self.database.read_session() as session:
            games = tuple(
                session.scalars(
                    select(Game)
                    .where(
                        Game.created_by_user_id == owner_user_id,
                        Game.mode == GameMode.SOLO.value,
                        Game.status == StoredGameStatus.PAUSED.value,
                    )
                    .order_by(Game.updated_at.desc(), Game.id)
                )
            )
            summaries: list[PausedSoloGame] = []
            for game in games:
                state: Any = game.state_json or {}
                try:
                    snapshot = deserialize_room_snapshot(state)
                except (TypeError, ValueError, KeyError) as error:
                    raise RoomSnapshotCorruptError(
                        f"game {game.id!r} has an invalid solo snapshot"
                    ) from error
                if (
                    snapshot.mode is not RoomMode.SOLO_BOT
                    or snapshot.seat_for_user(owner_user_id) is None
                    or snapshot.room_id != game.id
                ):
                    raise RoomSnapshotCorruptError(
                        f"game {game.id!r} ownership projection disagrees"
                    )
                _validate_paused_projection(game, snapshot)
                remaining = snapshot.paused_remaining_seconds
                summaries.append(
                    PausedSoloGame(
                        game_id=game.id,
                        theme_key=snapshot.theme_key,
                        bot_difficulty=snapshot.bot_difficulty,
                        bot_count=sum(
                            seat.owner_user_id is None
                            for seat in snapshot.players
                        ),
                        turn_seconds=snapshot.turn_seconds,
                        state_version=snapshot.state_version,
                        move_count=len(snapshot.history),
                        paused_remaining_seconds=(
                            math.ceil(remaining)
                            if remaining is not None
                            else None
                        ),
                        updated_at=_aware_utc(game.updated_at),
                    )
                )
            return tuple(summaries)


def _identifier(
    value: str,
    name: str,
    *,
    maximum: int = 36,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.isspace()
        or len(value) > maximum
    ):
        raise ValueError(f"{name} must contain 1-{maximum} characters")
    return value


def _validate_paused_projection(game: Game, snapshot: RoomSnapshot) -> None:
    last_turn = snapshot.history[-1] if snapshot.history else None
    remaining = snapshot.paused_remaining_seconds
    expected_remaining = math.ceil(remaining) if remaining is not None else None
    expected_bot_count = sum(
        seat.owner_user_id is None
        and seat.controller is SeatController.BOT
        for seat in snapshot.players
    )
    if (
        snapshot.status is not RoomStatus.PAUSED
        or game.room_id is not None
        or game.mode != GameMode.SOLO.value
        or game.status != StoredGameStatus.PAUSED.value
        or game.state_version != snapshot.state_version
        or game.current_turn_index != snapshot.current_turn
        or game.theme_key != snapshot.theme_key
        or game.bot_difficulty != snapshot.bot_difficulty
        or game.turn_time_seconds != snapshot.turn_seconds
        or game.bot_count != expected_bot_count
        or game.current_word_surface
        != (last_turn.surface if last_turn else None)
        or game.current_word_reading
        != (last_turn.reading if last_turn else None)
        or game.expected_kana != snapshot.expected_kana
        or game.deadline_at is not None
        or game.paused_remaining_seconds != expected_remaining
        or game.winner_user_id is not None
        or game.finished_at is not None
    ):
        raise RoomSnapshotCorruptError(
            f"game {game.id!r} projection disagrees with its solo snapshot"
        )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "PausedSoloGame",
    "SoloGameAuthorizationError",
    "SoloGameError",
    "SoloGameNotFound",
    "SoloGameService",
]
