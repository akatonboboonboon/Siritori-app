"""Durable, versioned onboarding progress for authenticated users.

The browser is never authoritative for tutorial completion.  Each operation
re-checks the current user row so a removed or disabled account cannot read or
mutate onboarding state through a stale NiceGUI session.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import Database
from .models import User, UserOnboarding, utc_now


CURRENT_TUTORIAL_VERSION = 1


class OnboardingUserUnavailableError(LookupError):
    """Hide whether an account is missing or disabled."""

    def __init__(self) -> None:
        super().__init__("このアカウントではチュートリアルを保存できません。")


@dataclass(frozen=True, slots=True)
class OnboardingProgress:
    """Account-ID-free completion state safe to return to the web layer."""

    tutorial_version: int
    completed_at: datetime
    updated_at: datetime


def _user_id(value: str) -> str:
    if not isinstance(value, str):
        raise OnboardingUserUnavailableError()
    identifier = value.strip()
    if not identifier or len(identifier) > 36:
        raise OnboardingUserUnavailableError()
    return identifier


def _tutorial_version(value: int) -> int:
    if type(value) is not int or value != CURRENT_TUTORIAL_VERSION:
        raise ValueError(
            "version must equal CURRENT_TUTORIAL_VERSION"
        )
    return value


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _stored_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class OnboardingRepository:
    """Small repository which makes the per-user locking rule explicit."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def get_active_user(
        session: Session,
        user_id: str,
        *,
        for_update: bool,
    ) -> User | None:
        statement = select(User).where(User.id == user_id)
        if for_update:
            statement = statement.with_for_update()
        user = session.scalar(statement)
        if user is None or user.disabled_at is not None:
            return None
        return user

    @staticmethod
    def get_progress(
        session: Session,
        user_id: str,
    ) -> UserOnboarding | None:
        return session.get(UserOnboarding, user_id)


class OnboardingService:
    """Read and complete the current account tutorial idempotently."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] = utc_now,
        repository: OnboardingRepository | None = None,
    ) -> None:
        self.database = database
        self._clock = clock
        self.repository = repository or OnboardingRepository(database)
        if self.repository.database is not database:
            raise ValueError("repository must use the same database")
        # PostgreSQL's user-row lock is authoritative across workers. A
        # bounded in-process stripe also gives SQLite (which ignores
        # ``FOR UPDATE``) deterministic behavior in local development/tests.
        self._completion_locks = tuple(Lock() for _ in range(32))

    def needs_tutorial(self, user_id: str) -> bool:
        """Return whether the fresh, active account lacks this tutorial."""

        owner_id = _user_id(user_id)
        with self.database.read_session() as session:
            user = self.repository.get_active_user(
                session,
                owner_id,
                for_update=False,
            )
            self._require_active_user(user)
            progress = self.repository.get_progress(session, owner_id)
            return (
                progress is None
                or progress.tutorial_version
                < CURRENT_TUTORIAL_VERSION
            )

    def complete(
        self,
        user_id: str,
        *,
        version: int = CURRENT_TUTORIAL_VERSION,
    ) -> OnboardingProgress:
        """Complete one tutorial version without overwriting a replay.

        Locking the durable user row serializes first completion on PostgreSQL,
        including when no onboarding row exists yet.  The uniqueness fallback
        keeps the same operation idempotent on databases such as SQLite where
        ``SELECT FOR UPDATE`` is ignored.
        """

        owner_id = _user_id(user_id)
        completed_version = _tutorial_version(version)
        lock = self._completion_locks[
            hash(owner_id) % len(self._completion_locks)
        ]
        with lock:
            try:
                return self._complete_once(owner_id, completed_version)
            except IntegrityError as error:
                # A concurrent first completion in another process can win
                # between our read and insert on a database which ignores the
                # user-row lock.
                with self.database.read_session() as session:
                    user = self.repository.get_active_user(
                        session,
                        owner_id,
                        for_update=False,
                    )
                    self._require_active_user(user)
                    progress = self.repository.get_progress(
                        session,
                        owner_id,
                    )
                    if (
                        progress is not None
                        and progress.tutorial_version >= completed_version
                    ):
                        return self._view(progress)
                raise error

    def _complete_once(
        self,
        user_id: str,
        version: int,
    ) -> OnboardingProgress:
        with self.database.transaction() as session:
            user = self.repository.get_active_user(
                session,
                user_id,
                for_update=True,
            )
            self._require_active_user(user)
            progress = self.repository.get_progress(session, user_id)
            if progress is not None and progress.tutorial_version >= version:
                return self._view(progress)

            now = _aware_utc(self._clock())
            if progress is None:
                progress = UserOnboarding(
                    user_id=user_id,
                    tutorial_version=version,
                    completed_at=now,
                    updated_at=now,
                )
                session.add(progress)
            else:
                progress.tutorial_version = version
                progress.completed_at = now
                progress.updated_at = now
            session.flush()
            return self._view(progress)

    @staticmethod
    def _require_active_user(user: User | None) -> None:
        if user is None:
            raise OnboardingUserUnavailableError()

    @staticmethod
    def _view(progress: UserOnboarding) -> OnboardingProgress:
        return OnboardingProgress(
            tutorial_version=progress.tutorial_version,
            completed_at=_stored_utc(progress.completed_at),
            updated_at=_stored_utc(progress.updated_at),
        )


__all__ = [
    "CURRENT_TUTORIAL_VERSION",
    "OnboardingProgress",
    "OnboardingRepository",
    "OnboardingService",
    "OnboardingUserUnavailableError",
]
