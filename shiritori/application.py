"""Composition root for the authenticated multiplayer application.

The UI layer imports one :class:`ApplicationServices` instance instead of
constructing database repositories ad hoc.  This keeps the authoritative
room state, presence hub, background runtime, and dictionary/theme registries
shared by every NiceGUI page in the process.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Final

from .auth import AuthService
from .bot_catalog import build_bot_catalog, get_default_word_index
from .bots import BotStrategy, EasyBot, HardBot, NormalBot, WordIndex
from .database import Database, GameRepository
from .lobby import LobbyService
from .lobby_persistence import SQLAlchemyLobbyRepository
from .room_persistence import SQLAlchemyRoomRepository
from .room_runtime import (
    RoomRuntime,
    RoomRuntimeCapabilityError,
    RoomRuntimeClosed,
)
from .rooms import (
    LexiconRoomService,
    PlayerSeat,
    RoomCoordinator,
    RoomHub,
    RoomSnapshot,
)
from .settings import Settings
from .solo import SoloGameService
from .themes import ALL_THEME_ID, ThemeCatalog, ThemeDefinition


SUPPORTED_BOT_DIFFICULTIES: Final = frozenset({"easy", "normal", "hard"})


@dataclass(slots=True)
class ApplicationServices:
    """Long-lived services shared by all HTTP and NiceGUI handlers."""

    settings: Settings
    database: Database
    auth: AuthService
    games: GameRepository
    lobby_repository: SQLAlchemyLobbyRepository
    lobby: LobbyService
    room_repository: SQLAlchemyRoomRepository
    room_hub: RoomHub
    rooms: RoomCoordinator
    themes: ThemeCatalog
    room_words: LexiconRoomService
    runtime: RoomRuntime
    solo: SoloGameService
    _bot_strategies: dict[str, BotStrategy] = field(repr=False)
    _theme_word_indexes: dict[ThemeDefinition, WordIndex] = field(
        default_factory=dict,
        repr=False,
    )
    _started: bool = field(default=False, init=False, repr=False)

    @classmethod
    def build(cls, settings: Settings) -> "ApplicationServices":
        """Build the production service graph without touching the schema."""

        database = Database(settings.database_url)
        auth = AuthService(database)
        games = GameRepository(database)
        themes = ThemeCatalog()
        lobby_repository = SQLAlchemyLobbyRepository(database)
        lobby = LobbyService(
            lobby_repository,
            theme_resolver=themes.get,
        )
        room_repository = SQLAlchemyRoomRepository(database)
        room_hub = RoomHub()
        rooms = RoomCoordinator(room_repository, hub=room_hub)
        strategies: dict[str, BotStrategy] = {
            "easy": EasyBot(seed="server-easy"),
            "normal": NormalBot(seed="server-normal"),
            "hard": HardBot(seed="server-hard"),
        }

        # The runtime closures intentionally dereference the service object
        # only after construction. This lets user-owned EasyBot and theme data
        # be registered at startup without replacing the runtime.
        holder: dict[str, ApplicationServices] = {}

        def strategy_resolver(
            snapshot: RoomSnapshot,
            _seat: PlayerSeat,
        ) -> BotStrategy:
            return holder["services"].bot_strategy_for(
                snapshot.bot_difficulty
            )

        def word_index_resolver(snapshot: RoomSnapshot) -> WordIndex:
            return holder["services"].word_index_for(snapshot.theme_key)

        runtime = RoomRuntime(
            rooms,
            strategy_resolver=strategy_resolver,
            word_index_resolver=word_index_resolver,
        )

        def notify_runtime(room_id: str) -> None:
            try:
                runtime.notify(room_id)
            except RoomRuntimeClosed:
                # A committed state remains authoritative during shutdown and
                # will be recovered when the next process starts.
                return

        rooms.set_activity_notifier(notify_runtime)

        def strategy_by_name(difficulty: str) -> BotStrategy:
            return holder["services"].bot_strategy_for(difficulty)

        solo = SoloGameService(
            database,
            rooms,
            runtime,
            themes,
            strategy_resolver=strategy_by_name,
        )
        services = cls(
            settings=settings,
            database=database,
            auth=auth,
            games=games,
            lobby_repository=lobby_repository,
            lobby=lobby,
            room_repository=room_repository,
            room_hub=room_hub,
            rooms=rooms,
            themes=themes,
            room_words=LexiconRoomService(rooms, themes=themes),
            runtime=runtime,
            solo=solo,
            _bot_strategies=strategies,
        )
        holder["services"] = services
        return services

    def register_bot_strategy(
        self,
        difficulty: str,
        strategy: BotStrategy,
        *,
        replace: bool = False,
    ) -> None:
        """Register the user-owned EasyBot or deliberately replace a strategy."""

        key = str(difficulty).strip().lower()
        if key not in SUPPORTED_BOT_DIFFICULTIES:
            raise ValueError("difficulty must be easy, normal, or hard")
        if not isinstance(strategy, BotStrategy):
            raise TypeError("strategy must implement BotStrategy")
        if key in self._bot_strategies and not replace:
            raise ValueError(f"Bot strategy already registered: {key}")
        self._bot_strategies[key] = strategy

    def bot_strategy_for(self, difficulty: str) -> BotStrategy:
        """Resolve a server-owned strategy or fail closed."""

        key = str(difficulty).strip().lower()
        try:
            return self._bot_strategies[key]
        except KeyError as error:
            raise RoomRuntimeCapabilityError(
                f"Bot strategy is not registered: {key}"
            ) from error

    def register_theme(
        self,
        theme: ThemeDefinition,
        *,
        replace: bool = False,
    ) -> None:
        """Register user-authored theme data and invalidate derived indexes."""

        self.themes.register(theme, replace=replace)
        self._theme_word_indexes.clear()

    def word_index_for(self, theme_key: str) -> WordIndex:
        """Return a dictionary-validated Bot index for one persisted theme."""

        theme = self.themes.get(theme_key)
        if theme.theme_id == ALL_THEME_ID:
            return get_default_word_index()
        cached = self._theme_word_indexes.get(theme)
        if cached is not None:
            return cached
        surfaces = tuple(
            sorted({entry.surface for entry in theme.entries})
        )
        index = build_bot_catalog(surfaces, theme=theme).index
        self._theme_word_indexes[theme] = index
        return index

    async def start(self) -> None:
        """Recover automatic work for authoritative active rooms."""

        if self._started:
            return
        active_room_ids = await self.room_repository.list_active_room_ids()
        await asyncio.gather(
            *(
                self.rooms.recover_after_restart(room_id)
                for room_id in active_room_ids
            )
        )
        self._started = True

    async def close(self) -> None:
        """Stop background tasks and release pooled database connections."""

        await self.runtime.close()
        self.database.dispose()


__all__ = ["ApplicationServices", "SUPPORTED_BOT_DIFFICULTIES"]
