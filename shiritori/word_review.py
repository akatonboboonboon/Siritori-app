"""Administrative review and human-approved lexicon support.

This module keeps three trust boundaries explicit:

* only an active database user whose normalized username is in the
  server-owned administrator allowlist may inspect or decide requests;
* a request can move from ``pending`` to one final decision exactly once;
* approved human words are available to human input validation, but are never
  added to the separately curated Bot vocabulary.

The SQLAlchemy models are defined in :mod:`shiritori.models`.  This module
expects a one-to-one ``WordSuggestionReview`` audit model with
``suggestion_id``, ``reviewer_user_id``, ``decision``, ``review_note``, and
``reviewed_at``.  It also expects an ``ApprovedWord`` model with ``id``,
``surface``, ``reading``, ``approved_by_user_id``,
``source_suggestion_id``, and ``approved_at``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import logging
from threading import Lock
from types import MappingProxyType
import unicodedata

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session, aliased

from .database import Database
from .lexicon import (
    LexiconCandidate,
    LexiconCode,
    LexiconResult,
    LexiconValidator,
    get_default_validator,
    normalize_surface,
)
from .models import (
    ApprovedWord,
    User,
    WordSuggestion,
    WordSuggestionReview,
    new_id,
    utc_now,
)
from .word_suggestions import (
    WordSuggestionValidationError,
    normalize_suggestion_note,
    normalize_suggestion_reading,
    normalize_suggestion_surface,
)


LOGGER = logging.getLogger(__name__)

MAX_REVIEW_LIST_LIMIT = 100
_APPROVED_DICTIONARY_ID = 2_147_483_647
_APPROVED_PART_OF_SPEECH = (
    "名詞",
    "普通名詞",
    "一般",
    "*",
    "*",
    "*",
)
_APPROVED_FALLBACK_CODES = frozenset(
    {
        LexiconCode.NOT_IN_DICTIONARY,
        LexiconCode.UNSUPPORTED_PART_OF_SPEECH,
        LexiconCode.NO_USABLE_READING,
    }
)


class WordReviewError(RuntimeError):
    """Base error safe for the administrative UI to handle."""


class WordReviewAuthorizationError(WordReviewError, PermissionError):
    """Raised without disclosing whether an account exists."""

    def __init__(self) -> None:
        super().__init__("単語を審査する権限がありません。")


class WordReviewConfigurationError(WordReviewError):
    """Raised when an allowlisted administrator has no database account."""


class WordReviewNotFoundError(WordReviewError, LookupError):
    """Raised when an exact suggestion group does not exist."""

    def __init__(self) -> None:
        super().__init__("対象の申請が見つかりません。")


class WordReviewConflictError(WordReviewError):
    """Raised when a final decision conflicts with the requested decision."""

    def __init__(self) -> None:
        super().__init__(
            "この申請はすでに別の判定で審査されています。"
            "画面を再読み込みしてください。"
        )


class WordReviewValidationError(ValueError):
    """Raised for malformed public review input."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class ReviewDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class PendingSuggestionDetail:
    """One applicant note inside a grouped queue item."""

    submitter_display_name: str
    note: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PendingSuggestionGroup:
    """All pending submissions for one normalized surface and reading."""

    surface: str
    reading: str
    submission_count: int
    first_submitted_at: datetime
    last_submitted_at: datetime
    submissions: tuple[PendingSuggestionDetail, ...]


@dataclass(frozen=True, slots=True)
class ReviewedSuggestionView:
    """One immutable audit row for the recent-review list."""

    surface: str
    reading: str
    status: str
    review_note: str | None
    reviewed_at: datetime
    reviewer_display_name: str
    submitter_display_name: str


@dataclass(frozen=True, slots=True)
class WordReviewResult:
    """Result of one group decision."""

    surface: str
    reading: str
    decision: ReviewDecision
    reviewed_count: int
    replayed: bool
    approved_word_added: bool


@dataclass(frozen=True, slots=True)
class ApprovedWordEntry:
    """Small immutable projection used by the in-process lexicon catalog."""

    id: str
    surface: str
    reading: str
    word_id: int


def admin_username_keys_from_value(raw_value: str | None) -> frozenset[str]:
    """Parse ``ADMIN_USERNAMES`` into normalized database username keys.

    The environment value is a comma-separated list of already registered
    usernames.  Empty configuration means that no administrator is enabled;
    empty items inside a non-empty list are rejected to catch deployment
    typos instead of silently weakening the intended policy.
    """

    if raw_value is None or not raw_value.strip():
        return frozenset()
    usernames = raw_value.split(",")
    keys: set[str] = set()
    for username in usernames:
        normalized = unicodedata.normalize("NFKC", username).strip()
        if not normalized:
            raise WordReviewConfigurationError(
                "ADMIN_USERNAMES contains an empty username"
            )
        key = normalized.casefold()
        if len(key) > 64:
            raise WordReviewConfigurationError(
                "ADMIN_USERNAMES contains a username which is too long"
            )
        keys.add(key)
    return frozenset(keys)


def _normalize_admin_keys(values: Iterable[str]) -> frozenset[str]:
    if isinstance(values, str):
        raise TypeError(
            "admin_username_keys must be an iterable of normalized keys, "
            "not one comma-separated string"
        )
    keys: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError("administrator username keys must be strings")
        key = unicodedata.normalize("NFKC", value).strip().casefold()
        if not key or len(key) > 64:
            raise ValueError(
                "administrator username keys must contain 1-64 characters"
            )
        keys.add(key)
    return frozenset(keys)


def _user_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.isspace()
        or len(value) > 36
    ):
        raise WordReviewAuthorizationError()
    return value


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_REVIEW_LIST_LIMIT:
        raise ValueError(
            f"limit must be an integer from 1 to {MAX_REVIEW_LIST_LIMIT}"
        )
    return value


def _decision(value: ReviewDecision | str) -> ReviewDecision:
    if isinstance(value, ReviewDecision):
        return value
    if not isinstance(value, str):
        raise WordReviewValidationError(
            "decision", "承認または見送りを選んでください。"
        )
    try:
        return ReviewDecision(value.strip().lower())
    except ValueError as error:
        raise WordReviewValidationError(
            "decision", "承認または見送りを選んでください。"
        ) from error


def _review_target(
    surface: str | None,
    reading: str | None,
) -> tuple[str, str]:
    try:
        clean_surface = normalize_suggestion_surface(surface)
    except WordSuggestionValidationError as error:
        raise WordReviewValidationError("surface", str(error)) from error
    try:
        clean_reading = normalize_suggestion_reading(reading)
    except WordSuggestionValidationError as error:
        raise WordReviewValidationError("reading", str(error)) from error
    return clean_surface, clean_reading


def _review_note(value: str | None) -> str | None:
    try:
        return normalize_suggestion_note(value)
    except WordSuggestionValidationError as error:
        raise WordReviewValidationError("review_note", str(error)) from error


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _stored_utc(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("review timestamp is required")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _display_name(
    username: str,
    display_name: str | None,
) -> str:
    return display_name or username


def _stable_word_id(identifier: str) -> int:
    # Keep the synthetic ID non-negative so pending-reading snapshots retain
    # the same invariants as Sudachi candidates.  It is metadata only; spoken
    # duplicate detection continues to use the normalized reading.
    digest = sha256(identifier.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def _approved_entry(word: ApprovedWord) -> ApprovedWordEntry:
    surface, reading = _review_target(word.surface, word.reading)
    if surface != word.surface or reading != word.reading:
        raise ValueError("approved word is not stored in normalized form")
    return ApprovedWordEntry(
        id=word.id,
        surface=surface,
        reading=reading,
        word_id=_stable_word_id(word.id),
    )


class ApprovedWordCatalog:
    """Thread-safe, atomically refreshed catalog of human-approved words."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._write_lock = Lock()
        self._by_surface: Mapping[
            str, tuple[ApprovedWordEntry, ...]
        ] = MappingProxyType({})

    def refresh(self) -> int:
        """Replace the complete snapshot from the authoritative database."""

        with self.database.read_session() as session:
            words = tuple(
                session.scalars(
                    select(ApprovedWord).order_by(
                        ApprovedWord.surface.asc(),
                        ApprovedWord.reading.asc(),
                        ApprovedWord.id.asc(),
                    )
                )
            )

        grouped: dict[str, list[ApprovedWordEntry]] = {}
        for word in words:
            try:
                entry = _approved_entry(word)
            except (ValueError, WordReviewValidationError):
                LOGGER.error(
                    "ignoring malformed approved word row %s",
                    getattr(word, "id", "<unknown>"),
                )
                continue
            grouped.setdefault(entry.surface, []).append(entry)

        snapshot = MappingProxyType(
            {
                surface: tuple(
                    sorted(
                        entries,
                        key=lambda entry: (
                            entry.reading,
                            entry.word_id,
                        ),
                    )
                )
                for surface, entries in grouped.items()
            }
        )
        with self._write_lock:
            self._by_surface = snapshot
        return sum(len(entries) for entries in snapshot.values())

    def add(
        self,
        *,
        word_id: str,
        surface: str,
        reading: str,
    ) -> ApprovedWordEntry:
        """Add or replay one committed row using copy-on-write publication."""

        clean_surface, clean_reading = _review_target(surface, reading)
        entry = ApprovedWordEntry(
            id=word_id,
            surface=clean_surface,
            reading=clean_reading,
            word_id=_stable_word_id(word_id),
        )
        with self._write_lock:
            current = dict(self._by_surface)
            existing = {
                (item.surface, item.reading): item
                for item in current.get(clean_surface, ())
            }
            existing[(clean_surface, clean_reading)] = entry
            current[clean_surface] = tuple(
                sorted(
                    existing.values(),
                    key=lambda item: (item.reading, item.word_id),
                )
            )
            self._by_surface = MappingProxyType(current)
        return entry

    def lookup(self, raw_surface: str | None) -> tuple[ApprovedWordEntry, ...]:
        """Return one immutable snapshot without holding a lock."""

        surface = normalize_surface(raw_surface)
        snapshot = self._by_surface
        return snapshot.get(surface, ())

    @property
    def entry_count(self) -> int:
        snapshot = self._by_surface
        return sum(len(entries) for entries in snapshot.values())


class ApprovedLexiconValidator:
    """Use Sudachi first, then exact approved-word fallback.

    Structural failures never reach the fallback.  A reviewed word can only
    supplement a missing/unsupported Sudachi noun; it cannot override length,
    character, whitespace, or one-kana rules.
    """

    def __init__(
        self,
        catalog: ApprovedWordCatalog,
        base_validator: LexiconValidator | None = None,
    ) -> None:
        self.catalog = catalog
        self.base_validator = base_validator or get_default_validator()

    def validate(self, raw_surface: str | None) -> LexiconResult:
        base_result = self.base_validator.validate(raw_surface)
        if base_result.code not in _APPROVED_FALLBACK_CODES:
            return base_result

        entries = self.catalog.lookup(base_result.surface)
        if not entries:
            return base_result

        candidates = tuple(
            LexiconCandidate(
                surface=entry.surface,
                reading=entry.reading,
                lemma=entry.surface,
                normalized_form=entry.surface,
                part_of_speech=_APPROVED_PART_OF_SPEECH,
                dictionary_id=_APPROVED_DICTIONARY_ID,
                word_id=entry.word_id,
                canonical_key=entry.reading,
            )
            for entry in entries
        )
        readings = tuple(
            dict.fromkeys(candidate.reading for candidate in candidates)
        )
        code = (
            LexiconCode.ACCEPTED
            if len(readings) == 1
            else LexiconCode.MULTIPLE_READINGS
        )
        message = (
            "審査で承認された単語です。"
            if code is LexiconCode.ACCEPTED
            else "読みが複数あります。しりとりで使う読みを選んでください。"
        )
        return LexiconResult(
            code=code,
            surface=base_result.surface,
            message=message,
            candidates=candidates,
        )


def _recent_reviews_statement(maximum: int):
    """Build the audit query with PostgreSQL-safe JOIN ordering.

    PostgreSQL does not allow an ``ON`` clause to reference a table which
    appears in a later JOIN.  Keeping this builder separate also lets the
    production dialect be compiled in a regression test without connecting
    to Neon.
    """

    submitter = aliased(User)
    reviewer = aliased(User)
    return (
        select(
            WordSuggestion.surface,
            WordSuggestion.reading,
            WordSuggestionReview.decision,
            WordSuggestionReview.review_note,
            WordSuggestionReview.reviewed_at,
            submitter.username.label("submitter_username"),
            submitter.display_name.label("submitter_display_name"),
            reviewer.username.label("reviewer_username"),
            reviewer.display_name.label("reviewer_display_name"),
        )
        .join(
            WordSuggestionReview,
            WordSuggestionReview.suggestion_id == WordSuggestion.id,
        )
        .join(
            submitter,
            submitter.id == WordSuggestion.user_id,
        )
        .join(
            reviewer,
            reviewer.id == WordSuggestionReview.reviewer_user_id,
        )
        .where(
            WordSuggestionReview.decision.in_(
                (
                    ReviewDecision.APPROVED.value,
                    ReviewDecision.REJECTED.value,
                )
            )
        )
        .order_by(
            WordSuggestionReview.reviewed_at.desc(),
            WordSuggestion.id.desc(),
        )
        .limit(maximum)
    )


class WordReviewService:
    """Authorize, list, and atomically decide grouped word suggestions."""

    def __init__(
        self,
        database: Database,
        admin_username_keys: Iterable[str],
        catalog: ApprovedWordCatalog,
        *,
        clock=utc_now,
    ) -> None:
        if catalog.database is not database:
            raise ValueError("catalog must use the same database")
        self.database = database
        self.admin_username_keys = _normalize_admin_keys(
            admin_username_keys
        )
        self.catalog = catalog
        self._clock = clock

    def validate_configured_admins(self) -> tuple[str, ...]:
        """Fail closed when a configured username has not been registered."""

        if not self.admin_username_keys:
            return ()
        with self.database.read_session() as session:
            rows = tuple(
                session.execute(
                    select(User.id, User.username_key).where(
                        User.username_key.in_(
                            self.admin_username_keys
                        )
                    )
                )
            )
        found = {row.username_key: row.id for row in rows}
        missing = self.admin_username_keys.difference(found)
        if missing:
            raise WordReviewConfigurationError(
                "ADMIN_USERNAMES must contain only already registered accounts"
            )
        return tuple(
            found[key] for key in sorted(self.admin_username_keys)
        )

    def is_admin(self, user_id: str) -> bool:
        """Check the current database account on every call."""

        try:
            identifier = _user_id(user_id)
        except WordReviewAuthorizationError:
            return False
        with self.database.read_session() as session:
            user = session.get(User, identifier)
            return self._is_allowed_user(user)

    def list_pending_groups(
        self,
        reviewer_user_id: str,
        *,
        limit: int = 100,
    ) -> tuple[PendingSuggestionGroup, ...]:
        """List oldest grouped pending requests after fresh authorization."""

        reviewer_id = _user_id(reviewer_user_id)
        maximum = _limit(limit)
        with self.database.read_session() as session:
            self._require_admin(session, reviewer_id)
            grouped_rows = tuple(
                session.execute(
                    select(
                        WordSuggestion.surface.label("surface"),
                        WordSuggestion.reading.label("reading"),
                        func.count(WordSuggestion.id).label(
                            "submission_count"
                        ),
                        func.min(WordSuggestion.created_at).label(
                            "first_submitted_at"
                        ),
                        func.max(WordSuggestion.created_at).label(
                            "last_submitted_at"
                        ),
                    )
                    .where(WordSuggestion.status == "pending")
                    .group_by(
                        WordSuggestion.surface,
                        WordSuggestion.reading,
                    )
                    .order_by(
                        func.min(WordSuggestion.created_at).asc(),
                        WordSuggestion.surface.asc(),
                        WordSuggestion.reading.asc(),
                    )
                    .limit(maximum)
                ).mappings()
            )
            if not grouped_rows:
                return ()

            keys = tuple(
                (row["surface"], row["reading"])
                for row in grouped_rows
            )
            detail_rows = tuple(
                session.execute(
                    select(
                        WordSuggestion.surface,
                        WordSuggestion.reading,
                        WordSuggestion.note,
                        WordSuggestion.created_at,
                        User.username,
                        User.display_name,
                    )
                    .join(User, User.id == WordSuggestion.user_id)
                    .where(
                        WordSuggestion.status == "pending",
                        tuple_(
                            WordSuggestion.surface,
                            WordSuggestion.reading,
                        ).in_(keys),
                    )
                    .order_by(
                        WordSuggestion.created_at.asc(),
                        WordSuggestion.id.asc(),
                    )
                ).mappings()
            )

        details: dict[
            tuple[str, str], list[PendingSuggestionDetail]
        ] = {key: [] for key in keys}
        for row in detail_rows:
            details[(row["surface"], row["reading"])].append(
                PendingSuggestionDetail(
                    submitter_display_name=_display_name(
                        row["username"],
                        row["display_name"],
                    ),
                    note=row["note"],
                    created_at=_stored_utc(row["created_at"]),
                )
            )

        return tuple(
            PendingSuggestionGroup(
                surface=row["surface"],
                reading=row["reading"],
                submission_count=int(row["submission_count"]),
                first_submitted_at=_stored_utc(
                    row["first_submitted_at"]
                ),
                last_submitted_at=_stored_utc(
                    row["last_submitted_at"]
                ),
                submissions=tuple(
                    details[(row["surface"], row["reading"])]
                ),
            )
            for row in grouped_rows
        )

    def list_recent_reviews(
        self,
        reviewer_user_id: str,
        *,
        limit: int = 50,
    ) -> tuple[ReviewedSuggestionView, ...]:
        """List recent immutable decision rows for audit display."""

        reviewer_id = _user_id(reviewer_user_id)
        maximum = _limit(limit)
        with self.database.read_session() as session:
            self._require_admin(session, reviewer_id)
            rows = tuple(
                session.execute(
                    _recent_reviews_statement(maximum)
                ).mappings()
            )
        return tuple(
            ReviewedSuggestionView(
                surface=row["surface"],
                reading=row["reading"],
                status=row["decision"],
                review_note=row["review_note"],
                reviewed_at=_stored_utc(row["reviewed_at"]),
                reviewer_display_name=_display_name(
                    row["reviewer_username"],
                    row["reviewer_display_name"],
                ),
                submitter_display_name=_display_name(
                    row["submitter_username"],
                    row["submitter_display_name"],
                ),
            )
            for row in rows
        )

    def review_group(
        self,
        reviewer_user_id: str,
        surface: str | None,
        reading: str | None,
        decision: ReviewDecision | str,
        review_note: str | None = None,
    ) -> WordReviewResult:
        """Finalize every currently stored request in one exact group."""

        reviewer_id = _user_id(reviewer_user_id)
        clean_surface, clean_reading = _review_target(surface, reading)
        selected_decision = _decision(decision)
        clean_note = _review_note(review_note)
        approved_entry: ApprovedWordEntry | None = None
        approved_word_added = False

        with self.database.transaction() as session:
            self._require_admin(
                session,
                reviewer_id,
                for_update=True,
            )
            suggestions = tuple(
                session.scalars(
                    select(WordSuggestion)
                    .where(
                        WordSuggestion.surface == clean_surface,
                        WordSuggestion.reading == clean_reading,
                    )
                    .order_by(
                        WordSuggestion.created_at.asc(),
                        WordSuggestion.id.asc(),
                    )
                    .with_for_update()
                )
            )
            if not suggestions:
                raise WordReviewNotFoundError()

            review_rows = tuple(
                session.scalars(
                    select(WordSuggestionReview)
                    .where(
                        WordSuggestionReview.suggestion_id.in_(
                            tuple(
                                suggestion.id
                                for suggestion in suggestions
                            )
                        )
                    )
                    .with_for_update()
                )
            )
            reviews_by_suggestion_id = {
                review.suggestion_id: review
                for review in review_rows
            }
            finalized = tuple(
                suggestion
                for suggestion in suggestions
                if suggestion.status != "pending"
            )
            if any(
                suggestion.status != selected_decision.value
                or (
                    review := reviews_by_suggestion_id.get(
                        suggestion.id
                    )
                )
                is None
                or review.decision != suggestion.status
                for suggestion in finalized
            ):
                raise WordReviewConflictError()

            approved_word = session.scalar(
                select(ApprovedWord)
                .where(
                    ApprovedWord.surface == clean_surface,
                    ApprovedWord.reading == clean_reading,
                )
                .with_for_update()
            )
            if (
                selected_decision is ReviewDecision.REJECTED
                and approved_word is not None
            ):
                raise WordReviewConflictError()

            pending = tuple(
                suggestion
                for suggestion in suggestions
                if suggestion.status == "pending"
            )
            if not pending:
                if selected_decision is ReviewDecision.APPROVED:
                    if approved_word is None:
                        # Repair a legacy/incomplete approved decision without
                        # changing its immutable review audit fields.
                        source = suggestions[0]
                        source_review = reviews_by_suggestion_id.get(
                            source.id
                        )
                        approved_word = ApprovedWord(
                            id=new_id(),
                            surface=clean_surface,
                            reading=clean_reading,
                            approved_by_user_id=(
                                source_review.reviewer_user_id
                                if source_review is not None
                                else reviewer_id
                            ),
                            source_suggestion_id=source.id,
                            approved_at=(
                                source_review.reviewed_at
                                if source_review is not None
                                else (
                                    source.reviewed_at
                                    or _aware_utc(self._clock())
                                )
                            ),
                        )
                        session.add(approved_word)
                        session.flush()
                        approved_word_added = True
                    approved_entry = _approved_entry(approved_word)
                result = WordReviewResult(
                    surface=clean_surface,
                    reading=clean_reading,
                    decision=selected_decision,
                    reviewed_count=0,
                    replayed=True,
                    approved_word_added=approved_word_added,
                )
            else:
                reviewed_at = _aware_utc(self._clock())
                for suggestion in pending:
                    suggestion.status = selected_decision.value
                    suggestion.reviewed_at = reviewed_at
                    suggestion.updated_at = reviewed_at
                    session.add(
                        WordSuggestionReview(
                            suggestion_id=suggestion.id,
                            reviewer_user_id=reviewer_id,
                            decision=selected_decision.value,
                            review_note=clean_note,
                            reviewed_at=reviewed_at,
                        )
                    )

                if selected_decision is ReviewDecision.APPROVED:
                    if approved_word is None:
                        approved_word = ApprovedWord(
                            id=new_id(),
                            surface=clean_surface,
                            reading=clean_reading,
                            approved_by_user_id=reviewer_id,
                            source_suggestion_id=pending[0].id,
                            approved_at=reviewed_at,
                        )
                        session.add(approved_word)
                        approved_word_added = True
                    session.flush()
                    approved_entry = _approved_entry(approved_word)
                else:
                    session.flush()

                result = WordReviewResult(
                    surface=clean_surface,
                    reading=clean_reading,
                    decision=selected_decision,
                    reviewed_count=len(pending),
                    replayed=False,
                    approved_word_added=approved_word_added,
                )

        # Publish only after the transaction commits.  A rollback can never
        # make an uncommitted word playable in this process.
        if approved_entry is not None:
            self.catalog.add(
                word_id=approved_entry.id,
                surface=approved_entry.surface,
                reading=approved_entry.reading,
            )
        return result

    def _require_admin(
        self,
        session: Session,
        user_id: str,
        *,
        for_update: bool = False,
    ) -> User:
        statement = select(User).where(User.id == user_id)
        if for_update:
            statement = statement.with_for_update()
        user = session.scalar(statement)
        if not self._is_allowed_user(user):
            raise WordReviewAuthorizationError()
        return user

    def _is_allowed_user(self, user: User | None) -> bool:
        return (
            user is not None
            and user.disabled_at is None
            and user.username_key in self.admin_username_keys
        )


__all__ = [
    "ApprovedLexiconValidator",
    "ApprovedWordCatalog",
    "ApprovedWordEntry",
    "MAX_REVIEW_LIST_LIMIT",
    "PendingSuggestionDetail",
    "PendingSuggestionGroup",
    "ReviewDecision",
    "ReviewedSuggestionView",
    "WordReviewAuthorizationError",
    "WordReviewConfigurationError",
    "WordReviewConflictError",
    "WordReviewError",
    "WordReviewNotFoundError",
    "WordReviewResult",
    "WordReviewService",
    "WordReviewValidationError",
    "admin_username_keys_from_value",
]
