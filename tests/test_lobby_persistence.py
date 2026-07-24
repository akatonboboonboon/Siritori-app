"""SQLite integration tests for durable waiting-room lobby workflows."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from sqlalchemy import inspect, select

from shiritori.auth import AuthService
from shiritori.database import Database
from shiritori.lobby import (
    LobbyCapacityError,
    LobbyRevisionConflict,
    LobbyRoomNotFound,
    LobbyService,
    SpectatorsDisabledError,
)
from shiritori.lobby_persistence import SQLAlchemyLobbyRepository
from shiritori.models import (
    Game,
    GameMode,
    Room,
    RoomCommandReceipt,
    RoomMembership,
    RoomRole,
    RoomStatus as StoredRoomStatus,
    StoredGameStatus,
)
from shiritori.room_persistence import (
    RoomSnapshotCorruptError,
    SQLAlchemyRoomRepository,
)
from shiritori.rooms import RoomCoordinator, RoomMode, create_room_snapshot


GAME_ID = "00000000-0000-0000-0000-000000000101"
STALE_GAME_ID = "00000000-0000-0000-0000-000000000102"


class LobbyPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        path = Path(self.temporary_directory.name, "lobby.sqlite3")
        self.database_url = f"sqlite+pysqlite:///{path.as_posix()}"
        self.database = Database(self.database_url)
        self.database.create_schema_for_testing()
        hasher = PasswordHasher(
            time_cost=1,
            memory_cost=8 * 1024,
            parallelism=1,
            hash_len=16,
            salt_len=16,
        )
        auth = AuthService(self.database, password_hasher=hasher)
        self.owner = auth.register("lobby-owner", "owner-password-123")
        self.guest = auth.register("lobby-guest", "guest-password-123")
        self.third = auth.register("lobby-third", "third-password-123")
        self.watcher = auth.register("lobby-watcher", "watcher-password-123")
        self.repository = SQLAlchemyLobbyRepository(self.database)
        self.service = LobbyService(
            self.repository,
            code_factory=lambda: "SQLA22",
            game_id_factory=lambda: GAME_ID,
            seat_picker=lambda count: count - 1,
        )

    def tearDown(self) -> None:
        self.database.dispose()
        self.temporary_directory.cleanup()

    def create_room(self, **overrides: object):
        settings: dict[str, object] = {
            "name": "Persistent room",
            "max_players": 2,
            "allow_spectators": True,
            "theme_key": "food",
            "turn_seconds": 30,
        }
        settings.update(overrides)
        return self.service.create_pvp_room(
            self.owner.id,
            **settings,  # type: ignore[arg-type]
        )

    def _start_lookup_room(self):
        lobby = self.create_room()
        self.service.join_as_player(self.guest.id, lobby.room_code)
        self.service.join_as_spectator(self.watcher.id, lobby.room_code)
        self.service.set_ready(self.owner.id, lobby.room_code, ready=True)
        self.service.set_ready(self.guest.id, lobby.room_code, ready=True)
        return lobby, self.service.start(self.owner.id, lobby.room_code)

    def test_create_and_exact_lookup_survive_repository_restart(self) -> None:
        created = self.create_room(max_players=4, turn_seconds=None)

        with self.database.read_session() as session:
            stored = session.get(Room, created.id)
            owner = session.get(
                RoomMembership,
                {"room_id": created.id, "user_id": self.owner.id},
            )
            self.assertEqual(stored.theme_key, "food")
            self.assertIsNone(stored.turn_seconds)
            self.assertEqual(stored.revision, 0)
            self.assertFalse(owner.ready)

        restarted_database = Database(self.database_url)
        restarted = SQLAlchemyLobbyRepository(restarted_database)
        try:
            loaded = restarted.get_by_code("SQLA22")
            self.assertEqual(loaded, created)
            self.assertEqual(restarted.get_by_code("sqla22"), created)
            self.assertIsNone(restarted.get_by_code("SQLA23"))
        finally:
            restarted_database.dispose()

    def test_active_game_lookup_is_member_only_and_survives_restart(self) -> None:
        lobby = self.create_room()
        with self.assertRaises(LobbyRoomNotFound):
            self.service.active_game_id(self.owner.id, lobby.room_code)

        self.service.join_as_player(self.guest.id, lobby.room_code)
        self.service.join_as_spectator(self.watcher.id, lobby.room_code)
        self.service.set_ready(self.owner.id, lobby.room_code, ready=True)
        self.service.set_ready(self.guest.id, lobby.room_code, ready=True)
        started = self.service.start(self.owner.id, lobby.room_code)

        for user_id in (self.owner.id, self.guest.id, self.watcher.id):
            with self.subTest(user_id=user_id):
                self.assertEqual(
                    self.service.active_game_id(
                        user_id,
                        lobby.room_code.lower(),
                    ),
                    started.game_id,
                )
        with self.assertRaises(LobbyRoomNotFound):
            self.service.active_game_id(self.third.id, lobby.room_code)

        restarted_database = Database(self.database_url)
        restarted_service = LobbyService(
            SQLAlchemyLobbyRepository(restarted_database)
        )
        try:
            for user_id in (self.owner.id, self.guest.id, self.watcher.id):
                self.assertEqual(
                    restarted_service.active_game_id(user_id, lobby.room_code),
                    started.game_id,
                )
            with self.assertRaises(LobbyRoomNotFound):
                restarted_service.active_game_id(self.third.id, lobby.room_code)
        finally:
            restarted_database.dispose()

    def test_active_spectator_leave_closes_membership_atomically(self) -> None:
        lobby, started = self._start_lookup_room()
        coordinator = RoomCoordinator(
            SQLAlchemyRoomRepository(self.database)
        )

        async def leave_spectator():
            await coordinator.connect_client(
                started.game_id,
                self.owner.id,
                "owner-tab",
            )
            return await coordinator.leave(
                started.game_id,
                self.watcher.id,
                expected_version=0,
                operation_id="watcher-leave",
            )

        outcome = asyncio.run(leave_spectator())

        assert outcome.snapshot is not None
        self.assertEqual(outcome.snapshot.spectators, ())
        for user_id in (self.owner.id, self.guest.id):
            self.assertEqual(
                self.service.active_game_id(user_id, lobby.room_code),
                started.game_id,
            )
        with self.assertRaises(LobbyRoomNotFound):
            self.service.active_game_id(self.watcher.id, lobby.room_code)

        with self.database.read_session() as session:
            membership = session.get(
                RoomMembership,
                {"room_id": lobby.id, "user_id": self.watcher.id},
            )
            self.assertIsNotNone(membership.left_at)
            self.assertEqual(membership.presence, "offline")
            self.assertEqual(membership.connected_count, 0)
            self.assertIsNone(membership.presence_expires_at)
            self.assertFalse(membership.is_bot_substituting)
            self.assertFalse(membership.ready)

    def test_spectator_leave_mismatch_rolls_back_game_update(self) -> None:
        lobby, started = self._start_lookup_room()
        repository = SQLAlchemyRoomRepository(self.database)
        coordinator = RoomCoordinator(repository)
        with self.database.transaction() as session:
            membership = session.get(
                RoomMembership,
                {"room_id": lobby.id, "user_id": self.watcher.id},
            )
            membership.role = RoomRole.PLAYER.value

        async def leave_spectator():
            await coordinator.connect_client(
                started.game_id,
                self.owner.id,
                "owner-tab",
            )
            await coordinator.leave(
                started.game_id,
                self.watcher.id,
                expected_version=0,
                operation_id="mismatched-watcher-leave",
            )

        with self.assertRaises(RoomSnapshotCorruptError):
            asyncio.run(leave_spectator())

        persisted = asyncio.run(repository.load(started.game_id))
        self.assertEqual(persisted.state_version, 0)
        self.assertEqual(persisted.spectators, (self.watcher.id,))
        with self.database.read_session() as session:
            membership = session.get(
                RoomMembership,
                {"room_id": lobby.id, "user_id": self.watcher.id},
            )
            receipt = session.get(
                RoomCommandReceipt,
                {
                    "room_id": started.game_id,
                    "operation_id": "mismatched-watcher-leave",
                },
            )
            self.assertEqual(membership.role, RoomRole.PLAYER.value)
            self.assertIsNone(membership.left_at)
            self.assertIsNone(receipt)

    def test_active_game_lookup_fails_closed_for_missing_game(self) -> None:
        lobby, _ = self._start_lookup_room()
        with self.database.transaction() as session:
            session.delete(session.get(Game, GAME_ID))

        with self.assertRaises(LobbyRoomNotFound):
            self.service.active_game_id(self.owner.id, lobby.room_code)

    def test_active_game_lookup_fails_closed_for_multiple_games(self) -> None:
        lobby, _ = self._start_lookup_room()
        with self.database.transaction() as session:
            session.add(
                Game(
                    id=STALE_GAME_ID,
                    room_id=lobby.id,
                    created_by_user_id=self.owner.id,
                    mode=GameMode.MULTIPLAYER.value,
                    status=StoredGameStatus.ACTIVE.value,
                    theme_key=lobby.theme_key,
                    turn_time_seconds=lobby.turn_seconds,
                    bot_count=0,
                    bot_difficulty="normal",
                    settings_json={},
                    state_json={},
                    starting_seat_index=0,
                    current_turn_index=0,
                    state_version=0,
                )
            )

        with self.assertRaises(LobbyRoomNotFound):
            self.service.active_game_id(self.owner.id, lobby.room_code)

    def test_active_game_lookup_fails_closed_for_corrupt_snapshot(self) -> None:
        lobby, _ = self._start_lookup_room()
        with self.database.transaction() as session:
            game = session.get(Game, GAME_ID)
            game.state_json = {"corrupt": True}

        with self.assertRaises(LobbyRoomNotFound):
            self.service.active_game_id(self.owner.id, lobby.room_code)

    def test_database_unique_code_drives_service_collision_retry(self) -> None:
        first = self.create_room()
        codes = iter((first.room_code, "SQLA23"))
        second_service = LobbyService(
            self.repository,
            code_factory=lambda: next(codes),
        )

        second = second_service.create_pvp_room(
            self.guest.id,
            name="Another room",
        )

        self.assertEqual(second.room_code, "SQLA23")

    def test_concurrent_last_seat_join_has_exactly_one_winner(self) -> None:
        room = self.create_room(max_players=2)
        second_database = Database(self.database_url)
        second_repository = SQLAlchemyLobbyRepository(second_database)
        services = (
            self.service,
            LobbyService(second_repository),
        )

        def join(index_and_user: tuple[int, str]) -> str:
            index, user_id = index_and_user
            try:
                services[index].join_as_player(user_id, room.room_code)
            except LobbyCapacityError:
                return "full"
            return "joined"

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = tuple(
                    executor.map(
                        join,
                        (
                            (0, self.guest.id),
                            (1, self.third.id),
                        ),
                    )
                )
        finally:
            second_database.dispose()

        self.assertEqual(sorted(outcomes), ["full", "joined"])
        persisted = self.service.get_room(room.room_code)
        self.assertEqual(len(persisted.players), 2)
        self.assertEqual(
            tuple(player.seat_index for player in persisted.players),
            (0, 1),
        )

    def test_spectator_flag_and_ready_state_are_persisted(self) -> None:
        blocked = self.create_room(allow_spectators=False)
        with self.assertRaises(SpectatorsDisabledError):
            self.service.join_as_spectator(
                self.watcher.id,
                blocked.room_code,
            )

        joined = self.service.join_as_player(self.guest.id, blocked.room_code)
        ready = self.service.set_ready(
            self.guest.id,
            blocked.room_code,
            ready=True,
        )
        self.assertEqual(ready.revision, joined.revision + 1)
        with self.database.read_session() as session:
            membership = session.get(
                RoomMembership,
                {"room_id": blocked.id, "user_id": self.guest.id},
            )
            self.assertTrue(membership.ready)

    def test_start_atomically_creates_room_repository_compatible_game(self) -> None:
        lobby = self.create_room(theme_key="country", turn_seconds=3)
        self.service.join_as_player(self.guest.id, lobby.room_code)
        self.service.join_as_spectator(self.watcher.id, lobby.room_code)
        self.service.set_ready(self.owner.id, lobby.room_code, ready=True)
        self.service.set_ready(self.guest.id, lobby.room_code, ready=True)

        started = self.service.start(self.owner.id, lobby.room_code)

        self.assertEqual(started.game_id, GAME_ID)
        self.assertEqual(started.active_room.current_turn, 1)
        self.assertIsNone(started.active_room.expected_kana)
        self.assertEqual(started.active_room.history, ())
        with self.database.read_session() as session:
            stored_room = session.get(Room, lobby.id)
            stored_game = session.get(Game, GAME_ID)
            receipts = tuple(session.scalars(select(RoomCommandReceipt)))
            self.assertEqual(stored_room.status, StoredRoomStatus.ACTIVE.value)
            self.assertEqual(stored_room.revision, started.lobby.revision)
            self.assertEqual(stored_game.status, StoredGameStatus.ACTIVE.value)
            self.assertEqual(stored_game.theme_key, "country")
            self.assertEqual(stored_game.bot_difficulty, "normal")
            self.assertEqual(stored_game.turn_time_seconds, 3)
            self.assertEqual(stored_game.starting_seat_index, 1)
            self.assertEqual(stored_game.current_turn_index, 1)
            self.assertEqual(stored_game.state_version, 0)
            self.assertEqual(receipts, ())

        active_repository = SQLAlchemyRoomRepository(self.database)
        loaded = asyncio.run(active_repository.load(GAME_ID))
        self.assertEqual(loaded, started.active_room)
        initialized = asyncio.run(
            active_repository.initialize(started.active_room)
        )
        self.assertEqual(initialized, started.active_room)

    def test_stale_start_rolls_back_without_creating_game(self) -> None:
        lobby = self.create_room(max_players=3)
        self.service.join_as_player(self.guest.id, lobby.room_code)
        self.service.set_ready(self.owner.id, lobby.room_code, ready=True)
        ready = self.service.set_ready(
            self.guest.id,
            lobby.room_code,
            ready=True,
        )
        active = create_room_snapshot(
            STALE_GAME_ID,
            (player.user_id for player in ready.players),
            mode=RoomMode.PVP,
            spectators=(member.user_id for member in ready.spectators),
            turn_seconds=ready.turn_seconds,
            theme_key=ready.theme_key,
            bot_difficulty="normal",
            seat_picker=lambda _: 0,
        )
        self.repository.join_waiting(
            room_id=ready.id,
            user_id=self.watcher.id,
            role=RoomRole.SPECTATOR,
        )

        with self.assertRaises(LobbyRevisionConflict):
            self.repository.activate_waiting(
                room_id=ready.id,
                requesting_owner_user_id=self.owner.id,
                expected_revision=ready.revision,
                game_id=STALE_GAME_ID,
                active_room=active,
                theme_key=ready.theme_key,
                turn_seconds=ready.turn_seconds,
            )

        with self.database.read_session() as session:
            self.assertIsNone(session.get(Game, STALE_GAME_ID))
            room = session.get(Room, ready.id)
            self.assertEqual(room.status, StoredRoomStatus.WAITING.value)

    def test_leave_is_durable_transfers_owner_compacts_and_deletes(self) -> None:
        lobby = self.create_room(max_players=3)
        self.service.join_as_player(self.guest.id, lobby.room_code)
        self.service.join_as_player(self.third.id, lobby.room_code)
        self.service.join_as_spectator(self.watcher.id, lobby.room_code)

        self.service.leave(self.watcher.id, lobby.room_code)
        transferred = self.service.leave(self.owner.id, lobby.room_code)
        self.assertEqual(transferred.room.owner_user_id, self.guest.id)
        self.assertEqual(
            tuple(
                (player.user_id, player.seat_index)
                for player in transferred.room.players
            ),
            ((self.guest.id, 0), (self.third.id, 1)),
        )

        with self.database.read_session() as session:
            owner_membership = session.get(
                RoomMembership,
                {"room_id": lobby.id, "user_id": self.owner.id},
            )
            watcher_membership = session.get(
                RoomMembership,
                {"room_id": lobby.id, "user_id": self.watcher.id},
            )
            self.assertIsNotNone(owner_membership.left_at)
            self.assertIsNotNone(watcher_membership.left_at)
            self.assertFalse(owner_membership.ready)

        self.service.leave(self.guest.id, lobby.room_code)
        deleted = self.service.leave(self.third.id, lobby.room_code)
        self.assertTrue(deleted.deleted)
        with self.assertRaises(LobbyRoomNotFound):
            self.service.get_room(lobby.room_code)
        with self.database.read_session() as session:
            stored = session.get(Room, lobby.id)
            self.assertEqual(stored.status, StoredRoomStatus.CLOSED.value)
            self.assertIsNotNone(stored.deleted_at)

    def test_departed_member_can_rejoin_same_membership_row(self) -> None:
        lobby = self.create_room()
        self.service.join_as_spectator(self.watcher.id, lobby.room_code)
        self.service.leave(self.watcher.id, lobby.room_code)

        rejoined = self.service.join_as_spectator(
            self.watcher.id,
            lobby.room_code,
        )

        self.assertEqual(
            tuple(member.user_id for member in rejoined.spectators),
            (self.watcher.id,),
        )
        with self.database.read_session() as session:
            rows = tuple(
                session.scalars(
                    select(RoomMembership).where(
                        RoomMembership.room_id == lobby.id,
                        RoomMembership.user_id == self.watcher.id,
                    )
                )
            )
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0].left_at)

    def test_spectator_order_is_stable_after_database_restart(self) -> None:
        lobby = self.create_room()
        self.service.join_as_spectator(self.watcher.id, lobby.room_code)
        restarted_database = Database(self.database_url)
        restarted_service = LobbyService(
            SQLAlchemyLobbyRepository(restarted_database)
        )
        try:
            updated = restarted_service.join_as_spectator(
                self.third.id,
                lobby.room_code,
            )
        finally:
            restarted_database.dispose()

        self.assertEqual(
            tuple(member.user_id for member in updated.spectators),
            tuple(sorted((self.watcher.id, self.third.id))),
        )


class LobbyPersistenceMigrationTests(unittest.TestCase):
    def test_migration_matches_models_and_preserves_receipt_schema(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "migration.sqlite3")
            url = f"sqlite+pysqlite:///{path.as_posix()}"
            import os

            previous = os.environ.get("DIRECT_DATABASE_URL")
            os.environ["DIRECT_DATABASE_URL"] = url
            try:
                config = Config(str(root / "alembic.ini"))
                command.upgrade(config, "head")
                command.check(config)
                database = Database(url)
                try:
                    schema = inspect(database.engine)
                    room_columns = {
                        column["name"]
                        for column in schema.get_columns("rooms")
                    }
                    membership_columns = {
                        column["name"]
                        for column in schema.get_columns("room_memberships")
                    }
                    receipt_columns = {
                        column["name"]
                        for column in schema.get_columns(
                            "room_command_receipts"
                        )
                    }
                    self.assertTrue(
                        {"theme_key", "turn_seconds", "revision"}.issubset(
                            room_columns
                        )
                    )
                    self.assertIn("ready", membership_columns)
                    self.assertEqual(
                        receipt_columns,
                        {
                            "room_id",
                            "operation_id",
                            "command_kind",
                            "command_fingerprint",
                            "expected_version",
                            "result_snapshot",
                            "deleted",
                            "created_at",
                        },
                    )
                finally:
                    database.dispose()
            finally:
                if previous is None:
                    os.environ.pop("DIRECT_DATABASE_URL", None)
                else:
                    os.environ["DIRECT_DATABASE_URL"] = previous


if __name__ == "__main__":
    unittest.main()
