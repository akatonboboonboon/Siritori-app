"""Tests for official daily-attempt persistence and private rankings."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest

from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from sqlalchemy import inspect

from shiritori.auth import AuthService
from shiritori.daily_challenge import DailyChallengeCondition
from shiritori.daily_challenge_persistence import (
    DailyChallengeProjectionError,
    DailyChallengeRunFinishedError,
    DailyChallengeRunOwnershipError,
    DailyChallengeUserUnavailableError,
    SQLAlchemyDailyChallengeService,
    StaleDailyChallengeStateError,
)
from shiritori.database import Database
from shiritori.game_session import SessionCode
from shiritori.models import DailyChallengeRun, User
from shiritori.score_attack import (
    ScoreAttackFinishReason,
    ScoreAttackStatus,
)
from shiritori.statistics import StatisticsRepository
from tests.test_score_attack import (
    FakeLexicon,
    ManualClock,
    START,
    accepted,
)


WORDS = {
    "林檎": accepted("林檎", "りんご"),
    "語尾": accepted("語尾", "ごり"),
    "りんご": accepted("りんご", "りんご"),
    "ゴーン": accepted("ゴーン", "ごーん"),
}


def fixed_condition(challenge_date: date) -> DailyChallengeCondition:
    return DailyChallengeCondition.create(
        challenge_date,
        "林檎",
        "りんご",
    )


class DailyChallengePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        path = Path(self.temporary_directory.name, "daily.sqlite3")
        self.database = Database(
            f"sqlite+pysqlite:///{path.as_posix()}"
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
        self.alice = auth.register(
            "daily-alice",
            "alice-password-123",
            display_name="Alice",
        )
        self.bob = auth.register(
            "daily-bob",
            "bob-password-123",
            display_name="Bob",
        )
        self.carol = auth.register(
            "daily-carol",
            "carol-password-123",
            display_name="Carol",
        )
        self.clock = ManualClock()
        self.lexicon = FakeLexicon(WORDS)
        self.service = SQLAlchemyDailyChallengeService(
            self.database,
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
            condition_factory=fixed_condition,
        )

    def tearDown(self) -> None:
        self.database.dispose()
        self.temporary_directory.cleanup()

    def _finish_one_word(self, user_id: str):
        run = self.service.start_today(user_id)
        accepted_result = self.service.submit(
            user_id=user_id,
            run_id=run.id,
            surface="語尾",
            expected_version=run.state_version,
        )
        return self.service.submit(
            user_id=user_id,
            run_id=run.id,
            surface="りんご",
            expected_version=accepted_result.run.state_version,
        ).run

    def _finish_zero_words(self, user_id: str):
        run = self.service.start_today(user_id)
        return self.service.submit(
            user_id=user_id,
            run_id=run.id,
            surface="ゴーン",
            expected_version=run.state_version,
        ).run

    def test_start_is_one_per_jst_date_and_idempotently_resumes(self) -> None:
        first = self.service.start_today(self.alice.id)
        repeated = self.service.start_today(self.alice.id)
        current = self.service.current(self.alice.id)

        self.assertEqual(repeated, first)
        self.assertEqual(current, first)
        self.assertEqual(first.challenge_date, date(2026, 7, 26))
        self.assertEqual(first.score, 0)
        self.assertEqual(first.accepted_count, 0)
        self.assertEqual(first.state_version, 0)
        self.assertEqual(first.status, ScoreAttackStatus.ACTIVE.value)
        self.assertEqual(first.condition, fixed_condition(first.challenge_date))

        finished = self._finish_one_word(self.alice.id)
        same_day = self.service.start_today(self.alice.id)
        self.assertEqual(same_day.id, first.id)
        self.assertEqual(same_day, finished)

        with self.database.read_session() as session:
            attempts = tuple(
                session.query(DailyChallengeRun).filter_by(
                    user_id=self.alice.id
                )
            )
        self.assertEqual(len(attempts), 1)

    def test_cas_owner_and_finished_attempt_are_enforced(self) -> None:
        run = self.service.start_today(self.alice.id)
        accepted_result = self.service.submit(
            user_id=self.alice.id,
            run_id=run.id,
            surface="語尾",
            expected_version=0,
        )

        self.assertEqual(accepted_result.result.code, SessionCode.ACCEPTED)
        self.assertEqual(accepted_result.run.state_version, 1)
        self.assertEqual(accepted_result.run.score, 14)
        with self.assertRaises(StaleDailyChallengeStateError):
            self.service.submit(
                user_id=self.alice.id,
                run_id=run.id,
                surface="りんご",
                expected_version=0,
            )
        with self.assertRaises(DailyChallengeRunOwnershipError):
            self.service.get(self.bob.id, run.id)

        finished = self.service.submit(
            user_id=self.alice.id,
            run_id=run.id,
            surface="りんご",
            expected_version=1,
        ).run
        self.assertEqual(finished.state_version, 2)
        self.assertEqual(
            finished.finish_reason,
            ScoreAttackFinishReason.DUPLICATE.value,
        )
        with self.assertRaises(DailyChallengeRunFinishedError):
            self.service.submit(
                user_id=self.alice.id,
                run_id=run.id,
                surface="語尾",
                expected_version=2,
            )

    def test_rejected_word_does_not_advance_version_or_score(self) -> None:
        run = self.service.start_today(self.alice.id)

        rejected = self.service.submit(
            user_id=self.alice.id,
            run_id=run.id,
            surface="存在しない",
            expected_version=0,
        )

        self.assertEqual(
            rejected.result.code,
            SessionCode.LEXICON_REJECTED,
        )
        self.assertEqual(rejected.run.state_version, 0)
        self.assertEqual(rejected.run.score, 0)

    def test_restart_and_startup_finalization_never_extend_deadline(
        self,
    ) -> None:
        alice_run = self.service.start_today(self.alice.id)
        bob_run = self.service.start_today(self.bob.id)
        deadline = alice_run.deadline_at
        self.clock.advance(181)

        restarted = SQLAlchemyDailyChallengeService(
            self.database,
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
            condition_factory=fixed_condition,
        )
        alice_finished = restarted.current(self.alice.id)

        self.assertEqual(
            alice_finished.finish_reason,
            ScoreAttackFinishReason.TIMEOUT.value,
        )
        self.assertEqual(alice_finished.finished_at, deadline)
        finalized = restarted.finalize_expired_active_runs(limit=1)
        self.assertEqual(tuple(run.id for run in finalized), (bob_run.id,))
        self.assertEqual(finalized[0].finished_at, bob_run.deadline_at)
        self.assertEqual(restarted.finalize_expired_active_runs(), ())

    def test_attempt_crossing_jst_midnight_finishes_before_new_day(self) -> None:
        self.clock.current = datetime(
            2026,
            7,
            26,
            14,
            59,
            tzinfo=timezone.utc,
        )
        previous = self.service.start_today(self.alice.id)
        self.clock.advance(60)

        still_previous = self.service.start_today(self.alice.id)

        self.assertEqual(still_previous.id, previous.id)
        self.assertEqual(
            still_previous.challenge_date,
            date(2026, 7, 26),
        )
        accepted_result = self.service.submit(
            user_id=self.alice.id,
            run_id=previous.id,
            surface="語尾",
            expected_version=previous.state_version,
        )
        self.service.submit(
            user_id=self.alice.id,
            run_id=previous.id,
            surface="りんご",
            expected_version=accepted_result.run.state_version,
        )

        today = self.service.start_today(self.alice.id)
        self.assertNotEqual(today.id, previous.id)
        self.assertEqual(today.challenge_date, date(2026, 7, 27))

    def test_missing_and_disabled_users_fail_closed(self) -> None:
        with self.assertRaises(DailyChallengeUserUnavailableError):
            self.service.start_today("missing-user")

        with self.database.transaction() as session:
            session.get(User, self.alice.id).disabled_at = self.clock.current

        with self.assertRaises(DailyChallengeUserUnavailableError):
            self.service.start_today(self.alice.id)
        with self.assertRaises(DailyChallengeUserUnavailableError):
            self.service.current(self.alice.id)

    def test_projection_tampering_fails_closed(self) -> None:
        run = self.service.start_today(self.alice.id)
        with self.database.transaction() as session:
            session.get(DailyChallengeRun, run.id).score = 999

        with self.assertRaises(DailyChallengeProjectionError):
            self.service.get(self.alice.id, run.id)

    def test_daily_ranking_is_opt_in_date_scoped_and_uses_shared_ties(
        self,
    ) -> None:
        bob = self._finish_one_word(self.bob.id)
        self.clock.advance(1)
        alice = self._finish_one_word(self.alice.id)
        self.clock.advance(1)
        carol = self._finish_zero_words(self.carol.id)

        self.assertEqual(self.service.list_daily_leaderboard(), ())
        statistics = StatisticsRepository(self.database)
        for user in (self.alice, self.bob, self.carol):
            statistics.set_leaderboard_visibility(user.id, True)

        ranking = self.service.list_daily_leaderboard()

        self.assertEqual(
            tuple(
                (
                    entry.rank,
                    entry.display_name,
                    entry.score,
                    entry.accepted_count,
                )
                for entry in ranking
            ),
            (
                (1, "Bob", 14, 1),
                (1, "Alice", 14, 1),
                (3, "Carol", 0, 0),
            ),
        )
        self.assertEqual(ranking[0].finished_at, bob.finished_at)
        self.assertEqual(ranking[1].finished_at, alice.finished_at)
        self.assertEqual(ranking[2].finished_at, carol.finished_at)
        self.assertEqual(
            self.service.list_daily_leaderboard(
                date(2026, 7, 27)
            ),
            (),
        )

        with self.database.transaction() as session:
            session.get(User, self.bob.id).disabled_at = self.clock.current
        filtered = self.service.list_daily_leaderboard()
        self.assertEqual(
            tuple((entry.rank, entry.display_name) for entry in filtered),
            ((1, "Alice"), (2, "Carol")),
        )


class DailyChallengeMigrationTests(unittest.TestCase):
    def test_migration_adds_official_attempt_constraints_and_downgrades(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "daily-migration.sqlite3")
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
                        "daily_challenge_runs",
                        schema.get_table_names(),
                    )
                    columns = {
                        column["name"]
                        for column in schema.get_columns(
                            "daily_challenge_runs"
                        )
                    }
                    self.assertTrue(
                        {
                            "challenge_date",
                            "condition_key",
                            "snapshot",
                            "state_version",
                            "score",
                            "deadline_at",
                            "finished_at",
                        }.issubset(columns)
                    )
                    uniques = {
                        constraint["name"]
                        for constraint in schema.get_unique_constraints(
                            "daily_challenge_runs"
                        )
                    }
                    self.assertIn(
                        "uq_daily_challenge_runs_user_date",
                        uniques,
                    )
                    indexes = {
                        index["name"]: index
                        for index in schema.get_indexes(
                            "daily_challenge_runs"
                        )
                    }
                    self.assertTrue(
                        indexes[
                            "uq_daily_challenge_runs_active_user"
                        ]["unique"]
                    )
                finally:
                    database.dispose()

                command.downgrade(config, "0006_word_suggestions")
                downgraded = Database(url)
                try:
                    schema = inspect(downgraded.engine)
                    self.assertNotIn(
                        "daily_challenge_runs",
                        schema.get_table_names(),
                    )
                    self.assertIn(
                        "word_suggestions",
                        schema.get_table_names(),
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
