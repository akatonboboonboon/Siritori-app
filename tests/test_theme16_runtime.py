from __future__ import annotations

from collections import Counter
import unittest

from shiritori.theme_data import load_word_theme_rows
from shiritori.themes import ThemeCatalog
from shiritori.user_themes import THEME_LABELS, USER_THEMES


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
    "person_job": 2_726,
    "nature": 1_534,
    "place_building": 445,
    "body": 499,
    "clothing": 255,
    "daily_tools": 260,
    "music": 269,
}

EXPECTED_NEW_LABELS = {
    "person_job": "人物・職業",
    "nature": "自然",
    "place_building": "場所・建物",
    "body": "体・体の部位",
    "clothing": "服・身につけるもの",
    "daily_tools": "道具・生活用品",
    "music": "音楽・楽器",
}

POSITIVE_EXAMPLES = {
    "person_job": (("大工", "だいく"), ("看護師", "かんごし")),
    "nature": (("台風", "たいふう"), ("雷", "かみなり")),
    "place_building": (("公園", "こうえん"), ("神社", "じんじゃ")),
    "body": (("膵臓", "すいぞう"), ("眼球", "がんきゅう")),
    "clothing": (("帽子", "ぼうし"), ("着物", "きもの")),
    "daily_tools": (
        ("冷蔵庫", "れいぞうこ"),
        ("洗濯機", "せんたくき"),
    ),
    "music": (("歌", "うた"), ("楽譜", "がくふ")),
}

DENIED_SURFACES = {
    "person_job": {"ご存じ", "交友", "右腕"},
    "nature": {"母", "父", "赤ちゃん", "西", "バス"},
    "place_building": {"可動", "ナンパ", "合宿", "泊まり"},
    "body": {"ぼぼ", "ぐう", "縫合"},
    "clothing": {"防水", "シングル", "シートベルト", "出で立ち"},
    "daily_tools": {"主事", "とじ込み", "下ろし", "ポースレン"},
    "music": {"前文", "序説", "救世主", "器官", "三角形"},
}


class Theme16RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.themes = {theme.theme_id: theme for theme in USER_THEMES}
        cls.rows = load_word_theme_rows()

    def test_catalog_exposes_all_sixteen_unique_themes(self) -> None:
        self.assertEqual(len(USER_THEMES), 16)
        self.assertEqual(tuple(self.themes), tuple(EXPECTED_THEME_COUNTS))
        self.assertEqual(len(set(THEME_LABELS.values())), 16)
        for theme_id, label in EXPECTED_NEW_LABELS.items():
            with self.subTest(theme_id=theme_id):
                self.assertEqual(THEME_LABELS[theme_id], label)

        catalog = ThemeCatalog(USER_THEMES)
        self.assertEqual(len(catalog.themes), 17)
        for theme_id in EXPECTED_THEME_COUNTS:
            self.assertIs(catalog.get(theme_id), self.themes[theme_id])

    def test_unified_mapping_counts_are_pinned(self) -> None:
        self.assertEqual(len(self.rows), 7_371)
        self.assertEqual(
            Counter(row.source_kind for row in self.rows),
            Counter({"auto": 6_671, "reviewed": 700}),
        )
        self.assertEqual(
            sum(len(row.theme_ids) for row in self.rows),
            9_144,
        )
        self.assertEqual(
            {
                theme_id: len(self.themes[theme_id].entries)
                for theme_id in EXPECTED_THEME_COUNTS
            },
            EXPECTED_THEME_COUNTS,
        )

    def test_each_new_theme_contains_stable_positive_examples(self) -> None:
        for theme_id, examples in POSITIVE_EXAMPLES.items():
            for surface, reading in examples:
                with self.subTest(
                    theme_id=theme_id,
                    surface=surface,
                ):
                    self.assertTrue(
                        self.themes[theme_id].contains(surface, reading)
                    )

    def test_reviewed_hierarchical_multi_labels_are_preserved(self) -> None:
        expected = {
            ("猫", "ねこ"): {"animal", "nature"},
            ("杉", "すぎ"): {"plant", "nature"},
            ("川", "かわ"): {"nature", "place_building"},
            ("太鼓", "たいこ"): {"instrument", "music"},
        }
        actual = {
            (row.surface, row.reading): set(row.theme_ids)
            for row in self.rows
            if (row.surface, row.reading) in expected
        }
        self.assertEqual(actual, expected)

    def test_review_deny_surfaces_stay_out_of_each_target_theme(self) -> None:
        self.assertEqual(
            sum(len(surfaces) for surfaces in DENIED_SURFACES.values()),
            28,
        )
        for theme_id, denied in DENIED_SURFACES.items():
            actual_surfaces = {
                entry.surface for entry in self.themes[theme_id].entries
            }
            for surface in denied:
                with self.subTest(theme_id=theme_id, surface=surface):
                    self.assertNotIn(surface, actual_surfaces)


if __name__ == "__main__":
    unittest.main()
