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
from sqlalchemy.exc import IntegrityError

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
    RoomNotFound,
    RoomSnapshot,
    RoomStatus,
    SeatController,
    create_room_snapshot,
)
from .themes import ALL_THEME_ID, ThemeCatalog


StrategyResolver = Callable[[str], BotStrategy]


class SoloGameError(RuntimeError):
    """Base error for solo lifecycle commands."""


class SoloGameNotFound(SoloGameError):
    pass


class SoloGameAuthorizationError(SoloGameError):
    pass


class SoloGameStateError(SoloGameError):
    """Raised when a solo lifecycle command targets the wrong state."""


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
        theme_key: str | None = None,
        turn_seconds: int | None = None,
        now: datetime | None = None,
    ) -> RoomSnapshot:
        """Create an unrestricted solo game; legacy theme input is ignored."""

        owner = _identifier(user_id, "user_id")
        if type(bot_count) is not int or not 1 <= bot_count <= 7:
            raise ValueError("bot_count must be from 1 to 7")
        difficulty = str(bot_difficulty).strip().lower()
        # Resolve before writing so an unregistered user-owned EasyBot fails
        # closed instead of leaving an unplayable Game row.
        self._strategy_resolver(difficulty)
        theme = self.themes.get(ALL_THEME_ID)
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

    async def rematch(
        self,
        user_id: str,
        finished_game_id: str,
        *,
        now: datetime | None = None,
    ) -> RoomSnapshot:
        """Create a fresh game with the settings of one owned finished game.

        The finished snapshot is read only and maps to exactly one fresh game
        ID. Concurrent transport retries therefore converge on the same child
        without overwriting or recording the completed match a second time.
        """

        owner = _identifier(user_id, "user_id")
        finished_id = _identifier(finished_game_id, "finished_game_id")
        try:
            finished = await self.coordinator.load_snapshot(finished_id)
        except RoomNotFound as error:
            raise SoloGameNotFound(finished_id) from error
        if finished.mode is not RoomMode.SOLO_BOT:
            raise SoloGameAuthorizationError("game is not a solo Bot match")
        if finished.seat_for_user(owner) is None:
            raise SoloGameAuthorizationError(
                "user does not own this solo game"
            )
        if finished.status is not RoomStatus.FINISHED:
            raise SoloGameStateError(
                "only a finished solo Bot match can be retried"
            )

        existing_id = await asyncio.to_thread(
            self._find_rematch_id_sync,
            finished_id,
        )
        if existing_id is not None:
            return await self._load_existing_rematch(
                owner,
                finished,
                existing_id,
            )

        permanent_bot_count = sum(
            seat.owner_user_id is None for seat in finished.players
        )
        self._strategy_resolver(finished.bot_difficulty)
        theme = self.themes.get(finished.theme_key)
        created_at = now or datetime.now(timezone.utc)
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        created_at = created_at.astimezone(timezone.utc)
        rematch = create_room_snapshot(
            str(uuid4()),
            (owner,),
            mode=RoomMode.SOLO_BOT,
            permanent_bot_count=permanent_bot_count,
            turn_seconds=finished.turn_seconds,
            theme_key=theme.theme_id,
            bot_difficulty=finished.bot_difficulty,
            now=created_at,
        )
        try:
            await asyncio.to_thread(
                self._insert_snapshot,
                owner,
                rematch,
                created_at,
                rematch_of_game_id=finished_id,
            )
        except IntegrityError:
            existing_id = await asyncio.to_thread(
                self._find_rematch_id_sync,
                finished_id,
            )
            if existing_id is None:
                raise
            return await self._load_existing_rematch(
                owner,
                finished,
                existing_id,
            )
        self.runtime.notify(rematch.room_id)
        return rematch

    async def _load_existing_rematch(
        self,
        owner_user_id: str,
        source: RoomSnapshot,
        game_id: str,
    ) -> RoomSnapshot:
        try:
            existing = await self.coordinator.load_snapshot(game_id)
        except RoomNotFound as error:
            raise SoloGameStateError(
                "the existing rematch is unavailable"
            ) from error
        expected_bot_count = sum(
            seat.owner_user_id is None for seat in source.players
        )
        actual_bot_count = sum(
            seat.owner_user_id is None for seat in existing.players
        )
        if (
            existing.room_id == source.room_id
            or existing.mode is not RoomMode.SOLO_BOT
            or existing.seat_for_user(owner_user_id) is None
            or actual_bot_count != expected_bot_count
            or existing.bot_difficulty != source.bot_difficulty
            or existing.theme_key != source.theme_key
            or existing.turn_seconds != source.turn_seconds
        ):
            raise RoomSnapshotCorruptError(
                "existing solo rematch settings disagree with its source"
            )
        if existing.status is RoomStatus.ACTIVE:
            self.runtime.notify(existing.room_id)
        return existing

    async def list_paused(self, user_id: str) -> tuple[PausedSoloGame, ...]:
        """List authoritative paused solo snapshots owned by ``user_id``."""

        owner = _identifier(user_id, "user_id")
        return await asyncio.to_thread(self._list_paused_sync, owner)

    def _insert_snapshot(
        self,
        owner_user_id: str,
        snapshot: RoomSnapshot,
        created_at: datetime,
        *,
        rematch_of_game_id: str | None = None,
    ) -> None:
        document = serialize_room_snapshot(snapshot)
        with self.database.transaction() as session:
            session.add(
                Game(
                    id=snapshot.room_id,
                    room_id=None,
                    created_by_user_id=owner_user_id,
                    rematch_of_game_id=rematch_of_game_id,
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

    def _find_rematch_id_sync(
        self,
        finished_game_id: str,
    ) -> str | None:
        with self.database.read_session() as session:
            return session.scalar(
                select(Game.id).where(
                    Game.rematch_of_game_id == finished_game_id
                )
            )

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
    "SoloGameStateError",
]
