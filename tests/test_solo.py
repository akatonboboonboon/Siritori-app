from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from argon2 import PasswordHasher
from sqlalchemy import select, update

from shiritori.auth import AuthService
from shiritori.bots import EasyBot, HardBot, NormalBot
from shiritori.database import Database
from shiritori.models import Game, MatchParticipation
from shiritori.room_persistence import (
    RoomSnapshotCorruptError,
    SQLAlchemyRoomRepository,
)
from shiritori.room_runtime import RoomRuntime
from shiritori.rooms import (
    InMemoryRoomRepository,
    RoomCoordinator,
    RoomMode,
    RoomRuleSet,
    RoomStatus,
    create_room_snapshot,
)
from shiritori.solo import (
    SoloGameAuthorizationError,
    SoloGameNotFound,
    SoloGameService,
    SoloGameStateError,
)
from shiritori.themes import ThemeCatalog


NOW = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)


class SoloGameServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name, "solo.sqlite3")
        self.database = Database(
            f"sqlite+pysqlite:///{database_path.as_posix()}"
        )
        self.database.create_schema_for_testing()
        auth = AuthService(
            self.database,
            password_hasher=PasswordHasher(
                time_cost=1,
                memory_cost=8 * 1024,
                parallelism=1,
                hash_len=16,
                salt_len=16,
            ),
        )
        self.owner = auth.register("owner", "owner-password-123")
        repository = SQLAlchemyRoomRepository(self.database)
        self.coordinator = RoomCoordinator(
            repository,
            disconnect_grace_seconds=0,
            clock=lambda: NOW,
        )
        strategies = {
            "normal": NormalBot(seed=1),
            "easy": EasyBot(seed=0),
            "hard": HardBot(seed=2),
        }
        self.runtime = RoomRuntime(
            self.coordinator,
            strategy_resolver=lambda snapshot, _seat: strategies[
                snapshot.bot_difficulty
            ],
            word_index_resolver=lambda _snapshot: __import__(
                "shiritori.bot_catalog",
                fromlist=["get_default_word_index"],
            ).get_default_word_index(),
            bot_delay_seconds=5,
            clock=lambda: NOW,
        )
        self.service = SoloGameService(
            self.database,
            self.coordinator,
            self.runtime,
            ThemeCatalog(),
            strategy_resolver=lambda difficulty: strategies[difficulty],
        )

    async def asyncTearDown(self) -> None:
        await self.runtime.close()
        self.database.dispose()
        self.temporary_directory.cleanup()

    async def test_create_disconnect_pause_list_and_reconnect(self) -> None:
        snapshot = await self.service.create(
            self.owner.id,
            bot_count=1,
            lives_per_player=3,
            bot_difficulty="hard",
            rule_set=RoomRuleSet.ONI,
            turn_seconds=30,
            now=NOW,
        )
        self.assertEqual(len(snapshot.players), 2)
        self.assertEqual(snapshot.remaining_lives, (3, 3))
        self.assertIs(snapshot.rule_set, RoomRuleSet.ONI)

        connected = await self.service.connect(
            self.owner.id,
            snapshot.room_id,
            "owner-tab",
            now=NOW,
        )
        delayed_task = await self.coordinator.disconnect_client(
            snapshot.room_id,
            "owner-tab",
        )
        self.assertIsNone(delayed_task)

        paused = await self.service.list_paused(self.owner.id)
        self.assertEqual(len(paused), 1)
        self.assertEqual(paused[0].game_id, snapshot.room_id)
        self.assertEqual(paused[0].bot_count, 1)
        self.assertEqual(paused[0].lives_per_player, 3)
        self.assertIs(paused[0].rule_set, RoomRuleSet.ONI)
        self.assertEqual(paused[0].paused_remaining_seconds, 30)

        resumed = await self.service.connect(
            self.owner.id,
            snapshot.room_id,
            "owner-returned",
            now=NOW,
        )
        # Pause and reconnect are separate persisted transitions.
        self.assertEqual(connected.state_version + 2, resumed.state_version)
        self.assertEqual(resumed.status, RoomStatus.ACTIVE)
        self.assertIs(resumed.rule_set, RoomRuleSet.ONI)
        self.assertIsNotNone(resumed.deadline_at)
        self.assertEqual(await self.service.list_paused(self.owner.id), ())

    async def test_easy_game_can_be_created(self) -> None:
        snapshot = await self.service.create(
            self.owner.id,
            bot_difficulty="easy",
            now=NOW,
        )

        self.assertEqual(snapshot.bot_difficulty, "easy")
        self.assertIs(snapshot.rule_set, RoomRuleSet.STANDARD)
        self.assertEqual(len(snapshot.players), 2)

    async def test_create_rejects_invalid_life_counts(self) -> None:
        for invalid in (True, 0, 6):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    await self.service.create(
                        self.owner.id,
                        lives_per_player=invalid,  # type: ignore[arg-type]
                    )

    async def test_legacy_theme_argument_is_ignored_and_all_is_persisted(
        self,
    ) -> None:
        snapshot = await self.service.create(
            self.owner.id,
            theme_key="food",
            now=NOW,
        )

        self.assertEqual(snapshot.theme_key, "all")
        with self.database.read_session() as session:
            game = session.get(Game, snapshot.room_id)
            self.assertIsNotNone(game)
            self.assertEqual(game.theme_key, "all")

    async def test_rematch_keeps_finished_game_and_copies_settings(self) -> None:
        source = await self.service.create(
            self.owner.id,
            bot_count=3,
            lives_per_player=5,
            bot_difficulty="hard",
            turn_seconds=45,
            now=NOW,
        )
        with self.assertRaises(SoloGameStateError):
            await self.service.rematch(
                self.owner.id,
                source.room_id,
                now=NOW,
            )

        finished_outcome = await self.coordinator.surrender(
            source.room_id,
            self.owner.id,
            expected_version=source.state_version,
            operation_id="finish-for-rematch",
            now=NOW,
        )
        finished = finished_outcome.snapshot
        self.assertEqual(finished.status, RoomStatus.FINISHED)
        with self.assertRaises(SoloGameAuthorizationError):
            await self.service.rematch(
                "different-user",
                source.room_id,
                now=NOW,
            )
        with self.assertRaises(SoloGameNotFound):
            await self.service.rematch(
                self.owner.id,
                "missing-finished-game",
                now=NOW,
            )

        pvp = create_room_snapshot(
            "finished-pvp-game",
            (self.owner.id, "guest"),
            mode=RoomMode.PVP,
            now=NOW,
        )
        finished_pvp = replace(
            pvp,
            status=RoomStatus.FINISHED,
            deadline_at=None,
            end_reason="no_legal_move",
            state_version=1,
        )
        pvp_service = SoloGameService(
            self.database,
            RoomCoordinator(
                InMemoryRoomRepository([finished_pvp]),
                clock=lambda: NOW,
            ),
            self.runtime,
            ThemeCatalog(),
            strategy_resolver=lambda _: NormalBot(seed=3),
        )
        with self.assertRaises(SoloGameAuthorizationError):
            await pvp_service.rematch(
                self.owner.id,
                finished_pvp.room_id,
                now=NOW,
            )

        retried, duplicate = await asyncio.gather(
            self.service.rematch(
                self.owner.id,
                source.room_id,
                now=NOW,
            ),
            self.service.rematch(
                self.owner.id,
                source.room_id,
                now=NOW,
            ),
        )

        self.assertNotEqual(retried.room_id, source.room_id)
        self.assertEqual(duplicate.room_id, retried.room_id)
        self.assertEqual(retried.status, RoomStatus.ACTIVE)
        self.assertEqual(retried.state_version, 0)
        self.assertEqual(retried.history, ())
        self.assertIsNone(retried.expected_kana)
        self.assertEqual(retried.bot_difficulty, "hard")
        self.assertIs(retried.rule_set, RoomRuleSet.STANDARD)
        self.assertEqual(retried.lives_per_player, 5)
        self.assertEqual(retried.remaining_lives, (5, 5, 5, 5))
        self.assertEqual(retried.turn_seconds, 45)
        self.assertEqual(retried.theme_key, source.theme_key)
        self.assertEqual(
            sum(
                seat.owner_user_id is None
                for seat in retried.players
            ),
            3,
        )
        self.assertEqual(
            await self.coordinator.load_snapshot(source.room_id),
            finished,
        )
        with self.database.read_session() as session:
            participations = tuple(
                session.scalars(
                    select(MatchParticipation).where(
                        MatchParticipation.game_id == source.room_id
                    )
                )
            )
        self.assertEqual(len(participations), 1)
        with self.database.read_session() as session:
            rematches = tuple(
                session.scalars(
                    select(Game).where(
                        Game.rematch_of_game_id == source.room_id
                    )
                )
            )
        self.assertEqual(len(rematches), 1)
        self.assertEqual(rematches[0].id, retried.room_id)

    async def test_oni_rematch_preserves_fixed_settings(self) -> None:
        source = await self.service.create(
            self.owner.id,
            bot_count=1,
            bot_difficulty="hard",
            rule_set=RoomRuleSet.ONI,
            lives_per_player=3,
            turn_seconds=30,
            now=NOW,
        )
        finished_outcome = await self.coordinator.surrender(
            source.room_id,
            self.owner.id,
            expected_version=source.state_version,
            operation_id="finish-oni-for-rematch",
            now=NOW,
        )
        self.assertEqual(finished_outcome.snapshot.status, RoomStatus.FINISHED)

        retried = await self.service.rematch(
            self.owner.id,
            source.room_id,
            now=NOW,
        )

        self.assertIs(retried.rule_set, RoomRuleSet.ONI)
        self.assertEqual(retried.bot_difficulty, "hard")
        self.assertEqual(retried.lives_per_player, 3)
        self.assertEqual(retried.remaining_lives, (3, 3))
        self.assertEqual(retried.turn_seconds, 30)

    async def test_paused_listing_rejects_projection_drift(self) -> None:
        snapshot = await self.service.create(self.owner.id, now=NOW)
        await self.service.connect(
            self.owner.id,
            snapshot.room_id,
            "owner-tab",
            now=NOW,
        )
        delayed_task = await self.coordinator.disconnect_client(
            snapshot.room_id,
            "owner-tab",
        )
        self.assertIsNone(delayed_task)

        with self.database.transaction() as session:
            session.execute(
                update(Game)
                .where(Game.id == snapshot.room_id)
                .values(state_version=999)
            )

        with self.assertRaises(RoomSnapshotCorruptError):
            await self.service.list_paused(self.owner.id)


if __name__ == "__main__":
    unittest.main()
