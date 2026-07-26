"""Framework-independent daily score challenge.

Every Japanese calendar day maps to one stable, server-owned starting word.
The starting word is inserted before play begins so all players inherit the
same first kana.  It is deliberately excluded from the player's score and
accepted-word count.

The actual word rules, duplicate detection, reading choices, and fixed
three-minute deadline remain owned by :class:`ScoreAttackSession`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from typing import Final
from zoneinfo import ZoneInfo

from .game_session import (
    HistoryEntry,
    HistoryResult,
    PendingReading,
    SessionCode,
    SessionResult,
    ending_chain_kana,
)
from .lexicon import (
    LexiconValidator,
    katakana_to_hiragana,
    normalize_surface,
)
from .score_attack import (
    SCORE_ATTACK_DURATION_SECONDS,
    ScoreAttackFinishReason,
    ScoreAttackSession,
    ScoreAttackStatus,
    count_scored_words,
    score_history,
)


DAILY_CHALLENGE_DURATION_SECONDS: Final = SCORE_ATTACK_DURATION_SECONDS
DAILY_CHALLENGE_RULES_VERSION: Final = 1
DAILY_CHALLENGE_SNAPSHOT_VERSION: Final = 1
JAPAN_TIME_ZONE: Final = ZoneInfo("Asia/Tokyo")

# Rules version 1 is frozen: do not edit, reorder, or append to this tuple.
# A vocabulary change requires a new versioned tuple and an incremented
# DAILY_CHALLENGE_RULES_VERSION.  The fingerprint below makes an accidental
# in-place edit fail closed instead of silently changing past conditions.
_DAILY_STARTING_WORDS: Final = (
    ("林檎", "りんご"),
    ("ゴリラ", "ごりら"),
    ("ラッパ", "らっぱ"),
    ("パンダ", "ぱんだ"),
    ("トマト", "とまと"),
    ("バナナ", "ばなな"),
    ("カメラ", "かめら"),
    ("テレビ", "てれび"),
    ("ピアノ", "ぴあの"),
    ("サラダ", "さらだ"),
)
_DAILY_STARTING_WORDS_V1_SHA256: Final = (
    "db9b6f997a499d30b8d02a38b0be4a5d0f6a4409ef227479bf2495981d03bc63"
)


class DailyChallengeError(RuntimeError):
    """Base error for invalid daily challenge state."""


class DailyChallengeConfigurationError(DailyChallengeError):
    """Raised when the pinned starting word no longer validates exactly."""


@dataclass(frozen=True, slots=True)
class DailyChallengeCondition:
    """Versioned condition shared by all players on one JST date."""

    challenge_date: date
    rules_version: int
    duration_seconds: int
    start_surface: str
    start_reading: str
    condition_key: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.challenge_date, date)
            or isinstance(self.challenge_date, datetime)
        ):
            raise ValueError("challenge_date must be a date")
        if (
            type(self.rules_version) is not int
            or self.rules_version != DAILY_CHALLENGE_RULES_VERSION
        ):
            raise ValueError("unsupported daily challenge rules_version")
        if self.duration_seconds != DAILY_CHALLENGE_DURATION_SECONDS:
            raise ValueError("daily challenge duration must be 180 seconds")

        surface = normalize_surface(self.start_surface)
        reading = katakana_to_hiragana(
            normalize_surface(self.start_reading)
        )
        if (
            surface != self.start_surface
            or not surface
            or len(surface) > 30
        ):
            raise ValueError("invalid daily starting surface")
        if (
            reading != self.start_reading
            or not reading
            or len(reading) > 60
        ):
            raise ValueError("invalid daily starting reading")
        try:
            expected_kana = ending_chain_kana(reading)
        except ValueError as error:
            raise ValueError("invalid daily starting reading") from error
        if expected_kana == "ん":
            raise ValueError("daily starting word cannot end with ん")

        expected_key = _condition_key(
            self.challenge_date,
            rules_version=self.rules_version,
            duration_seconds=self.duration_seconds,
            start_surface=surface,
            start_reading=reading,
        )
        if self.condition_key != expected_key:
            raise ValueError("daily condition key does not match its fields")

    @property
    def expected_kana(self) -> str:
        """Kana which the first player-entered word must begin with."""

        return ending_chain_kana(self.start_reading)

    @classmethod
    def create(
        cls,
        challenge_date: date,
        start_surface: str,
        start_reading: str,
        *,
        rules_version: int = DAILY_CHALLENGE_RULES_VERSION,
        duration_seconds: int = DAILY_CHALLENGE_DURATION_SECONDS,
    ) -> "DailyChallengeCondition":
        """Build a condition with its integrity key derived server-side."""

        normalized_surface = normalize_surface(start_surface)
        normalized_reading = katakana_to_hiragana(
            normalize_surface(start_reading)
        )
        return cls(
            challenge_date=challenge_date,
            rules_version=rules_version,
            duration_seconds=duration_seconds,
            start_surface=normalized_surface,
            start_reading=normalized_reading,
            condition_key=_condition_key(
                challenge_date,
                rules_version=rules_version,
                duration_seconds=duration_seconds,
                start_surface=normalized_surface,
                start_reading=normalized_reading,
            ),
        )

    def to_snapshot(self) -> dict[str, object]:
        return {
            "challenge_date": self.challenge_date.isoformat(),
            "rules_version": self.rules_version,
            "duration_seconds": self.duration_seconds,
            "start_surface": self.start_surface,
            "start_reading": self.start_reading,
            "condition_key": self.condition_key,
        }

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Mapping[str, object],
    ) -> "DailyChallengeCondition":
        try:
            raw_date = snapshot["challenge_date"]
            if not isinstance(raw_date, str):
                raise ValueError
            challenge_date = date.fromisoformat(raw_date)
            rules_version = _snapshot_integer(
                snapshot["rules_version"],
                "rules_version",
            )
            duration_seconds = _snapshot_integer(
                snapshot["duration_seconds"],
                "duration_seconds",
            )
            start_surface = _snapshot_text(
                snapshot["start_surface"],
                "start_surface",
            )
            start_reading = _snapshot_text(
                snapshot["start_reading"],
                "start_reading",
            )
            condition_key = _snapshot_text(
                snapshot["condition_key"],
                "condition_key",
            )
            return cls(
                challenge_date=challenge_date,
                rules_version=rules_version,
                duration_seconds=duration_seconds,
                start_surface=start_surface,
                start_reading=start_reading,
                condition_key=condition_key,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid daily challenge condition") from error


def challenge_date_at(now: datetime) -> date:
    """Return the authoritative Japanese calendar date for ``now``."""

    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(JAPAN_TIME_ZONE).date()


def daily_condition_for(challenge_date: date) -> DailyChallengeCondition:
    """Derive the stable rules-version-1 condition for one JST date."""

    if (
        not isinstance(challenge_date, date)
        or isinstance(challenge_date, datetime)
    ):
        raise ValueError("challenge_date must be a date")
    vocabulary_fingerprint = sha256(
        "\n".join(
            f"{surface}\t{reading}"
            for surface, reading in _DAILY_STARTING_WORDS
        ).encode("utf-8")
    ).hexdigest()
    if vocabulary_fingerprint != _DAILY_STARTING_WORDS_V1_SHA256:
        raise DailyChallengeConfigurationError(
            "daily rules v1 starting words were modified without a version bump"
        )
    selection = sha256(
        (
            f"shiritori-daily-v{DAILY_CHALLENGE_RULES_VERSION}:"
            f"{challenge_date.isoformat()}"
        ).encode("utf-8")
    ).digest()
    surface, reading = _DAILY_STARTING_WORDS[
        int.from_bytes(selection[:8], "big")
        % len(_DAILY_STARTING_WORDS)
    ]
    return DailyChallengeCondition.create(
        challenge_date,
        surface,
        reading,
    )


class DailyChallengeSession:
    """One daily attempt composed from the existing score-attack domain."""

    def __init__(
        self,
        condition: DailyChallengeCondition,
        validator: LexiconValidator | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(condition, DailyChallengeCondition):
            raise TypeError("condition must be a DailyChallengeCondition")
        self.condition = condition
        self._validator = validator
        self._clock = clock
        self._attack = ScoreAttackSession(validator, clock=clock)

    @property
    def status(self) -> ScoreAttackStatus:
        return self._attack.status

    @property
    def finish_reason(self) -> ScoreAttackFinishReason | None:
        return self._attack.finish_reason

    @property
    def started_at(self) -> datetime | None:
        return self._attack.started_at

    @property
    def deadline_at(self) -> datetime | None:
        return self._attack.deadline_at

    @property
    def history(self) -> tuple[HistoryEntry, ...]:
        """Player-entered history, excluding the server-owned starting word."""

        if self.status is ScoreAttackStatus.IDLE:
            return ()
        return self._attack.history[1:]

    @property
    def pending_reading(self) -> PendingReading | None:
        return self._attack.pending_reading

    @property
    def expected_kana(self) -> str | None:
        return self._attack.expected_kana

    @property
    def score(self) -> int:
        return score_history(self.history)

    @property
    def accepted_count(self) -> int:
        return count_scored_words(self.history)

    def remaining_seconds(self) -> float | None:
        return self._attack.remaining_seconds()

    def start(self) -> "DailyChallengeSession":
        """Start once and insert the day's trusted seed before user play."""

        self._attack.start()
        result = self._attack.submit(self.condition.start_surface)
        if result.code is SessionCode.READING_CHOICE_REQUIRED:
            if self.condition.start_reading not in result.reading_choices:
                raise DailyChallengeConfigurationError(
                    "daily starting reading is not offered by the dictionary"
                )
            result = self._attack.resolve_reading(
                self.condition.start_reading
            )
        if (
            result.code is not SessionCode.ACCEPTED
            or result.entry is None
            or result.entry.surface != self.condition.start_surface
            or result.entry.reading != self.condition.start_reading
            or result.entry.result is not HistoryResult.ACCEPTED
        ):
            raise DailyChallengeConfigurationError(
                "daily starting word did not validate exactly"
            )
        self._validate_seed()
        return self

    def submit(self, raw_surface: str | None) -> SessionResult:
        return self._attack.submit(raw_surface)

    def resolve_reading(self, reading: str) -> SessionResult:
        return self._attack.resolve_reading(reading)

    def cancel_reading_choice(self) -> SessionResult:
        return self._attack.cancel_reading_choice()

    def expire_if_due(self) -> SessionResult | None:
        return self._attack.expire_if_due()

    def to_snapshot(self) -> dict[str, object]:
        return {
            "snapshot_version": DAILY_CHALLENGE_SNAPSHOT_VERSION,
            "rules_version": self.condition.rules_version,
            "duration_seconds": self.condition.duration_seconds,
            "condition": self.condition.to_snapshot(),
            "status": self.status.value,
            "finish_reason": (
                self.finish_reason.value
                if self.finish_reason is not None
                else None
            ),
            "score": self.score,
            "accepted_count": self.accepted_count,
            "score_attack": self._attack.to_snapshot(),
        }

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Mapping[str, object],
        validator: LexiconValidator | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        expected_condition: DailyChallengeCondition | None = None,
    ) -> "DailyChallengeSession":
        try:
            version = _snapshot_integer(
                snapshot["snapshot_version"],
                "snapshot_version",
            )
            rules_version = _snapshot_integer(
                snapshot["rules_version"],
                "rules_version",
            )
            duration_seconds = _snapshot_integer(
                snapshot["duration_seconds"],
                "duration_seconds",
            )
            raw_condition = snapshot["condition"]
            if not isinstance(raw_condition, Mapping):
                raise ValueError
            condition = DailyChallengeCondition.from_snapshot(raw_condition)
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
            stored_score = _snapshot_integer(snapshot["score"], "score")
            stored_count = _snapshot_integer(
                snapshot["accepted_count"],
                "accepted_count",
            )
            raw_attack = snapshot["score_attack"]
            if not isinstance(raw_attack, Mapping):
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid daily challenge snapshot") from error

        if (
            version != DAILY_CHALLENGE_SNAPSHOT_VERSION
            or rules_version != condition.rules_version
            or duration_seconds != condition.duration_seconds
            or stored_score < 0
            or stored_count < 0
            or (
                expected_condition is not None
                and condition != expected_condition
            )
        ):
            raise ValueError("invalid daily challenge snapshot")

        daily = cls(condition, validator, clock=clock)
        try:
            daily._attack = ScoreAttackSession.from_snapshot(
                raw_attack,
                validator,
                clock=clock,
            )
            if daily.status is not ScoreAttackStatus.IDLE:
                daily._validate_seed()
        except (DailyChallengeConfigurationError, ValueError) as error:
            raise ValueError("invalid daily challenge snapshot") from error

        if (
            daily.status is not status
            or daily.finish_reason is not finish_reason
            or daily.score != stored_score
            or daily.accepted_count != stored_count
        ):
            raise ValueError("daily challenge snapshot projections disagree")
        return daily

    def _validate_seed(self) -> None:
        history = self._attack.history
        if not history:
            raise DailyChallengeConfigurationError(
                "started daily challenge has no starting word"
            )
        seed = history[0]
        if (
            seed.turn_number != 1
            or seed.surface != self.condition.start_surface
            or seed.reading != self.condition.start_reading
            or seed.canonical_key != self.condition.start_reading
            or seed.result is not HistoryResult.ACCEPTED
        ):
            raise DailyChallengeConfigurationError(
                "daily starting word does not match its condition"
            )


def _condition_key(
    challenge_date: date,
    *,
    rules_version: int,
    duration_seconds: int,
    start_surface: str,
    start_reading: str,
) -> str:
    payload = "\x1f".join(
        (
            str(rules_version),
            str(duration_seconds),
            challenge_date.isoformat(),
            start_surface,
            start_reading,
        )
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _snapshot_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _snapshot_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be non-empty text")
    return value


def aware_utc(value: datetime, field: str = "timestamp") -> datetime:
    """Normalize an aware timestamp for persistence adapters."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "DAILY_CHALLENGE_DURATION_SECONDS",
    "DAILY_CHALLENGE_RULES_VERSION",
    "DailyChallengeCondition",
    "DailyChallengeConfigurationError",
    "DailyChallengeError",
    "DailyChallengeSession",
    "JAPAN_TIME_ZONE",
    "aware_utc",
    "challenge_date_at",
    "daily_condition_for",
]
