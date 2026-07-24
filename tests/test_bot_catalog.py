from __future__ import annotations

import unittest

from shiritori.bot_catalog import (
    CatalogSkipReason,
    build_bot_catalog,
)
from shiritori.lexicon import (
    LexiconCandidate,
    LexiconCode,
    LexiconResult,
    LexiconValidator,
)
from shiritori.themes import ThemeDefinition


def candidate(
    surface: str,
    reading: str,
    word_id: int = 1,
) -> LexiconCandidate:
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


def accepted(surface: str, reading: str) -> LexiconResult:
    return LexiconResult(
        code=LexiconCode.ACCEPTED,
        surface=surface,
        message="ok",
        candidates=(candidate(surface, reading),),
    )


class FakeValidator:
    def __init__(self, results: dict[str, LexiconResult]) -> None:
        self.results = results
        self.calls: list[str | None] = []

    def validate(self, raw_surface: str | None) -> LexiconResult:
        self.calls.append(raw_surface)
        assert raw_surface is not None
        return self.results[raw_surface]


class BotCatalogTests(unittest.TestCase):
    def test_every_surface_is_validated_and_skips_are_diagnostic(self) -> None:
        validator = FakeValidator(
            {
                "林檎": accepted("林檎", "りんご"),
                "リンゴ": accepted("リンゴ", "りんご"),
                "日本": LexiconResult(
                    code=LexiconCode.MULTIPLE_READINGS,
                    surface="日本",
                    message="choose",
                    candidates=(
                        candidate("日本", "にっぽん", 1),
                        candidate("日本", "にほん", 2),
                    ),
                ),
                "架空語": LexiconResult(
                    code=LexiconCode.NOT_IN_DICTIONARY,
                    surface="架空語",
                    message="missing",
                ),
            }
        )

        catalog = build_bot_catalog(
            ("林檎", "リンゴ", "日本", "架空語"),
            validator=validator,
        )

        self.assertEqual(
            validator.calls,
            ["林檎", "リンゴ", "日本", "架空語"],
        )
        self.assertEqual(catalog.attempted_count, 4)
        self.assertEqual(catalog.accepted_count, 1)
        self.assertEqual(catalog.options[0].surface, "林檎")
        self.assertEqual(catalog.options[0].rank, 0)
        self.assertEqual(
            [item.reason for item in catalog.diagnostics],
            [
                CatalogSkipReason.DUPLICATE_CANONICAL_KEY,
                CatalogSkipReason.AMBIGUOUS_READING,
                CatalogSkipReason.INVALID_LEXICON,
            ],
        )

    def test_theme_filters_after_dictionary_validation(self) -> None:
        validator = FakeValidator(
            {
                "寿司": accepted("寿司", "すし"),
                "林檎": accepted("林檎", "りんご"),
            }
        )
        food = ThemeDefinition.from_entries(
            "food",
            "食べ物",
            [("寿司", "すし")],
        )

        catalog = build_bot_catalog(
            ("寿司", "林檎"),
            validator=validator,
            theme=food,
        )

        self.assertEqual(validator.calls, ["寿司", "林檎"])
        self.assertEqual(
            tuple(option.surface for option in catalog.options),
            ("寿司",),
        )
        self.assertEqual(
            catalog.diagnostics[0].reason,
            CatalogSkipReason.OUTSIDE_THEME,
        )

    def test_options_are_immediately_usable_by_word_index(self) -> None:
        validator = FakeValidator(
            {
                "林檎": accepted("林檎", "りんご"),
                "ゴマ": accepted("ゴマ", "ごま"),
            }
        )

        catalog = build_bot_catalog(
            ("林檎", "ゴマ"),
            validator=validator,
        )

        self.assertEqual(
            tuple(
                option.surface
                for option in catalog.index.starting_with("り")
            ),
            ("林檎",),
        )
        self.assertEqual(
            tuple(
                option.surface
                for option in catalog.index.starting_with("ご")
            ),
            ("ゴマ",),
        )

    def test_small_real_sudachi_smoke(self) -> None:
        catalog = build_bot_catalog(
            ("林檎", "日本", "😀"),
            validator=LexiconValidator(),
        )

        self.assertIn("林檎", {option.surface for option in catalog.options})
        diagnostics = {
            item.surface: item.reason for item in catalog.diagnostics
        }
        self.assertEqual(
            diagnostics["日本"],
            CatalogSkipReason.AMBIGUOUS_READING,
        )
        self.assertEqual(
            diagnostics["😀"],
            CatalogSkipReason.INVALID_LEXICON,
        )

    def test_default_catalog_has_useful_validated_coverage(self) -> None:
        catalog = build_bot_catalog()
        start_kana = {option.first_kana for option in catalog.options}
        safe_final_kana = {
            option.last_kana
            for option in catalog.options
            if not option.ends_with_n
        }

        self.assertGreaterEqual(catalog.accepted_count, 40)
        self.assertTrue(
            {
                "い",
                "う",
                "か",
                "き",
                "く",
                "ご",
                "し",
                "す",
                "に",
                "ま",
                "り",
                "わ",
            }
            <= start_kana
        )
        self.assertTrue(safe_final_kana <= start_kana)

if __name__ == "__main__":
    unittest.main()
