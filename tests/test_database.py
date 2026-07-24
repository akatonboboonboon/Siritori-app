from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest

from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from sqlalchemy import inspect

from shiritori.auth import AuthService
from shiritori.database import (
    AuthoritativeGameStateError,
    Database,
    GameRepository,
    IdempotencyConflictError,
    MoveCommand,
    StaleGameStateError,
    normalize_database_url,
    runtime_database_url,
)
from shiritori.models import ActorKind, GameMode, RoomRole, StoredGameStatus


class GameRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name, "games.sqlite3")
        self.database = Database(
            f"sqlite+pysqlite:///{database_path.as_posix()}"
        )
        self.database.create_schema_for_testing()
        hasher = PasswordHasher(
            time_cost=1,
            memory_cost=8 * 1024,
            parallelism=1,
            hash_len=16,
            salt_len=16,
        )
        auth = AuthService(self.database, password_hasher=hasher)
        self.owner = auth.register("owner", "owner-password-123")
        self.guest = auth.register("guest", "guest-password-123")
        self.games = GameRepository(self.database)

    def tearDown(self) -> None:
        self.database.dispose()
        self.temporary_directory.cleanup()

    def test_schema_covers_required_persistence_entities(self) -> None:
        tables = set(inspect(self.database.engine).get_table_names())
        self.assertTrue(
            {
                "users",
                "sessions",
                "rooms",
                "room_memberships",
                "games",
                "moves",
                "solo_game_saves",
            }.issubset(tables)
        )

    def test_room_tracks_roles_seats_and_presence(self) -> None:
        room = self.games.create_room(
            owner_user_id=self.owner.id,
            room_code="ABCD12",
            name="テスト部屋",
            max_players=4,
        )
        self.assertEqual(room.memberships[0].role, RoomRole.PLAYER.value)
        self.assertEqual(room.memberships[0].seat_index, 0)

        updated = self.games.set_membership(
            room_id=room.id,
            user_id=self.guest.id,
            role=RoomRole.SPECTATOR.value,
            seat_index=None,
            presence="connected",
            connected_count=2,
        )
        guest = next(
            member
            for member in updated.memberships
            if member.user_id == self.guest.id
        )
        self.assertEqual(guest.role, "spectator")
        self.assertEqual(guest.connected_count, 2)
        self.assertIsNone(guest.seat_index)

    def test_move_submission_is_versioned_and_idempotent(self) -> None:
        game = self.games.create_game(
            created_by_user_id=self.owner.id,
            mode=GameMode.SOLO.value,
            bot_count=1,
            bot_difficulty="normal",
            state={"used": []},
            current_turn_index=0,
        )
        command_value = MoveCommand(
            game_id=game.id,
            operation_id="browser-operation-0001",
            expected_version=0,
            actor_user_id=self.owner.id,
            actor_kind=ActorKind.USER.value,
            actor_seat_index=0,
            surface="林檎",
            reading="りんご",
            canonical_key="りんご",
            next_turn_index=1,
            expected_kana="ご",
            next_state={"used": ["りんご"]},
        )

        accepted = self.games.submit_move(command_value)
        self.assertFalse(accepted.replayed)
        self.assertEqual(accepted.snapshot.state_version, 1)
        self.assertEqual(accepted.snapshot.current_word_surface, "林檎")
        self.assertEqual(len(accepted.snapshot.moves), 1)

        replay = self.games.submit_move(command_value)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.move.id, accepted.move.id)
        self.assertEqual(len(replay.snapshot.moves), 1)

        with self.assertRaises(IdempotencyConflictError):
            self.games.submit_move(replace(command_value, surface="リンゴ"))

        with self.assertRaises(StaleGameStateError):
            self.games.submit_move(
                MoveCommand(
                    game_id=game.id,
                    operation_id="browser-operation-0002",
                    expected_version=0,
                    actor_user_id=self.owner.id,
                    actor_kind="user",
                    actor_seat_index=0,
                    surface="ごりら",
                    reading="ごりら",
                    canonical_key="ごりら",
                    next_turn_index=1,
                    expected_kana="ら",
                    next_state={"used": ["りんご", "ごりら"]},
                )
            )

    def test_solo_game_can_be_saved_and_resumed_with_deadline(self) -> None:
        game = self.games.create_game(
            created_by_user_id=self.owner.id,
            mode=GameMode.SOLO.value,
            bot_count=2,
            bot_difficulty="hard",
            turn_time_seconds=30,
            state={"round": 4},
        )
        saved = self.games.save_solo_game(
            user_id=self.owner.id,
            game_id=game.id,
            expected_version=0,
            remaining_seconds=12,
            snapshot={"round": 4, "history": ["しりとり"]},
        )
        self.assertEqual(saved.saved_state_version, 1)
        self.assertEqual(self.games.get_game_snapshot(game.id).status, "paused")
        self.assertEqual(len(self.games.list_solo_saves(self.owner.id)), 1)

        resumed_at = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
        resumed = self.games.resume_solo_game(
            user_id=self.owner.id,
            game_id=game.id,
            expected_version=1,
            now=resumed_at,
        )
        self.assertEqual(resumed.status, StoredGameStatus.ACTIVE.value)
        self.assertEqual(resumed.state_version, 2)
        self.assertEqual(
            resumed.deadline_at,
            resumed_at + timedelta(seconds=12),
        )
        self.assertEqual(self.games.list_solo_saves(self.owner.id), ())


    def test_legacy_mutations_reject_authoritative_room_snapshot(self) -> None:
        game = self.games.create_game(
            created_by_user_id=self.owner.id,
            mode=GameMode.SOLO.value,
            bot_count=1,
            bot_difficulty="normal",
            state={
                "room_repository_schema": 2,
                "deleted": False,
                "snapshot": {},
            },
        )
        command_value = MoveCommand(
            game_id=game.id,
            operation_id="legacy-operation-rejected",
            expected_version=0,
            actor_user_id=self.owner.id,
            actor_kind=ActorKind.USER.value,
            actor_seat_index=0,
            surface="りんご",
            reading="りんご",
            canonical_key="りんご",
            next_turn_index=1,
            expected_kana="ご",
            next_state={"used": ["りんご"]},
        )

        with self.assertRaises(AuthoritativeGameStateError):
            self.games.submit_move(command_value)
        with self.assertRaises(AuthoritativeGameStateError):
            self.games.save_solo_game(
                user_id=self.owner.id,
                game_id=game.id,
                expected_version=0,
                remaining_seconds=None,
            )
        with self.assertRaises(AuthoritativeGameStateError):
            self.games.resume_solo_game(
                user_id=self.owner.id,
                game_id=game.id,
                expected_version=0,
            )

        unchanged = self.games.get_game_snapshot(game.id)
        self.assertEqual(unchanged.state_version, 0)
        self.assertEqual(unchanged.status, StoredGameStatus.ACTIVE.value)
        self.assertEqual(unchanged.moves, ())


class DatabaseConfigurationTests(unittest.TestCase):
    def test_runtime_url_uses_database_url_not_direct_url(self) -> None:
        values = {
            "DATABASE_URL": "postgresql://runtime.example/app",
            "DIRECT_DATABASE_URL": "postgresql://direct.example/app",
        }
        self.assertEqual(
            runtime_database_url(values),
            "postgresql+psycopg://runtime.example/app",
        )
        self.assertEqual(
            normalize_database_url("postgres://host/db"),
            "postgresql+psycopg://host/db",
        )

    def test_initial_migration_upgrades_and_downgrades_sqlite(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory, "migration.sqlite3")
            direct_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
            previous_direct = os.environ.get("DIRECT_DATABASE_URL")
            previous_runtime = os.environ.get("DATABASE_URL")
            os.environ["DIRECT_DATABASE_URL"] = direct_url
            os.environ["DATABASE_URL"] = "not-used-by-migrations"
            try:
                config = Config(str(repository_root / "alembic.ini"))
                command.upgrade(config, "head")
                command.check(config)
                migrated_database = Database(direct_url)
                try:
                    tables = set(
                        inspect(migrated_database.engine).get_table_names()
                    )
                    self.assertIn("users", tables)
                    self.assertIn("solo_game_saves", tables)
                finally:
                    migrated_database.dispose()

                command.downgrade(config, "base")
                downgraded_database = Database(direct_url)
                try:
                    tables = set(
                        inspect(downgraded_database.engine).get_table_names()
                    )
                    self.assertNotIn("users", tables)
                    self.assertNotIn("games", tables)
                finally:
                    downgraded_database.dispose()
            finally:
                if previous_direct is None:
                    os.environ.pop("DIRECT_DATABASE_URL", None)
                else:
                    os.environ["DIRECT_DATABASE_URL"] = previous_direct
                if previous_runtime is None:
                    os.environ.pop("DATABASE_URL", None)
                else:
                    os.environ["DATABASE_URL"] = previous_runtime


if __name__ == "__main__":
    unittest.main()
