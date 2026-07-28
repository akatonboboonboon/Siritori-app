from __future__ import annotations

from collections import Counter
import csv
import time
import unittest

from shiritori.bot_catalog import get_default_bot_catalog
from shiritori.bot_data import BOT_DATA_PATH, load_bot_word_options
from shiritori.bots import BotContext, HardBot, WordIndex
from shiritori.lexicon import LexiconValidator


class BotDataTests(unittest.TestCase):
    def test_checked_in_vocabulary_is_large_unique_and_connected(self) -> None:
        options = load_bot_word_options()

        self.assertGreaterEqual(len(options), 30_000)
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
        # ヴ始まりはブ始まりと同じ接続bucketへ統合される。
        self.assertEqual(len(start_kana), 67)
        self.assertTrue(safe_final_kana <= start_kana)

    def test_every_row_still_matches_the_pinned_sudachi_dictionary(self) -> None:
        validator = LexiconValidator()
        for option in load_bot_word_options():
            with self.subTest(surface=option.surface):
                result = validator.validate(option.surface)
                self.assertTrue(result.is_dictionary_word)
                self.assertTrue(
                    result.candidates_for_reading(option.reading)
                )

    def test_commonness_tiers_include_natural_multi_reading_words(self) -> None:
        with BOT_DATA_PATH.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        pairs = {(row["surface"], row["reading"]) for row in rows}
        must_have = {
            ("本", "ほん"),
            ("人", "ひと"),
            ("魚", "さかな"),
            ("車", "くるま"),
            ("木", "き"),
            ("目", "め"),
            ("手", "て"),
            ("犬", "いぬ"),
            ("猫", "ねこ"),
            ("林檎", "りんご"),
        }
        self.assertTrue(must_have <= pairs)

        tiers = Counter(row["commonness_tier"] for row in rows)
        sources = Counter(
            row["source_ref"].split(":", 1)[0] for row in rows
        )
        self.assertGreaterEqual(tiers["basic"], 250)
        self.assertGreaterEqual(tiers["core"], 1_000)
        self.assertGreaterEqual(sources["tkg"], 250)

    def test_hard_choices_are_natural_diverse_and_fast(self) -> None:
        with BOT_DATA_PATH.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        options = load_bot_word_options()
        index = WordIndex(options)
        starts = sorted({option.first_kana for option in options})
        tier_by_pair = {
            (row["surface"], row["reading"]): row["commonness_tier"]
            for row in rows
        }

        started = time.perf_counter()
        choices = [
            HardBot(seed=0).choose(BotContext(kana), index)
            for kana in starts
        ]
        elapsed = time.perf_counter() - started

        self.assertTrue(all(choice is not None for choice in choices))
        selected = [choice for choice in choices if choice is not None]
        endings = Counter(choice.last_kana for choice in selected)
        natural_count = sum(
            tier_by_pair[(choice.surface, choice.reading)]
            in {"curated", "basic", "core"}
            for choice in selected
        )
        self.assertLess(elapsed, 5.0)
        self.assertLessEqual(max(endings.values()), 12)
        self.assertLessEqual(endings["む"], 4)
        self.assertGreaterEqual(natural_count, 50)

    def test_default_catalog_uses_large_data_and_hard_can_choose(self) -> None:
        catalog = get_default_bot_catalog()

        self.assertGreaterEqual(catalog.accepted_count, 30_000)
        selected = HardBot(seed=9).choose(
            BotContext("り"),
            catalog.index,
        )
        self.assertIsNotNone(selected)


if __name__ == "__main__":
    unittest.main()
