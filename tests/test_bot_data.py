from __future__ import annotations

import unittest

from shiritori.bot_catalog import get_default_bot_catalog
from shiritori.bot_data import load_bot_word_options
from shiritori.bots import BotContext, HardBot
from shiritori.lexicon import LexiconCode, LexiconValidator


class BotDataTests(unittest.TestCase):
    def test_checked_in_vocabulary_is_large_unique_and_connected(self) -> None:
        options = load_bot_word_options()

        self.assertGreaterEqual(len(options), 25_000)
        self.assertEqual(
            len({option.surface for option in options}),
            len(options),
        )
        self.assertEqual(
            len({option.canonical_key for option in options}),
            len(options),
        )
        start_kana = {option.first_kana for option in options}
        safe_final_kana = {
            option.last_kana
            for option in options
            if not option.ends_with_n
        }
        self.assertTrue(safe_final_kana <= start_kana)

    def test_every_row_still_matches_the_pinned_sudachi_dictionary(self) -> None:
        validator = LexiconValidator()
        for option in load_bot_word_options():
            with self.subTest(surface=option.surface):
                result = validator.validate(option.surface)
                self.assertEqual(result.code, LexiconCode.ACCEPTED)
                common_readings = {
                    candidate.reading
                    for candidate in result.candidates
                    if candidate.part_of_speech[1] == "普通名詞"
                }
                self.assertEqual(common_readings, {option.reading})

    def test_default_catalog_uses_large_data_and_hard_can_choose(self) -> None:
        catalog = get_default_bot_catalog()

        self.assertGreaterEqual(catalog.accepted_count, 25_000)
        selected = HardBot(seed=9).choose(
            BotContext("し"),
            catalog.index,
        )
        self.assertIsNotNone(selected)


if __name__ == "__main__":
    unittest.main()
