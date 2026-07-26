from __future__ import annotations

from collections import Counter
from functools import lru_cache
import unittest

from shiritori.bot_catalog import build_bot_catalog
from shiritori.bot_data import load_bot_word_options
from shiritori.lexicon import LexiconResult, get_default_validator
from shiritori.theme_data import (
    THEME_DATA_DIRECTORY,
    load_theme_rows,
    load_word_theme_rows,
)
from shiritori.user_themes import (
    FOOD_ADDITIONS,
    FOOD_THEME,
    USER_THEMES,
)


EXPECTED_LEGACY_CSV_COUNTS = {
    "food": 120,
    "animal": 120,
    "plant": 100,
    "sport": 60,
    "country": 80,
    "instrument": 70,
    "vehicle": 100,
    "fruit": 60,
    "vegetable": 55,
}

EXPECTED_THEME_COUNTS = {
    "food": 928,
    "animal": 819,
    "plant": 631,
    "sport": 95,
    "country": 92,
    "instrument": 116,
    "vehicle": 277,
    "fruit": 121,
    "vegetable": 77,
}

EXPECTED_FOOD_ADDITIONS = frozenset(
    {
        ("\u6797\u6a8e", "\u308a\u3093\u3054"),
        ("\u871c\u67d1", "\u307f\u304b\u3093"),
        ("\u897f\u74dc", "\u3059\u3044\u304b"),
        (
            "\u30e6\u30fc\u30ea\u30f3\u30c1\u30fc",
            "\u3086\u30fc\u308a\u3093\u3061\u30fc",
        ),
        ("\u6cb9\u6dcb\u9d8f", "\u3086\u30fc\u308a\u3093\u3061\u30fc"),
        ("\u6e6f\u8c46\u8150", "\u3086\u3069\u3046\u3075"),
    }
)

FRUIT_FOOD_PLANT_PAIRS = frozenset(
    {
        ("\u6797\u6a8e", "\u308a\u3093\u3054"),
        ("\u871c\u67d1", "\u307f\u304b\u3093"),
        ("\u897f\u74dc", "\u3059\u3044\u304b"),
    }
)

_SHARED_VALIDATOR = get_default_validator()


@lru_cache(maxsize=None)
def _validate_surface(surface: str) -> LexiconResult:
    """Reuse every exact Sudachi lookup across data and Bot-index tests."""

    return _SHARED_VALIDATOR.validate(surface)


class _CachedValidator:
    def validate(self, surface: str | None) -> LexiconResult:
        if surface is None:
            return _SHARED_VALIDATOR.validate(None)
        return _validate_surface(surface)


def _themes_for_pair(surface: str, reading: str) -> set[str]:
    return {
        theme.theme_id
        for theme in USER_THEMES
        if theme.contains(surface, reading)
    }


def _themes_for_surface(surface: str) -> set[str]:
    return {
        theme.theme_id
        for theme in USER_THEMES
        if any(entry.surface == surface for entry in theme.entries)
    }


class UserThemeTests(unittest.TestCase):
    def test_theme_ids_labels_and_generated_counts_are_stable(self) -> None:
        self.assertEqual(len(USER_THEMES), 9)

        theme_ids = tuple(theme.theme_id for theme in USER_THEMES)
        labels = tuple(theme.label for theme in USER_THEMES)
        actual_counts = {
            theme.theme_id: len(theme.entries)
            for theme in USER_THEMES
        }

        self.assertEqual(len(theme_ids), len(set(theme_ids)))
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(set(theme_ids), set(EXPECTED_THEME_COUNTS))
        self.assertEqual(actual_counts, EXPECTED_THEME_COUNTS)

    def test_unified_mapping_size_and_provenance_counts_are_stable(
        self,
    ) -> None:
        rows = load_word_theme_rows()

        self.assertEqual(len(rows), 2_872)
        self.assertEqual(
            Counter(row.source_kind for row in rows),
            Counter({"auto": 2_172, "reviewed": 700}),
        )
        self.assertEqual(
            len({(row.surface, row.reading) for row in rows}),
            len(rows),
        )

        bot_pairs = {
            (option.surface, option.reading)
            for option in load_bot_word_options()
        }
        reviewed_only = sum(
            row.source_kind == "reviewed"
            and (row.surface, row.reading) not in bot_pairs
            for row in rows
        )
        self.assertEqual(reviewed_only, 113)

    def test_every_theme_grows_beyond_its_legacy_seed_csv(self) -> None:
        actual_counts = {
            theme.theme_id: len(theme.entries)
            for theme in USER_THEMES
        }

        for theme_id, legacy_count in EXPECTED_LEGACY_CSV_COUNTS.items():
            with self.subTest(theme_id=theme_id):
                self.assertGreater(actual_counts[theme_id], legacy_count)
                self.assertGreaterEqual(
                    actual_counts[theme_id],
                    EXPECTED_THEME_COUNTS[theme_id],
                )

    def test_legacy_seed_csv_counts_are_stable(self) -> None:
        for theme_id, expected_count in (
            EXPECTED_LEGACY_CSV_COUNTS.items()
        ):
            with self.subTest(theme_id=theme_id):
                rows = load_theme_rows(
                    THEME_DATA_DIRECTORY / f"{theme_id}.csv"
                )
                self.assertEqual(len(rows), expected_count)

    def test_every_legacy_exact_pair_is_retained_in_unified_theme(
        self,
    ) -> None:
        themes_by_id = {
            theme.theme_id: theme
            for theme in USER_THEMES
        }

        for theme_id in EXPECTED_LEGACY_CSV_COUNTS:
            rows = load_theme_rows(
                THEME_DATA_DIRECTORY / f"{theme_id}.csv"
            )
            for row in rows:
                with self.subTest(
                    theme_id=theme_id,
                    pair=(row.surface, row.reading),
                ):
                    self.assertTrue(
                        themes_by_id[theme_id].contains(
                            row.surface,
                            row.reading,
                        )
                    )

    def test_all_six_user_food_additions_are_preserved_as_a_set(
        self,
    ) -> None:
        self.assertEqual(set(FOOD_ADDITIONS), EXPECTED_FOOD_ADDITIONS)
        self.assertEqual(len(FOOD_ADDITIONS), 6)

        for surface, reading in EXPECTED_FOOD_ADDITIONS:
            with self.subTest(surface=surface, reading=reading):
                self.assertTrue(FOOD_THEME.contains(surface, reading))

    def test_reviewed_polysemy_and_false_positive_regressions(self) -> None:
        expected_botanical = {"food", "plant", "fruit"}
        for surface, reading in FRUIT_FOOD_PLANT_PAIRS:
            with self.subTest(surface=surface):
                self.assertEqual(
                    _themes_for_pair(surface, reading),
                    expected_botanical,
                )

        self.assertIn("vehicle", _themes_for_surface("\u98db\u884c\u6a5f"))
        self.assertNotIn("animal", _themes_for_surface("\u98db\u884c\u6a5f"))
        self.assertNotIn("animal", _themes_for_surface("\u4eba"))
        self.assertNotIn("food", _themes_for_surface("\u80a9"))
        self.assertEqual(
            _themes_for_pair("\u30d0\u30b9", "\u3070\u3059"),
            {"vehicle"},
        )
        self.assertEqual(
            _themes_for_pair(
                "\u30b9\u30ab\u30c3\u30b7\u30e5",
                "\u3059\u304b\u3063\u3057\u3085",
            ),
            {"sport", "vegetable"},
        )

        for surface, reading in EXPECTED_FOOD_ADDITIONS:
            with self.subTest(food_pair=(surface, reading)):
                self.assertIn(
                    "food",
                    _themes_for_pair(surface, reading),
                )

    def test_every_legacy_seed_reading_matches_pinned_dictionary(
        self,
    ) -> None:
        for theme_id in EXPECTED_LEGACY_CSV_COUNTS:
            rows = load_theme_rows(
                THEME_DATA_DIRECTORY / f"{theme_id}.csv"
            )
            for row in rows:
                with self.subTest(
                    theme_id=theme_id,
                    surface=row.surface,
                    reading=row.reading,
                ):
                    result = _validate_surface(row.surface)
                    self.assertTrue(
                        result.is_dictionary_word,
                        result.message,
                    )
                    self.assertIn(row.reading, result.readings)

    def test_each_theme_builds_a_nonempty_connected_bot_index(self) -> None:
        validator = _CachedValidator()

        for theme in USER_THEMES:
            with self.subTest(theme_id=theme.theme_id):
                catalog = build_bot_catalog(
                    (entry.surface for entry in theme.entries),
                    validator=validator,
                    theme=theme,
                )
                self.assertTrue(catalog.options)

                canonical_keys = tuple(
                    option.canonical_key for option in catalog.options
                )
                self.assertEqual(
                    len(canonical_keys),
                    len(set(canonical_keys)),
                )
                self.assertGreaterEqual(
                    len({option.first_kana for option in catalog.options}),
                    2,
                )


if __name__ == "__main__":
    unittest.main()
