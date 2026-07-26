"""Tests for durable, versioned account onboarding."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
import tempfile
import unittest
from uuid import uuid4

from argon2 import PasswordHasher
from sqlalchemy import func, select

from shiritori.auth import AuthService
from shiritori.database import Database
from shiritori.models import User, UserOnboarding
from shiritori.onboarding import (
    CURRENT_TUTORIAL_VERSION,
    OnboardingService,
    OnboardingUserUnavailableError,
)


UTC = timezone.utc
BASE_TIME = datetime(2026, 7, 26, 15, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self) -> None:
        self.current = BASE_TIME

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int = 1) -> None:
        self.current += timedelta(seconds=seconds)


class OnboardingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        path = Path(self.temporary_directory.name, "onboarding.sqlite3")
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
            "tutorial-alice",
            "alice-password-123",
            display_name="Alice",
        )
        self.bob = auth.register(
            "tutorial-bob",
            "bob-password-123",
            display_name="Bob",
        )
        self.clock = MutableClock()
        self.service = OnboardingService(
            self.database,
            clock=self.clock,
        )

    def tearDown(self) -> None:
        self.database.dispose()
        self.temporary_directory.cleanup()

    def test_new_user_needs_tutorial_and_completion_is_durable(self) -> None:
        self.assertTrue(self.service.needs_tutorial(self.alice.id))

        completion = self.service.complete(self.alice.id)

        self.assertFalse(self.service.needs_tutorial(self.alice.id))
        self.assertEqual(
            completion.tutorial_version,
            CURRENT_TUTORIAL_VERSION,
        )
        self.assertEqual(completion.completed_at, BASE_TIME)
        self.assertEqual(completion.updated_at, BASE_TIME)
        self.assertNotIn(
            "user_id",
            completion.__dataclass_fields__,
        )
        with self.database.read_session() as session:
            stored = session.get(UserOnboarding, self.alice.id)
            self.assertIsNotNone(stored)
            self.assertEqual(
                stored.tutorial_version,
                CURRENT_TUTORIAL_VERSION,
            )

    def test_repeated_completion_preserves_first_timestamp(self) -> None:
        first = self.service.complete(self.alice.id)
        self.clock.advance(60)

        replay = self.service.complete(self.alice.id)

        self.assertEqual(replay, first)
        with self.database.read_session() as session:
            count = session.scalar(
                select(func.count()).select_from(UserOnboarding)
            )
        self.assertEqual(count, 1)

    def test_completion_is_isolated_per_account(self) -> None:
        self.service.complete(self.alice.id)

        self.assertFalse(self.service.needs_tutorial(self.alice.id))
        self.assertTrue(self.service.needs_tutorial(self.bob.id))

    def test_missing_and_disabled_users_are_indistinguishable(self) -> None:
        missing = str(uuid4())
        for operation in (
            lambda: self.service.needs_tutorial(missing),
            lambda: self.service.complete(missing),
        ):
            with self.subTest(account="missing"):
                with self.assertRaises(OnboardingUserUnavailableError):
                    operation()

        with self.database.transaction() as session:
            user = session.get(User, self.alice.id)
            self.assertIsNotNone(user)
            user.disabled_at = BASE_TIME

        for operation in (
            lambda: self.service.needs_tutorial(self.alice.id),
            lambda: self.service.complete(self.alice.id),
        ):
            with self.subTest(account="disabled"):
                with self.assertRaises(OnboardingUserUnavailableError):
                    operation()

    def test_identifiers_and_versions_are_strictly_validated(self) -> None:
        for identifier in (None, "", " ", "x" * 37):
            with self.subTest(identifier=identifier):
                with self.assertRaises(
                    OnboardingUserUnavailableError
                ):
                    self.service.needs_tutorial(identifier)

        for version in (
            0,
            -1,
            True,
            CURRENT_TUTORIAL_VERSION + 1,
        ):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    self.service.complete(
                        self.alice.id,
                        version=version,
                    )

    def test_naive_clock_is_rejected_without_persisting_progress(self) -> None:
        service = OnboardingService(
            self.database,
            clock=lambda: BASE_TIME.replace(tzinfo=None),
        )

        with self.assertRaises(ValueError):
            service.complete(self.alice.id)

        self.assertTrue(service.needs_tutorial(self.alice.id))

    def test_simultaneous_completion_creates_one_stable_row(self) -> None:
        workers = 8
        barrier = Barrier(workers)

        def complete_once(_index: int):
            barrier.wait(timeout=5)
            return self.service.complete(self.alice.id)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = tuple(executor.map(complete_once, range(workers)))

        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(results[0].completed_at, BASE_TIME)
        with self.database.read_session() as session:
            rows = tuple(session.scalars(select(UserOnboarding)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].user_id, self.alice.id)
        self.assertEqual(
            rows[0].tutorial_version,
            CURRENT_TUTORIAL_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
