"""Validated, review-only user suggestions for missing dictionary words.

Suggestions are deliberately isolated from the lexicon and Bot catalogs.
Submitting a row never makes a word playable; a separate human review and
curated data release is required for that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
import unicodedata

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import Database
from .lexicon import LexiconResult, get_default_validator
from .models import User, WordSuggestion, new_id, utc_now


MAX_SURFACE_LENGTH = 30
MAX_READING_LENGTH = 60
MAX_NOTE_LENGTH = 200
DEFAULT_MAX_PENDING_PER_USER = 20
MAX_LIST_LIMIT = 100

_SURFACE_MARKS = frozenset({"々", "〆", "ー", "・", "ゝ", "ゞ", "ヽ", "ヾ"})
_INVALID_SURFACE_START = _SURFACE_MARKS
_READING_MARKS = frozenset({"ー", "ゝ", "ゞ"})
_INVALID_READING_START = _READING_MARKS


class WordSuggestionError(RuntimeError):
    """Base class for failures safe for an authenticated UI to handle."""


class WordSuggestionValidationError(ValueError):
    """Raised when one public submission field is malformed."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class WordSuggestionUserUnavailableError(WordSuggestionError, LookupError):
    """Raised for missing or disabled accounts without distinguishing them."""

    def __init__(self) -> None:
        super().__init__("このアカウントでは単語を申請できません。")


class WordSuggestionPendingLimitError(WordSuggestionError):
    """Raised when a user already has the configured number under review."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"審査待ちの申請は1人{limit}件までです。"
            "審査が終わってからもう一度お試しください。"
        )
        self.limit = limit


class _SuggestionLexiconValidator(Protocol):
    def validate(self, raw_surface: str | None) -> LexiconResult:
        """Return whether one normalized surface is already playable."""


@dataclass(frozen=True, slots=True)
class WordSuggestionView:
    """UI-safe projection which intentionally omits row and account IDs."""

    surface: str
    reading: str
    note: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    reviewed_at: datetime | None


@dataclass(frozen=True, slots=True)
class WordSuggestionSubmission:
    suggestion: WordSuggestionView
    replayed: bool


def _normalized_text(
    value: str | None,
    *,
    field: str,
    label: str,
) -> str:
    if not isinstance(value, str):
        raise WordSuggestionValidationError(
            field,
            f"{label}は文字列で入力してください。",
        )
    normalized = unicodedata.normalize("NFKC", value)
    return unicodedata.normalize("NFC", normalized).strip()


def _is_hiragana(character: str) -> bool:
    return "\u3041" <= character <= "\u3096" or character in {"ゝ", "ゞ"}


def _is_katakana(character: str) -> bool:
    return (
        "\u30a1" <= character <= "\u30fa"
        or character in {"ヽ", "ヾ"}
        or "\u31f0" <= character <= "\u31ff"
    )


def _is_han(character: str) -> bool:
    name = unicodedata.name(character, "")
    return name.startswith("CJK UNIFIED IDEOGRAPH") or name.startswith(
        "CJK COMPATIBILITY IDEOGRAPH"
    )


def normalize_suggestion_surface(value: str | None) -> str:
    """Normalize and validate one Japanese surface without dictionary lookup."""

    surface = _normalized_text(value, field="surface", label="単語")
    if not surface:
        raise WordSuggestionValidationError("surface", "単語を入力してください。")
    if len(surface) > MAX_SURFACE_LENGTH:
        raise WordSuggestionValidationError(
            "surface",
            f"単語は{MAX_SURFACE_LENGTH}文字以内にしてください。",
        )
    if any(character.isspace() for character in surface):
        raise WordSuggestionValidationError(
            "surface",
            "単語の途中に空白は使えません。",
        )
    if not all(
        _is_hiragana(character)
        or _is_katakana(character)
        or _is_han(character)
        or character in _SURFACE_MARKS
        for character in surface
    ):
        raise WordSuggestionValidationError(
            "surface",
            "単語には、ひらがな・カタカナ・漢字だけを使用してください。",
        )
    if surface[0] in _INVALID_SURFACE_START or surface[-1] == "・":
        raise WordSuggestionValidationError(
            "surface",
            "単語の先頭または末尾の記号が不正です。",
        )
    if len(surface) == 1 and _is_hiragana(surface):
        raise WordSuggestionValidationError(
            "surface",
            "ひらがな1文字だけの単語は申請できません。",
        )
    return surface


def normalize_suggestion_reading(value: str | None) -> str:
    """Require an explicit hiragana reading, preserving the long-vowel mark."""

    reading = _normalized_text(value, field="reading", label="読み")
    if not reading:
        raise WordSuggestionValidationError(
            "reading",
            "ひらがなの読みを入力してください。",
        )
    if len(reading) > MAX_READING_LENGTH:
        raise WordSuggestionValidationError(
            "reading",
            f"読みは{MAX_READING_LENGTH}文字以内にしてください。",
        )
    if (
        reading[0] in _INVALID_READING_START
        or not all(
            _is_hiragana(character) or character in _READING_MARKS
            for character in reading
        )
    ):
        raise WordSuggestionValidationError(
            "reading",
            "読みはひらがなで入力してください（長音記号「ー」は使用できます）。",
        )
    return reading


def normalize_suggestion_note(value: str | None) -> str | None:
    """Normalize an optional, short, single-line review hint."""

    if value is None:
        return None
    note = _normalized_text(value, field="note", label="補足")
    if not note:
        return None
    if len(note) > MAX_NOTE_LENGTH:
        raise WordSuggestionValidationError(
            "note",
            f"補足は{MAX_NOTE_LENGTH}文字以内にしてください。",
        )
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        for character in note
    ):
        raise WordSuggestionValidationError(
            "note",
            "補足には改行や制御文字を使用できません。",
        )
    return note


def _stored_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _user_id(value: str) -> str:
    if not isinstance(value, str):
        raise WordSuggestionUserUnavailableError()
    identifier = value.strip()
    if not identifier or len(identifier) > 36:
        raise WordSuggestionUserUnavailableError()
    return identifier


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_LIST_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIST_LIMIT}")
    return value


class WordSuggestionRepository:
    """Small SQLAlchemy repository with per-user serialization helpers."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def lock_user(session: Session, user_id: str) -> User | None:
        return session.scalar(
            select(User).where(User.id == user_id).with_for_update()
        )

    @staticmethod
    def find_exact(
        session: Session,
        *,
        user_id: str,
        surface: str,
        reading: str,
    ) -> WordSuggestion | None:
        return session.scalar(
            select(WordSuggestion).where(
                WordSuggestion.user_id == user_id,
                WordSuggestion.surface == surface,
                WordSuggestion.reading == reading,
            )
        )

    @staticmethod
    def pending_count(session: Session, user_id: str) -> int:
        return int(
            session.scalar(
                select(func.count())
                .select_from(WordSuggestion)
                .where(
                    WordSuggestion.user_id == user_id,
                    WordSuggestion.status == "pending",
                )
            )
            or 0
        )

    @staticmethod
    def list_for_user(
        session: Session,
        user_id: str,
        *,
        limit: int,
    ) -> tuple[WordSuggestion, ...]:
        return tuple(
            session.scalars(
                select(WordSuggestion)
                .where(WordSuggestion.user_id == user_id)
                .order_by(
                    WordSuggestion.created_at.desc(),
                    WordSuggestion.id.desc(),
                )
                .limit(limit)
            )
        )


class WordSuggestionService:
    """Validate and persist review requests without touching playable words."""

    def __init__(
        self,
        database: Database,
        *,
        max_pending_per_user: int = DEFAULT_MAX_PENDING_PER_USER,
        clock=utc_now,
        repository: WordSuggestionRepository | None = None,
        validator: _SuggestionLexiconValidator | None = None,
    ) -> None:
        if (
            type(max_pending_per_user) is not int
            or not 1 <= max_pending_per_user <= 100
        ):
            raise ValueError("max_pending_per_user must be an integer from 1 to 100")
        self.database = database
        self.max_pending_per_user = max_pending_per_user
        self._clock = clock
        self.repository = repository or WordSuggestionRepository(database)
        if self.repository.database is not database:
            raise ValueError("repository must use the same database")
        self.validator = validator or get_default_validator()
        if not callable(getattr(self.validator, "validate", None)):
            raise TypeError("validator must provide validate")

    def submit(
        self,
        user_id: str,
        surface: str | None,
        reading: str | None,
        note: str | None = None,
    ) -> WordSuggestionSubmission:
        """Create one pending request, or replay the exact prior request."""

        owner_id = _user_id(user_id)
        clean_surface = normalize_suggestion_surface(surface)
        clean_reading = normalize_suggestion_reading(reading)
        clean_note = normalize_suggestion_note(note)
        if self.validator.validate(clean_surface).is_dictionary_word:
            raise WordSuggestionValidationError(
                "surface",
                "この単語はすでにしりとりで使用できるため、"
                "申請する必要はありません。",
            )
        try:
            with self.database.transaction() as session:
                user = self.repository.lock_user(session, owner_id)
                self._require_available_user(user)
                existing = self.repository.find_exact(
                    session,
                    user_id=owner_id,
                    surface=clean_surface,
                    reading=clean_reading,
                )
                if existing is not None:
                    return WordSuggestionSubmission(
                        suggestion=self._view(existing),
                        replayed=True,
                    )
                if (
                    self.repository.pending_count(session, owner_id)
                    >= self.max_pending_per_user
                ):
                    raise WordSuggestionPendingLimitError(
                        self.max_pending_per_user
                    )
                now = _aware_utc(self._clock())
                suggestion = WordSuggestion(
                    id=new_id(),
                    user_id=owner_id,
                    surface=clean_surface,
                    reading=clean_reading,
                    note=clean_note,
                    status="pending",
                    created_at=now,
                    updated_at=now,
                    reviewed_at=None,
                )
                session.add(suggestion)
                session.flush()
                return WordSuggestionSubmission(
                    suggestion=self._view(suggestion),
                    replayed=False,
                )
        except IntegrityError as error:
            # The database uniqueness rule is authoritative if concurrent
            # requests pass application checks at the same time.
            with self.database.read_session() as session:
                user = session.get(User, owner_id)
                self._require_available_user(user)
                existing = self.repository.find_exact(
                    session,
                    user_id=owner_id,
                    surface=clean_surface,
                    reading=clean_reading,
                )
                if existing is not None:
                    return WordSuggestionSubmission(
                        suggestion=self._view(existing),
                        replayed=True,
                    )
            raise error

    def list_mine(
        self,
        user_id: str,
        *,
        limit: int = 50,
    ) -> tuple[WordSuggestionView, ...]:
        """List only the caller's own requests, newest first."""

        owner_id = _user_id(user_id)
        maximum = _limit(limit)
        with self.database.read_session() as session:
            user = session.get(User, owner_id)
            self._require_available_user(user)
            return tuple(
                self._view(suggestion)
                for suggestion in self.repository.list_for_user(
                    session,
                    owner_id,
                    limit=maximum,
                )
            )

    @staticmethod
    def _require_available_user(user: User | None) -> None:
        if user is None or user.disabled_at is not None:
            raise WordSuggestionUserUnavailableError()

    @staticmethod
    def _view(suggestion: WordSuggestion) -> WordSuggestionView:
        return WordSuggestionView(
            surface=suggestion.surface,
            reading=suggestion.reading,
            note=suggestion.note,
            status=suggestion.status,
            created_at=_stored_utc(suggestion.created_at),
            updated_at=_stored_utc(suggestion.updated_at),
            reviewed_at=_stored_utc(suggestion.reviewed_at),
        )


__all__ = [
    "DEFAULT_MAX_PENDING_PER_USER",
    "MAX_LIST_LIMIT",
    "MAX_NOTE_LENGTH",
    "MAX_READING_LENGTH",
    "MAX_SURFACE_LENGTH",
    "WordSuggestionError",
    "WordSuggestionPendingLimitError",
    "WordSuggestionRepository",
    "WordSuggestionService",
    "WordSuggestionSubmission",
    "WordSuggestionUserUnavailableError",
    "WordSuggestionValidationError",
    "WordSuggestionView",
    "normalize_suggestion_note",
    "normalize_suggestion_reading",
    "normalize_suggestion_surface",
]
