"""Versioned theme definitions plus the user's own three food words."""

from __future__ import annotations

from typing import Final

from .theme_data import THEME_DATA_DIRECTORY, load_theme_entries
from .themes import ThemeDefinition, ThemeEntryInput


# These three entries are user-authored. Keep them explicit and separate from
# generated data so future CSV rebuilds can never remove them.
FOOD_ADDITIONS: Final[tuple[ThemeEntryInput, ...]] = (
    ("林檎", "りんご"),
    ("蜜柑", "みかん"),
    ("西瓜", "すいか"),
)

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


def _build_theme(theme_id: str, label: str) -> ThemeDefinition:
    entries: list[ThemeEntryInput] = list(
        load_theme_entries(THEME_DATA_DIRECTORY / f"{theme_id}.csv")
    )
    if theme_id == "food":
        entries.extend(FOOD_ADDITIONS)
    return ThemeDefinition.from_entries(theme_id, label, entries)


USER_THEMES: Final[tuple[ThemeDefinition, ...]] = tuple(
    _build_theme(theme_id, label)
    for theme_id, label in THEME_LABELS.items()
)

# Preserve the original public name used while only the hand-written food
# theme existed.
FOOD_THEME: Final = USER_THEMES[0]


__all__ = ["FOOD_ADDITIONS", "FOOD_THEME", "THEME_LABELS", "USER_THEMES"]
