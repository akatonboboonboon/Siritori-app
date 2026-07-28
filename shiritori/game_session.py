"""Dictionary-backed shiritori game state.

This module is independent from NiceGUI and persistence adapters.  It accepts
only dictionary results produced by :class:`~shiritori.lexicon.LexiconValidator`
and keeps the authoritative history, duplicate set, and turn deadline.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from shiritori.lexicon import (
    LexiconCandidate,
    LexiconCode,
    LexiconResult,
    LexiconValidator,
    get_default_validator,
    katakana_to_hiragana,
    normalize_surface,
)


LEGACY_SNAPSHOT_VERSION = 1
PREVIOUS_SNAPSHOT_VERSION = 2
SNAPSHOT_VERSION = 3

_SMALL_TO_LARGE_KANA = {
    "ぁ": "あ",
    "ぃ": "い",
    "ぅ": "う",
    "ぇ": "え",
    "ぉ": "お",
    "っ": "つ",
    "ゃ": "や",
    "ゅ": "ゆ",
    "ょ": "よ",
    "ゎ": "わ",
    "ゕ": "か",
    "ゖ": "け",
}

_LEGACY_DAKUON_CHAIN_EQUIVALENTS = {
    "ぢ": "じ",
    "づ": "ず",
}

_DAKUON_CHAIN_EQUIVALENTS = {
    **_LEGACY_DAKUON_CHAIN_EQUIVALENTS,
    "ゔ": "ぶ",
}

_VU_MORA_CHAIN_EQUIVALENTS = {
    "ゔぁ": "ば",
    "ゔぃ": "び",
    "ゔぇ": "べ",
    "ゔぉ": "ぼ",
}

_VOWEL_GROUPS = {
    "あ": "あぁかがさざただなはばぱまやゃらわゎゕ",
    "い": "いぃきぎしじちぢにひびぴみりゐ",
    "う": "うぅくぐすずつづっぬふぶぷむゆゅるゔ",
    "え": "えぇけげせぜてでねへべぺめれゑゖ",
    "お": "おぉこごそぞとどのほぼぽもよょろを",
}
_VOWEL_BY_KANA = {
    kana: vowel
    for vowel, kana_group in _VOWEL_GROUPS.items()
    for kana in kana_group
}


class SessionStatus(str, Enum):
    """Overall status of one match."""

    ACTIVE = "active"
    LOST_BY_N = "lost_by_n"
    LOST_BY_DUPLICATE = "lost_by_duplicate"
    LOST_BY_TIMEOUT = "lost_by_timeout"


class DeadlinePolicy(str, Enum):
    """How a configured deadline advances during one session."""

    PER_TURN = "per_turn"
    FIXED_MATCH = "fixed_match"


class SessionCode(str, Enum):
    """Machine-readable result of an operation."""

    ACCEPTED = "accepted"
    READING_CHOICE_REQUIRED = "reading_choice_required"
    LEXICON_REJECTED = "lexicon_rejected"
    INVALID_LEXICON_RESULT = "invalid_lexicon_result"
    NOT_CHAINED = "not_chained"
    DUPLICATE = "duplicate"
    ENDS_WITH_N = "ends_with_n"
    INVALID_READING_CHOICE = "invalid_reading_choice"
    NO_READING_CHOICE_PENDING = "no_reading_choice_pending"
    READING_CHOICE_CANCELLED = "reading_choice_cancelled"
    TIMED_OUT = "timed_out"
    GAME_ALREADY_OVER = "game_already_over"


class HistoryResult(str, Enum):
    """Result saved with an accepted word."""

    ACCEPTED = "accepted"
    ENDS_WITH_N = "ends_with_n"


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One accepted word in display and database order."""

    surface: str
    reading: str
    canonical_key: str
    turn_number: int
    timestamp: datetime
    result: HistoryResult

    def to_snapshot(self) -> dict[str, object]:
        return {
            "surface": self.surface,
            "reading": self.reading,
            "canonical_key": self.canonical_key,
            "turn_number": self.turn_number,
            "timestamp": self.timestamp.isoformat(),
            "result": self.result.value,
        }

    @classmethod
    def from_snapshot(cls, data: Mapping[str, object]) -> HistoryEntry:
        try:
            surface = _snapshot_text(data["surface"], "surface")
            reading = _snapshot_text(data["reading"], "reading")
            canonical_key = _snapshot_text(
                data["canonical_key"], "canonical_key"
            )
            raw_turn_number = data["turn_number"]
            if (
                isinstance(raw_turn_number, bool)
                or not isinstance(raw_turn_number, int)
            ):
                raise ValueError
            turn_number = raw_turn_number
            timestamp = _parse_datetime(data["timestamp"], "timestamp")
            raw_result = data["result"]
            if not isinstance(raw_result, str):
                raise ValueError
            result = HistoryResult(raw_result)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid history entry in snapshot") from error

        if normalize_surface(surface) != surface or turn_number < 1:
            raise ValueError("invalid history entry in snapshot")
        return cls(
            surface=surface,
            reading=reading,
            canonical_key=canonical_key,
            turn_number=turn_number,
            timestamp=timestamp,
            result=result,
        )


@dataclass(frozen=True, slots=True)
class PendingReading:
    """An ambiguous dictionary result waiting for an explicit choice."""

    surface: str
    candidates: tuple[LexiconCandidate, ...]
    submitted_at: datetime

    @property
    def readings(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                _normalized_reading(candidate.reading)
                for candidate in self.candidates
            )
        )

    def candidates_for_reading(
        self, reading: str
    ) -> tuple[LexiconCandidate, ...]:
        normalized = _normalized_reading(reading)
        return tuple(
            candidate
            for candidate in self.candidates
            if _normalized_reading(candidate.reading) == normalized
        )

    def to_snapshot(self) -> dict[str, object]:
        return {
            "surface": self.surface,
            "submitted_at": self.submitted_at.isoformat(),
            "candidates": [
                _candidate_to_snapshot(candidate)
                for candidate in self.candidates
            ],
        }

    @classmethod
    def from_snapshot(cls, data: Mapping[str, object]) -> PendingReading:
        try:
            surface = _snapshot_text(data["surface"], "surface")
            submitted_at = _parse_datetime(
                data["submitted_at"], "submitted_at"
            )
            raw_candidates = data["candidates"]
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid pending reading in snapshot") from error

        if (
            normalize_surface(surface) != surface
            or not isinstance(raw_candidates, list)
            or not raw_candidates
        ):
            raise ValueError("invalid pending reading in snapshot")
        candidates = tuple(
            _candidate_from_snapshot(candidate)
            for candidate in raw_candidates
        )
        if any(candidate.surface != surface for candidate in candidates):
            raise ValueError(
                "pending candidate surface must match pending surface"
            )
        pending = cls(
            surface=surface,
            candidates=candidates,
            submitted_at=submitted_at,
        )
        if len(pending.readings) < 2:
            raise ValueError(
                "pending reading must contain at least two readings"
            )
        return pending


@dataclass(frozen=True, slots=True)
class SessionResult:
    """Outcome returned by submit, reading selection, or timeout checks."""

    code: SessionCode
    message: str
    surface: str = ""
    reading: str | None = None
    accepted: bool = False
    game_over: bool = False
    entry: HistoryEntry | None = None
    reading_choices: tuple[str, ...] = ()
    lexicon_result: LexiconResult | None = None


def canonical_kana(kana: str) -> str:
    """Return the canonical kana used only for shiritori connections."""

    if kana in _VU_MORA_CHAIN_EQUIVALENTS:
        return _VU_MORA_CHAIN_EQUIVALENTS[kana]
    expanded = _SMALL_TO_LARGE_KANA.get(kana, kana)
    return _DAKUON_CHAIN_EQUIVALENTS.get(expanded, expanded)


def first_chain_kana(reading: str) -> str:
    """Return the normalized first kana used for a connection check."""

    normalized = _normalized_reading(reading)
    if not normalized or normalized[0] == "ー":
        raise ValueError("reading has no usable first kana")
    for alternate, canonical in _VU_MORA_CHAIN_EQUIVALENTS.items():
        if normalized.startswith(alternate):
            return canonical
    return canonical_kana(normalized[0])


def ending_chain_kana(reading: str) -> str:
    """Return the kana which must start the next word.

    When a reading ends in one or more long sound marks, the vowel of the
    preceding mora is returned.  For example, ``こーひー`` resolves to ``い``.
    """

    normalized = _normalized_reading(reading)
    if not normalized:
        raise ValueError("reading has no usable ending kana")

    if normalized[-1] != "ー":
        for alternate, canonical in _VU_MORA_CHAIN_EQUIVALENTS.items():
            if normalized.endswith(alternate):
                return canonical
        return canonical_kana(normalized[-1])

    index = len(normalized) - 2
    while index >= 0 and normalized[index] == "ー":
        index -= 1
    if index < 0:
        raise ValueError("reading cannot consist only of long sound marks")

    vowel = _VOWEL_BY_KANA.get(normalized[index])
    if vowel is None:
        raise ValueError("long sound mark does not follow a vowel-bearing kana")
    return vowel


def _legacy_canonical_kana(kana: str) -> str:
    """Return the connection kana used by snapshot versions 1 and 2."""

    expanded = _SMALL_TO_LARGE_KANA.get(kana, kana)
    return _LEGACY_DAKUON_CHAIN_EQUIVALENTS.get(expanded, expanded)


def _legacy_first_chain_kana(reading: str) -> str:
    normalized = _normalized_reading(reading)
    if not normalized or normalized[0] == "ー":
        raise ValueError("reading has no usable first kana")
    return _legacy_canonical_kana(normalized[0])


def _legacy_ending_chain_kana(reading: str) -> str:
    normalized = _normalized_reading(reading)
    if not normalized:
        raise ValueError("reading has no usable ending kana")

    if normalized[-1] != "ー":
        return _legacy_canonical_kana(normalized[-1])

    index = len(normalized) - 2
    while index >= 0 and normalized[index] == "ー":
        index -= 1
    if index < 0:
        raise ValueError("reading cannot consist only of long sound marks")

    vowel = _VOWEL_BY_KANA.get(normalized[index])
    if vowel is None:
        raise ValueError(
            "long sound mark does not follow a vowel-bearing kana"
        )
    return vowel


def validate_time_limit(time_limit_seconds: int | None) -> int | None:
    """Validate the approved unlimited or 3–180 second setting."""

    if time_limit_seconds is None:
        return None
    if (
        isinstance(time_limit_seconds, bool)
        or not isinstance(time_limit_seconds, int)
        or not 3 <= time_limit_seconds <= 180
    ):
        raise ValueError(
            "time_limit_seconds must be None or an integer from 3 to 180"
        )
    return time_limit_seconds


def validate_deadline_policy(
    deadline_policy: DeadlinePolicy | str,
) -> DeadlinePolicy:
    """Return one supported deadline policy without coercing other types."""

    if not isinstance(deadline_policy, (DeadlinePolicy, str)):
        raise ValueError(
            "deadline_policy must be 'per_turn' or 'fixed_match'"
        )
    try:
        return DeadlinePolicy(deadline_policy)
    except ValueError as error:
        raise ValueError(
            "deadline_policy must be 'per_turn' or 'fixed_match'"
        ) from error


class GameSession:
    """Authoritative dictionary-backed state for one shiritori match."""

    def __init__(
        self,
        validator: LexiconValidator | None = None,
        *,
        time_limit_seconds: int | None = None,
        deadline_policy: DeadlinePolicy | str = DeadlinePolicy.PER_TURN,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._validator = (
            validator if validator is not None else get_default_validator()
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.time_limit_seconds = validate_time_limit(time_limit_seconds)
        self.deadline_policy = validate_deadline_policy(deadline_policy)
        if (
            self.deadline_policy is DeadlinePolicy.FIXED_MATCH
            and self.time_limit_seconds is None
        ):
            raise ValueError(
                "fixed_match deadline_policy requires a time limit"
            )
        self._history: list[HistoryEntry] = []
        self._used_canonical_keys: set[str] = set()
        self._pending_reading: PendingReading | None = None
        self.status = SessionStatus.ACTIVE
        self.started_at = self._now()
        self.ended_at: datetime | None = None
        self._deadline_at: datetime | None = None
        self._start_deadline(self.started_at)

    @property
    def history(self) -> tuple[HistoryEntry, ...]:
        return tuple(self._history)

    @property
    def current_entry(self) -> HistoryEntry | None:
        return self._history[-1] if self._history else None

    @property
    def expected_kana(self) -> str | None:
        if self.current_entry is None:
            return None
        return ending_chain_kana(self.current_entry.reading)

    @property
    def turn_count(self) -> int:
        return len(self._history)

    @property
    def is_over(self) -> bool:
        return self.status is not SessionStatus.ACTIVE

    @property
    def pending_reading(self) -> PendingReading | None:
        return self._pending_reading

    @property
    def used_canonical_keys(self) -> frozenset[str]:
        """Return an immutable duplicate set for Bot and persistence adapters."""

        return frozenset(self._used_canonical_keys)

    @property
    def deadline_at(self) -> datetime | None:
        return self._deadline_at

    def remaining_seconds(self) -> float | None:
        """Return display-only remaining time without changing game state."""

        if self._deadline_at is None:
            return None
        return max(
            0.0,
            (self._deadline_at - self._now()).total_seconds(),
        )

    def submit(self, raw_surface: str | None) -> SessionResult:
        """Validate a surface and apply it, or request a reading choice."""

        now = self._now()
        unavailable = self._unavailable_result(now)
        if unavailable is not None:
            return unavailable

        lexicon_result = self._validator.validate(raw_surface)
        if not lexicon_result.is_dictionary_word:
            return SessionResult(
                code=SessionCode.LEXICON_REJECTED,
                message=lexicon_result.message,
                surface=lexicon_result.surface,
                lexicon_result=lexicon_result,
            )

        candidates = lexicon_result.candidates
        readings = _candidate_readings(candidates)
        if not candidates or not readings:
            return SessionResult(
                code=SessionCode.INVALID_LEXICON_RESULT,
                message="辞書から有効な読みを取得できませんでした。",
                surface=lexicon_result.surface,
                lexicon_result=lexicon_result,
            )

        if len(readings) > 1:
            self._pending_reading = PendingReading(
                surface=lexicon_result.surface,
                candidates=candidates,
                submitted_at=now,
            )
            return SessionResult(
                code=SessionCode.READING_CHOICE_REQUIRED,
                message="使用する読みを選んでください。",
                surface=lexicon_result.surface,
                reading_choices=readings,
                lexicon_result=lexicon_result,
            )

        candidate = next(
            candidate
            for candidate in candidates
            if _normalized_reading(candidate.reading) == readings[0]
        )
        return self._apply_candidate(
            candidate,
            now=now,
            lexicon_result=lexicon_result,
        )

    def resolve_reading(self, reading: str) -> SessionResult:
        """Explicitly choose one reading from the pending candidates."""

        now = self._now()
        unavailable = self._unavailable_result(now)
        if unavailable is not None:
            return unavailable

        pending = self._pending_reading
        if pending is None:
            return SessionResult(
                code=SessionCode.NO_READING_CHOICE_PENDING,
                message="選択待ちの読みはありません。",
            )

        candidates = pending.candidates_for_reading(reading)
        if not candidates:
            return SessionResult(
                code=SessionCode.INVALID_READING_CHOICE,
                message="表示された候補から読みを選んでください。",
                surface=pending.surface,
                reading_choices=pending.readings,
            )

        return self._apply_candidate(candidates[0], now=now)

    def cancel_reading_choice(self) -> SessionResult:
        """Cancel the pending choice without advancing or resetting time."""

        now = self._now()
        unavailable = self._unavailable_result(now)
        if unavailable is not None:
            return unavailable
        if self._pending_reading is None:
            return SessionResult(
                code=SessionCode.NO_READING_CHOICE_PENDING,
                message="選択待ちの読みはありません。",
            )
        surface = self._pending_reading.surface
        self._pending_reading = None
        return SessionResult(
            code=SessionCode.READING_CHOICE_CANCELLED,
            message="読みの選択を取り消しました。",
            surface=surface,
        )

    def expire_if_due(self) -> SessionResult | None:
        """End the active game once when its authoritative deadline is due."""

        if self.status is not SessionStatus.ACTIVE:
            return None
        return self._expire_if_due(self._now())

    def reset(self) -> None:
        """Start a new empty match while retaining the timer configuration."""

        now = self._now()
        self._history.clear()
        self._used_canonical_keys.clear()
        self._pending_reading = None
        self.status = SessionStatus.ACTIVE
        self.started_at = now
        self.ended_at = None
        self._start_deadline(now)

    def to_snapshot(self) -> dict[str, object]:
        """Return a JSON-compatible snapshot suitable for database storage."""

        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "status": self.status.value,
            "time_limit_seconds": self.time_limit_seconds,
            "deadline_policy": self.deadline_policy.value,
            "started_at": self.started_at.isoformat(),
            "ended_at": (
                self.ended_at.isoformat()
                if self.ended_at is not None
                else None
            ),
            "deadline_at": (
                self._deadline_at.isoformat()
                if self._deadline_at is not None
                else None
            ),
            "history": [
                entry.to_snapshot() for entry in self._history
            ],
            "pending_reading": (
                self._pending_reading.to_snapshot()
                if self._pending_reading is not None
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
    ) -> GameSession:
        """Restore a validated snapshot without extending its deadline."""

        try:
            version = snapshot["snapshot_version"]
            if isinstance(version, bool) or not isinstance(version, int):
                raise ValueError
            raw_status = snapshot["status"]
            if not isinstance(raw_status, str):
                raise ValueError
            status = SessionStatus(raw_status)
            raw_limit = snapshot["time_limit_seconds"]
            time_limit = validate_time_limit(raw_limit)  # type: ignore[arg-type]
            if version == LEGACY_SNAPSHOT_VERSION:
                if "deadline_policy" in snapshot:
                    raise ValueError
                deadline_policy = DeadlinePolicy.PER_TURN
            elif version in {
                PREVIOUS_SNAPSHOT_VERSION,
                SNAPSHOT_VERSION,
            }:
                deadline_policy = validate_deadline_policy(
                    snapshot["deadline_policy"]  # type: ignore[arg-type]
                )
            else:
                raise ValueError
            if (
                deadline_policy is DeadlinePolicy.FIXED_MATCH
                and time_limit is None
            ):
                raise ValueError
            started_at = _parse_datetime(
                snapshot["started_at"], "started_at"
            )
            ended_at = _parse_optional_datetime(
                snapshot.get("ended_at"), "ended_at"
            )
            deadline_at = _parse_optional_datetime(
                snapshot.get("deadline_at"), "deadline_at"
            )
            raw_history = snapshot["history"]
            raw_pending = snapshot.get("pending_reading")
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid game session snapshot") from error

        if version not in {
            LEGACY_SNAPSHOT_VERSION,
            PREVIOUS_SNAPSHOT_VERSION,
            SNAPSHOT_VERSION,
        }:
            raise ValueError(f"unsupported snapshot version: {version}")
        if not isinstance(raw_history, list):
            raise ValueError("snapshot history must be a list")

        history = [
            HistoryEntry.from_snapshot(entry)
            for entry in raw_history
            if isinstance(entry, Mapping)
        ]
        if len(history) != len(raw_history):
            raise ValueError("invalid history entry in snapshot")
        if [entry.turn_number for entry in history] != list(
            range(1, len(history) + 1)
        ):
            raise ValueError("history turn numbers must be consecutive")
        if len({entry.canonical_key for entry in history}) != len(history):
            raise ValueError("snapshot history contains duplicate words")
        _validate_snapshot_history(
            history,
            allow_legacy_connections=version
            in {LEGACY_SNAPSHOT_VERSION, PREVIOUS_SNAPSHOT_VERSION},
        )

        pending: PendingReading | None = None
        if raw_pending is not None:
            if not isinstance(raw_pending, Mapping):
                raise ValueError("invalid pending reading in snapshot")
            pending = PendingReading.from_snapshot(raw_pending)

        if status is SessionStatus.ACTIVE:
            if ended_at is not None:
                raise ValueError("active snapshot cannot have ended_at")
            if time_limit is not None and deadline_at is None:
                raise ValueError("timed active snapshot requires a deadline")
        else:
            if ended_at is None:
                raise ValueError("finished snapshot requires ended_at")
            if deadline_at is not None:
                raise ValueError("finished snapshot cannot have a deadline")
            if pending is not None:
                raise ValueError(
                    "finished snapshot cannot have a pending reading"
                )
        if time_limit is None and deadline_at is not None:
            raise ValueError("unlimited snapshot cannot have a deadline")
        if (
            status is SessionStatus.LOST_BY_N
            and (
                not history
                or history[-1].result is not HistoryResult.ENDS_WITH_N
            )
        ):
            raise ValueError("lost_by_n snapshot requires an ending entry")
        if (
            status is not SessionStatus.LOST_BY_N
            and history
            and history[-1].result is HistoryResult.ENDS_WITH_N
        ):
            raise ValueError("ends_with_n entry requires lost_by_n status")
        if status is SessionStatus.LOST_BY_DUPLICATE and not history:
            raise ValueError(
                "lost_by_duplicate snapshot requires prior history"
            )

        session = cls(
            validator=validator,
            time_limit_seconds=time_limit,
            deadline_policy=deadline_policy,
            clock=clock,
        )
        logical_now = session.started_at
        _validate_snapshot_timeline(
            status=status,
            time_limit_seconds=time_limit,
            deadline_policy=deadline_policy,
            started_at=started_at,
            ended_at=ended_at,
            deadline_at=deadline_at,
            history=history,
            pending=pending,
            logical_now=logical_now,
        )
        session._history = history
        session._used_canonical_keys = {
            entry.canonical_key for entry in history
        }
        session._pending_reading = pending
        session.status = status
        session.started_at = started_at
        session.ended_at = ended_at
        session._deadline_at = deadline_at
        return session

    def _apply_candidate(
        self,
        candidate: LexiconCandidate,
        *,
        now: datetime,
        lexicon_result: LexiconResult | None = None,
    ) -> SessionResult:
        try:
            reading = _normalized_reading(candidate.reading)
            first_kana = first_chain_kana(reading)
            last_kana = ending_chain_kana(reading)
        except ValueError:
            return SessionResult(
                code=SessionCode.INVALID_LEXICON_RESULT,
                message="辞書からしりとりに使える読みを取得できませんでした。",
                surface=candidate.surface,
                lexicon_result=lexicon_result,
            )

        expected = self.expected_kana
        if expected is not None and first_kana != expected:
            return SessionResult(
                code=SessionCode.NOT_CHAINED,
                message=f"「{expected}」から始まる単語を入力してください。",
                surface=candidate.surface,
                reading=reading,
                reading_choices=(
                    self._pending_reading.readings
                    if self._pending_reading is not None
                    else ()
                ),
                lexicon_result=lexicon_result,
            )

        # The spoken reading is deliberately the canonical duplicate key.
        # This makes kana/kanji variants and distinct homophones equivalent.
        canonical_key = reading
        if canonical_key in self._used_canonical_keys:
            self.status = SessionStatus.LOST_BY_DUPLICATE
            self.ended_at = now
            self._deadline_at = None
            self._pending_reading = None
            return SessionResult(
                code=SessionCode.DUPLICATE,
                message=f"「{candidate.surface}」と同じ読みは使用済みです。",
                surface=candidate.surface,
                reading=reading,
                game_over=True,
                lexicon_result=lexicon_result,
            )

        history_result = (
            HistoryResult.ENDS_WITH_N
            if last_kana == "ん"
            else HistoryResult.ACCEPTED
        )
        entry = HistoryEntry(
            surface=candidate.surface,
            reading=reading,
            canonical_key=canonical_key,
            turn_number=len(self._history) + 1,
            timestamp=now,
            result=history_result,
        )
        self._history.append(entry)
        self._used_canonical_keys.add(canonical_key)
        self._pending_reading = None

        if history_result is HistoryResult.ENDS_WITH_N:
            self.status = SessionStatus.LOST_BY_N
            self.ended_at = now
            self._deadline_at = None
            return SessionResult(
                code=SessionCode.ENDS_WITH_N,
                message=f"「{candidate.surface}」は「ん」で終わります。",
                surface=candidate.surface,
                reading=reading,
                accepted=True,
                game_over=True,
                entry=entry,
                lexicon_result=lexicon_result,
            )

        if self.deadline_policy is DeadlinePolicy.PER_TURN:
            self._start_deadline(now)
        return SessionResult(
            code=SessionCode.ACCEPTED,
            message=f"「{candidate.surface}」を受け付けました。",
            surface=candidate.surface,
            reading=reading,
            accepted=True,
            entry=entry,
            lexicon_result=lexicon_result,
        )

    def _unavailable_result(
        self, now: datetime
    ) -> SessionResult | None:
        if self.status is not SessionStatus.ACTIVE:
            return SessionResult(
                code=SessionCode.GAME_ALREADY_OVER,
                message="ゲームは終了しています。",
                game_over=True,
            )
        return self._expire_if_due(now)

    def _expire_if_due(self, now: datetime) -> SessionResult | None:
        if self._deadline_at is None or now < self._deadline_at:
            return None
        expired_at = self._deadline_at
        self.status = SessionStatus.LOST_BY_TIMEOUT
        self.ended_at = expired_at
        self._deadline_at = None
        self._pending_reading = None
        return SessionResult(
            code=SessionCode.TIMED_OUT,
            message="制限時間を超えました。",
            game_over=True,
        )

    def _start_deadline(self, now: datetime) -> None:
        self._deadline_at = (
            None
            if self.time_limit_seconds is None
            else now + timedelta(seconds=self.time_limit_seconds)
        )

    def _now(self) -> datetime:
        return _as_utc(self._clock(), "clock")


def _normalized_reading(reading: str) -> str:
    return katakana_to_hiragana(normalize_surface(reading))


def _candidate_readings(
    candidates: tuple[LexiconCandidate, ...],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            reading
            for candidate in candidates
            if (reading := _normalized_reading(candidate.reading))
        )
    )


def _as_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _parse_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO datetime") from error
    return _as_utc(parsed, field)


def _parse_optional_datetime(
    value: object, field: str
) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value, field)


def _snapshot_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _validate_snapshot_reading(reading: str) -> str:
    normalized = _normalized_reading(reading)
    if (
        reading != normalized
        or not normalized
        or normalized[0] == "ー"
        or any(
            not (
                "\u3041" <= character <= "\u3096"
                or character in {"ゝ", "ゞ", "ー"}
            )
            for character in normalized
        )
    ):
        raise ValueError("snapshot reading is not usable hiragana")
    # These helpers also reject a reading made only from long-sound marks or
    # a final mark which does not follow a vowel-bearing mora.
    first_chain_kana(normalized)
    ending_chain_kana(normalized)
    return normalized


def _validate_snapshot_history(
    history: list[HistoryEntry],
    *,
    allow_legacy_connections: bool = False,
) -> None:
    previous: HistoryEntry | None = None
    for index, entry in enumerate(history):
        normalized = _validate_snapshot_reading(entry.reading)
        if entry.canonical_key != normalized:
            raise ValueError(
                "history canonical key must equal normalized reading"
            )

        ends_with_n = ending_chain_kana(normalized) == "ん"
        if (entry.result is HistoryResult.ENDS_WITH_N) != ends_with_n:
            raise ValueError(
                "history result must match whether the reading ends in ん"
            )
        if (
            entry.result is HistoryResult.ENDS_WITH_N
            and index != len(history) - 1
        ):
            raise ValueError("ends_with_n entry must be the final entry")

        if (
            previous is not None and not (
                ending_chain_kana(previous.reading)
                == first_chain_kana(normalized)
                or (
                    allow_legacy_connections
                    and _legacy_ending_chain_kana(previous.reading)
                    == _legacy_first_chain_kana(normalized)
                )
            )
        ):
            raise ValueError("adjacent history entries do not chain")
        previous = entry


def _validate_snapshot_timeline(
    *,
    status: SessionStatus,
    time_limit_seconds: int | None,
    deadline_policy: DeadlinePolicy,
    started_at: datetime,
    ended_at: datetime | None,
    deadline_at: datetime | None,
    history: list[HistoryEntry],
    pending: PendingReading | None,
    logical_now: datetime,
) -> None:
    if started_at > logical_now:
        raise ValueError("snapshot cannot start in the future")

    if ended_at is not None:
        if ended_at < started_at or ended_at > logical_now:
            raise ValueError("snapshot ended_at is outside its timeline")
        upper_bound = ended_at
    else:
        upper_bound = logical_now

    previous_at = started_at
    for entry in history:
        if not previous_at <= entry.timestamp <= upper_bound:
            raise ValueError(
                "history timestamps must be monotonic within the session"
            )
        previous_at = entry.timestamp

    if status is SessionStatus.LOST_BY_N:
        if not history or ended_at != history[-1].timestamp:
            raise ValueError(
                "lost_by_n must end when its final entry is recorded"
            )

    if pending is not None:
        if not previous_at <= pending.submitted_at <= logical_now:
            raise ValueError(
                "pending reading timestamp is outside its timeline"
            )
        if deadline_at is not None and pending.submitted_at >= deadline_at:
            raise ValueError(
                "pending reading must be submitted before the deadline"
            )

    if status is SessionStatus.ACTIVE and time_limit_seconds is not None:
        deadline_base = (
            started_at
            if deadline_policy is DeadlinePolicy.FIXED_MATCH
            else history[-1].timestamp if history else started_at
        )
        expected_deadline = deadline_base + timedelta(
            seconds=time_limit_seconds
        )
        if deadline_at != expected_deadline:
            raise ValueError(
                "active deadline does not match its deadline policy"
            )

def _candidate_to_snapshot(
    candidate: LexiconCandidate,
) -> dict[str, object]:
    return {
        "surface": candidate.surface,
        "reading": candidate.reading,
        "lemma": candidate.lemma,
        "normalized_form": candidate.normalized_form,
        "part_of_speech": list(candidate.part_of_speech),
        "dictionary_id": candidate.dictionary_id,
        "word_id": candidate.word_id,
        "canonical_key": candidate.canonical_key,
    }


def _candidate_from_snapshot(data: object) -> LexiconCandidate:
    if not isinstance(data, Mapping):
        raise ValueError("invalid candidate in snapshot")
    try:
        part_of_speech = data["part_of_speech"]
        if (
            not isinstance(part_of_speech, list)
            or len(part_of_speech) != 6
            or any(
                not isinstance(part, str) or not part
                for part in part_of_speech
            )
        ):
            raise ValueError
        surface = _snapshot_text(data["surface"], "surface")
        reading = _snapshot_text(data["reading"], "reading")
        lemma = _snapshot_text(data["lemma"], "lemma")
        normalized_form = _snapshot_text(
            data["normalized_form"], "normalized_form"
        )
        canonical_key = _snapshot_text(
            data["canonical_key"], "canonical_key"
        )
        dictionary_id = data["dictionary_id"]
        word_id = data["word_id"]
        if (
            isinstance(dictionary_id, bool)
            or not isinstance(dictionary_id, int)
            or dictionary_id < 0
            or isinstance(word_id, bool)
            or not isinstance(word_id, int)
            or word_id < 0
        ):
            raise ValueError
        normalized_reading = _validate_snapshot_reading(reading)
        if (
            normalize_surface(surface) != surface
            or canonical_key != normalized_reading
        ):
            raise ValueError
        return LexiconCandidate(
            surface=surface,
            reading=reading,
            lemma=lemma,
            normalized_form=normalized_form,
            part_of_speech=tuple(part_of_speech),
            dictionary_id=dictionary_id,
            word_id=word_id,
            canonical_key=canonical_key,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid candidate in snapshot") from error
