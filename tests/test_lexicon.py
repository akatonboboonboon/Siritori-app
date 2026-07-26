"""Sudachi辞書を使う単語判定のテスト。"""

from __future__ import annotations

import unittest

from shiritori.lexicon import (
    LexiconCode,
    LexiconValidator,
    MAX_SURFACE_LENGTH,
    katakana_to_hiragana,
    normalize_surface,
)


class LexiconValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = LexiconValidator()

    def test_normalizes_width_composition_and_edge_whitespace(self) -> None:
        self.assertEqual(normalize_surface("  ﾘﾝｺﾞ  "), "リンゴ")
        self.assertEqual(normalize_surface("か\u3099"), "が")
        self.assertEqual(normalize_surface(None), "")

    def test_converts_katakana_reading_to_hiragana(self) -> None:
        self.assertEqual(katakana_to_hiragana("コーヒー"), "こーひー")
        self.assertEqual(
            katakana_to_hiragana("ヴァイオリン"),
            "ゔぁいおりん",
        )

    def test_rejects_empty_input(self) -> None:
        for raw in (None, "", " \t\r\n "):
            with self.subTest(raw=raw):
                result = self.validator.validate(raw)
                self.assertEqual(result.code, LexiconCode.EMPTY)
                self.assertFalse(result.is_dictionary_word)

    def test_rejects_internal_whitespace(self) -> None:
        for raw in ("りん ご", "りん\u3000ご", "りん\nご"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    self.validator.validate(raw).code,
                    LexiconCode.INTERNAL_WHITESPACE,
                )

    def test_rejects_overlong_input_before_lookup(self) -> None:
        result = self.validator.validate(
            "猫" * (MAX_SURFACE_LENGTH + 1)
        )
        self.assertEqual(result.code, LexiconCode.TOO_LONG)

    def test_rejects_emoji_latin_digits_controls_and_bad_marks(self) -> None:
        for raw in (
            "りんご😀",
            "apple",
            "りんご1",
            "りん\u200bご",
            "・りんご",
            "りんご・",
            "ーすし",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    self.validator.validate(raw).code,
                    LexiconCode.INVALID_CHARACTERS,
                )

    def test_rejects_single_hiragana(self) -> None:
        result = self.validator.validate("か")
        self.assertEqual(result.code, LexiconCode.SINGLE_HIRAGANA)

    def test_rejects_single_katakana(self) -> None:
        result = self.validator.validate("カ")
        self.assertEqual(result.code, LexiconCode.SINGLE_KATAKANA)

    def test_accepts_dictionary_backed_single_kanji_noun(self) -> None:
        result = self.validator.validate("蚊")

        self.assertEqual(result.code, LexiconCode.ACCEPTED)
        self.assertTrue(result.accepted)
        self.assertEqual(result.readings, ("か",))
        candidate = result.candidates[0]
        self.assertEqual(candidate.surface, "蚊")
        self.assertEqual(candidate.reading, "か")
        self.assertEqual(candidate.lemma, "蚊")
        self.assertEqual(
            candidate.part_of_speech[:2],
            ("名詞", "普通名詞"),
        )
        self.assertEqual(candidate.dictionary_id, 0)
        self.assertIsInstance(candidate.word_id, int)
        self.assertEqual(candidate.canonical_key, "か")

    def test_accepts_kanji_and_returns_hiragana_reading(self) -> None:
        result = self.validator.validate("林檎")

        self.assertEqual(result.code, LexiconCode.ACCEPTED)
        self.assertEqual(result.surface, "林檎")
        self.assertEqual(result.readings, ("りんご",))
        self.assertTrue(
            all(
                candidate.reading == "りんご"
                for candidate in result.candidates
            )
        )

    def test_kana_and_kanji_variants_share_a_canonical_key(self) -> None:
        results = [
            self.validator.validate(surface)
            for surface in ("林檎", "りんご", "リンゴ")
        ]

        self.assertTrue(all(result.accepted for result in results))
        keys = {
            result.candidates[0].canonical_key
            for result in results
        }
        self.assertEqual(keys, {"りんご"})

    def test_accepts_multi_character_katakana(self) -> None:
        result = self.validator.validate("コーヒー")

        self.assertEqual(result.code, LexiconCode.ACCEPTED)
        self.assertEqual(result.readings, ("こーひー",))
        self.assertGreaterEqual(len(result.candidates), 1)

    def test_accepts_proper_nouns_added_by_the_full_dictionary(self) -> None:
        expected_readings = {
            "すみっコぐらし": "すみっこぐらし",
            "鬼滅の刃": "きめつのやいば",
            "初音ミク": "はつねみく",
            "ちいかわ": "ちいかわ",
            "スプラトゥーン": "すぷらとぅーん",
        }

        for surface, reading in expected_readings.items():
            with self.subTest(surface=surface):
                result = self.validator.validate(surface)
                self.assertEqual(result.code, LexiconCode.ACCEPTED)
                self.assertEqual(result.readings, (reading,))
                self.assertTrue(
                    all(
                        candidate.part_of_speech[:2]
                        == ("名詞", "固有名詞")
                        for candidate in result.candidates
                    )
                )

    def test_exposes_multiple_readings_instead_of_choosing_one(self) -> None:
        result = self.validator.validate("日本")

        self.assertEqual(result.code, LexiconCode.MULTIPLE_READINGS)
        self.assertFalse(result.accepted)
        self.assertTrue(result.is_dictionary_word)
        self.assertTrue(result.requires_reading_choice)
        self.assertIn("にっぽん", result.readings)
        self.assertIn("にほん", result.readings)
        for reading in result.readings:
            self.assertTrue(result.candidates_for_reading(reading))

    def test_rejects_non_noun_dictionary_entry(self) -> None:
        result = self.validator.validate("そして")
        self.assertEqual(
            result.code,
            LexiconCode.UNSUPPORTED_PART_OF_SPEECH,
        )

    def test_rejects_unknown_exact_surface_even_if_parts_are_words(
        self,
    ) -> None:
        result = self.validator.validate("猫犬猫犬猫犬")
        self.assertEqual(result.code, LexiconCode.NOT_IN_DICTIONARY)

    def test_accepts_explicit_yurinchi_alias_and_canonical_surface(
        self,
    ) -> None:
        alias_surface = "\u30e6\u30fc\u30ea\u30f3\u30c1\u30fc"
        canonical_surface = "\u6cb9\u6dcb\u9d8f"
        expected_reading = "\u3086\u30fc\u308a\u3093\u3061\u30fc"

        alias_result = self.validator.validate(alias_surface)
        canonical_result = self.validator.validate(canonical_surface)

        for result in (alias_result, canonical_result):
            with self.subTest(surface=result.surface):
                self.assertEqual(result.code, LexiconCode.ACCEPTED)
                self.assertEqual(result.readings, (expected_reading,))

        self.assertTrue(
            all(
                candidate.surface == alias_surface
                and candidate.lemma == canonical_surface
                and candidate.normalized_form == canonical_surface
                for candidate in alias_result.candidates
            )
        )
        self.assertTrue(
            all(
                candidate.surface == canonical_surface
                for candidate in canonical_result.candidates
            )
        )

    def test_accepts_yudofu_with_exact_reading(self) -> None:
        result = self.validator.validate("\u6e6f\u8c46\u8150")

        self.assertEqual(result.code, LexiconCode.ACCEPTED)
        self.assertEqual(result.readings, ("\u3086\u3069\u3046\u3075",))

if __name__ == "__main__":
    unittest.main()
