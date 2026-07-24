"""Theme membership for dictionary-approved shiritori words.

Themes deliberately identify a word by both its normalized surface and its
dictionary reading.  A reading alone is not enough: Japanese homophones can
have unrelated meanings, so adding ``橋（はし）`` must not silently add
``箸（はし）`` as well.

This module never trusts a reading supplied by a browser.  A selected reading
is accepted only when it is present in the original :class:`LexiconResult`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from types import MappingProxyType
from typing import Iterable, Mapping

from .lexicon import (
    LexiconCandidate,
    LexiconResult,
    katakana_to_hiragana,
    normalize_surface,
)


ALL_THEME_ID = "all"
_THEME_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def normalize_reading(reading: str) -> str:
    """Normalize a stored/selected dictionary reading to hiragana."""

    return katakana_to_hiragana(normalize_surface(reading))


@dataclass(frozen=True, slots=True, order=True)
class ThemeEntry:
    """One explicit semantic member of a theme."""

    surface: str
    reading: str

    def __post_init__(self) -> None:
        surface = normalize_surface(self.surface)
        reading = normalize_reading(self.reading)
        if not surface:
            raise ValueError("theme entry surface is required")
        if not reading:
            raise ValueError("theme entry reading is required")
        object.__setattr__(self, "surface", surface)
        object.__setattr__(self, "reading", reading)

    @property
    def key(self) -> tuple[str, str]:
        return (self.surface, self.reading)


ThemeEntryInput = ThemeEntry | tuple[str, str]


def _coerce_entry(entry: ThemeEntryInput) -> ThemeEntry:
    if isinstance(entry, ThemeEntry):
        return entry
    try:
        surface, reading = entry
    except (TypeError, ValueError) as error:
        raise TypeError(
            "theme entries must be ThemeEntry or (surface, reading) pairs"
        ) from error
    return ThemeEntry(surface, reading)


@dataclass(frozen=True, slots=True)
class ThemeDefinition:
    """A named immutable set of surface-and-reading pairs."""

    theme_id: str
    label: str
    entries: frozenset[ThemeEntry] = frozenset()
    allows_everything: bool = False

    def __post_init__(self) -> None:
        theme_id = str(self.theme_id).strip().lower()
        label = str(self.label).strip()
        if not _THEME_ID_PATTERN.fullmatch(theme_id):
            raise ValueError(
                "theme_id must start with a-z and contain only "
                "a-z, 0-9, '_' or '-' (maximum 32 characters)"
            )
        if not label:
            raise ValueError("theme label is required")
        entries = frozenset(_coerce_entry(entry) for entry in self.entries)
        if self.allows_everything and theme_id != ALL_THEME_ID:
            raise ValueError("only the built-in 'all' theme may allow everything")
        object.__setattr__(self, "theme_id", theme_id)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "entries", entries)

    @classmethod
    def from_entries(
        cls,
        theme_id: str,
        label: str,
        entries: Iterable[ThemeEntryInput],
    ) -> "ThemeDefinition":
        """Create a theme from easy-to-edit ``(surface, reading)`` pairs."""

        return cls(
            theme_id=theme_id,
            label=label,
            entries=frozenset(_coerce_entry(entry) for entry in entries),
        )

    def contains(self, surface: str, reading: str) -> bool:
        if self.allows_everything:
            return True
        return ThemeEntry(surface, reading) in self.entries


ALL_THEME = ThemeDefinition(
    theme_id=ALL_THEME_ID,
    label="すべて",
    allows_everything=True,
)


class ThemeFilterCode(str, Enum):
    """Machine-readable outcome of applying a theme to a lexicon result."""

    ALLOWED = "allowed"
    READING_REQUIRED = "reading_required"
    OUTSIDE_THEME = "outside_theme"
    INVALID_READING_CHOICE = "invalid_reading_choice"
    LEXICON_REJECTED = "lexicon_rejected"


_FILTER_MESSAGES = {
    ThemeFilterCode.ALLOWED: "テーマに含まれる単語です。",
    ThemeFilterCode.READING_REQUIRED: "使用する読みを選んでください。",
    ThemeFilterCode.OUTSIDE_THEME: "選択中のテーマに含まれない単語です。",
    ThemeFilterCode.INVALID_READING_CHOICE: (
        "辞書候補にない読みは選択できません。"
    ),
}


@dataclass(frozen=True, slots=True)
class ThemeFilterResult:
    """The result of checking one lexicon result against one theme."""

    code: ThemeFilterCode
    theme: ThemeDefinition
    lexicon_result: LexiconResult
    message: str
    allowed_readings: tuple[str, ...] = ()
    selected_candidate: LexiconCandidate | None = None

    @property
    def accepted(self) -> bool:
        return self.code is ThemeFilterCode.ALLOWED

    @property
    def requires_reading_choice(self) -> bool:
        return self.code is ThemeFilterCode.READING_REQUIRED

    @property
    def outside_theme(self) -> bool:
        return self.code is ThemeFilterCode.OUTSIDE_THEME


def _eligible_candidates(
    result: LexiconResult,
    theme: ThemeDefinition,
) -> tuple[LexiconCandidate, ...]:
    return tuple(
        candidate
        for candidate in result.candidates
        if theme.contains(result.surface, candidate.reading)
    )


def filter_lexicon_result(
    result: LexiconResult,
    theme: ThemeDefinition,
    *,
    selected_reading: str | None = None,
) -> ThemeFilterResult:
    """Apply ``theme`` without ever inventing or auto-selecting a reading.

    For an ambiguous dictionary result, the caller receives
    :attr:`ThemeFilterCode.READING_REQUIRED` even if only one of the readings
    belongs to the theme.  The player must explicitly select it.  On the next
    call, ``selected_reading`` is matched against the server-created candidates
    before theme membership is checked.
    """

    if not result.is_dictionary_word:
        return ThemeFilterResult(
            code=ThemeFilterCode.LEXICON_REJECTED,
            theme=theme,
            lexicon_result=result,
            message=result.message,
        )

    eligible = _eligible_candidates(result, theme)
    eligible_readings = tuple(
        dict.fromkeys(candidate.reading for candidate in eligible)
    )

    if selected_reading is None:
        if not eligible:
            return ThemeFilterResult(
                code=ThemeFilterCode.OUTSIDE_THEME,
                theme=theme,
                lexicon_result=result,
                message=_FILTER_MESSAGES[ThemeFilterCode.OUTSIDE_THEME],
            )
        if result.requires_reading_choice:
            return ThemeFilterResult(
                code=ThemeFilterCode.READING_REQUIRED,
                theme=theme,
                lexicon_result=result,
                message=_FILTER_MESSAGES[ThemeFilterCode.READING_REQUIRED],
                allowed_readings=eligible_readings,
            )
        return ThemeFilterResult(
            code=ThemeFilterCode.ALLOWED,
            theme=theme,
            lexicon_result=result,
            message=_FILTER_MESSAGES[ThemeFilterCode.ALLOWED],
            allowed_readings=eligible_readings,
            selected_candidate=eligible[0],
        )

    normalized_reading = normalize_reading(selected_reading)
    dictionary_candidates = result.candidates_for_reading(normalized_reading)
    if not dictionary_candidates:
        return ThemeFilterResult(
            code=ThemeFilterCode.INVALID_READING_CHOICE,
            theme=theme,
            lexicon_result=result,
            message=_FILTER_MESSAGES[
                ThemeFilterCode.INVALID_READING_CHOICE
            ],
            allowed_readings=eligible_readings,
        )

    matching = tuple(
        candidate
        for candidate in dictionary_candidates
        if theme.contains(result.surface, candidate.reading)
    )
    if not matching:
        return ThemeFilterResult(
            code=ThemeFilterCode.OUTSIDE_THEME,
            theme=theme,
            lexicon_result=result,
            message=_FILTER_MESSAGES[ThemeFilterCode.OUTSIDE_THEME],
            allowed_readings=eligible_readings,
        )
    return ThemeFilterResult(
        code=ThemeFilterCode.ALLOWED,
        theme=theme,
        lexicon_result=result,
        message=_FILTER_MESSAGES[ThemeFilterCode.ALLOWED],
        allowed_readings=eligible_readings,
        selected_candidate=matching[0],
    )


class ThemeCatalog:
    """Registry for the built-in ``all`` theme and user-defined themes."""

    def __init__(
        self,
        themes: Iterable[ThemeDefinition] = (),
    ) -> None:
        self._themes: dict[str, ThemeDefinition] = {
            ALL_THEME_ID: ALL_THEME
        }
        for theme in themes:
            self.register(theme)

    @property
    def themes(self) -> tuple[ThemeDefinition, ...]:
        return tuple(self._themes.values())

    @property
    def by_id(self) -> Mapping[str, ThemeDefinition]:
        return MappingProxyType(self._themes)

    def get(self, theme_id: str) -> ThemeDefinition:
        normalized_id = str(theme_id).strip().lower()
        try:
            return self._themes[normalized_id]
        except KeyError as error:
            raise KeyError(f"unknown theme: {normalized_id}") from error

    def register(
        self,
        theme: ThemeDefinition,
        *,
        replace: bool = False,
    ) -> None:
        if theme.theme_id == ALL_THEME_ID:
            raise ValueError("the built-in 'all' theme cannot be replaced")
        if theme.theme_id in self._themes and not replace:
            raise ValueError(f"theme already registered: {theme.theme_id}")
        self._themes[theme.theme_id] = theme

    def filter(
        self,
        theme_id: str,
        result: LexiconResult,
        *,
        selected_reading: str | None = None,
    ) -> ThemeFilterResult:
        return filter_lexicon_result(
            result,
            self.get(theme_id),
            selected_reading=selected_reading,
        )

    # Descriptive alias for callers that prefer the longer name.
    filter_result = filter
