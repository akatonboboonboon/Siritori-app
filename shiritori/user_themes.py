"""Nine compatible themes backed by one unified offline mapping."""

from __future__ import annotations

from typing import Final

from .theme_data import THEME_SEPARATOR, load_word_theme_rows
from .themes import ThemeDefinition, ThemeEntryInput


THEME_LABELS: Final[dict[str, str]] = {
    "food": "食べ物・飲み物",
    "animal": "動物",
    "plant": "植物",
    "sport": "スポーツ",
    "country": "国・地域",
    "instrument": "楽器",
    "vehicle": "乗り物",
    "fruit": "果物・木の実",
    "vegetable": "野菜・きのこ",
}

_WORD_THEME_ROWS: Final = load_word_theme_rows()
_MANUAL_FOOD_SOURCE_REF: Final = "manual:user-food-v1"

# Preserve the public compatibility name without duplicating vocabulary in
# Python.  These six reviewed rows live in the generated unified mapping.
FOOD_ADDITIONS: Final[tuple[ThemeEntryInput, ...]] = tuple(
    (row.surface, row.reading)
    for row in _WORD_THEME_ROWS
    if _MANUAL_FOOD_SOURCE_REF in row.source_ref.split(THEME_SEPARATOR)
)


def _build_theme(theme_id: str, label: str) -> ThemeDefinition:
    return ThemeDefinition.from_entries(
        theme_id,
        label,
        (
            (row.surface, row.reading)
            for row in _WORD_THEME_ROWS
            if theme_id in row.theme_ids
        ),
    )


USER_THEMES: Final[tuple[ThemeDefinition, ...]] = tuple(
    _build_theme(theme_id, label)
    for theme_id, label in THEME_LABELS.items()
)

# Preserve the original public name used while only the hand-written food
# theme existed.
FOOD_THEME: Final = USER_THEMES[0]


__all__ = ["FOOD_ADDITIONS", "FOOD_THEME", "THEME_LABELS", "USER_THEMES"]
