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
CSV_HEADER: Final = (
    "surface",
    "reading",
    "source_ref",
    "commonness_tier",
)
SOURCE_REFERENCE: Final = re.compile(
    r"^(?:curated|wnja:\d{8}-[nvars]|tkg:[A-Za-z0-9][A-Za-z0-9_-]*)$"
)
COMMONNESS_TIERS: Final = (
    "curated",
    "basic",
    "core",
    "general",
    "wordnet",
)
_TIER_ORDER: Final = {
    tier: position for position, tier in enumerate(COMMONNESS_TIERS)
}


class BotDataError(ValueError):
    """Raised when the checked-in Bot vocabulary is malformed."""


def _is_hiragana_reading(reading: str) -> bool:
    return bool(reading) and all(
        "\u3041" <= character <= "\u3096" or character == "ー"
        for character in reading
    )


def _row_error(row_number: int, message: str) -> BotDataError:
    return BotDataError(f"Bot data row {row_number}: {message}")


def _source_matches_tier(source_ref: str, tier: str) -> bool:
    if tier == "curated":
        return source_ref == "curated"
    if source_ref == "curated":
        return False
    if source_ref.startswith("tkg:"):
        return tier in {"basic", "core"}
    return source_ref.startswith("wnja:")


@lru_cache(maxsize=1)
def load_bot_word_options() -> tuple[WordOption, ...]:
    """Return immutable options ordered by their offline commonness tier."""

    options: list[WordOption] = []
    seen_surfaces: set[str] = set()
    seen_readings: set[str] = set()
    previous_tier_order = -1
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
            commonness_tier = row.get("commonness_tier", "")
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
            if commonness_tier not in _TIER_ORDER:
                raise _row_error(row_number, "commonness_tier is invalid")
            tier_order = _TIER_ORDER[commonness_tier]
            if tier_order < previous_tier_order:
                raise _row_error(
                    row_number, "commonness_tier order is invalid"
                )
            if not _source_matches_tier(source_ref, commonness_tier):
                raise _row_error(
                    row_number,
                    "source_ref and commonness_tier do not match",
                )
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
            previous_tier_order = tier_order
            seen_surfaces.add(surface)
            seen_readings.add(reading)
            options.append(option)

    if not options:
        raise BotDataError("Bot vocabulary is empty")
    return tuple(options)


__all__ = [
    "BOT_DATA_PATH",
    "BotDataError",
    "COMMONNESS_TIERS",
    "CSV_HEADER",
    "load_bot_word_options",
]
