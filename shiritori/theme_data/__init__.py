"""Strict loader for checked-in theme vocabulary CSV files.

Theme data is intentionally read from local, versioned files.  The loader
never downloads data and never guesses missing readings or provenance.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Final

from ..theme_rules import THEME_IDS
from ..themes import ThemeEntry


THEME_DATA_DIRECTORY: Final = Path(__file__).resolve().parent
THEME_DATA_HEADER: Final = ("surface", "reading", "source_ref")
WORD_THEME_DATA_PATH: Final = THEME_DATA_DIRECTORY / "word_themes.csv"
WORD_THEME_DATA_HEADER: Final = (
    "surface",
    "reading",
    "theme_ids",
    "source_kind",
    "source_ref",
)
REVIEWED_THEME_DATA_PATH: Final = (
    THEME_DATA_DIRECTORY / "reviewed_additions.csv"
)
REVIEWED_THEME_DATA_HEADER: Final = (
    "surface",
    "reading",
    "theme_ids",
    "source_ref",
)
THEME_SEPARATOR: Final = "|"
AUTO_THEME_SOURCE_REF: Final = "wnja:1.1-compatible-roots-v1"
WORD_THEME_SOURCE_KINDS: Final = ("auto", "reviewed")
REVIEWED_SOURCE_REF: Final = re.compile(
    r"^(?:wnja:\d{8}-[nvars]|manual:[a-z0-9][a-z0-9-]*)$"
)


class ThemeDataFormatError(ValueError):
    """Raised when a theme CSV does not follow the repository format."""


@dataclass(frozen=True, slots=True)
class ThemeDataRow:
    """One normalized theme entry and its non-empty source reference."""

    surface: str
    reading: str
    source_ref: str

    @property
    def entry(self) -> ThemeEntry:
        return ThemeEntry(self.surface, self.reading)


def load_theme_rows(path: str | Path) -> tuple[ThemeDataRow, ...]:
    """Load a strict UTF-8 ``surface,reading,source_ref`` CSV.

    The header and every row must contain exactly three columns. Empty values,
    malformed CSV, and duplicate normalized ``(surface, reading)`` pairs are
    rejected instead of being silently discarded by ``ThemeDefinition``.
    """

    csv_path = Path(path)
    rows: list[ThemeDataRow] = []
    seen_entries: set[tuple[str, str]] = set()

    try:
        with csv_path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            try:
                header = next(reader)
            except StopIteration as error:
                raise ThemeDataFormatError(
                    f"{csv_path}: CSV is empty"
                ) from error

            if tuple(header) != THEME_DATA_HEADER:
                expected = ",".join(THEME_DATA_HEADER)
                raise ThemeDataFormatError(
                    f"{csv_path}: header must be exactly {expected}"
                )

            for values in reader:
                line_number = reader.line_num
                if len(values) != len(THEME_DATA_HEADER):
                    raise ThemeDataFormatError(
                        f"{csv_path}:{line_number}: expected exactly "
                        f"{len(THEME_DATA_HEADER)} columns"
                    )

                surface, reading, source_ref = values
                if not surface.strip():
                    raise ThemeDataFormatError(
                        f"{csv_path}:{line_number}: surface is required"
                    )
                if not reading.strip():
                    raise ThemeDataFormatError(
                        f"{csv_path}:{line_number}: reading is required"
                    )
                if not source_ref.strip():
                    raise ThemeDataFormatError(
                        f"{csv_path}:{line_number}: source_ref is required"
                    )

                entry = ThemeEntry(surface, reading)
                if entry.key in seen_entries:
                    raise ThemeDataFormatError(
                        f"{csv_path}:{line_number}: duplicate theme entry "
                        f"{entry.surface!r}, {entry.reading!r}"
                    )
                seen_entries.add(entry.key)
                rows.append(
                    ThemeDataRow(
                        surface=entry.surface,
                        reading=entry.reading,
                        source_ref=source_ref.strip(),
                    )
                )
    except csv.Error as error:
        raise ThemeDataFormatError(
            f"{csv_path}: malformed CSV: {error}"
        ) from error

    return tuple(rows)


def load_theme_entries(path: str | Path) -> tuple[ThemeEntry, ...]:
    """Return only the normalized entries accepted by ``ThemeDefinition``."""

    return tuple(row.entry for row in load_theme_rows(path))


@dataclass(frozen=True, slots=True)
class WordThemeRow:
    """One exact pair with labels and explicit generation provenance."""

    surface: str
    reading: str
    theme_ids: frozenset[str]
    source_kind: str
    source_ref: str

    @property
    def entry(self) -> ThemeEntry:
        return ThemeEntry(self.surface, self.reading)


@dataclass(frozen=True, slots=True)
class ReviewedThemeDataRow:
    """One manually reviewed build-time addition to the unified mapping."""

    surface: str
    reading: str
    theme_ids: frozenset[str]
    source_ref: str

    @property
    def entry(self) -> ThemeEntry:
        return ThemeEntry(self.surface, self.reading)


def _parse_theme_ids(
    encoded: str,
    csv_path: Path,
    line_number: int,
) -> tuple[str, ...]:
    values = tuple(encoded.split(THEME_SEPARATOR))
    if any(not value for value in values) or len(values) != len(set(values)):
        raise ThemeDataFormatError(
            f"{csv_path}:{line_number}: theme_ids are malformed"
        )
    if set(values) - set(THEME_IDS):
        raise ThemeDataFormatError(
            f"{csv_path}:{line_number}: unknown theme_id"
        )
    canonical = tuple(theme_id for theme_id in THEME_IDS if theme_id in values)
    if values != canonical:
        raise ThemeDataFormatError(
            f"{csv_path}:{line_number}: theme_ids are not in canonical order"
        )
    return values


def _parse_reviewed_source_ref(
    encoded: str,
    csv_path: Path,
    line_number: int,
) -> tuple[str, ...]:
    values = tuple(encoded.split(THEME_SEPARATOR))
    if (
        any(REVIEWED_SOURCE_REF.fullmatch(value) is None for value in values)
        or values != tuple(sorted(set(values)))
    ):
        raise ThemeDataFormatError(
            f"{csv_path}:{line_number}: reviewed source_ref is malformed"
        )
    return values


def load_reviewed_theme_rows(
    path: str | Path = REVIEWED_THEME_DATA_PATH,
) -> tuple[ReviewedThemeDataRow, ...]:
    """Load explicit build-time additions; runtime never reads this file."""

    csv_path = Path(path)
    rows: list[ReviewedThemeDataRow] = []
    seen_entries: set[tuple[str, str]] = set()
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            try:
                header = next(reader)
            except StopIteration as error:
                raise ThemeDataFormatError(
                    f"{csv_path}: CSV is empty"
                ) from error
            if tuple(header) != REVIEWED_THEME_DATA_HEADER:
                expected = ",".join(REVIEWED_THEME_DATA_HEADER)
                raise ThemeDataFormatError(
                    f"{csv_path}: header must be exactly {expected}"
                )
            for values in reader:
                line_number = reader.line_num
                if len(values) != len(REVIEWED_THEME_DATA_HEADER):
                    raise ThemeDataFormatError(
                        f"{csv_path}:{line_number}: expected exactly "
                        f"{len(REVIEWED_THEME_DATA_HEADER)} columns"
                    )
                surface, reading, encoded_theme_ids, source_ref = values
                if not all(values):
                    raise ThemeDataFormatError(
                        f"{csv_path}:{line_number}: values must be non-empty"
                    )
                entry = ThemeEntry(surface, reading)
                if entry.surface != surface or entry.reading != reading:
                    raise ThemeDataFormatError(
                        f"{csv_path}:{line_number}: pair is not normalized"
                    )
                if entry.key in seen_entries:
                    raise ThemeDataFormatError(
                        f"{csv_path}:{line_number}: duplicate theme entry"
                    )
                theme_ids = _parse_theme_ids(
                    encoded_theme_ids, csv_path, line_number
                )
                _parse_reviewed_source_ref(source_ref, csv_path, line_number)
                seen_entries.add(entry.key)
                rows.append(
                    ReviewedThemeDataRow(
                        surface=entry.surface,
                        reading=entry.reading,
                        theme_ids=frozenset(theme_ids),
                        source_ref=source_ref,
                    )
                )
    except csv.Error as error:
        raise ThemeDataFormatError(
            f"{csv_path}: malformed CSV: {error}"
        ) from error
    return tuple(rows)


def load_word_theme_rows(
    path: str | Path = WORD_THEME_DATA_PATH,
) -> tuple[WordThemeRow, ...]:
    """Load the unified runtime map with provenance-aware pair checks.

    Automatic rows must refer to an exact pair in the general Bot vocabulary.
    Reviewed rows may intentionally coexist with a conflicting general reading;
    the offline builder validates every reviewed reading through Sudachi.
    """

    from ..bot_data import load_bot_word_options

    csv_path = Path(path)
    bot_entries = {
        (option.surface, option.reading)
        for option in load_bot_word_options()
    }
    rows: list[WordThemeRow] = []
    seen_entries: set[tuple[str, str]] = set()
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            try:
                header = next(reader)
            except StopIteration as error:
                raise ThemeDataFormatError(
                    f"{csv_path}: CSV is empty"
                ) from error
            if tuple(header) != WORD_THEME_DATA_HEADER:
                expected = ",".join(WORD_THEME_DATA_HEADER)
                raise ThemeDataFormatError(
                    f"{csv_path}: header must be exactly {expected}"
                )

            for values in reader:
                line_number = reader.line_num
                if len(values) != len(WORD_THEME_DATA_HEADER):
                    raise ThemeDataFormatError(
                        f"{csv_path}:{line_number}: expected exactly "
                        f"{len(WORD_THEME_DATA_HEADER)} columns"
                    )
                (
                    surface,
                    reading,
                    encoded_theme_ids,
                    source_kind,
                    source_ref,
                ) = values
                if not all(values):
                    raise ThemeDataFormatError(
                        f"{csv_path}:{line_number}: values must be non-empty"
                    )
                entry = ThemeEntry(surface, reading)
                if entry.surface != surface or entry.reading != reading:
                    raise ThemeDataFormatError(
                        f"{csv_path}:{line_number}: pair is not normalized"
                    )
                if entry.key in seen_entries:
                    raise ThemeDataFormatError(
                        f"{csv_path}:{line_number}: duplicate theme entry"
                    )
                theme_ids = _parse_theme_ids(
                    encoded_theme_ids, csv_path, line_number
                )
                if source_kind not in WORD_THEME_SOURCE_KINDS:
                    raise ThemeDataFormatError(
                        f"{csv_path}:{line_number}: unknown source_kind"
                    )
                if source_kind == "auto":
                    if source_ref != AUTO_THEME_SOURCE_REF:
                        raise ThemeDataFormatError(
                            f"{csv_path}:{line_number}: invalid auto source_ref"
                        )
                    if entry.key not in bot_entries:
                        raise ThemeDataFormatError(
                            f"{csv_path}:{line_number}: automatic pair is "
                            "absent from the general Bot vocabulary"
                        )
                else:
                    _parse_reviewed_source_ref(
                        source_ref, csv_path, line_number
                    )

                seen_entries.add(entry.key)
                rows.append(
                    WordThemeRow(
                        surface=entry.surface,
                        reading=entry.reading,
                        theme_ids=frozenset(theme_ids),
                        source_kind=source_kind,
                        source_ref=source_ref,
                    )
                )
    except csv.Error as error:
        raise ThemeDataFormatError(
            f"{csv_path}: malformed CSV: {error}"
        ) from error
    return tuple(rows)

__all__ = [
    "AUTO_THEME_SOURCE_REF",
    "REVIEWED_THEME_DATA_HEADER",
    "REVIEWED_THEME_DATA_PATH",
    "THEME_DATA_DIRECTORY",
    "THEME_DATA_HEADER",
    "THEME_SEPARATOR",
    "WORD_THEME_DATA_HEADER",
    "WORD_THEME_DATA_PATH",
    "WORD_THEME_SOURCE_KINDS",
    "ReviewedThemeDataRow",
    "ThemeDataFormatError",
    "ThemeDataRow",
    "WordThemeRow",
    "load_reviewed_theme_rows",
    "load_theme_entries",
    "load_theme_rows",
    "load_word_theme_rows",
]
