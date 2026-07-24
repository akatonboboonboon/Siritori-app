from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from argon2 import PasswordHasher
from sqlalchemy import update

from shiritori.auth import AuthService
from shiritori.bots import HardBot, NormalBot
from shiritori.database import Database
from shiritori.models import Game
from shiritori.room_persistence import (
    RoomSnapshotCorruptError,
    SQLAlchemyRoomRepository,
)
from shiritori.room_runtime import RoomRuntime
from shiritori.rooms import RoomCoordinator, RoomStatus
from shiritori.solo import SoloGameService
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
            bot_count=2,
            bot_difficulty="hard",
            turn_seconds=30,
            now=NOW,
        )
        self.assertEqual(len(snapshot.players), 3)

        connected = await self.service.connect(
            self.owner.id,
            snapshot.room_id,
            "owner-tab",
            now=NOW,
        )
        task = await self.coordinator.disconnect_client(
            snapshot.room_id,
            "owner-tab",
        )
        assert task is not None
        await task

        paused = await self.service.list_paused(self.owner.id)
        self.assertEqual(len(paused), 1)
        self.assertEqual(paused[0].game_id, snapshot.room_id)
        self.assertEqual(paused[0].bot_count, 2)
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
        self.assertIsNotNone(resumed.deadline_at)
        self.assertEqual(await self.service.list_paused(self.owner.id), ())

    async def test_unregistered_easy_fails_before_creating_game(self) -> None:
        with self.assertRaises(KeyError):
            await self.service.create(
                self.owner.id,
                bot_difficulty="easy",
                now=NOW,
            )

        self.assertEqual(await self.service.list_paused(self.owner.id), ())


    async def test_paused_listing_rejects_projection_drift(self) -> None:
        snapshot = await self.service.create(self.owner.id, now=NOW)
        await self.service.connect(
            self.owner.id,
            snapshot.room_id,
            "owner-tab",
            now=NOW,
        )
        task = await self.coordinator.disconnect_client(
            snapshot.room_id,
            "owner-tab",
        )
        assert task is not None
        await task

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
