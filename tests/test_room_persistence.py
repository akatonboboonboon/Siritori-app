"""Tests for durable SQLAlchemy RoomRepository persistence."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest

from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from sqlalchemy import inspect, select

from shiritori.auth import AuthService
from shiritori.database import Database, GameRepository
from shiritori.models import (
    Game,
    GameMode,
    MatchParticipation,
    MatchResult,
    Room,
    RoomMembership,
    StoredGameStatus,
)
from shiritori.room_persistence import (
    RoomAlreadyInitialized,
    RoomOperationConflictError,
    RoomSnapshotCorruptError,
    SNAPSHOT_SCHEMA_VERSION,
    SQLAlchemyRoomRepository,
    deserialize_room_snapshot,
    serialize_room_snapshot,
)
from shiritori.rooms import (
    PlayerSeat,
    RepositoryStatus,
    RoomMode,
    RoomSnapshot,
    RoomStatus,
    SeatController,
    TurnRecord,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 24, 9, 30, tzinfo=UTC)


def command_fingerprint(*semantic_parts: object) -> str:
    return sha256(repr(semantic_parts).encode("utf-8")).hexdigest()


async def cas(
    repository: SQLAlchemyRoomRepository,
    room_id: str,
    expected_version: int,
    operation_id: str,
    next_snapshot: RoomSnapshot,
):
    return await repository.compare_and_swap(
        room_id,
        expected_version,
        operation_id,
        next_snapshot,
        command_fingerprint=command_fingerprint(
            "compare_and_swap",
            room_id,
            expected_version,
            next_snapshot,
        ),
    )


async def delete(
    repository: SQLAlchemyRoomRepository,
    room_id: str,
    expected_version: int,
    operation_id: str,
):
    return await repository.delete_if_version(
        room_id,
        expected_version,
        operation_id,
        command_fingerprint=command_fingerprint(
            "delete",
            room_id,
            expected_version,
        ),
    )


class RoomPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        path = Path(self.temporary_directory.name, "rooms.sqlite3")
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
        self.owner = auth.register("room-owner", "owner-password-123")
        self.guest = auth.register("room-guest", "guest-password-123")
        games = GameRepository(self.database)
        lobby = games.create_room(
            owner_user_id=self.owner.id,
            room_code="ROOM42",
            name="Persistence room",
        )
        games.set_membership(
            room_id=lobby.id,
            user_id=self.guest.id,
            role="player",
            seat_index=1,
        )
        game = games.create_game(
            created_by_user_id=self.owner.id,
            mode=GameMode.MULTIPLAYER.value,
            room_id=lobby.id,
            current_turn_index=1,
        )
        with self.database.transaction() as session:
            stored_lobby = session.get(Room, lobby.id)
            stored_lobby.status = "active"
            stored_lobby.current_game_id = game.id
        self.lobby_id = lobby.id
        self.game_id = game.id
        self.initial = RoomSnapshot(
            room_id=game.id,
            mode=RoomMode.PVP,
            status=RoomStatus.ACTIVE,
            players=(
                PlayerSeat(0, self.owner.id, SeatController.HUMAN),
                PlayerSeat(1, self.guest.id, SeatController.HUMAN),
            ),
            current_turn=1,
            state_version=0,
            theme_key="food",
            bot_difficulty="hard",
            spectators=("spectator-id",),
            history=(
                TurnRecord(
                    surface="林檎",
                    reading="りんご",
                    canonical_key="りんご",
                    seat_index=0,
                    actor_user_id=self.owner.id,
                    by_bot=False,
                    submitted_at=NOW,
                ),
            ),
            expected_kana="ご",
            turn_seconds=30,
            deadline_at=NOW + timedelta(seconds=30),
        )
        self.repository = SQLAlchemyRoomRepository(self.database)

    def tearDown(self) -> None:
        self.database.dispose()
        self.temporary_directory.cleanup()

    async def test_strict_serializer_round_trips_complete_snapshot(self) -> None:
        paused = replace(
            self.initial,
            status=RoomStatus.PAUSED,
            players=(
                replace(
                    self.initial.players[0],
                    controller=SeatController.BOT,
                    handback_pending=True,
                ),
                self.initial.players[1],
            ),
            deadline_at=None,
            paused_remaining_seconds=12.5,
            timed_out_seat=1,
            losing_seat=1,
            end_reason="disconnect",
        )
        document = serialize_room_snapshot(paused)
        self.assertEqual(deserialize_room_snapshot(document), paused)

        self.assertEqual(
            document["room_repository_schema"], SNAPSHOT_SCHEMA_VERSION
        )
        self.assertEqual(document["snapshot"]["theme_key"], "food")
        self.assertEqual(document["snapshot"]["bot_difficulty"], "hard")

        corrupted = dict(document)
        corrupted["room_repository_schema"] = SNAPSHOT_SCHEMA_VERSION - 1
        with self.assertRaises(RoomSnapshotCorruptError):
            deserialize_room_snapshot(corrupted)

        for field, value in (
            ("theme_key", "../food"),
            ("bot_difficulty", "expert"),
        ):
            with self.subTest(field=field):
                bad_payload = dict(document["snapshot"])
                bad_payload[field] = value
                bad_document = dict(document)
                bad_document["snapshot"] = bad_payload
                with self.assertRaises(RoomSnapshotCorruptError):
                    deserialize_room_snapshot(bad_document)

        missing_payload = dict(document["snapshot"])
        missing_payload.pop("theme_key")
        missing_document = dict(document)
        missing_document["snapshot"] = missing_payload
        with self.assertRaises(RoomSnapshotCorruptError):
            deserialize_room_snapshot(missing_document)

    async def test_initialize_is_atomic_idempotent_and_survives_restart(self) -> None:
        initialized = await self.repository.initialize(self.initial)
        self.assertEqual(initialized, self.initial)
        self.assertEqual(await self.repository.initialize(self.initial), self.initial)

        restarted_database = Database(self.database_url)
        restarted = SQLAlchemyRoomRepository(restarted_database)
        try:
            self.assertEqual(await restarted.load(self.game_id), self.initial)
            with self.assertRaises(RoomAlreadyInitialized):
                await restarted.initialize(replace(self.initial, expected_kana="か"))
        finally:
            restarted_database.dispose()

    async def test_cas_preserves_original_receipt_after_later_changes(self) -> None:
        await self.repository.initialize(self.initial)
        paused = replace(
            self.initial,
            status=RoomStatus.PAUSED,
            state_version=1,
            deadline_at=None,
            paused_remaining_seconds=12.5,
        )
        first = await cas(self.repository,
            self.game_id, 0, "pause-command", paused
        )
        self.assertEqual(first.status, RepositoryStatus.APPLIED)
        assert first.receipt is not None
        self.assertEqual(first.receipt.command_kind, "compare_and_swap")
        self.assertEqual(first.receipt.expected_version, 0)
        self.assertEqual(
            first.receipt.fingerprint,
            command_fingerprint(
                "compare_and_swap", self.game_id, 0, paused
            ),
        )

        resumed = replace(
            paused,
            status=RoomStatus.ACTIVE,
            state_version=2,
            deadline_at=NOW + timedelta(seconds=13),
            paused_remaining_seconds=None,
        )
        second = await cas(self.repository,
            self.game_id, 1, "resume-command", resumed
        )
        self.assertEqual(second.status, RepositoryStatus.APPLIED)

        retry = await cas(self.repository,
            self.game_id, 0, "pause-command", paused
        )
        self.assertEqual(retry.status, RepositoryStatus.DUPLICATE)
        self.assertEqual(retry.receipt.snapshot, paused)
        self.assertEqual(retry.current_snapshot, resumed)
        self.assertEqual(
            (await self.repository.find_operation(self.game_id, "pause-command")).snapshot,
            paused,
        )

        with self.database.read_session() as session:
            game = session.get(Game, self.game_id)
            self.assertEqual(game.state_version, 2)
            self.assertEqual(game.status, StoredGameStatus.ACTIVE.value)
            self.assertEqual(game.current_turn_index, resumed.current_turn)
            self.assertEqual(game.theme_key, "food")
            self.assertEqual(game.bot_difficulty, "hard")
            self.assertEqual(game.paused_remaining_seconds, None)

    async def test_operation_id_reuse_with_different_content_is_rejected(self) -> None:
        await self.repository.initialize(self.initial)
        next_snapshot = replace(self.initial, state_version=1)
        await cas(self.repository,
            self.game_id, 0, "same-operation", next_snapshot
        )
        with self.assertRaises(RoomOperationConflictError):
            await cas(self.repository,
                self.game_id,
                0,
                "same-operation",
                replace(next_snapshot, expected_kana="か"),
            )

    async def test_operation_id_reuse_rejects_kind_and_expected_version(self) -> None:
        await self.repository.initialize(self.initial)
        fingerprint = command_fingerprint("one-semantic-command")
        next_snapshot = replace(self.initial, state_version=1)
        await self.repository.compare_and_swap(
            self.game_id,
            0,
            "metadata-bound",
            next_snapshot,
            command_fingerprint=fingerprint,
        )

        with self.assertRaises(RoomOperationConflictError):
            await self.repository.compare_and_swap(
                self.game_id,
                1,
                "metadata-bound",
                replace(next_snapshot, state_version=2),
                command_fingerprint=fingerprint,
            )
        with self.assertRaises(RoomOperationConflictError):
            await self.repository.delete_if_version(
                self.game_id,
                0,
                "metadata-bound",
                command_fingerprint=fingerprint,
            )

    async def test_version_conflict_returns_authoritative_snapshot(self) -> None:
        await self.repository.initialize(self.initial)
        result = await cas(self.repository,
            self.game_id,
            9,
            "stale-command",
            replace(self.initial, state_version=10),
        )
        self.assertEqual(result.status, RepositoryStatus.VERSION_CONFLICT)
        self.assertEqual(result.current_snapshot, self.initial)
        missing = await cas(self.repository,
            "00000000-0000-0000-0000-000000000000",
            0,
            "missing-command",
            replace(
                self.initial,
                room_id="00000000-0000-0000-0000-000000000000",
                state_version=1,
            ),
        )
        self.assertEqual(missing.status, RepositoryStatus.NOT_FOUND)

    async def test_concurrent_cas_has_one_winner(self) -> None:
        await self.repository.initialize(self.initial)
        left = replace(self.initial, state_version=1, expected_kana="か")
        right = replace(self.initial, state_version=1, expected_kana="き")
        results = await asyncio.gather(
            cas(self.repository,
                self.game_id, 0, "concurrent-left", left
            ),
            cas(self.repository,
                self.game_id, 0, "concurrent-right", right
            ),
        )
        self.assertEqual(
            sorted(result.status.value for result in results),
            sorted([
                RepositoryStatus.APPLIED.value,
                RepositoryStatus.VERSION_CONFLICT.value,
            ]),
        )
        winner = next(
            result.receipt.snapshot
            for result in results
            if result.status is RepositoryStatus.APPLIED
        )
        self.assertEqual(await self.repository.load(self.game_id), winner)

    async def test_concurrent_retry_returns_same_original_result(self) -> None:
        await self.repository.initialize(self.initial)
        next_snapshot = replace(self.initial, state_version=1)
        second_database = Database(self.database_url)
        second_repository = SQLAlchemyRoomRepository(second_database)
        repositories = (self.repository, second_repository) * 2
        try:
            results = await asyncio.gather(
                *(
                    cas(repository,
                        self.game_id, 0, "retry-command", next_snapshot
                    )
                    for repository in repositories
                )
            )
        finally:
            second_database.dispose()
        self.assertEqual(
            sum(result.status is RepositoryStatus.APPLIED for result in results),
            1,
        )
        self.assertEqual(
            sum(result.status is RepositoryStatus.DUPLICATE for result in results),
            3,
        )
        self.assertTrue(
            all(result.receipt.snapshot == next_snapshot for result in results)
        )

    async def test_list_active_room_ids_is_sorted_and_validates_state(self) -> None:
        await self.repository.initialize(self.initial)
        games = GameRepository(self.database)
        second_game = games.create_game(
            created_by_user_id=self.owner.id,
            mode=GameMode.SOLO.value,
            bot_count=1,
            bot_difficulty="easy",
        )
        second_snapshot = RoomSnapshot(
            room_id=second_game.id,
            mode=RoomMode.SOLO_BOT,
            status=RoomStatus.ACTIVE,
            players=(
                PlayerSeat(0, self.owner.id, SeatController.HUMAN),
                PlayerSeat(1, None, SeatController.BOT),
            ),
            current_turn=0,
            theme_key="country",
            bot_difficulty="easy",
        )
        await self.repository.initialize(second_snapshot)
        # An active Game with no coordinator schema belongs to another layer.
        games.create_game(
            created_by_user_id=self.owner.id,
            mode=GameMode.SOLO.value,
            bot_count=1,
            bot_difficulty="normal",
            state={"legacy": True},
        )

        self.assertEqual(
            await self.repository.list_active_room_ids(),
            tuple(sorted((self.game_id, second_game.id))),
        )

        paused = replace(
            self.initial,
            status=RoomStatus.PAUSED,
            state_version=1,
            deadline_at=None,
            paused_remaining_seconds=5,
        )
        await cas(self.repository,
            self.game_id, 0, "pause-before-recovery", paused
        )
        self.assertEqual(
            await self.repository.list_active_room_ids(),
            (second_game.id,),
        )

        with self.database.transaction() as session:
            game = session.get(Game, second_game.id)
            game.state_json = {
                "room_repository_schema": SNAPSHOT_SCHEMA_VERSION,
                "deleted": False,
                "snapshot": {},
            }
        with self.assertRaises(RoomSnapshotCorruptError):
            await self.repository.list_active_room_ids()

    async def test_finished_pvp_atomically_reopens_and_is_not_recoverable(
        self,
    ) -> None:
        await self.repository.initialize(self.initial)
        finished = replace(
            self.initial,
            status=RoomStatus.FINISHED,
            current_turn=1,
            state_version=1,
            eliminated_seats=(0,),
            expected_kana=None,
            deadline_at=None,
            losing_seat=0,
            end_reason="surrender",
        )
        await cas(
            self.repository,
            self.game_id,
            0,
            "finish-before-restart",
            finished,
        )

        self.assertEqual(await self.repository.list_active_room_ids(), ())
        self.assertEqual(await self.repository.list_recoverable_room_ids(), ())
        with self.database.read_session() as session:
            lobby = session.get(Room, self.lobby_id)
            memberships = tuple(
                session.scalars(
                    select(RoomMembership).where(
                        RoomMembership.room_id == self.lobby_id
                    )
                )
            )
            self.assertEqual(lobby.status, "waiting")
            self.assertIsNone(lobby.current_game_id)
            self.assertTrue(all(not member.ready for member in memberships))

        with self.database.transaction() as session:
            lobby = session.get(Room, self.lobby_id)
            lobby.status = "closed"
            lobby.deleted_at = NOW

        self.assertEqual(
            await self.repository.list_recoverable_room_ids(),
            (),
        )

    async def test_finish_records_human_results_once_and_preserves_them(
        self,
    ) -> None:
        await self.repository.initialize(self.initial)
        finished = replace(
            self.initial,
            status=RoomStatus.FINISHED,
            current_turn=1,
            state_version=1,
            eliminated_seats=(0,),
            expected_kana=None,
            deadline_at=None,
            losing_seat=0,
            end_reason="surrender",
        )

        first = await cas(
            self.repository,
            self.game_id,
            0,
            "finish-with-results",
            finished,
        )
        replay = await cas(
            self.repository,
            self.game_id,
            0,
            "finish-with-results",
            finished,
        )

        self.assertEqual(first.status, RepositoryStatus.APPLIED)
        self.assertEqual(replay.status, RepositoryStatus.DUPLICATE)
        with self.database.read_session() as session:
            rows = tuple(
                session.scalars(
                    select(MatchParticipation)
                    .where(MatchParticipation.game_id == self.game_id)
                    .order_by(MatchParticipation.seat_index)
                )
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                tuple(
                    (
                        row.user_id,
                        row.result,
                        row.placement,
                        row.word_count,
                    )
                    for row in rows
                ),
                (
                    (
                        self.owner.id,
                        MatchResult.LOSS.value,
                        2,
                        1,
                    ),
                    (
                        self.guest.id,
                        MatchResult.WIN.value,
                        1,
                        0,
                    ),
                ),
            )
            self.assertTrue(
                all(row.mode == GameMode.MULTIPLAYER.value for row in rows)
            )
            self.assertTrue(all(row.player_count == 2 for row in rows))
            self.assertTrue(all(row.end_reason == "surrender" for row in rows))

        deleted = await delete(
            self.repository,
            self.game_id,
            1,
            "delete-finished-room",
        )
        self.assertEqual(deleted.status, RepositoryStatus.APPLIED)
        self.assertFalse(deleted.receipt.deleted)
        self.assertEqual(deleted.receipt.snapshot, finished)
        replayed_delete = await delete(
            self.repository,
            self.game_id,
            1,
            "delete-finished-room",
        )
        self.assertEqual(replayed_delete.status, RepositoryStatus.DUPLICATE)
        self.assertFalse(replayed_delete.receipt.deleted)
        with self.database.read_session() as session:
            rows = tuple(
                session.scalars(
                    select(MatchParticipation).where(
                        MatchParticipation.game_id == self.game_id
                    )
                )
            )
            game = session.get(Game, self.game_id)
            lobby = session.get(Room, self.lobby_id)
            self.assertEqual(len(rows), 2)
            self.assertEqual(game.finished_at, rows[0].finished_at)
            self.assertEqual(game.status, StoredGameStatus.FINISHED.value)
            self.assertEqual(lobby.status, "waiting")
            self.assertIsNone(lobby.current_game_id)
            self.assertIsNone(lobby.deleted_at)

    async def test_multi_seat_bot_winner_records_only_humans_with_places(
        self,
    ) -> None:
        initial = replace(
            self.initial,
            players=(
                self.initial.players[0],
                self.initial.players[1],
                PlayerSeat(2, None, SeatController.BOT),
            ),
            current_turn=0,
            spectators=(),
        )
        await self.repository.initialize(initial)
        finished = replace(
            initial,
            status=RoomStatus.FINISHED,
            current_turn=2,
            state_version=1,
            eliminated_seats=(0, 1),
            expected_kana=None,
            deadline_at=None,
            losing_seat=1,
            end_reason="ends_with_n",
        )
        await cas(
            self.repository,
            self.game_id,
            0,
            "bot-wins-three-seats",
            finished,
        )

        with self.database.read_session() as session:
            rows = tuple(
                session.scalars(
                    select(MatchParticipation)
                    .where(MatchParticipation.game_id == self.game_id)
                    .order_by(MatchParticipation.seat_index)
                )
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                tuple((row.result, row.placement) for row in rows),
                (
                    (MatchResult.LOSS.value, 3),
                    (MatchResult.LOSS.value, 2),
                ),
            )
            self.assertTrue(all(row.player_count == 3 for row in rows))

    async def test_delete_closes_lobby_and_receipt_survives_restart(self) -> None:
        await self.repository.initialize(self.initial)
        deleted = await delete(self.repository,
            self.game_id, 0, "delete-room"
        )
        self.assertEqual(deleted.status, RepositoryStatus.APPLIED)
        self.assertTrue(deleted.receipt.deleted)
        self.assertEqual(deleted.receipt.command_kind, "delete")
        self.assertEqual(deleted.receipt.expected_version, 0)
        self.assertIsNone(await self.repository.load(self.game_id))

        with self.database.read_session() as session:
            game = session.get(Game, self.game_id)
            lobby = session.get(Room, self.lobby_id)
            self.assertEqual(game.status, StoredGameStatus.ABANDONED.value)
            self.assertEqual(game.state_version, 1)
            self.assertEqual(lobby.status, "closed")
            self.assertIsNotNone(lobby.deleted_at)
            self.assertEqual(
                tuple(
                    session.scalars(
                        select(MatchParticipation).where(
                            MatchParticipation.game_id == self.game_id
                        )
                    )
                ),
                (),
            )

        restarted_database = Database(self.database_url)
        restarted = SQLAlchemyRoomRepository(restarted_database)
        try:
            receipt = await restarted.find_operation(
                self.game_id, "delete-room"
            )
            self.assertTrue(receipt.deleted)
            retry = await delete(restarted,
                self.game_id, 0, "delete-room"
            )
            self.assertEqual(retry.status, RepositoryStatus.DUPLICATE)
            self.assertTrue(retry.receipt.deleted)
        finally:
            restarted_database.dispose()


class RoomPersistenceMigrationTests(unittest.TestCase):
    def test_alembic_schema_includes_command_receipts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "migration.sqlite3")
            url = f"sqlite+pysqlite:///{path.as_posix()}"
            import os
            previous = os.environ.get("DIRECT_DATABASE_URL")
            os.environ["DIRECT_DATABASE_URL"] = url
            try:
                command.upgrade(Config(str(root / "alembic.ini")), "head")
                database = Database(url)
                try:
                    self.assertIn(
                        "room_command_receipts",
                        inspect(database.engine).get_table_names(),
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