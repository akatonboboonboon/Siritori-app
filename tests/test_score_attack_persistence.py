"""Tests for transactional and resumable score attack persistence."""

from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path
import tempfile
import unittest

from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from sqlalchemy import inspect

from shiritori.auth import AuthService
from shiritori.database import Database
from shiritori.game_session import SessionCode
from shiritori.models import ScoreAttackRun, User
from shiritori.score_attack import (
    SCORE_ATTACK_DURATION_SECONDS,
    ScoreAttackFinishReason,
    ScoreAttackStatus,
)
from shiritori.score_attack_persistence import (
    SQLAlchemyScoreAttackService,
    ScoreAttackActiveRunExistsError,
    ScoreAttackProjectionError,
    ScoreAttackRunFinishedError,
    ScoreAttackRunOwnershipError,
    StaleScoreAttackStateError,
)
from shiritori.statistics import StatisticsRepository
from tests.test_score_attack import FakeLexicon, ManualClock, START, WORDS


SURFACES = tuple(WORDS)
FIRST_WORD = SURFACES[0]
DUPLICATE_WORD = SURFACES[1]
SECOND_WORD = SURFACES[2]
ENDS_WITH_N_WORD = SURFACES[5]
AMBIGUOUS_WORD = SURFACES[-1]
AMBIGUOUS_READING = WORDS[AMBIGUOUS_WORD].candidates[0].reading
NON_OPTION_READING = WORDS[FIRST_WORD].candidates[0].reading


class ScoreAttackPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        path = Path(self.temporary_directory.name, "score-attack.sqlite3")
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
            "score-alice",
            "alice-password-123",
            display_name="Alice",
        )
        self.bob = auth.register(
            "score-bob",
            "bob-password-123",
            display_name="Bob",
        )
        self.carol = auth.register(
            "score-carol",
            "carol-password-123",
            display_name="Carol",
        )
        self.clock = ManualClock()
        self.lexicon = FakeLexicon(WORDS)
        self.service = SQLAlchemyScoreAttackService(
            self.database,
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )

    def tearDown(self) -> None:
        self.database.dispose()
        self.temporary_directory.cleanup()

    def _finish_two_word_run(self, user_id: str):
        run = self.service.start(user_id)
        first = self.service.submit(
            user_id=user_id,
            run_id=run.id,
            surface=FIRST_WORD,
            expected_version=run.state_version,
        )
        second = self.service.submit(
            user_id=user_id,
            run_id=run.id,
            surface=SECOND_WORD,
            expected_version=first.run.state_version,
        )
        return self.service.submit(
            user_id=user_id,
            run_id=run.id,
            surface=DUPLICATE_WORD,
            expected_version=second.run.state_version,
        ).run

    def test_explicit_start_single_active_resume_and_owner_checks(
        self,
    ) -> None:
        run = self.service.start(self.alice.id)

        self.assertEqual(run.status, ScoreAttackStatus.ACTIVE.value)
        self.assertEqual(run.state_version, 0)
        self.assertEqual(run.score, 0)
        self.assertEqual(run.accepted_count, 0)
        self.assertEqual(run.started_at, START)
        self.assertEqual(
            run.deadline_at,
            START + timedelta(seconds=SCORE_ATTACK_DURATION_SECONDS),
        )
        with self.assertRaises(ScoreAttackActiveRunExistsError):
            self.service.start(self.alice.id)

        restarted_service = SQLAlchemyScoreAttackService(
            self.database,
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )
        resumed = restarted_service.resume_active(self.alice.id)
        self.assertEqual(resumed, run)

        with self.assertRaises(ScoreAttackRunOwnershipError):
            restarted_service.get(self.bob.id, run.id)
        with self.assertRaises(ScoreAttackRunOwnershipError):
            restarted_service.submit(
                user_id=self.bob.id,
                run_id=run.id,
                surface=FIRST_WORD,
                expected_version=0,
            )

    def test_cas_rejects_duplicate_submission_before_double_scoring(
        self,
    ) -> None:
        run = self.service.start(self.alice.id)

        accepted = self.service.submit(
            user_id=self.alice.id,
            run_id=run.id,
            surface=FIRST_WORD,
            expected_version=0,
        )

        self.assertEqual(accepted.result.code, SessionCode.ACCEPTED)
        self.assertEqual(accepted.run.state_version, 1)
        self.assertEqual(accepted.run.score, 16)
        self.assertEqual(accepted.run.accepted_count, 1)
        with self.assertRaises(StaleScoreAttackStateError) as stale:
            self.service.submit(
                user_id=self.alice.id,
                run_id=run.id,
                surface=FIRST_WORD,
                expected_version=0,
            )
        self.assertEqual(stale.exception.expected, 0)
        self.assertEqual(stale.exception.actual, 1)

        unchanged = self.service.get(self.alice.id, run.id)
        self.assertEqual(unchanged.score, 16)
        self.assertEqual(unchanged.accepted_count, 1)
        self.assertEqual(unchanged.state_version, 1)

        rejected = self.service.submit(
            user_id=self.alice.id,
            run_id=run.id,
            surface="missing-test-word",
            expected_version=1,
        )
        self.assertEqual(
            rejected.result.code,
            SessionCode.LEXICON_REJECTED,
        )
        self.assertEqual(rejected.run.state_version, 1)
        self.assertEqual(rejected.run.score, 16)

    def test_pending_reading_resolve_and_cancel_are_versioned(
        self,
    ) -> None:
        run = self.service.start(self.alice.id)
        no_pending = self.service.cancel_reading_choice(
            user_id=self.alice.id,
            run_id=run.id,
            expected_version=0,
        )
        self.assertEqual(
            no_pending.result.code,
            SessionCode.NO_READING_CHOICE_PENDING,
        )
        self.assertEqual(no_pending.run.state_version, 0)

        pending = self.service.submit(
            user_id=self.alice.id,
            run_id=run.id,
            surface=AMBIGUOUS_WORD,
            expected_version=0,
        )
        self.assertEqual(
            pending.result.code,
            SessionCode.READING_CHOICE_REQUIRED,
        )
        self.assertEqual(pending.run.state_version, 1)

        invalid = self.service.resolve_reading(
            user_id=self.alice.id,
            run_id=run.id,
            reading=NON_OPTION_READING,
            expected_version=1,
        )
        self.assertEqual(
            invalid.result.code,
            SessionCode.INVALID_READING_CHOICE,
        )
        self.assertEqual(invalid.run.state_version, 1)

        cancelled = self.service.cancel_reading_choice(
            user_id=self.alice.id,
            run_id=run.id,
            expected_version=1,
        )
        self.assertEqual(
            cancelled.result.code,
            SessionCode.READING_CHOICE_CANCELLED,
        )
        self.assertEqual(cancelled.run.state_version, 2)

        pending_again = self.service.submit(
            user_id=self.alice.id,
            run_id=run.id,
            surface=AMBIGUOUS_WORD,
            expected_version=2,
        )
        resolved = self.service.resolve_reading(
            user_id=self.alice.id,
            run_id=run.id,
            reading=AMBIGUOUS_READING,
            expected_version=3,
        )
        self.assertEqual(pending_again.run.state_version, 3)
        self.assertEqual(resolved.result.code, SessionCode.ACCEPTED)
        self.assertEqual(resolved.run.state_version, 4)
        self.assertEqual(resolved.run.accepted_count, 1)

    def test_finished_run_is_immutable_and_new_run_can_start(self) -> None:
        run = self.service.start(self.alice.id)
        finished = self.service.submit(
            user_id=self.alice.id,
            run_id=run.id,
            surface=ENDS_WITH_N_WORD,
            expected_version=0,
        ).run

        self.assertEqual(finished.status, ScoreAttackStatus.FINISHED.value)
        self.assertEqual(
            finished.finish_reason,
            ScoreAttackFinishReason.ENDS_WITH_N.value,
        )
        self.assertEqual(finished.state_version, 1)
        self.assertIsNone(finished.deadline_at)
        self.assertEqual(finished.finished_at, START)
        with self.assertRaises(StaleScoreAttackStateError):
            self.service.submit(
                user_id=self.alice.id,
                run_id=run.id,
                surface=FIRST_WORD,
                expected_version=0,
            )
        with self.assertRaises(ScoreAttackRunFinishedError):
            self.service.submit(
                user_id=self.alice.id,
                run_id=run.id,
                surface=FIRST_WORD,
                expected_version=1,
            )

        new_run = self.service.start(self.alice.id)
        self.assertNotEqual(new_run.id, run.id)
        self.assertEqual(new_run.status, ScoreAttackStatus.ACTIVE.value)
        self.assertEqual(self.service.get(self.alice.id, run.id), finished)

    def test_resume_and_startup_finalize_expired_rows_once(self) -> None:
        alice_run = self.service.start(self.alice.id)
        bob_run = self.service.start(self.bob.id)
        carol_run = self.service.start(self.carol.id)
        expected_finished_at = alice_run.deadline_at
        self.clock.advance(SCORE_ATTACK_DURATION_SECONDS + 50)

        alice_finished = self.service.resume_active(self.alice.id)
        self.assertEqual(
            alice_finished.finish_reason,
            ScoreAttackFinishReason.TIMEOUT.value,
        )
        self.assertEqual(alice_finished.state_version, 1)
        self.assertEqual(
            alice_finished.finished_at,
            expected_finished_at,
        )

        finalized = self.service.finalize_expired_active_runs(limit=1)
        self.assertSetEqual(
            {run.id for run in finalized},
            {bob_run.id, carol_run.id},
        )
        self.assertEqual(
            finalized[0].finish_reason,
            ScoreAttackFinishReason.TIMEOUT.value,
        )
        self.assertTrue(
            all(
                run.finished_at == expected_finished_at
                for run in finalized
            )
        )
        self.assertEqual(
            self.service.finalize_expired_active_runs(),
            (),
        )
        self.assertIsNone(self.service.resume_active(self.bob.id))

        replacement = self.service.start(self.alice.id)
        self.assertNotEqual(replacement.id, alice_run.id)

    def test_start_finalizes_an_overdue_active_row(self) -> None:
        first = self.service.start(self.alice.id)
        self.clock.advance(SCORE_ATTACK_DURATION_SECONDS)

        replacement = self.service.start(self.alice.id)

        old = self.service.get(self.alice.id, first.id)
        self.assertEqual(old.status, ScoreAttackStatus.FINISHED.value)
        self.assertEqual(
            old.finish_reason,
            ScoreAttackFinishReason.TIMEOUT.value,
        )
        self.assertEqual(replacement.status, ScoreAttackStatus.ACTIVE.value)

    def test_projection_tampering_fails_closed(self) -> None:
        run = self.service.start(self.alice.id)
        with self.database.transaction() as session:
            session.get(ScoreAttackRun, run.id).score = 999

        with self.assertRaises(ScoreAttackProjectionError):
            self.service.get(self.alice.id, run.id)

    def test_current_rules_personal_best_and_private_ranking(self) -> None:
        bob_best = self._finish_two_word_run(self.bob.id)
        self.clock.advance(1)
        alice_best = self._finish_two_word_run(self.alice.id)

        alice_short = self.service.start(self.alice.id)
        short_result = self.service.submit(
            user_id=self.alice.id,
            run_id=alice_short.id,
            surface=FIRST_WORD,
            expected_version=0,
        )
        self.clock.advance(SCORE_ATTACK_DURATION_SECONDS)
        self.service.expire(
            user_id=self.alice.id,
            run_id=alice_short.id,
            expected_version=short_result.run.state_version,
        )

        statistics = StatisticsRepository(self.database)
        personal = statistics.get_score_attack_personal_best(
            self.alice.id
        )
        self.assertEqual(personal.run_id, alice_best.id)
        self.assertEqual(personal.score, 32)
        self.assertEqual(personal.accepted_count, 2)
        self.assertEqual(statistics.list_score_attack_leaderboard(), ())

        statistics.set_leaderboard_visibility(self.alice.id, True)
        statistics.set_leaderboard_visibility(self.bob.id, True)
        ranking = statistics.list_score_attack_leaderboard()
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
                (1, "Bob", 32, 2),
                (2, "Alice", 32, 2),
            ),
        )
        self.assertLess(
            ranking[0].finished_at,
            ranking[1].finished_at,
        )

        with self.database.transaction() as session:
            session.get(User, self.bob.id).disabled_at = self.clock.current
        self.assertEqual(
            tuple(
                entry.display_name
                for entry in statistics.list_score_attack_leaderboard()
            ),
            ("Alice",),
        )
        self.assertEqual(bob_best.score, alice_best.score)


class ScoreAttackMigrationTests(unittest.TestCase):
    def test_migration_adds_partial_unique_and_downgrades(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "score-migration.sqlite3")
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
                        "score_attack_runs",
                        schema.get_table_names(),
                    )
                    columns = {
                        column["name"]
                        for column in schema.get_columns(
                            "score_attack_runs"
                        )
                    }
                    self.assertTrue(
                        {
                            "snapshot",
                            "state_version",
                            "rules_version",
                            "score",
                            "accepted_count",
                            "deadline_at",
                            "finished_at",
                        }.issubset(columns)
                    )
                    indexes = {
                        index["name"]: index
                        for index in schema.get_indexes(
                            "score_attack_runs"
                        )
                    }
                    self.assertTrue(
                        indexes[
                            "uq_score_attack_runs_active_user"
                        ]["unique"]
                    )
                finally:
                    database.dispose()

                command.downgrade(config, "0003_match_statistics")
                downgraded = Database(url)
                try:
                    schema = inspect(downgraded.engine)
                    self.assertNotIn(
                        "score_attack_runs",
                        schema.get_table_names(),
                    )
                    self.assertIn(
                        "match_participations",
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
