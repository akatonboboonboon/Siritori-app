from __future__ import annotations

from functools import lru_cache
import unittest

from shiritori.bot_catalog import build_bot_catalog
from shiritori.lexicon import LexiconResult, get_default_validator
from shiritori.theme_data import THEME_DATA_DIRECTORY, load_theme_rows
from shiritori.user_themes import (
    FOOD_ADDITIONS,
    FOOD_THEME,
    USER_THEMES,
)


EXPECTED_CSV_COUNTS = {
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

EXPECTED_FOOD_ADDITIONS = (
    ("林檎", "りんご"),
    ("蜜柑", "みかん"),
    ("西瓜", "すいか"),
)

KNOWN_SEMANTIC_MISMATCHES = {
    "plant": {"将棋", "雷魚", "花王", "チーズ", "コーラ", "高粱酒"},
    "sport": {"戦い", "戦闘"},
    "country": {
        "ソビエト社会主義共和国連邦",
        "ユーゴスラビア",
        "ドイツ民主共和国",
        "ビルマ",
        "越南",
        "カンプチア",
        "スワジランド",
    },
    "instrument": {"真鍮", "ペット", "三角形", "音叉"},
    "vehicle": {
        "キャット",
        "海賊",
        "馬力",
        "装甲",
        "仏頂面",
        "弾道弾",
        "キャタピラ",
    },
    "fruit": {
        "トウモロコシ",
        "エンパイア",
        "ダイズ",
        "南京豆",
        "ラッカセイ",
        "エノキ",
        "亜麻仁",
        "ヒヨコマメ",
        "ササゲ",
        "クミン",
        "ニワトコ",
        "蓖麻子",
        "グリーンピース",
        "蜀黍",
        "トチノキ",
        "扁豆",
    },
    "vegetable": {
        "フライドポテト",
        "マッシュポテト",
        "ベークドポテト",
    },
}

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


class UserThemeTests(unittest.TestCase):
    def test_theme_ids_and_labels_are_unique(self) -> None:
        self.assertEqual(len(USER_THEMES), 9)

        theme_ids = tuple(theme.theme_id for theme in USER_THEMES)
        labels = tuple(theme.label for theme in USER_THEMES)

        self.assertEqual(len(theme_ids), len(set(theme_ids)))
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(set(theme_ids), set(EXPECTED_CSV_COUNTS))

    def test_user_food_additions_are_preserved_in_food_theme(self) -> None:
        self.assertEqual(FOOD_ADDITIONS, EXPECTED_FOOD_ADDITIONS)

        for surface, reading in EXPECTED_FOOD_ADDITIONS:
            with self.subTest(surface=surface, reading=reading):
                self.assertTrue(FOOD_THEME.contains(surface, reading))

    def test_user_food_additions_stay_out_of_generated_food_csv(self) -> None:
        generated_entries = {
            (row.surface, row.reading)
            for row in load_theme_rows(THEME_DATA_DIRECTORY / "food.csv")
        }

        self.assertTrue(
            generated_entries.isdisjoint(FOOD_ADDITIONS),
            "User-authored food additions must not be absorbed into generated data",
        )

    def test_csv_row_counts_are_stable(self) -> None:
        for theme_id, expected_count in EXPECTED_CSV_COUNTS.items():
            with self.subTest(theme_id=theme_id):
                rows = load_theme_rows(
                    THEME_DATA_DIRECTORY / f"{theme_id}.csv"
                )
                self.assertEqual(len(rows), expected_count)

    def test_known_semantic_mismatches_are_excluded(self) -> None:
        for theme_id, excluded_surfaces in KNOWN_SEMANTIC_MISMATCHES.items():
            actual_surfaces = {
                row.surface
                for row in load_theme_rows(
                    THEME_DATA_DIRECTORY / f"{theme_id}.csv"
                )
            }
            for surface in excluded_surfaces:
                with self.subTest(theme_id=theme_id, surface=surface):
                    self.assertNotIn(surface, actual_surfaces)

    def test_every_csv_reading_matches_the_pinned_dictionary(self) -> None:
        for theme_id in EXPECTED_CSV_COUNTS:
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
