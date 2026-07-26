"""Tests for immutable match statistics and privacy-aware rankings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from sqlalchemy import inspect

from shiritori.auth import AuthService
from shiritori.database import Database
from shiritori.models import (
    Game,
    GameMode,
    MatchParticipation,
    MatchResult,
    StoredGameStatus,
    User,
)
from shiritori.statistics import (
    StatisticsRepository,
    StatisticsUserNotFound,
)


UTC = timezone.utc
BASE_TIME = datetime(2026, 7, 26, 3, 0, tzinfo=UTC)


class StatisticsRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        path = Path(self.temporary_directory.name, "statistics.sqlite3")
        self.database = Database(
            f"sqlite+pysqlite:///{path.as_posix()}"
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
        self.alice = auth.register(
            "stats-alice",
            "alice-password-123",
            display_name="Alice",
        )
        self.bob = auth.register(
            "stats-bob",
            "bob-password-123",
            display_name="Bob",
        )
        self.carol = auth.register(
            "stats-carol",
            "carol-password-123",
            display_name="Carol",
        )
        self.repository = StatisticsRepository(self.database)

    def tearDown(self) -> None:
        self.database.dispose()
        self.temporary_directory.cleanup()

    def _record_game(
        self,
        *,
        creator_id: str,
        mode: str,
        finished_at: datetime,
        results: tuple[
            tuple[str, int, str, int | None, int],
            ...,
        ],
        end_reason: str,
        player_count: int | None = None,
    ) -> str:
        game_id = str(uuid4())
        with self.database.transaction() as session:
            session.add(
                Game(
                    id=game_id,
                    created_by_user_id=creator_id,
                    mode=mode,
                    status=StoredGameStatus.FINISHED.value,
                    theme_key="all",
                    bot_count=1 if mode == GameMode.SOLO.value else 0,
                    bot_difficulty=(
                        "normal" if mode == GameMode.SOLO.value else None
                    ),
                    settings_json={},
                    state_json={},
                    state_version=1,
                    created_at=finished_at - timedelta(minutes=2),
                    updated_at=finished_at,
                    finished_at=finished_at,
                )
            )
            session.flush()
            stored_player_count = player_count or len(results)
            for (
                user_id,
                seat_index,
                result,
                placement,
                word_count,
            ) in results:
                session.add(
                    MatchParticipation(
                        game_id=game_id,
                        user_id=user_id,
                        mode=mode,
                        seat_index=seat_index,
                        result=result,
                        placement=placement,
                        player_count=stored_player_count,
                        word_count=word_count,
                        end_reason=end_reason,
                        finished_at=finished_at,
                    )
                )
        return game_id

    def _seed_results(self) -> tuple[str, str, str]:
        first = self._record_game(
            creator_id=self.alice.id,
            mode=GameMode.MULTIPLAYER.value,
            finished_at=BASE_TIME,
            results=(
                (self.alice.id, 0, MatchResult.WIN.value, 1, 4),
                (self.bob.id, 1, MatchResult.LOSS.value, 2, 3),
            ),
            end_reason="ends_with_n",
        )
        second = self._record_game(
            creator_id=self.carol.id,
            mode=GameMode.MULTIPLAYER.value,
            finished_at=BASE_TIME + timedelta(minutes=5),
            results=(
                (self.alice.id, 0, MatchResult.LOSS.value, 2, 2),
                (self.carol.id, 1, MatchResult.WIN.value, 1, 5),
            ),
            end_reason="surrender",
        )
        solo = self._record_game(
            creator_id=self.alice.id,
            mode=GameMode.SOLO.value,
            finished_at=BASE_TIME + timedelta(minutes=10),
            results=(
                (self.alice.id, 0, MatchResult.WIN.value, 1, 6),
                # A permanent Bot is intentionally not represented by a row.
            ),
            end_reason="no_legal_move",
            player_count=2,
        )
        return first, second, solo

    def test_summary_and_recent_matches_are_derived_from_results(self) -> None:
        first, second, solo = self._seed_results()

        summary = self.repository.get_user_summary(self.alice.id)

        self.assertEqual(summary.games_played, 3)
        self.assertEqual(summary.wins, 2)
        self.assertEqual(summary.losses, 1)
        self.assertEqual(summary.draws, 0)
        self.assertEqual(summary.pvp_wins, 1)
        self.assertEqual(summary.solo_wins, 1)
        self.assertEqual(summary.accepted_words, 12)
        self.assertAlmostEqual(summary.win_rate, 2 / 3)
        self.assertFalse(summary.leaderboard_visible)

        recent = self.repository.list_recent_matches(
            self.alice.id,
            limit=2,
        )
        self.assertEqual(
            tuple(match.game_id for match in recent),
            (solo, second),
        )
        self.assertEqual(recent[0].mode, GameMode.SOLO.value)
        self.assertEqual(recent[0].move_count, 6)
        self.assertEqual(recent[1].end_reason, "surrender")
        self.assertNotEqual(first, recent[0].game_id)

    def test_missing_user_and_limit_validation_fail_closed(self) -> None:
        with self.assertRaises(StatisticsUserNotFound):
            self.repository.get_user_summary(str(uuid4()))
        with self.assertRaises(StatisticsUserNotFound):
            self.repository.list_recent_matches(str(uuid4()))
        with self.assertRaises(ValueError):
            self.repository.list_recent_matches(self.alice.id, limit=0)
        with self.assertRaises(ValueError):
            self.repository.set_leaderboard_visibility(
                self.alice.id,
                visible=1,
            )

    def test_pvp_win_ranking_is_opt_in_private_and_deterministic(self) -> None:
        self._seed_results()
        self.assertEqual(self.repository.list_pvp_win_leaderboard(), ())

        self.assertTrue(
            self.repository.set_leaderboard_visibility(
                self.alice.id,
                True,
            )
        )
        self.repository.set_leaderboard_visibility(self.bob.id, True)
        alice_only = self.repository.list_pvp_win_leaderboard()
        self.assertEqual(
            tuple((row.rank, row.display_name, row.wins) for row in alice_only),
            ((1, "Alice", 1),),
        )

        self.repository.set_leaderboard_visibility(self.carol.id, True)
        tied = self.repository.list_pvp_win_leaderboard()
        self.assertEqual(
            tuple(
                (
                    row.rank,
                    row.display_name,
                    row.wins,
                    row.games_played,
                )
                for row in tied
            ),
            (
                (1, "Carol", 1, 1),
                (1, "Alice", 1, 2),
            ),
        )
        self.assertAlmostEqual(tied[0].win_rate, 1.0)
        self.assertAlmostEqual(tied[1].win_rate, 0.5)

        with self.database.transaction() as session:
            session.get(User, self.carol.id).disabled_at = BASE_TIME
        self.assertEqual(
            tuple(
                row.display_name
                for row in self.repository.list_pvp_win_leaderboard()
            ),
            ("Alice",),
        )


class StatisticsMigrationTests(unittest.TestCase):
    def test_migration_adds_and_removes_statistics_schema(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "statistics-migration.sqlite3")
            url = f"sqlite+pysqlite:///{path.as_posix()}"
            previous = os.environ.get("DIRECT_DATABASE_URL")
            os.environ["DIRECT_DATABASE_URL"] = url
            try:
                config = Config(str(root / "alembic.ini"))
                command.upgrade(config, "head")
                command.check(config)
                database = Database(url)
                try:
                    schema = inspect(database.engine)
                    self.assertIn(
                        "match_participations",
                        schema.get_table_names(),
                    )
                    user_columns = {
                        column["name"]
                        for column in schema.get_columns("users")
                    }
                    self.assertIn(
                        "leaderboard_visible",
                        user_columns,
                    )
                    participation_indexes = {
                        index["name"]
                        for index in schema.get_indexes(
                            "match_participations"
                        )
                    }
                    self.assertTrue(
                        {
                            "ix_match_participations_user_finished",
                            "ix_match_participations_pvp_result",
                        }.issubset(participation_indexes)
                    )
                finally:
                    database.dispose()

                command.downgrade(config, "0002_room_discovery")
                downgraded = Database(url)
                try:
                    schema = inspect(downgraded.engine)
                    self.assertNotIn(
                        "match_participations",
                        schema.get_table_names(),
                    )
                    self.assertNotIn(
                        "leaderboard_visible",
                        {
                            column["name"]
                            for column in schema.get_columns("users")
                        },
                    )
                finally:
                    downgraded.dispose()
            finally:
                if previous is None:
                    os.environ.pop("DIRECT_DATABASE_URL", None)
                else:
                    os.environ["DIRECT_DATABASE_URL"] = previous


if __name__ == "__main__":
    unittest.main()
