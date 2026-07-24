from __future__ import annotations

import unittest

from shiritori.lexicon import (
    LexiconCandidate,
    LexiconCode,
    LexiconResult,
)
from shiritori.themes import (
    ALL_THEME_ID,
    ThemeCatalog,
    ThemeDefinition,
    ThemeEntry,
    ThemeFilterCode,
    filter_lexicon_result,
)


def candidate(surface: str, reading: str, word_id: int) -> LexiconCandidate:
    return LexiconCandidate(
        surface=surface,
        reading=reading,
        lemma=surface,
        normalized_form=surface,
        part_of_speech=("名詞", "普通名詞", "一般", "*", "*", "*"),
        dictionary_id=0,
        word_id=word_id,
        canonical_key=reading,
    )


def result(
    surface: str,
    readings: tuple[str, ...],
) -> LexiconResult:
    code = (
        LexiconCode.ACCEPTED
        if len(readings) == 1
        else LexiconCode.MULTIPLE_READINGS
    )
    return LexiconResult(
        code=code,
        surface=surface,
        message=code.value,
        candidates=tuple(
            candidate(surface, reading, number)
            for number, reading in enumerate(readings)
        ),
    )


class ThemeDefinitionTests(unittest.TestCase):
    def test_easy_pair_api_normalizes_surface_and_reading(self) -> None:
        theme = ThemeDefinition.from_entries(
            "fruit",
            "果物",
            [("  ﾘﾝｺﾞ ", "リンゴ"), ThemeEntry("苺", "いちご")],
        )

        self.assertTrue(theme.contains("リンゴ", "りんご"))
        self.assertTrue(theme.contains("苺", "イチゴ"))
        self.assertFalse(theme.contains("林檎", "りんご"))

    def test_surface_and_reading_prevent_homophone_leakage(self) -> None:
        landmarks = ThemeDefinition.from_entries(
            "landmarks",
            "場所",
            [("橋", "はし")],
        )

        bridge = filter_lexicon_result(
            result("橋", ("はし",)),
            landmarks,
        )
        chopsticks = filter_lexicon_result(
            result("箸", ("はし",)),
            landmarks,
        )

        self.assertTrue(bridge.accepted)
        self.assertEqual(chopsticks.code, ThemeFilterCode.OUTSIDE_THEME)

    def test_catalog_always_contains_all_and_registers_user_theme(self) -> None:
        catalog = ThemeCatalog()
        catalog.register(
            ThemeDefinition.from_entries("food", "食べ物", [("寿司", "すし")])
        )

        self.assertEqual(catalog.themes[0].theme_id, ALL_THEME_ID)
        self.assertTrue(
            catalog.filter("all", result("箸", ("はし",))).accepted
        )
        self.assertTrue(
            catalog.filter("FOOD", result("寿司", ("すし",))).accepted
        )
        with self.assertRaises(ValueError):
            catalog.register(
                ThemeDefinition.from_entries("food", "重複", [])
            )


class ThemeFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.country = ThemeDefinition.from_entries(
            "country",
            "国",
            [("日本", "にほん")],
        )
        self.ambiguous = result("日本", ("にっぽん", "にほん"))

    def test_ambiguous_result_is_never_auto_selected(self) -> None:
        filtered = filter_lexicon_result(self.ambiguous, self.country)

        self.assertEqual(
            filtered.code,
            ThemeFilterCode.READING_REQUIRED,
        )
        self.assertFalse(filtered.accepted)
        self.assertEqual(filtered.allowed_readings, ("にほん",))
        self.assertIsNone(filtered.selected_candidate)
        self.assertEqual(filtered.lexicon_result, self.ambiguous)

    def test_selected_reading_must_match_server_candidates_and_theme(self) -> None:
        accepted = filter_lexicon_result(
            self.ambiguous,
            self.country,
            selected_reading="ニホン",
        )
        wrong_semantic_reading = filter_lexicon_result(
            self.ambiguous,
            self.country,
            selected_reading="にっぽん",
        )
        forged = filter_lexicon_result(
            self.ambiguous,
            self.country,
            selected_reading="にほんん",
        )

        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.selected_candidate.reading, "にほん")
        self.assertEqual(
            wrong_semantic_reading.code,
            ThemeFilterCode.OUTSIDE_THEME,
        )
        self.assertEqual(
            forged.code,
            ThemeFilterCode.INVALID_READING_CHOICE,
        )

    def test_rejected_lexicon_result_stays_explicit(self) -> None:
        rejected = LexiconResult(
            code=LexiconCode.NOT_IN_DICTIONARY,
            surface="架空語",
            message="辞書にありません。",
        )

        filtered = filter_lexicon_result(rejected, self.country)

        self.assertEqual(filtered.code, ThemeFilterCode.LEXICON_REJECTED)
        self.assertEqual(filtered.message, rejected.message)


if __name__ == "__main__":
    unittest.main()
