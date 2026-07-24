"""Strict loader for checked-in theme vocabulary CSV files.

Theme data is intentionally read from local, versioned files.  The loader
never downloads data and never guesses missing readings or provenance.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..themes import ThemeEntry


THEME_DATA_DIRECTORY: Final = Path(__file__).resolve().parent
THEME_DATA_HEADER: Final = ("surface", "reading", "source_ref")


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


__all__ = [
    "THEME_DATA_DIRECTORY",
    "THEME_DATA_HEADER",
    "ThemeDataFormatError",
    "ThemeDataRow",
    "load_theme_entries",
    "load_theme_rows",
]
