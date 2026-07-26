"""Load the checked-in, offline-validated general Bot vocabulary."""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
import re
from typing import Final

from ..bots import WordOption
from ..lexicon import katakana_to_hiragana, normalize_surface


BOT_DATA_PATH: Final = Path(__file__).with_name("words.csv")
CSV_HEADER: Final = ("surface", "reading", "source_ref")
SOURCE_REFERENCE: Final = re.compile(
    r"^(?:curated|wnja:\d{8}-[nvars])$"
)


class BotDataError(ValueError):
    """Raised when the checked-in Bot vocabulary is malformed."""


def _is_hiragana_reading(reading: str) -> bool:
    return bool(reading) and all(
        "\u3041" <= character <= "\u3096" or character == "ー"
        for character in reading
    )


def _row_error(row_number: int, message: str) -> BotDataError:
    return BotDataError(f"Bot data row {row_number}: {message}")


@lru_cache(maxsize=1)
def load_bot_word_options() -> tuple[WordOption, ...]:
    """Return immutable options without any runtime external lookup."""

    options: list[WordOption] = []
    seen_surfaces: set[str] = set()
    seen_readings: set[str] = set()
    with BOT_DATA_PATH.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != CSV_HEADER:
            raise BotDataError("Bot data header is invalid")
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise _row_error(row_number, "unexpected extra column")
            surface = row.get("surface", "")
            reading = row.get("reading", "")
            source_ref = row.get("source_ref", "")
            if (
                not surface
                or normalize_surface(surface) != surface
                or any(character.isspace() for character in surface)
            ):
                raise _row_error(row_number, "surface is not normalized")
            if (
                not _is_hiragana_reading(reading)
                or katakana_to_hiragana(normalize_surface(reading))
                != reading
            ):
                raise _row_error(row_number, "reading is invalid")
            if SOURCE_REFERENCE.fullmatch(source_ref) is None:
                raise _row_error(row_number, "source_ref is invalid")
            if surface in seen_surfaces:
                raise _row_error(row_number, "surface is duplicated")
            if reading in seen_readings:
                raise _row_error(row_number, "reading is duplicated")

            option = WordOption(
                surface=surface,
                reading=reading,
                canonical_key=reading,
                rank=len(options),
            )
            try:
                option.first_kana
                option.last_kana
            except ValueError as error:
                raise _row_error(
                    row_number, "reading cannot be chained"
                ) from error
            seen_surfaces.add(surface)
            seen_readings.add(reading)
            options.append(option)

    if not options:
        raise BotDataError("Bot vocabulary is empty")
    return tuple(options)


__all__ = [
    "BOT_DATA_PATH",
    "BotDataError",
    "load_bot_word_options",
]
