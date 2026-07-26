"""Framework-independent, server-timed score attack domain state.

The mode deliberately composes :class:`~shiritori.game_session.GameSession`
instead of duplicating dictionary or shiritori rules.  A run is idle until
``start`` is called, then has one fixed three-minute deadline.  Scores are
always derived from the server-created history and are never accepted from a
browser.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from enum import Enum
from typing import Final

from .game_session import (
    DeadlinePolicy,
    GameSession,
    HistoryEntry,
    HistoryResult,
    PendingReading,
    SessionResult,
    SessionStatus,
)
from .lexicon import LexiconValidator


SCORE_ATTACK_DURATION_SECONDS: Final = 180
SCORE_RULES_VERSION: Final = 1
SCORE_ATTACK_SNAPSHOT_VERSION: Final = 1


class ScoreAttackStatus(str, Enum):
    """Lifecycle of one explicitly started score attack run."""

    IDLE = "idle"
    ACTIVE = "active"
    FINISHED = "finished"


class ScoreAttackFinishReason(str, Enum):
    """Terminal reason copied from the authoritative game session."""

    TIMEOUT = "timeout"
    ENDS_WITH_N = "ends_with_n"
    DUPLICATE = "duplicate"


class ScoreAttackError(RuntimeError):
    """Base error for invalid score attack commands."""


class ScoreAttackNotStartedError(ScoreAttackError):
    """Raised when a turn command is sent before explicit start."""


class ScoreAttackAlreadyStartedError(ScoreAttackError):
    """Raised when one run is started more than once."""


def points_for_entry(
    entry: HistoryEntry,
    *,
    prior_scored_words: int,
) -> int:
    """Return rules-version-1 points for one server-created history entry.

    Only an ordinary accepted word scores.  A word ending in ``ん`` remains in
    history for auditability but awards zero points.  The length and chain
    bonuses are capped so unusually long proper nouns and very long runs do
    not dominate without bound.
    """

    if (
        isinstance(prior_scored_words, bool)
        or not isinstance(prior_scored_words, int)
        or prior_scored_words < 0
    ):
        raise ValueError("prior_scored_words must be a non-negative integer")
    if entry.result is not HistoryResult.ACCEPTED:
        return 0
    return (
        10
        + 2 * min(len(entry.reading), 15)
        + min(2 * prior_scored_words, 20)
    )


def score_history(history: Iterable[HistoryEntry]) -> int:
    """Derive the total score without trusting a stored/client total."""

    score = 0
    scored_words = 0
    for entry in history:
        points = points_for_entry(
            entry,
            prior_scored_words=scored_words,
        )
        score += points
        if entry.result is HistoryResult.ACCEPTED:
            scored_words += 1
    return score


def count_scored_words(history: Iterable[HistoryEntry]) -> int:
    """Count ordinary accepted words, excluding an ``ん`` losing entry."""

    return sum(
        entry.result is HistoryResult.ACCEPTED
        for entry in history
    )


class ScoreAttackSession:
    """One explicit-start, fixed-three-minute score attack run."""

    def __init__(
        self,
        validator: LexiconValidator | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._validator = validator
        self._clock = clock
        self._game: GameSession | None = None

    @property
    def status(self) -> ScoreAttackStatus:
        if self._game is None:
            return ScoreAttackStatus.IDLE
        if self._game.status is SessionStatus.ACTIVE:
            return ScoreAttackStatus.ACTIVE
        return ScoreAttackStatus.FINISHED

    @property
    def finish_reason(self) -> ScoreAttackFinishReason | None:
        game = self._game
        if game is None or game.status is SessionStatus.ACTIVE:
            return None
        return _finish_reason(game.status)

    @property
    def started_at(self) -> datetime | None:
        return self._game.started_at if self._game is not None else None

    @property
    def deadline_at(self) -> datetime | None:
        return self._game.deadline_at if self._game is not None else None

    @property
    def history(self) -> tuple[HistoryEntry, ...]:
        return self._game.history if self._game is not None else ()

    @property
    def pending_reading(self) -> PendingReading | None:
        return (
            self._game.pending_reading
            if self._game is not None
            else None
        )

    @property
    def expected_kana(self) -> str | None:
        return (
            self._game.expected_kana
            if self._game is not None
            else None
        )

    @property
    def score(self) -> int:
        return score_history(self.history)

    @property
    def accepted_count(self) -> int:
        return count_scored_words(self.history)

    def remaining_seconds(self) -> float | None:
        if self._game is None:
            return None
        if self.status is ScoreAttackStatus.FINISHED:
            return 0.0
        return self._game.remaining_seconds()

    def start(self) -> ScoreAttackSession:
        """Start the authoritative clock exactly once."""

        if self._game is not None:
            raise ScoreAttackAlreadyStartedError(
                "score attack has already started"
            )
        self._game = GameSession(
            self._validator,
            time_limit_seconds=SCORE_ATTACK_DURATION_SECONDS,
            deadline_policy=DeadlinePolicy.FIXED_MATCH,
            clock=self._clock,
        )
        return self

    def submit(self, raw_surface: str | None) -> SessionResult:
        """Validate and apply one surface using the shared game rules."""

        return self._started_game().submit(raw_surface)

    def resolve_reading(self, reading: str) -> SessionResult:
        """Resolve a dictionary ambiguity while the fixed clock continues."""

        return self._started_game().resolve_reading(reading)

    def cancel_reading_choice(self) -> SessionResult:
        """Cancel a pending reading without changing time or score."""

        return self._started_game().cancel_reading_choice()

    def expire_if_due(self) -> SessionResult | None:
        """Finish at the server deadline; idle runs have no clock to expire."""

        if self._game is None:
            return None
        return self._game.expire_if_due()

    def to_snapshot(self) -> dict[str, object]:
        """Return a JSON-compatible, projection-checkable snapshot."""

        return {
            "snapshot_version": SCORE_ATTACK_SNAPSHOT_VERSION,
            "rules_version": SCORE_RULES_VERSION,
            "duration_seconds": SCORE_ATTACK_DURATION_SECONDS,
            "status": self.status.value,
            "finish_reason": (
                self.finish_reason.value
                if self.finish_reason is not None
                else None
            ),
            "score": self.score,
            "accepted_count": self.accepted_count,
            "game_session": (
                self._game.to_snapshot()
                if self._game is not None
                else None
            ),
        }

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Mapping[str, object],
        validator: LexiconValidator | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> ScoreAttackSession:
        """Restore a validated run without extending its fixed deadline."""

        try:
            version = _snapshot_integer(
                snapshot["snapshot_version"],
                "snapshot_version",
            )
            rules_version = _snapshot_integer(
                snapshot["rules_version"],
                "rules_version",
            )
            duration = _snapshot_integer(
                snapshot["duration_seconds"],
                "duration_seconds",
            )
            raw_status = snapshot["status"]
            if not isinstance(raw_status, str):
                raise ValueError
            status = ScoreAttackStatus(raw_status)
            raw_finish_reason = snapshot["finish_reason"]
            if raw_finish_reason is None:
                finish_reason = None
            elif isinstance(raw_finish_reason, str):
                finish_reason = ScoreAttackFinishReason(raw_finish_reason)
            else:
                raise ValueError
            stored_score = _snapshot_integer(
                snapshot["score"],
                "score",
            )
            stored_count = _snapshot_integer(
                snapshot["accepted_count"],
                "accepted_count",
            )
            raw_game = snapshot["game_session"]
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid score attack snapshot") from error

        if (
            version != SCORE_ATTACK_SNAPSHOT_VERSION
            or rules_version != SCORE_RULES_VERSION
            or duration != SCORE_ATTACK_DURATION_SECONDS
            or stored_score < 0
            or stored_count < 0
        ):
            raise ValueError("invalid score attack snapshot")

        attack = cls(validator, clock=clock)
        if status is ScoreAttackStatus.IDLE:
            if raw_game is not None or finish_reason is not None:
                raise ValueError("invalid idle score attack snapshot")
        else:
            if not isinstance(raw_game, Mapping):
                raise ValueError("started score attack requires game state")
            game = GameSession.from_snapshot(
                raw_game,
                validator,
                clock=clock,
            )
            if (
                game.time_limit_seconds
                != SCORE_ATTACK_DURATION_SECONDS
                or game.deadline_policy
                is not DeadlinePolicy.FIXED_MATCH
            ):
                raise ValueError(
                    "score attack game has invalid deadline settings"
                )
            attack._game = game

        if (
            attack.status is not status
            or attack.finish_reason is not finish_reason
            or attack.score != stored_score
            or attack.accepted_count != stored_count
        ):
            raise ValueError("score attack snapshot projections disagree")
        return attack

    def _started_game(self) -> GameSession:
        if self._game is None:
            raise ScoreAttackNotStartedError(
                "start score attack before submitting a word"
            )
        return self._game


def _finish_reason(status: SessionStatus) -> ScoreAttackFinishReason:
    mapping = {
        SessionStatus.LOST_BY_TIMEOUT: ScoreAttackFinishReason.TIMEOUT,
        SessionStatus.LOST_BY_N: ScoreAttackFinishReason.ENDS_WITH_N,
        SessionStatus.LOST_BY_DUPLICATE: ScoreAttackFinishReason.DUPLICATE,
    }
    try:
        return mapping[status]
    except KeyError as error:
        raise ValueError(
            f"session status is not terminal: {status.value}"
        ) from error


def _snapshot_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


__all__ = [
    "SCORE_ATTACK_DURATION_SECONDS",
    "SCORE_RULES_VERSION",
    "ScoreAttackAlreadyStartedError",
    "ScoreAttackError",
    "ScoreAttackFinishReason",
    "ScoreAttackNotStartedError",
    "ScoreAttackSession",
    "ScoreAttackStatus",
    "count_scored_words",
    "points_for_entry",
    "score_history",
]
