"""Transactional persistence and privacy-aware ranking for daily attempts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .daily_challenge import (
    DAILY_CHALLENGE_DURATION_SECONDS,
    DailyChallengeCondition,
    DailyChallengeSession,
    aware_utc,
    challenge_date_at,
    daily_condition_for,
)
from .database import Database
from .game_session import SessionResult
from .lexicon import LexiconValidator
from .models import DailyChallengeRun, User, new_id, utc_now
from .score_attack import ScoreAttackStatus


class DailyChallengePersistenceError(RuntimeError):
    """Base class for persistence and authorization failures."""


class DailyChallengeUserUnavailableError(LookupError):
    """Raised for a missing or disabled account."""


class DailyChallengeRunNotFoundError(LookupError):
    """Raised when a requested run does not exist."""


class DailyChallengeRunOwnershipError(PermissionError):
    """Raised when an account attempts to access another user's attempt."""


class DailyChallengeRunFinishedError(DailyChallengePersistenceError):
    """Raised when a command targets an immutable finished attempt."""


class StaleDailyChallengeStateError(DailyChallengePersistenceError):
    """Raised before a command when another tab already changed the run."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            "daily challenge state changed: "
            f"expected version {expected}, current version {actual}"
        )
        self.expected = expected
        self.actual = actual


class DailyChallengeProjectionError(DailyChallengePersistenceError):
    """Raised when stored projections disagree with the domain snapshot."""


@dataclass(frozen=True, slots=True)
class DailyChallengeRunView:
    """Detached immutable view for one authenticated user's attempt."""

    id: str
    user_id: str
    challenge_date: date
    condition: DailyChallengeCondition
    status: str
    state_version: int
    rules_version: int
    duration_seconds: int
    score: int
    accepted_count: int
    finish_reason: str | None
    started_at: datetime
    deadline_at: datetime | None
    finished_at: datetime | None
    snapshot: dict[str, object]


@dataclass(frozen=True, slots=True)
class DailyChallengeCommandResult:
    """Authoritative command result paired with the resulting DB state."""

    run: DailyChallengeRunView
    result: SessionResult | None


@dataclass(frozen=True, slots=True)
class DailyChallengeLeaderboardEntry:
    """Non-sensitive opted-in result shown on one day's ranking."""

    rank: int
    display_name: str
    score: int
    accepted_count: int
    finished_at: datetime


def _identifier(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.isspace()
        or len(value) > 36
    ):
        raise ValueError(f"{name} must contain 1-36 characters")
    return value


def _state_version(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("expected_version must be a non-negative integer")
    return value


def _batch_limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 1_000:
        raise ValueError("limit must be an integer from 1 to 1000")
    return value


def _ranking_limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    return value


def _challenge_date(value: date) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError("challenge_date must be a date")
    return value


def _stored_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _same_timestamp(
    stored: datetime | None,
    projected: datetime | None,
) -> bool:
    normalized_stored = _stored_utc(stored)
    normalized_projected = (
        aware_utc(projected) if projected is not None else None
    )
    return normalized_stored == normalized_projected


def _snapshot_finished_at(
    snapshot: Mapping[str, object],
) -> datetime | None:
    raw_attack = snapshot.get("score_attack")
    if not isinstance(raw_attack, Mapping):
        raise DailyChallengeProjectionError(
            "daily snapshot has no score attack"
        )
    raw_game = raw_attack.get("game_session")
    if not isinstance(raw_game, Mapping):
        raise DailyChallengeProjectionError(
            "started daily snapshot has no game session"
        )
    raw_value = raw_game.get("ended_at")
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise DailyChallengeProjectionError(
            "daily snapshot ended_at is invalid"
        )
    try:
        return aware_utc(
            datetime.fromisoformat(raw_value),
            "snapshot ended_at",
        )
    except ValueError as error:
        raise DailyChallengeProjectionError(
            "daily snapshot ended_at is invalid"
        ) from error


class DailyChallengeRunRepository:
    """Small row repository used under service-owned transactions."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def lock_user(session: Session, user_id: str) -> User | None:
        return session.scalar(
            select(User).where(User.id == user_id).with_for_update()
        )

    @staticmethod
    def lock_run(
        session: Session,
        run_id: str,
    ) -> DailyChallengeRun | None:
        return session.scalar(
            select(DailyChallengeRun)
            .where(DailyChallengeRun.id == run_id)
            .with_for_update()
        )

    @staticmethod
    def lock_active_run(
        session: Session,
        user_id: str,
    ) -> DailyChallengeRun | None:
        return session.scalar(
            select(DailyChallengeRun)
            .where(
                DailyChallengeRun.user_id == user_id,
                DailyChallengeRun.status
                == ScoreAttackStatus.ACTIVE.value,
            )
            .order_by(
                DailyChallengeRun.challenge_date.desc(),
                DailyChallengeRun.started_at.desc(),
            )
            .with_for_update()
        )

    @staticmethod
    def lock_date_run(
        session: Session,
        user_id: str,
        challenge_date: date,
    ) -> DailyChallengeRun | None:
        return session.scalar(
            select(DailyChallengeRun)
            .where(
                DailyChallengeRun.user_id == user_id,
                DailyChallengeRun.challenge_date == challenge_date,
            )
            .with_for_update()
        )

    def active_run_id(self, user_id: str) -> str | None:
        with self.database.read_session() as session:
            return session.scalar(
                select(DailyChallengeRun.id).where(
                    DailyChallengeRun.user_id == user_id,
                    DailyChallengeRun.status
                    == ScoreAttackStatus.ACTIVE.value,
                )
            )

    def date_run_id(
        self,
        user_id: str,
        challenge_date: date,
    ) -> str | None:
        with self.database.read_session() as session:
            return session.scalar(
                select(DailyChallengeRun.id).where(
                    DailyChallengeRun.user_id == user_id,
                    DailyChallengeRun.challenge_date == challenge_date,
                )
            )

    def expired_active_ids(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[str, ...]:
        with self.database.read_session() as session:
            return tuple(
                session.scalars(
                    select(DailyChallengeRun.id)
                    .where(
                        DailyChallengeRun.status
                        == ScoreAttackStatus.ACTIVE.value,
                        DailyChallengeRun.deadline_at <= now,
                    )
                    .order_by(
                        DailyChallengeRun.deadline_at.asc(),
                        DailyChallengeRun.id.asc(),
                    )
                    .limit(limit)
                )
            )


class SQLAlchemyDailyChallengeService:
    """Persist one official attempt per JST date under row lock and CAS."""

    def __init__(
        self,
        database: Database,
        validator: LexiconValidator | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        condition_factory: Callable[
            [date], DailyChallengeCondition
        ] = daily_condition_for,
        repository: DailyChallengeRunRepository | None = None,
    ) -> None:
        self.database = database
        self.validator = validator
        self._clock = clock or utc_now
        self._condition_factory = condition_factory
        self.repository = repository or DailyChallengeRunRepository(database)
        if self.repository.database is not database:
            raise ValueError("repository must use the same database")

    def today_condition(self) -> DailyChallengeCondition:
        """Return today's condition from the server's JST date."""

        return self._condition_for(challenge_date_at(self._now()))

    def start_today(self, user_id: str) -> DailyChallengeRunView:
        """Start today's attempt or idempotently return the existing one.

        A still-active attempt begun just before JST midnight is returned
        first.  It can run only until its original three-minute deadline;
        after it finishes, the user may start the new date's attempt.
        """

        owner_id = _identifier(user_id, "user_id")
        challenge_day = challenge_date_at(self._now())
        condition = self._condition_for(challenge_day)
        try:
            with self.database.transaction() as session:
                user = self.repository.lock_user(session, owner_id)
                self._require_available_user(user, owner_id)

                active = self.repository.lock_active_run(
                    session,
                    owner_id,
                )
                if active is not None:
                    attack = self._restore(active)
                    if attack.expire_if_due() is None:
                        return self._view(active, attack.condition)
                    self._persist_changed(
                        session,
                        active,
                        attack,
                        expected_version=active.state_version,
                    )

                existing = self.repository.lock_date_run(
                    session,
                    owner_id,
                    challenge_day,
                )
                if existing is not None:
                    restored = self._restore(existing)
                    return self._view(existing, restored.condition)

                attack = DailyChallengeSession(
                    condition,
                    self.validator,
                    clock=self._clock,
                ).start()
                snapshot = attack.to_snapshot()
                projection = self._projection(attack, snapshot)
                created_at = projection["started_at"]
                run = DailyChallengeRun(
                    id=new_id(),
                    user_id=owner_id,
                    challenge_date=challenge_day,
                    condition_key=condition.condition_key,
                    snapshot_json=snapshot,
                    state_version=0,
                    created_at=created_at,
                    updated_at=created_at,
                    **projection,
                )
                session.add(run)
                session.flush()
                restored = self._restore(run)
                return self._view(run, restored.condition)
        except IntegrityError as error:
            # SQLite ignores FOR UPDATE and multiple Render workers may race.
            # The two database uniques choose one official row; return it.
            run_id = (
                self.repository.active_run_id(owner_id)
                or self.repository.date_run_id(owner_id, challenge_day)
            )
            if run_id is not None:
                return self.get(owner_id, run_id)
            raise error

    def current(
        self,
        user_id: str,
    ) -> DailyChallengeRunView | None:
        """Resume an active attempt, otherwise return today's saved result."""

        owner_id = _identifier(user_id, "user_id")
        challenge_day = challenge_date_at(self._now())
        with self.database.transaction() as session:
            user = self.repository.lock_user(session, owner_id)
            self._require_available_user(user, owner_id)

            active = self.repository.lock_active_run(session, owner_id)
            if active is not None:
                attack = self._restore(active)
                if attack.expire_if_due() is None:
                    return self._view(active, attack.condition)
                self._persist_changed(
                    session,
                    active,
                    attack,
                    expected_version=active.state_version,
                )

            today = self.repository.lock_date_run(
                session,
                owner_id,
                challenge_day,
            )
            if today is None:
                return None
            restored = self._restore(today)
            return self._view(today, restored.condition)

    def get(
        self,
        user_id: str,
        run_id: str,
    ) -> DailyChallengeRunView:
        """Read one owned run without changing or extending its deadline."""

        owner_id = _identifier(user_id, "user_id")
        identifier = _identifier(run_id, "run_id")
        with self.database.read_session() as session:
            self._require_available_user(
                session.get(User, owner_id),
                owner_id,
            )
            run = session.get(DailyChallengeRun, identifier)
            self._authorize(run, owner_id, identifier)
            restored = self._restore(run)
            return self._view(run, restored.condition)

    def submit(
        self,
        *,
        user_id: str,
        run_id: str,
        surface: str | None,
        expected_version: int,
    ) -> DailyChallengeCommandResult:
        return self._mutate_owned(
            user_id=user_id,
            run_id=run_id,
            expected_version=expected_version,
            command=lambda attack: attack.submit(surface),
        )

    def resolve_reading(
        self,
        *,
        user_id: str,
        run_id: str,
        reading: str,
        expected_version: int,
    ) -> DailyChallengeCommandResult:
        return self._mutate_owned(
            user_id=user_id,
            run_id=run_id,
            expected_version=expected_version,
            command=lambda attack: attack.resolve_reading(reading),
        )

    def cancel_reading_choice(
        self,
        *,
        user_id: str,
        run_id: str,
        expected_version: int,
    ) -> DailyChallengeCommandResult:
        return self._mutate_owned(
            user_id=user_id,
            run_id=run_id,
            expected_version=expected_version,
            command=lambda attack: attack.cancel_reading_choice(),
        )

    def expire(
        self,
        *,
        user_id: str,
        run_id: str,
        expected_version: int,
    ) -> DailyChallengeCommandResult:
        return self._mutate_owned(
            user_id=user_id,
            run_id=run_id,
            expected_version=expected_version,
            command=lambda attack: attack.expire_if_due(),
        )

    def finalize_expired_active_runs(
        self,
        *,
        limit: int = 100,
    ) -> tuple[DailyChallengeRunView, ...]:
        """Finalize overdue rows in bounded restart-safe batches."""

        maximum = _batch_limit(limit)
        now = self._now()
        finalized: list[DailyChallengeRunView] = []
        previous_stalled_ids: tuple[str, ...] | None = None
        while True:
            run_ids = self.repository.expired_active_ids(
                now=now,
                limit=maximum,
            )
            if not run_ids:
                break
            finalized_before = len(finalized)
            for run_id in run_ids:
                view = self._finalize_expired_run(run_id)
                if view is not None:
                    finalized.append(view)
            if len(finalized) == finalized_before:
                if run_ids == previous_stalled_ids:
                    break
                previous_stalled_ids = run_ids
            else:
                previous_stalled_ids = None
        return tuple(finalized)

    def list_daily_leaderboard(
        self,
        challenge_date: date | None = None,
        *,
        limit: int = 50,
    ) -> tuple[DailyChallengeLeaderboardEntry, ...]:
        """Rank opted-in, enabled users for exactly one server condition."""

        maximum = _ranking_limit(limit)
        challenge_day = (
            challenge_date_at(self._now())
            if challenge_date is None
            else _challenge_date(challenge_date)
        )
        condition = self._condition_for(challenge_day)
        with self.database.read_session() as session:
            rows = session.execute(
                select(
                    User.username,
                    User.display_name,
                    DailyChallengeRun.score,
                    DailyChallengeRun.accepted_count,
                    DailyChallengeRun.finished_at,
                    DailyChallengeRun.id,
                )
                .join(
                    DailyChallengeRun,
                    DailyChallengeRun.user_id == User.id,
                )
                .where(
                    DailyChallengeRun.challenge_date == challenge_day,
                    DailyChallengeRun.condition_key
                    == condition.condition_key,
                    DailyChallengeRun.rules_version
                    == condition.rules_version,
                    DailyChallengeRun.status
                    == ScoreAttackStatus.FINISHED.value,
                    User.leaderboard_visible.is_(True),
                    User.disabled_at.is_(None),
                )
                .order_by(
                    DailyChallengeRun.score.desc(),
                    DailyChallengeRun.accepted_count.desc(),
                    DailyChallengeRun.finished_at.asc(),
                    User.username_key.asc(),
                    DailyChallengeRun.id.asc(),
                )
                .limit(maximum)
            ).all()

        entries: list[DailyChallengeLeaderboardEntry] = []
        previous_result: tuple[int, int] | None = None
        current_rank = 0
        for index, row in enumerate(rows, start=1):
            result = (int(row.score), int(row.accepted_count))
            if result != previous_result:
                current_rank = index
                previous_result = result
            finished_at = _stored_utc(row.finished_at)
            if finished_at is None:
                raise DailyChallengeProjectionError(
                    "finished ranking row has no finished_at"
                )
            entries.append(
                DailyChallengeLeaderboardEntry(
                    rank=current_rank,
                    display_name=row.display_name or row.username,
                    score=result[0],
                    accepted_count=result[1],
                    finished_at=finished_at,
                )
            )
        return tuple(entries)

    def _mutate_owned(
        self,
        *,
        user_id: str,
        run_id: str,
        expected_version: int,
        command: Callable[
            [DailyChallengeSession],
            SessionResult | None,
        ],
    ) -> DailyChallengeCommandResult:
        owner_id = _identifier(user_id, "user_id")
        identifier = _identifier(run_id, "run_id")
        expected = _state_version(expected_version)
        with self.database.transaction() as session:
            self._require_available_user(
                self.repository.lock_user(session, owner_id),
                owner_id,
            )
            run = self.repository.lock_run(session, identifier)
            self._authorize(run, owner_id, identifier)
            if run.state_version != expected:
                raise StaleDailyChallengeStateError(
                    expected,
                    run.state_version,
                )
            if run.status != ScoreAttackStatus.ACTIVE.value:
                raise DailyChallengeRunFinishedError(identifier)

            attack = self._restore(run)
            before = attack.to_snapshot()
            result = command(attack)
            after = attack.to_snapshot()
            if after != before:
                run = self._persist_changed(
                    session,
                    run,
                    attack,
                    expected_version=expected,
                )
            return DailyChallengeCommandResult(
                run=self._view(run, attack.condition),
                result=result,
            )

    def _finalize_expired_run(
        self,
        run_id: str,
    ) -> DailyChallengeRunView | None:
        with self.database.transaction() as session:
            run = self.repository.lock_run(session, run_id)
            if (
                run is None
                or run.status != ScoreAttackStatus.ACTIVE.value
            ):
                return None
            attack = self._restore(run)
            if attack.expire_if_due() is None:
                return None
            run = self._persist_changed(
                session,
                run,
                attack,
                expected_version=run.state_version,
            )
            return self._view(run, attack.condition)

    @staticmethod
    def _require_available_user(
        user: User | None,
        user_id: str,
    ) -> None:
        if user is None or user.disabled_at is not None:
            raise DailyChallengeUserUnavailableError(user_id)

    @staticmethod
    def _authorize(
        run: DailyChallengeRun | None,
        user_id: str,
        run_id: str,
    ) -> None:
        if run is None:
            raise DailyChallengeRunNotFoundError(run_id)
        if run.user_id != user_id:
            raise DailyChallengeRunOwnershipError(
                "daily challenge run belongs to another user"
            )

    def _condition_for(
        self,
        challenge_day: date,
    ) -> DailyChallengeCondition:
        day = _challenge_date(challenge_day)
        condition = self._condition_factory(day)
        if (
            not isinstance(condition, DailyChallengeCondition)
            or condition.challenge_date != day
            or condition.duration_seconds
            != DAILY_CHALLENGE_DURATION_SECONDS
        ):
            raise ValueError(
                "condition_factory returned an invalid daily condition"
            )
        return condition

    def _restore(
        self,
        run: DailyChallengeRun,
    ) -> DailyChallengeSession:
        try:
            condition = self._condition_for(run.challenge_date)
            snapshot = deepcopy(run.snapshot_json)
            attack = DailyChallengeSession.from_snapshot(
                snapshot,
                self.validator,
                clock=self._clock,
                expected_condition=condition,
            )
        except (TypeError, ValueError) as error:
            raise DailyChallengeProjectionError(
                "stored daily challenge snapshot is invalid"
            ) from error

        finish_reason = (
            attack.finish_reason.value
            if attack.finish_reason is not None
            else None
        )
        started_at = attack.started_at
        if (
            attack.status is ScoreAttackStatus.IDLE
            or started_at is None
            or run.challenge_date != condition.challenge_date
            or run.condition_key != condition.condition_key
            or run.status != attack.status.value
            or run.rules_version != condition.rules_version
            or run.duration_seconds != condition.duration_seconds
            or run.score != attack.score
            or run.accepted_count != attack.accepted_count
            or run.finish_reason != finish_reason
            or not _same_timestamp(run.started_at, started_at)
        ):
            raise DailyChallengeProjectionError(
                "stored daily challenge projections disagree"
            )

        finished_at = _snapshot_finished_at(snapshot)
        if attack.status is ScoreAttackStatus.ACTIVE:
            if (
                attack.deadline_at is None
                or not _same_timestamp(
                    run.deadline_at,
                    attack.deadline_at,
                )
                or run.finished_at is not None
                or finished_at is not None
            ):
                raise DailyChallengeProjectionError(
                    "stored active daily lifecycle disagrees"
                )
        elif (
            run.deadline_at is not None
            or finished_at is None
            or not _same_timestamp(run.finished_at, finished_at)
        ):
            raise DailyChallengeProjectionError(
                "stored finished daily lifecycle disagrees"
            )
        return attack

    def _persist_changed(
        self,
        session: Session,
        run: DailyChallengeRun,
        attack: DailyChallengeSession,
        *,
        expected_version: int,
    ) -> DailyChallengeRun:
        snapshot = attack.to_snapshot()
        projection = self._projection(attack, snapshot)
        next_version = expected_version + 1
        statement = (
            update(DailyChallengeRun)
            .where(
                DailyChallengeRun.id == run.id,
                DailyChallengeRun.state_version == expected_version,
                DailyChallengeRun.status
                == ScoreAttackStatus.ACTIVE.value,
            )
            .values(
                snapshot_json=snapshot,
                state_version=next_version,
                updated_at=self._now(),
                **projection,
            )
            .execution_options(synchronize_session=False)
        )
        outcome = session.execute(statement)
        if outcome.rowcount != 1:
            session.expire(run)
            session.refresh(run)
            raise StaleDailyChallengeStateError(
                expected_version,
                run.state_version,
            )
        session.flush()
        session.refresh(run)
        self._restore(run)
        return run

    @staticmethod
    def _projection(
        attack: DailyChallengeSession,
        snapshot: Mapping[str, object],
    ) -> dict[str, Any]:
        if (
            attack.status is ScoreAttackStatus.IDLE
            or attack.started_at is None
        ):
            raise DailyChallengeProjectionError(
                "idle daily challenge cannot be persisted"
            )
        finished_at = _snapshot_finished_at(snapshot)
        finish_reason = (
            attack.finish_reason.value
            if attack.finish_reason is not None
            else None
        )
        if attack.status is ScoreAttackStatus.ACTIVE:
            if attack.deadline_at is None or finished_at is not None:
                raise DailyChallengeProjectionError(
                    "active daily challenge has invalid lifecycle"
                )
            deadline_at = aware_utc(attack.deadline_at)
        else:
            if finished_at is None or finish_reason is None:
                raise DailyChallengeProjectionError(
                    "finished daily challenge has invalid lifecycle"
                )
            deadline_at = None
        return {
            "status": attack.status.value,
            "rules_version": attack.condition.rules_version,
            "duration_seconds": attack.condition.duration_seconds,
            "score": attack.score,
            "accepted_count": attack.accepted_count,
            "finish_reason": finish_reason,
            "started_at": aware_utc(attack.started_at),
            "deadline_at": deadline_at,
            "finished_at": finished_at,
        }

    def _view(
        self,
        run: DailyChallengeRun,
        condition: DailyChallengeCondition,
    ) -> DailyChallengeRunView:
        return DailyChallengeRunView(
            id=run.id,
            user_id=run.user_id,
            challenge_date=run.challenge_date,
            condition=condition,
            status=run.status,
            state_version=run.state_version,
            rules_version=run.rules_version,
            duration_seconds=run.duration_seconds,
            score=run.score,
            accepted_count=run.accepted_count,
            finish_reason=run.finish_reason,
            started_at=_stored_utc(run.started_at),
            deadline_at=_stored_utc(run.deadline_at),
            finished_at=_stored_utc(run.finished_at),
            snapshot=deepcopy(run.snapshot_json),
        )

    def _now(self) -> datetime:
        return aware_utc(self._clock(), "clock result")


__all__ = [
    "DailyChallengeCommandResult",
    "DailyChallengeLeaderboardEntry",
    "DailyChallengePersistenceError",
    "DailyChallengeProjectionError",
    "DailyChallengeRunFinishedError",
    "DailyChallengeRunNotFoundError",
    "DailyChallengeRunOwnershipError",
    "DailyChallengeRunRepository",
    "DailyChallengeRunView",
    "DailyChallengeUserUnavailableError",
    "SQLAlchemyDailyChallengeService",
    "StaleDailyChallengeStateError",
]
