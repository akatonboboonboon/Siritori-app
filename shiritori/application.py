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
from .daily_challenge_persistence import SQLAlchemyDailyChallengeService
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
from .onboarding import OnboardingService
from .rooms import (
    LexiconRoomService,
    PlayerSeat,
    RoomCoordinator,
    RoomHub,
    RoomSnapshot,
)
from .score_attack_persistence import SQLAlchemyScoreAttackService
from .settings import Settings
from .solo import SoloGameService
from .statistics import StatisticsRepository
from .themes import ALL_THEME_ID, ThemeCatalog, ThemeDefinition
from .word_review import (
    ApprovedLexiconValidator,
    ApprovedWordCatalog,
    WordReviewService,
)
from .word_suggestions import WordSuggestionService


SUPPORTED_BOT_DIFFICULTIES: Final = frozenset({"easy", "normal", "hard"})


@dataclass(slots=True)
class ApplicationServices:
    """Long-lived services shared by all HTTP and NiceGUI handlers."""

    settings: Settings
    database: Database
    auth: AuthService
    games: GameRepository
    statistics: StatisticsRepository
    word_suggestions: WordSuggestionService
    score_attack: SQLAlchemyScoreAttackService
    daily_challenge: SQLAlchemyDailyChallengeService
    onboarding: OnboardingService
    approved_words: ApprovedWordCatalog
    approved_validator: ApprovedLexiconValidator
    word_review: WordReviewService
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
        statistics = StatisticsRepository(database)
        word_suggestions = WordSuggestionService(database)
        approved_words = ApprovedWordCatalog(database)
        approved_validator = ApprovedLexiconValidator(approved_words)
        word_review = WordReviewService(
            database,
            settings.admin_username_keys,
            approved_words,
        )
        onboarding = OnboardingService(database)
        # Ranked modes deliberately retain the pinned Sudachi validator for
        # the whole rules version.  A word approved during the day must not
        # change the legal vocabulary between two leaderboard attempts.
        score_attack = SQLAlchemyScoreAttackService(database)
        daily_challenge = SQLAlchemyDailyChallengeService(database)
        themes = ThemeCatalog()
        lobby_repository = SQLAlchemyLobbyRepository(database)
        lobby = LobbyService(lobby_repository)
        room_repository = SQLAlchemyRoomRepository(database)
        room_hub = RoomHub()
        rooms = RoomCoordinator(room_repository, hub=room_hub)
        strategies: dict[str, BotStrategy] = {
            "easy": EasyBot(seed="server-easy"),
            "normal": NormalBot(seed="server-normal"),
            "hard": HardBot(seed="server-hard"),
        }

        # The runtime closures intentionally dereference the service object
        # only after construction so a user-owned EasyBot can be registered.
        holder: dict[str, ApplicationServices] = {}

        def strategy_resolver(
            snapshot: RoomSnapshot,
            _seat: PlayerSeat,
        ) -> BotStrategy:
            return holder["services"].bot_strategy_for(
                snapshot.bot_difficulty
            )

        def word_index_resolver(_snapshot: RoomSnapshot) -> WordIndex:
            return get_default_word_index()

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
            statistics=statistics,
            word_suggestions=word_suggestions,
            score_attack=score_attack,
            daily_challenge=daily_challenge,
            onboarding=onboarding,
            approved_words=approved_words,
            approved_validator=approved_validator,
            word_review=word_review,
            lobby_repository=lobby_repository,
            lobby=lobby,
            room_repository=room_repository,
            room_hub=room_hub,
            rooms=rooms,
            themes=themes,
            room_words=LexiconRoomService(
                rooms,
                validator=approved_validator,
                themes=themes,
            ),
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
        await asyncio.to_thread(self.approved_words.refresh)
        await asyncio.to_thread(
            self.word_review.validate_configured_admins
        )
        while True:
            finalized = await asyncio.to_thread(
                self.score_attack.finalize_expired_active_runs,
                limit=100,
            )
            if len(finalized) < 100:
                break
        while True:
            finalized = await asyncio.to_thread(
                self.daily_challenge.finalize_expired_active_runs,
                limit=100,
            )
            if len(finalized) < 100:
                break
        recoverable_room_ids = (
            await self.room_repository.list_recoverable_room_ids()
        )
        await asyncio.gather(
            *(
                self.rooms.recover_after_restart(room_id)
                for room_id in recoverable_room_ids
            )
        )
        self._started = True

    async def close(self) -> None:
        """Stop background tasks and release pooled database connections."""

        await self.runtime.close()
        self.database.dispose()


__all__ = ["ApplicationServices", "SUPPORTED_BOT_DIFFICULTIES"]
