"""Transactional persistence for authoritative score attack runs.

Only a validated :class:`ScoreAttackSession` may change the stored snapshot or
its derived score projections.  Browser values are limited to the requested
command and an optimistic state version.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import Database
from .game_session import SessionResult
from .lexicon import LexiconValidator
from .models import ScoreAttackRun, User, new_id, utc_now
from .score_attack import (
    SCORE_ATTACK_DURATION_SECONDS,
    SCORE_RULES_VERSION,
    ScoreAttackSession,
    ScoreAttackStatus,
)


class ScoreAttackPersistenceError(RuntimeError):
    """Base class for persistence and authorization failures."""


class ScoreAttackUserNotFoundError(LookupError):
    """Raised when a run is requested for an unknown account."""


class ScoreAttackRunNotFoundError(LookupError):
    """Raised when a run ID does not exist."""


class ScoreAttackRunOwnershipError(PermissionError):
    """Raised when an account attempts to access another user's run."""


class ScoreAttackActiveRunExistsError(ScoreAttackPersistenceError):
    """Raised when one user already owns a non-expired active run."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"an active score attack run already exists: {run_id}")
        self.run_id = run_id


class ScoreAttackRunFinishedError(ScoreAttackPersistenceError):
    """Raised when a command targets an immutable finished run."""


class StaleScoreAttackStateError(ScoreAttackPersistenceError):
    """Raised before a command when the browser state version is stale."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            "score attack state changed: "
            f"expected version {expected}, current version {actual}"
        )
        self.expected = expected
        self.actual = actual


class ScoreAttackProjectionError(ScoreAttackPersistenceError):
    """Raised when stored projections disagree with the domain snapshot."""


@dataclass(frozen=True, slots=True)
class ScoreAttackRunView:
    """Detached immutable view safe to return outside a DB transaction."""

    id: str
    user_id: str
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
class ScoreAttackCommandResult:
    """Authoritative command result paired with the resulting DB state."""

    run: ScoreAttackRunView
    result: SessionResult | None


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


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 1_000:
        raise ValueError("limit must be an integer from 1 to 1000")
    return value


def _aware_utc(value: datetime, name: str = "timestamp") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


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
        _aware_utc(projected) if projected is not None else None
    )
    return normalized_stored == normalized_projected


def _snapshot_finished_at(snapshot: Mapping[str, object]) -> datetime | None:
    game = snapshot.get("game_session")
    if not isinstance(game, Mapping):
        raise ScoreAttackProjectionError(
            "started score attack snapshot has no game session"
        )
    raw_value = game.get("ended_at")
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise ScoreAttackProjectionError("snapshot ended_at is invalid")
    try:
        parsed = datetime.fromisoformat(raw_value)
        return _aware_utc(parsed, "snapshot ended_at")
    except ValueError as error:
        raise ScoreAttackProjectionError(
            "snapshot ended_at is invalid"
        ) from error


class ScoreAttackRunRepository:
    """Small SQLAlchemy row repository used by the domain service."""

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
    ) -> ScoreAttackRun | None:
        return session.scalar(
            select(ScoreAttackRun)
            .where(ScoreAttackRun.id == run_id)
            .with_for_update()
        )

    @staticmethod
    def lock_active_run(
        session: Session,
        user_id: str,
    ) -> ScoreAttackRun | None:
        return session.scalar(
            select(ScoreAttackRun)
            .where(
                ScoreAttackRun.user_id == user_id,
                ScoreAttackRun.status == ScoreAttackStatus.ACTIVE.value,
            )
            .order_by(ScoreAttackRun.started_at.desc())
            .with_for_update()
        )

    def active_run_id(self, user_id: str) -> str | None:
        with self.database.read_session() as session:
            return session.scalar(
                select(ScoreAttackRun.id).where(
                    ScoreAttackRun.user_id == user_id,
                    ScoreAttackRun.status
                    == ScoreAttackStatus.ACTIVE.value,
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
                    select(ScoreAttackRun.id)
                    .where(
                        ScoreAttackRun.status
                        == ScoreAttackStatus.ACTIVE.value,
                        ScoreAttackRun.deadline_at <= now,
                    )
                    .order_by(
                        ScoreAttackRun.deadline_at.asc(),
                        ScoreAttackRun.id.asc(),
                    )
                    .limit(limit)
                )
            )


class SQLAlchemyScoreAttackService:
    """Persist score attack domain commands under row lock and CAS."""

    def __init__(
        self,
        database: Database,
        validator: LexiconValidator | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        repository: ScoreAttackRunRepository | None = None,
    ) -> None:
        self.database = database
        self.validator = validator
        self._clock = clock or utc_now
        self.repository = repository or ScoreAttackRunRepository(database)
        if self.repository.database is not database:
            raise ValueError("repository must use the same database")

    def start(self, user_id: str) -> ScoreAttackRunView:
        """Explicitly create one active run, finalizing an expired one first."""

        owner_id = _identifier(user_id, "user_id")
        try:
            with self.database.transaction() as session:
                user = self.repository.lock_user(session, owner_id)
                if user is None:
                    raise ScoreAttackUserNotFoundError(owner_id)

                existing = self.repository.lock_active_run(
                    session,
                    owner_id,
                )
                if existing is not None:
                    existing_attack = self._restore(existing)
                    expired = existing_attack.expire_if_due()
                    if expired is None:
                        raise ScoreAttackActiveRunExistsError(existing.id)
                    self._persist_changed(
                        session,
                        existing,
                        existing_attack,
                        expected_version=existing.state_version,
                    )

                attack = ScoreAttackSession(
                    self.validator,
                    clock=self._clock,
                ).start()
                snapshot = attack.to_snapshot()
                projection = self._projection(attack, snapshot)
                created_at = projection["started_at"]
                run = ScoreAttackRun(
                    id=new_id(),
                    user_id=owner_id,
                    snapshot_json=snapshot,
                    state_version=0,
                    created_at=created_at,
                    updated_at=created_at,
                    **projection,
                )
                session.add(run)
                session.flush()
                self._restore(run)
                return self._view(run)
        except IntegrityError as error:
            active_run_id = self.repository.active_run_id(owner_id)
            if active_run_id is not None:
                raise ScoreAttackActiveRunExistsError(
                    active_run_id
                ) from error
            raise

    def get(
        self,
        user_id: str,
        run_id: str,
    ) -> ScoreAttackRunView:
        """Read one owned run without mutating or extending its deadline."""

        owner_id = _identifier(user_id, "user_id")
        identifier = _identifier(run_id, "run_id")
        with self.database.read_session() as session:
            run = session.get(ScoreAttackRun, identifier)
            self._authorize(run, owner_id, identifier)
            self._restore(run)
            return self._view(run)

    def resume_active(
        self,
        user_id: str,
    ) -> ScoreAttackRunView | None:
        """Restore the user's active run and timeout it if already due."""

        owner_id = _identifier(user_id, "user_id")
        with self.database.transaction() as session:
            run = self.repository.lock_active_run(session, owner_id)
            if run is None:
                return None
            attack = self._restore(run)
            if attack.expire_if_due() is not None:
                run = self._persist_changed(
                    session,
                    run,
                    attack,
                    expected_version=run.state_version,
                )
            return self._view(run)

    def submit(
        self,
        *,
        user_id: str,
        run_id: str,
        surface: str | None,
        expected_version: int,
    ) -> ScoreAttackCommandResult:
        """Validate and apply one surface to the authoritative DB snapshot."""

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
    ) -> ScoreAttackCommandResult:
        """Resolve one pending dictionary reading under the same CAS."""

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
    ) -> ScoreAttackCommandResult:
        """Cancel pending ambiguity without accepting client state."""

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
    ) -> ScoreAttackCommandResult:
        """Apply an authoritative timeout when the fixed deadline is due."""

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
    ) -> tuple[ScoreAttackRunView, ...]:
        """Finalize every overdue row in bounded startup batches."""

        maximum = _limit(limit)
        now = self._now()
        finalized: list[ScoreAttackRunView] = []
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

    def _mutate_owned(
        self,
        *,
        user_id: str,
        run_id: str,
        expected_version: int,
        command: Callable[
            [ScoreAttackSession],
            SessionResult | None,
        ],
    ) -> ScoreAttackCommandResult:
        owner_id = _identifier(user_id, "user_id")
        identifier = _identifier(run_id, "run_id")
        expected = _state_version(expected_version)
        with self.database.transaction() as session:
            run = self.repository.lock_run(session, identifier)
            self._authorize(run, owner_id, identifier)
            if run.state_version != expected:
                raise StaleScoreAttackStateError(
                    expected,
                    run.state_version,
                )
            if run.status != ScoreAttackStatus.ACTIVE.value:
                raise ScoreAttackRunFinishedError(identifier)

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
            return ScoreAttackCommandResult(
                run=self._view(run),
                result=result,
            )

    def _finalize_expired_run(
        self,
        run_id: str,
    ) -> ScoreAttackRunView | None:
        with self.database.transaction() as session:
            run = self.repository.lock_run(session, run_id)
            if (
                run is None
                or run.status != ScoreAttackStatus.ACTIVE.value
            ):
                return None
            attack = self._restore(run)
            result = attack.expire_if_due()
            if result is None:
                return None
            run = self._persist_changed(
                session,
                run,
                attack,
                expected_version=run.state_version,
            )
            return self._view(run)

    @staticmethod
    def _authorize(
        run: ScoreAttackRun | None,
        user_id: str,
        run_id: str,
    ) -> None:
        if run is None:
            raise ScoreAttackRunNotFoundError(run_id)
        if run.user_id != user_id:
            raise ScoreAttackRunOwnershipError(
                "score attack run belongs to another user"
            )

    def _restore(self, run: ScoreAttackRun) -> ScoreAttackSession:
        try:
            snapshot = deepcopy(run.snapshot_json)
            attack = ScoreAttackSession.from_snapshot(
                snapshot,
                self.validator,
                clock=self._clock,
            )
        except (TypeError, ValueError) as error:
            raise ScoreAttackProjectionError(
                "stored score attack snapshot is invalid"
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
            or run.status != attack.status.value
            or run.rules_version != SCORE_RULES_VERSION
            or run.duration_seconds != SCORE_ATTACK_DURATION_SECONDS
            or run.score != attack.score
            or run.accepted_count != attack.accepted_count
            or run.finish_reason != finish_reason
            or not _same_timestamp(run.started_at, started_at)
        ):
            raise ScoreAttackProjectionError(
                "stored score attack projections disagree"
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
                raise ScoreAttackProjectionError(
                    "stored active score attack lifecycle disagrees"
                )
        elif (
            run.deadline_at is not None
            or finished_at is None
            or not _same_timestamp(run.finished_at, finished_at)
        ):
            raise ScoreAttackProjectionError(
                "stored finished score attack lifecycle disagrees"
            )
        return attack

    def _persist_changed(
        self,
        session: Session,
        run: ScoreAttackRun,
        attack: ScoreAttackSession,
        *,
        expected_version: int,
    ) -> ScoreAttackRun:
        snapshot = attack.to_snapshot()
        projection = self._projection(attack, snapshot)
        next_version = expected_version + 1
        statement = (
            update(ScoreAttackRun)
            .where(
                ScoreAttackRun.id == run.id,
                ScoreAttackRun.state_version == expected_version,
                ScoreAttackRun.status == ScoreAttackStatus.ACTIVE.value,
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
            raise StaleScoreAttackStateError(
                expected_version,
                run.state_version,
            )
        session.flush()
        session.refresh(run)
        self._restore(run)
        return run

    @staticmethod
    def _projection(
        attack: ScoreAttackSession,
        snapshot: Mapping[str, object],
    ) -> dict[str, Any]:
        if (
            attack.status is ScoreAttackStatus.IDLE
            or attack.started_at is None
        ):
            raise ScoreAttackProjectionError(
                "idle score attack cannot be persisted"
            )
        finished_at = _snapshot_finished_at(snapshot)
        finish_reason = (
            attack.finish_reason.value
            if attack.finish_reason is not None
            else None
        )
        if attack.status is ScoreAttackStatus.ACTIVE:
            if attack.deadline_at is None or finished_at is not None:
                raise ScoreAttackProjectionError(
                    "active score attack has invalid lifecycle"
                )
            deadline_at = _aware_utc(attack.deadline_at)
        else:
            if finished_at is None or finish_reason is None:
                raise ScoreAttackProjectionError(
                    "finished score attack has invalid lifecycle"
                )
            deadline_at = None
        return {
            "status": attack.status.value,
            "rules_version": SCORE_RULES_VERSION,
            "duration_seconds": SCORE_ATTACK_DURATION_SECONDS,
            "score": attack.score,
            "accepted_count": attack.accepted_count,
            "finish_reason": finish_reason,
            "started_at": _aware_utc(attack.started_at),
            "deadline_at": deadline_at,
            "finished_at": finished_at,
        }

    def _view(self, run: ScoreAttackRun) -> ScoreAttackRunView:
        return ScoreAttackRunView(
            id=run.id,
            user_id=run.user_id,
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
        return _aware_utc(self._clock(), "clock result")


__all__ = [
    "SQLAlchemyScoreAttackService",
    "ScoreAttackActiveRunExistsError",
    "ScoreAttackCommandResult",
    "ScoreAttackPersistenceError",
    "ScoreAttackProjectionError",
    "ScoreAttackRunFinishedError",
    "ScoreAttackRunNotFoundError",
    "ScoreAttackRunOwnershipError",
    "ScoreAttackRunRepository",
    "ScoreAttackRunView",
    "ScoreAttackUserNotFoundError",
    "StaleScoreAttackStateError",
]
