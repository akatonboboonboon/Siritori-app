"""SQLite integration tests for durable waiting-room lobby workflows."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from threading import Barrier
import unittest

from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from sqlalchemy import inspect, select, text

from shiritori.auth import AuthService
from shiritori.database import Database
from shiritori.lobby import (
    LobbyCapacityError,
    LobbyNameConflict,
    LobbyRevisionConflict,
    LobbyRoomNotFound,
    LobbyService,
    LobbyStateError,
    SpectatorsDisabledError,
    room_name_key,
)
from shiritori.lobby_persistence import SQLAlchemyLobbyRepository
from shiritori.models import (
    Game,
    GameMode,
    MatchParticipation,
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
from shiritori.rooms import (
    RoomCoordinator,
    RoomMode,
    SeatController,
    create_room_snapshot,
)


GAME_ID = "00000000-0000-0000-0000-000000000101"
STALE_GAME_ID = "00000000-0000-0000-0000-000000000102"
PRIVATE_GAME_ID = "00000000-0000-0000-0000-000000000104"
BLOCKED_GAME_ID = "00000000-0000-0000-0000-000000000105"
REMATCH_GAME_ID = "00000000-0000-0000-0000-000000000106"


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

    def test_ignored_theme_and_lookup_survive_repository_restart(self) -> None:
        created = self.create_room(max_players=4, turn_seconds=None)

        with self.database.read_session() as session:
            stored = session.get(Room, created.id)
            owner = session.get(
                RoomMembership,
                {"room_id": created.id, "user_id": self.owner.id},
            )
            self.assertEqual(created.theme_key, "all")
            self.assertEqual(stored.theme_key, "all")
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

    def test_finished_round_reopens_and_rematches_without_mutating_history(
        self,
    ) -> None:
        lobby = self.create_room(max_players=3)
        self.service.join_as_player(self.guest.id, lobby.room_code)
        self.service.join_as_spectator(self.watcher.id, lobby.room_code)
        self.service.set_ready(self.owner.id, lobby.room_code, ready=True)
        self.service.set_ready(self.guest.id, lobby.room_code, ready=True)
        first = self.service.start(self.owner.id, lobby.room_code)

        async def finish_and_disconnect_all():
            coordinator = RoomCoordinator(
                SQLAlchemyRoomRepository(self.database)
            )
            await coordinator.connect_client(
                first.game_id,
                self.owner.id,
                "owner-old-tab",
            )
            await coordinator.connect_client(
                first.game_id,
                self.guest.id,
                "guest-old-tab",
            )
            outcome = await coordinator.surrender(
                first.game_id,
                self.owner.id,
                expected_version=0,
                operation_id="finish-before-sql-rematch",
            )
            pending = await coordinator.disconnect_client(
                first.game_id,
                "owner-old-tab",
            )
            await coordinator.disconnect_client(
                first.game_id,
                "guest-old-tab",
            )
            if pending is not None:
                try:
                    await pending
                except asyncio.CancelledError:
                    pass
            return outcome.snapshot

        finished = asyncio.run(finish_and_disconnect_all())
        self.assertIsNotNone(finished)

        # The finish transaction already made the lobby durable and waiting.
        waiting = self.service.get_room(lobby.room_code)
        self.assertEqual(waiting.status, StoredRoomStatus.WAITING)
        self.assertEqual(waiting.room_code, lobby.room_code)
        self.assertTrue(all(not player.ready for player in waiting.players))
        self.assertEqual(
            self.service.return_to_waiting(
                self.watcher.id,
                first.game_id,
            ),
            waiting,
        )
        self.assertEqual(
            self.service.return_to_waiting(
                self.owner.id,
                first.game_id,
            ),
            waiting,
        )

        with self.database.read_session() as session:
            old_game = session.get(Game, first.game_id)
            old_state = dict(old_game.state_json)
            old_finished_at = old_game.finished_at
            rows = tuple(
                session.scalars(
                    select(MatchParticipation).where(
                        MatchParticipation.game_id == first.game_id
                    )
                )
            )
            delete_receipts = tuple(
                session.scalars(
                    select(RoomCommandReceipt).where(
                        RoomCommandReceipt.room_id == first.game_id,
                        RoomCommandReceipt.command_kind == "delete",
                    )
                )
            )
            stored_lobby = session.get(Room, lobby.id)
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(delete_receipts), 1)
            self.assertFalse(delete_receipts[0].deleted)
            self.assertEqual(
                old_game.status,
                StoredGameStatus.FINISHED.value,
            )
            self.assertIsNone(stored_lobby.current_game_id)
            self.assertIsNone(stored_lobby.deleted_at)

        # Exact retries remain read-only after legal waiting membership changes.
        joined = self.service.join_as_player(
            self.third.id,
            lobby.room_code,
        )
        self.assertEqual(
            self.service.return_to_waiting(
                self.third.id,
                first.game_id,
            ),
            joined,
        )
        after_spectator_leave = self.service.leave(
            self.watcher.id,
            lobby.room_code,
        ).room
        self.assertEqual(
            self.service.return_to_waiting(
                self.owner.id,
                first.game_id,
            ),
            after_spectator_leave,
        )

        # Starting again creates a new Game and retains the finished history.
        rematch_service = LobbyService(
            self.repository,
            game_id_factory=lambda: REMATCH_GAME_ID,
            seat_picker=lambda _: 0,
        )
        for user_id in (self.owner.id, self.guest.id, self.third.id):
            rematch_service.set_ready(user_id, lobby.room_code, ready=True)
        second = rematch_service.start(self.owner.id, lobby.room_code)

        self.assertEqual(second.game_id, REMATCH_GAME_ID)
        self.assertEqual(second.active_room.history, ())
        self.assertIsNone(second.active_room.expected_kana)
        self.assertEqual(
            tuple(seat.owner_user_id for seat in second.active_room.players),
            (self.owner.id, self.guest.id, self.third.id),
        )
        self.assertEqual(
            rematch_service.active_game_id(
                self.third.id,
                lobby.room_code,
            ),
            REMATCH_GAME_ID,
        )
        with self.database.read_session() as session:
            old_game = session.get(Game, first.game_id)
            stored_lobby = session.get(Room, lobby.id)
            self.assertEqual(old_game.state_json, old_state)
            self.assertEqual(old_game.finished_at, old_finished_at)
            self.assertEqual(
                stored_lobby.current_game_id,
                REMATCH_GAME_ID,
            )
            self.assertEqual(
                len(tuple(session.scalars(select(Game)))),
                2,
            )
            self.assertEqual(
                len(tuple(session.scalars(select(MatchParticipation)))),
                2,
            )
        self.assertEqual(
            rematch_service.open_room_for_game(
                self.third.id,
                first.game_id,
            ),
            second.lobby,
        )
        with self.assertRaises(LobbyRoomNotFound):
            rematch_service.open_room_for_game(
                "00000000-0000-0000-0000-000000000999",
                first.game_id,
            )
        with self.assertRaises(LobbyStateError):
            rematch_service.return_to_waiting(
                self.owner.id,
                first.game_id,
            )

    def test_active_public_spectator_join_updates_lobby_and_game_atomically(
        self,
    ) -> None:
        lobby = self.create_room(is_public=True)
        self.service.join_as_player(self.guest.id, lobby.room_code)
        self.service.set_ready(self.owner.id, lobby.room_code, ready=True)
        self.service.set_ready(self.guest.id, lobby.room_code, ready=True)
        started = self.service.start(self.owner.id, lobby.room_code)

        joined = self.service.join_as_spectator(
            self.third.id,
            lobby.room_code,
        )

        self.assertEqual(
            tuple(member.user_id for member in joined.spectators),
            (self.third.id,),
        )
        self.assertEqual(joined.revision, started.lobby.revision + 1)
        self.assertEqual(
            self.service.active_game_id(self.third.id, lobby.room_code),
            started.game_id,
        )
        active_repository = SQLAlchemyRoomRepository(self.database)
        active = asyncio.run(active_repository.load(started.game_id))
        self.assertEqual(active.spectators, (self.third.id,))
        self.assertEqual(active.state_version, 1)
        with self.database.read_session() as session:
            game = session.get(Game, started.game_id)
            membership = session.get(
                RoomMembership,
                {"room_id": lobby.id, "user_id": self.third.id},
            )
            self.assertEqual(game.state_version, 1)
            self.assertEqual(
                membership.role,
                RoomRole.SPECTATOR.value,
            )
            self.assertIsNone(membership.left_at)

        same = self.service.join_as_spectator(
            self.third.id,
            lobby.room_code,
        )
        self.assertEqual(same.revision, joined.revision)
        active = asyncio.run(active_repository.load(started.game_id))
        self.assertEqual(active.state_version, 1)
        self.assertEqual(active.spectators, (self.third.id,))

    def test_concurrent_active_spectator_joins_preserve_both_members(
        self,
    ) -> None:
        lobby = self.create_room(is_public=True)
        self.service.join_as_player(self.guest.id, lobby.room_code)
        self.service.set_ready(self.owner.id, lobby.room_code, ready=True)
        self.service.set_ready(self.guest.id, lobby.room_code, ready=True)
        started = self.service.start(self.owner.id, lobby.room_code)
        second_database = Database(self.database_url)
        second_service = LobbyService(
            SQLAlchemyLobbyRepository(second_database)
        )
        services = (self.service, second_service)

        def join(index_and_user: tuple[int, str]) -> str:
            index, user_id = index_and_user
            services[index].join_as_spectator(user_id, lobby.room_code)
            return user_id

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                joined_ids = tuple(
                    executor.map(
                        join,
                        (
                            (0, self.third.id),
                            (1, self.watcher.id),
                        ),
                    )
                )
        finally:
            second_database.dispose()

        self.assertEqual(
            set(joined_ids),
            {self.third.id, self.watcher.id},
        )
        current = self.service.get_room(lobby.room_code)
        self.assertEqual(
            tuple(member.user_id for member in current.spectators),
            tuple(sorted((self.third.id, self.watcher.id))),
        )
        self.assertEqual(current.revision, started.lobby.revision + 2)
        active = asyncio.run(
            SQLAlchemyRoomRepository(self.database).load(started.game_id)
        )
        self.assertEqual(
            active.spectators,
            tuple(sorted((self.third.id, self.watcher.id))),
        )
        self.assertEqual(active.state_version, 2)
        for user_id in (self.third.id, self.watcher.id):
            self.assertEqual(
                self.service.active_game_id(user_id, lobby.room_code),
                started.game_id,
            )

    def test_active_join_rejects_player_and_disabled_rooms(
        self,
    ) -> None:
        public = self.create_room(
            is_public=True,
            fill_empty_seats_with_bots=True,
        )
        self.service.set_ready(self.owner.id, public.room_code, ready=True)
        self.service.start(self.owner.id, public.room_code)
        with self.assertRaises(LobbyStateError):
            self.service.join_as_player(self.third.id, public.room_code)

        private_service = LobbyService(
            self.repository,
            code_factory=lambda: "PRIV22",
            game_id_factory=lambda: PRIVATE_GAME_ID,
            seat_picker=lambda _: 0,
        )
        private = private_service.create_pvp_room(
            self.guest.id,
            name="Private active",
            is_public=False,
            fill_empty_seats_with_bots=True,
        )
        private_service.set_ready(
            self.guest.id,
            private.room_code,
            ready=True,
        )
        private_service.start(self.guest.id, private.room_code)
        private_joined = private_service.join_as_spectator(
            self.watcher.id,
            private.room_code,
        )
        self.assertEqual(
            tuple(
                member.user_id
                for member in private_joined.spectators
            ),
            (self.watcher.id,),
        )
        self.assertNotIn(
            private.id,
            {room.id for room in self.repository.list_public_rooms()},
        )
        self.assertEqual(
            private_service.active_game_id(
                self.watcher.id,
                private.room_code,
            ),
            PRIVATE_GAME_ID,
        )
        private_same = self.repository.join_active_spectator(
            room_id=private.id,
            user_id=self.watcher.id,
        )
        self.assertEqual(private_same.revision, private_joined.revision)

        blocked_service = LobbyService(
            self.repository,
            code_factory=lambda: "BLOCK22",
            game_id_factory=lambda: BLOCKED_GAME_ID,
            seat_picker=lambda _: 0,
        )
        blocked = blocked_service.create_pvp_room(
            self.third.id,
            name="Blocked active",
            is_public=True,
            allow_spectators=False,
            fill_empty_seats_with_bots=True,
        )
        blocked_service.set_ready(
            self.third.id,
            blocked.room_code,
            ready=True,
        )
        blocked_service.start(self.third.id, blocked.room_code)
        with self.assertRaises(SpectatorsDisabledError):
            blocked_service.join_as_spectator(
                self.watcher.id,
                blocked.room_code,
            )
        with self.assertRaises(SpectatorsDisabledError):
            self.repository.join_active_spectator(
                room_id=blocked.id,
                user_id=self.watcher.id,
            )

    def test_finished_game_reopens_and_late_spectator_joins_next_round(
        self,
    ) -> None:
        lobby = self.create_room(is_public=True)
        self.service.join_as_player(self.guest.id, lobby.room_code)
        self.service.set_ready(self.owner.id, lobby.room_code, ready=True)
        self.service.set_ready(self.guest.id, lobby.room_code, ready=True)
        started = self.service.start(self.owner.id, lobby.room_code)
        coordinator = RoomCoordinator(
            SQLAlchemyRoomRepository(self.database)
        )
        asyncio.run(
            coordinator.surrender(
                started.game_id,
                self.owner.id,
                expected_version=0,
                operation_id="owner-finish-before-late-watch",
            )
        )

        joined = self.service.join_as_spectator(
            self.third.id,
            lobby.room_code,
        )
        self.assertEqual(joined.status, StoredRoomStatus.WAITING)
        self.assertEqual(
            tuple(member.user_id for member in joined.spectators),
            (self.third.id,),
        )
        self.assertIn(
            lobby.id,
            {room.id for room in self.repository.list_public_rooms()},
        )
        finished = asyncio.run(
            SQLAlchemyRoomRepository(self.database).load(started.game_id)
        )
        self.assertIsNotNone(finished)
        self.assertNotIn(self.third.id, finished.spectators)
        with self.database.read_session() as session:
            membership = session.get(
                RoomMembership,
                {"room_id": lobby.id, "user_id": self.third.id},
            )
            self.assertIsNotNone(membership)
            self.assertEqual(membership.role, RoomRole.SPECTATOR.value)

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

    def test_active_game_lookup_uses_current_pointer_with_game_history(self) -> None:
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

        self.assertEqual(
            self.service.active_game_id(self.owner.id, lobby.room_code),
            GAME_ID,
        )

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

    def test_duplicate_name_identity_is_normalized_and_case_insensitive(
        self,
    ) -> None:
        first = self.create_room(
            name=" \tＰｅｒｓｉｓｔｅｎｔ　Ｒｏｏｍ\n",
            is_public=True,
        )
        duplicate_service = LobbyService(
            self.repository,
            code_factory=lambda: "SQLA23",
        )

        with self.assertRaises(LobbyNameConflict):
            duplicate_service.create_pvp_room(
                self.guest.id,
                name="persistent room",
            )

        with self.database.read_session() as session:
            stored = session.get(Room, first.id)
            self.assertEqual(stored.name, "Persistent Room")
            self.assertEqual(
                stored.name_key,
                room_name_key("persistent room"),
            )

    def test_concurrent_duplicate_name_creation_has_one_winner(self) -> None:
        second_database = Database(self.database_url)
        second_repository = SQLAlchemyLobbyRepository(second_database)
        services = (
            LobbyService(
                self.repository,
                code_factory=lambda: "NAME22",
            ),
            LobbyService(
                second_repository,
                code_factory=lambda: "NAME23",
            ),
        )
        barrier = Barrier(2)

        def create(index_and_user: tuple[int, str]) -> str:
            index, user_id = index_and_user
            barrier.wait()
            try:
                services[index].create_pvp_room(
                    user_id,
                    name=("Shared Room" if index == 0 else "ＳＨＡＲＥＤ　ＲＯＯＭ"),
                )
            except LobbyNameConflict:
                return "conflict"
            return "created"

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = tuple(
                    executor.map(
                        create,
                        (
                            (0, self.owner.id),
                            (1, self.guest.id),
                        ),
                    )
                )
        finally:
            second_database.dispose()

        self.assertEqual(sorted(outcomes), ["conflict", "created"])
        with self.database.read_session() as session:
            matching = tuple(
                session.scalars(
                    select(Room).where(
                        Room.name_key == room_name_key("shared room"),
                        Room.deleted_at.is_(None),
                    )
                )
            )
            self.assertEqual(len(matching), 1)

    def test_deleted_room_releases_name_for_a_new_room(self) -> None:
        original = self.create_room(name="Reusable Room")
        self.assertTrue(
            self.service.leave(self.owner.id, original.room_code).deleted
        )
        replacement_service = LobbyService(
            self.repository,
            code_factory=lambda: "SQLA23",
        )

        replacement = replacement_service.create_pvp_room(
            self.guest.id,
            name="  ＲＥＵＳＡＢＬＥ　ＲＯＯＭ  ",
        )

        self.assertNotEqual(replacement.id, original.id)
        self.assertEqual(replacement.name, "REUSABLE ROOM")
        with self.database.read_session() as session:
            old = session.get(Room, original.id)
            new = session.get(Room, replacement.id)
            self.assertIsNotNone(old.deleted_at)
            self.assertIsNone(new.deleted_at)
            self.assertEqual(old.name_key, new.name_key)

    def test_public_waiting_listing_filters_and_keeps_creation_order(self) -> None:
        def create(
            code: str,
            owner_id: str,
            name: str,
            *,
            is_public: bool,
            fill: bool = False,
            allow_spectators: bool = True,
            game_id: str = STALE_GAME_ID,
        ):
            service = LobbyService(
                self.repository,
                code_factory=lambda: code,
                game_id_factory=lambda: game_id,
                seat_picker=lambda _: 0,
            )
            room = service.create_pvp_room(
                owner_id,
                name=name,
                is_public=is_public,
                fill_empty_seats_with_bots=fill,
                allow_spectators=allow_spectators,
            )
            return service, room

        _, first = create(
            "LIST21", self.owner.id, "First public", is_public=True
        )
        create("LIST22", self.guest.id, "Private", is_public=False)
        active_service, active = create(
            "LIST23",
            self.third.id,
            "Already active",
            is_public=True,
            fill=True,
        )
        active_service.set_ready(self.third.id, active.room_code, ready=True)
        active_service.start(self.third.id, active.room_code)
        blocked_service, blocked = create(
            "LIST26",
            self.owner.id,
            "Active without spectators",
            is_public=True,
            fill=True,
            allow_spectators=False,
            game_id=BLOCKED_GAME_ID,
        )
        blocked_service.set_ready(
            self.owner.id,
            blocked.room_code,
            ready=True,
        )
        blocked_service.start(self.owner.id, blocked.room_code)
        private_service, private = create(
            "LIST27",
            self.guest.id,
            "Private active",
            is_public=False,
            fill=True,
            game_id=PRIVATE_GAME_ID,
        )
        private_service.set_ready(
            self.guest.id,
            private.room_code,
            ready=True,
        )
        private_service.start(self.guest.id, private.room_code)
        deleted_service, deleted = create(
            "LIST24", self.watcher.id, "Deleted", is_public=True
        )
        deleted_service.leave(self.watcher.id, deleted.room_code)
        _, last = create(
            "LIST25", self.guest.id, "Last public", is_public=True
        )

        self.assertEqual(
            tuple(room.id for room in self.repository.list_public_waiting()),
            (first.id, last.id),
        )
        self.assertEqual(
            self.repository.list_public_waiting(limit=1),
            (first,),
        )
        self.assertEqual(
            tuple(
                room.id
                for room in self.repository.list_public_rooms()
            ),
            (first.id, active.id, last.id),
        )

    def test_bot_fill_projection_is_durable_and_lookup_consistent(self) -> None:
        room = self.create_room(
            max_players=4,
            is_public=True,
            fill_empty_seats_with_bots=True,
        )
        self.service.set_ready(self.owner.id, room.room_code, ready=True)

        started = self.service.start(self.owner.id, room.room_code)

        self.assertEqual(len(started.active_room.players), 4)
        self.assertEqual(
            tuple(seat.owner_user_id for seat in started.active_room.players),
            (self.owner.id, None, None, None),
        )
        self.assertEqual(
            self.service.active_game_id(self.owner.id, room.room_code),
            GAME_ID,
        )
        with self.database.read_session() as session:
            game = session.get(Game, GAME_ID)
            self.assertEqual(game.bot_count, 3)
            self.assertEqual(
                game.settings_json,
                {
                    "room_code": room.room_code,
                    "max_players": 4,
                    "allow_spectators": True,
                    "is_public": True,
                    "fill_empty_seats_with_bots": True,
                },
            )

    def test_active_lookup_accepts_owned_temporary_bot_with_permanent_tail(
        self,
    ) -> None:
        room = self.create_room(
            max_players=3,
            fill_empty_seats_with_bots=True,
        )
        self.service.join_as_player(self.guest.id, room.room_code)
        self.service.set_ready(self.owner.id, room.room_code, ready=True)
        self.service.set_ready(self.guest.id, room.room_code, ready=True)
        started = self.service.start(self.owner.id, room.room_code)
        coordinator = RoomCoordinator(
            SQLAlchemyRoomRepository(self.database)
        )

        async def leave_owner():
            await coordinator.connect_client(
                started.game_id,
                self.owner.id,
                "owner-tab",
            )
            await coordinator.connect_client(
                started.game_id,
                self.guest.id,
                "guest-tab",
            )
            return await coordinator.leave(
                started.game_id,
                self.owner.id,
                expected_version=0,
                operation_id="owner-temp-bot",
            )

        outcome = asyncio.run(leave_owner())

        assert outcome.snapshot is not None
        self.assertEqual(
            tuple(
                (seat.owner_user_id, seat.controller)
                for seat in outcome.snapshot.players
            ),
            (
                (self.owner.id, SeatController.BOT),
                (self.guest.id, SeatController.HUMAN),
                (None, SeatController.BOT),
            ),
        )
        for user_id in (self.owner.id, self.guest.id):
            with self.subTest(user_id=user_id):
                self.assertEqual(
                    self.service.active_game_id(user_id, room.room_code),
                    GAME_ID,
                )
        with self.database.read_session() as session:
            game = session.get(Game, GAME_ID)
            self.assertEqual(game.bot_count, 1)

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

    def test_waiting_setting_update_is_durable_and_resets_readiness(
        self,
    ) -> None:
        room = self.create_room(max_players=4)
        self.service.join_as_player(self.guest.id, room.room_code)
        self.service.set_ready(self.owner.id, room.room_code, ready=True)
        ready = self.service.set_ready(
            self.guest.id,
            room.room_code,
            ready=True,
        )

        updated = self.service.update_settings(
            self.owner.id,
            room.room_code,
            expected_revision=ready.revision,
            max_players=3,
            allow_spectators=False,
            turn_seconds=None,
            is_public=True,
            fill_empty_seats_with_bots=True,
        )

        self.assertEqual(updated.revision, ready.revision + 1)
        self.assertTrue(all(not player.ready for player in updated.players))
        with self.database.read_session() as session:
            stored = session.get(Room, room.id)
            memberships = tuple(
                session.scalars(
                    select(RoomMembership).where(
                        RoomMembership.room_id == room.id,
                        RoomMembership.left_at.is_(None),
                    )
                )
            )
            self.assertEqual(stored.max_players, 3)
            self.assertFalse(stored.allow_spectators)
            self.assertIsNone(stored.turn_seconds)
            self.assertTrue(stored.is_public)
            self.assertTrue(stored.fill_empty_seats_with_bots)
            self.assertEqual(stored.revision, ready.revision + 1)
            self.assertTrue(all(not member.ready for member in memberships))

        restarted_database = Database(self.database_url)
        restarted_service = LobbyService(
            SQLAlchemyLobbyRepository(restarted_database)
        )
        try:
            self.assertEqual(
                restarted_service.get_room(room.room_code),
                updated,
            )
        finally:
            restarted_database.dispose()

    def test_sql_ready_rejects_stale_displayed_gameplay_settings(
        self,
    ) -> None:
        room = self.create_room(max_players=4)
        joined = self.service.join_as_player(self.guest.id, room.room_code)
        displayed_settings = (
            joined.max_players,
            joined.turn_seconds,
            joined.fill_empty_seats_with_bots,
        )
        updated = self.service.update_settings(
            self.owner.id,
            room.room_code,
            expected_revision=joined.revision,
            max_players=3,
            allow_spectators=True,
            turn_seconds=None,
            is_public=False,
            fill_empty_seats_with_bots=True,
        )

        with self.assertRaises(LobbyRevisionConflict) as caught:
            self.repository.set_ready(
                room_id=room.id,
                user_id=self.guest.id,
                ready=True,
                expected_gameplay_settings=displayed_settings,
            )
        self.assertEqual(caught.exception.current_room, updated)
        current = self.service.get_room(room.room_code)
        self.assertTrue(all(not player.ready for player in current.players))

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
        self.assertEqual(lobby.theme_key, "all")
        self.assertEqual(started.active_room.theme_key, "all")
        with self.database.read_session() as session:
            stored_room = session.get(Room, lobby.id)
            stored_game = session.get(Game, GAME_ID)
            receipts = tuple(session.scalars(select(RoomCommandReceipt)))
            self.assertEqual(stored_room.status, StoredRoomStatus.ACTIVE.value)
            self.assertEqual(stored_room.revision, started.lobby.revision)
            self.assertEqual(stored_game.status, StoredGameStatus.ACTIVE.value)
            self.assertEqual(stored_game.theme_key, "all")
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
                    game_columns = {
                        column["name"]
                        for column in schema.get_columns("games")
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
                        {
                            "theme_key",
                            "turn_seconds",
                            "revision",
                            "name_key",
                            "is_public",
                            "fill_empty_seats_with_bots",
                        }.issubset(
                            room_columns
                        )
                    )
                    self.assertIn("current_game_id", room_columns)
                    self.assertIn("rematch_of_game_id", game_columns)
                    self.assertIn(
                        "uq_rooms_current_game_id",
                        {item["name"] for item in schema.get_unique_constraints("rooms")},
                    )
                    room_indexes = {
                        index["name"]: index
                        for index in schema.get_indexes("rooms")
                    }
                    active_name_index = room_indexes[
                        "uq_rooms_active_name_key"
                    ]
                    self.assertTrue(active_name_index["unique"])
                    self.assertEqual(
                        active_name_index["column_names"],
                        ["name_key"],
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

    def test_current_game_migration_backfills_only_active_and_downgrades(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "current-round-migration.sqlite3")
            url = f"sqlite+pysqlite:///{path.as_posix()}"
            import os

            previous = os.environ.get("DIRECT_DATABASE_URL")
            os.environ["DIRECT_DATABASE_URL"] = url
            try:
                config = Config(str(root / "alembic.ini"))
                command.upgrade(config, "0004_score_attack_runs")
                before = Database(url)
                try:
                    with before.engine.begin() as connection:
                        connection.execute(
                            text(
                                "INSERT INTO users "
                                "(id, username, username_key, password_hash) "
                                "VALUES "
                                "('migration-owner', 'migration-owner', "
                                "'migration-owner', 'not-a-real-hash')"
                            )
                        )
                        connection.execute(
                            text(
                                "INSERT INTO rooms "
                                "(id, room_code, owner_user_id, name, "
                                "name_key, status) VALUES "
                                "('active-room', 'ACTIVE1', "
                                "'migration-owner', 'Active room', "
                                "'active-room', 'active'), "
                                "('waiting-room', 'WAIT001', "
                                "'migration-owner', 'Waiting room', "
                                "'waiting-room', 'waiting')"
                            )
                        )
                        connection.execute(
                            text(
                                "INSERT INTO games "
                                "(id, room_id, created_by_user_id, mode, "
                                "status, created_at) VALUES "
                                "('active-old', 'active-room', "
                                "'migration-owner', 'multiplayer', 'finished', "
                                "'2026-07-25 00:00:00'), "
                                "('active-latest', 'active-room', "
                                "'migration-owner', 'multiplayer', 'active', "
                                "'2026-07-26 00:00:00'), "
                                "('waiting-history', 'waiting-room', "
                                "'migration-owner', 'multiplayer', 'finished', "
                                "'2026-07-26 00:00:00')"
                            )
                        )
                finally:
                    before.dispose()

                command.upgrade(config, "0005_room_current_game")
                migrated = Database(url)
                try:
                    schema = inspect(migrated.engine)
                    room_columns = {
                        column["name"]
                        for column in schema.get_columns("rooms")
                    }
                    game_columns = {
                        column["name"]
                        for column in schema.get_columns("games")
                    }
                    self.assertIn("current_game_id", room_columns)
                    self.assertIn("rematch_of_game_id", game_columns)
                    room_uniques = {
                        constraint["name"]
                        for constraint in schema.get_unique_constraints("rooms")
                    }
                    game_uniques = {
                        constraint["name"]
                        for constraint in schema.get_unique_constraints("games")
                    }
                    self.assertIn("uq_rooms_current_game_id", room_uniques)
                    self.assertIn(
                        "uq_games_rematch_of_game_id",
                        game_uniques,
                    )
                    room_foreign_keys = {
                        constraint["name"]: constraint
                        for constraint in schema.get_foreign_keys("rooms")
                    }
                    game_foreign_keys = {
                        constraint["name"]: constraint
                        for constraint in schema.get_foreign_keys("games")
                    }
                    self.assertEqual(
                        room_foreign_keys[
                            "fk_rooms_current_game_id_games"
                        ]["referred_table"],
                        "games",
                    )
                    self.assertEqual(
                        game_foreign_keys[
                            "fk_games_rematch_of_game_id_games"
                        ]["referred_table"],
                        "games",
                    )
                    with migrated.engine.connect() as connection:
                        pointers = {
                            row.id: row.current_game_id
                            for row in connection.execute(
                                text(
                                    "SELECT id, current_game_id FROM rooms"
                                )
                            )
                        }
                    self.assertEqual(
                        pointers["active-room"],
                        "active-latest",
                    )
                    self.assertIsNone(pointers["waiting-room"])
                finally:
                    migrated.dispose()

                command.downgrade(config, "0004_score_attack_runs")
                downgraded = Database(url)
                try:
                    schema = inspect(downgraded.engine)
                    self.assertNotIn(
                        "current_game_id",
                        {
                            column["name"]
                            for column in schema.get_columns("rooms")
                        },
                    )
                    self.assertNotIn(
                        "rematch_of_game_id",
                        {
                            column["name"]
                            for column in schema.get_columns("games")
                        },
                    )
                finally:
                    downgraded.dispose()
            finally:
                if previous is None:
                    os.environ.pop("DIRECT_DATABASE_URL", None)
                else:
                    os.environ["DIRECT_DATABASE_URL"] = previous

    def test_current_game_migration_emits_postgresql_round_trip_ddl(
        self,
    ) -> None:
        """Compile upgrade/downgrade with PostgreSQL's Alembic dialect."""

        from importlib import import_module
        from io import StringIO

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        migration = import_module(
            "migrations.versions.0005_room_current_game"
        )
        output = StringIO()
        context = MigrationContext.configure(
            url="postgresql://",
            opts={"as_sql": True, "output_buffer": output},
        )
        operations = Operations(context)

        class _EmptyResult:
            @staticmethod
            def scalars() -> tuple[()]:
                return ()

        class _OfflineOperations:
            def __init__(self, delegate: Operations) -> None:
                self.delegate = delegate

            def __getattr__(self, name: str):
                return getattr(self.delegate, name)

            def get_bind(self):
                return self

            def execute(self, _statement):
                return _EmptyResult()

        original_op = migration.op
        migration.op = _OfflineOperations(operations)
        try:
            migration.upgrade()
            migration.downgrade()
        finally:
            migration.op = original_op

        ddl = output.getvalue().lower()
        self.assertIn("add column rematch_of_game_id", ddl)
        self.assertIn("fk_games_rematch_of_game_id_games", ddl)
        self.assertIn("uq_games_rematch_of_game_id", ddl)
        self.assertIn("add column current_game_id", ddl)
        self.assertIn("fk_rooms_current_game_id_games", ddl)
        self.assertIn("uq_rooms_current_game_id", ddl)
        self.assertIn(
            "drop constraint uq_games_rematch_of_game_id",
            ddl,
        )
        self.assertIn("drop column rematch_of_game_id", ddl)
        self.assertIn("drop column current_game_id", ddl)

    def test_migration_normalizes_and_deduplicates_active_legacy_names(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "legacy-rooms.sqlite3")
            url = f"sqlite+pysqlite:///{path.as_posix()}"
            import os

            previous = os.environ.get("DIRECT_DATABASE_URL")
            os.environ["DIRECT_DATABASE_URL"] = url
            try:
                config = Config(str(root / "alembic.ini"))
                command.upgrade(config, "0001_initial_schema")
                database = Database(url)
                try:
                    with database.engine.begin() as connection:
                        connection.execute(
                            text(
                                "INSERT INTO users "
                                "(id, username, username_key, password_hash) "
                                "VALUES "
                                "(:id, :username, :username_key, :password_hash)"
                            ),
                            {
                                "id": "00000000-0000-0000-0000-000000000901",
                                "username": "migration-owner",
                                "username_key": "migration-owner",
                                "password_hash": "not-used-in-this-test",
                            },
                        )
                        connection.execute(
                            text(
                                "INSERT INTO rooms "
                                "(id, room_code, owner_user_id, name, status, "
                                "max_players, allow_spectators, theme_key, "
                                "revision, created_at, updated_at, deleted_at) "
                                "VALUES "
                                "(:id, :code, :owner, :name, 'waiting', 2, "
                                "1, 'all', 0, :created, :created, :deleted)"
                            ),
                            (
                                {
                                    "id": "00000000-0000-0000-0000-000000000911",
                                    "code": "MIGR21",
                                    "owner": "00000000-0000-0000-0000-000000000901",
                                    "name": "  Legacy　Room  ",
                                    "created": "2026-07-24 00:00:01",
                                    "deleted": None,
                                },
                                {
                                    "id": "00000000-0000-0000-0000-000000000912",
                                    "code": "MIGR22",
                                    "owner": "00000000-0000-0000-0000-000000000901",
                                    "name": "legacy room",
                                    "created": "2026-07-24 00:00:02",
                                    "deleted": None,
                                },
                                {
                                    "id": "00000000-0000-0000-0000-000000000913",
                                    "code": "MIGR23",
                                    "owner": "00000000-0000-0000-0000-000000000901",
                                    "name": "LEGACY ROOM",
                                    "created": "2026-07-24 00:00:03",
                                    "deleted": "2026-07-24 00:00:04",
                                },
                            ),
                        )
                    database.dispose()
                    command.upgrade(config, "head")
                    command.check(config)
                    migrated = Database(url)
                    try:
                        with migrated.read_session() as session:
                            rooms = tuple(
                                session.scalars(
                                    select(Room).order_by(Room.created_at)
                                )
                            )
                            self.assertEqual(
                                tuple(room.name for room in rooms),
                                ("Legacy Room", "legacy room (2)", "LEGACY ROOM"),
                            )
                            self.assertEqual(
                                tuple(room.name_key for room in rooms),
                                tuple(room_name_key(room.name) for room in rooms),
                            )
                            self.assertTrue(
                                all(len(room.name_key) == 64 for room in rooms)
                            )
                            self.assertTrue(
                                all(not room.is_public for room in rooms)
                            )
                            self.assertTrue(
                                all(
                                    not room.fill_empty_seats_with_bots
                                    for room in rooms
                                )
                            )
                    finally:
                        migrated.dispose()
                finally:
                    database.dispose()
            finally:
                if previous is None:
                    os.environ.pop("DIRECT_DATABASE_URL", None)
                else:
                    os.environ["DIRECT_DATABASE_URL"] = previous


if __name__ == "__main__":
    unittest.main()
