from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.build_bot_data import (
    TKG_COMMIT,
    TKG_INDEX_SHA256,
    TkgEntry,
    build_bot_data,
    index_tkg_entries,
    load_tkg_entries,
    select_rows,
)
from shiritori.lexicon import LexiconCode


@dataclass(frozen=True)
class FakeCandidate:
    reading: str
    part_of_speech: tuple[str, str, str, str, str, str] = (
        "名詞",
        "普通名詞",
        "一般",
        "*",
        "*",
        "*",
    )


class FakeResult:
    def __init__(self, code: LexiconCode, *readings: str) -> None:
        self.code = code
        self.candidates = tuple(
            FakeCandidate(reading) for reading in readings
        )

    @property
    def is_dictionary_word(self) -> bool:
        return self.code in {
            LexiconCode.ACCEPTED,
            LexiconCode.MULTIPLE_READINGS,
        }

    def candidates_for_reading(
        self, reading: str
    ) -> tuple[FakeCandidate, ...]:
        return tuple(
            candidate
            for candidate in self.candidates
            if candidate.reading == reading
        )


class FakeValidator:
    def __init__(self, results: dict[str, FakeResult]) -> None:
        self.results = results

    def validate(self, surface: str) -> FakeResult:
        return self.results.get(
            surface,
            FakeResult(LexiconCode.NOT_IN_DICTIONARY),
        )


def tkg(
    surface: str,
    reading: str,
    tier: str,
    *,
    position: int,
    entry_id: str | None = None,
) -> TkgEntry:
    return TkgEntry(
        entry_id=entry_id or f"{position:05d}_test",
        surface=surface,
        reading=reading,
        tier=tier,
        position=position,
    )


class BuildBotDataTests(unittest.TestCase):
    def test_tkg_input_is_pinned(self) -> None:
        self.assertEqual(
            TKG_COMMIT,
            "9dd2e89ef86212d249c013d77f843d59b110330c",
        )
        self.assertEqual(
            TKG_INDEX_SHA256.upper(),
            "CD7A5D73465118EA484C9809C09DF61E6419C4D34689B5A78ACA3A4CF36B8A4B",
        )

    def test_loader_keeps_only_normalized_ranked_noun_hints(self) -> None:
        payload = {
            "entries": [
                {
                    "id": "00001_ringo",
                    "headword": "リンゴ",
                    "reading": "リンゴ",
                    "vocabulary_tier": "basic",
                    "pos_tags": ["noun"],
                },
                {
                    "id": "00002_taberu",
                    "headword": "食べる",
                    "reading": "たべる",
                    "vocabulary_tier": "basic",
                    "pos_tags": ["verb-ichidan"],
                },
                {
                    "id": "00003_blank",
                    "headword": "空欄",
                    "reading": "くうらん",
                    "vocabulary_tier": "",
                    "pos_tags": ["noun"],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "entries_index.json")
            path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            entries = load_tkg_entries(path)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].surface, "リンゴ")
        self.assertEqual(entries[0].reading, "りんご")
        self.assertEqual(entries[0].tier, "basic")

    def test_tier_conflict_uses_the_strongest_tier(self) -> None:
        indexed = index_tkg_entries(
            [
                tkg("気持ち", "きもち", "core", position=1),
                tkg("気持ち", "きもち", "basic", position=2),
                tkg("気持ち", "きもち", "general", position=0),
            ]
        )

        self.assertEqual(len(indexed["気持ち"]), 1)
        self.assertEqual(indexed["気持ち"][0].tier, "basic")

    def test_matching_hint_safely_selects_one_ambiguous_reading(self) -> None:
        validator = FakeValidator(
            {
                "日本": FakeResult(
                    LexiconCode.MULTIPLE_READINGS,
                    "にっぽん",
                    "にほん",
                )
            }
        )
        rows = select_rows(
            {"日本": "00000001-n"},
            [tkg("日本", "にほん", "core", position=1)],
            validator=validator,  # type: ignore[arg-type]
        )

        self.assertIn(
            ("日本", "にほん", "wnja:00000001-n", "core"),
            rows,
        )

        unmatched = select_rows(
            {"日本": "00000001-n"},
            [tkg("日本", "にちほん", "basic", position=1)],
            validator=validator,  # type: ignore[arg-type]
        )
        self.assertFalse(any(row[0] == "日本" for row in unmatched))

    def test_only_basic_and_core_can_supplement_wordnet(self) -> None:
        validator = FakeValidator(
            {
                "補完語": FakeResult(LexiconCode.ACCEPTED, "ほかんご"),
                "一般語": FakeResult(LexiconCode.ACCEPTED, "いっぱんご"),
            }
        )
        rows = select_rows(
            {},
            [
                tkg("補完語", "ほかんご", "basic", position=1),
                tkg("一般語", "いっぱんご", "general", position=2),
            ],
            validator=validator,  # type: ignore[arg-type]
        )

        self.assertIn(
            ("補完語", "ほかんご", "tkg:00001_test", "basic"),
            rows,
        )
        self.assertFalse(any(row[0] == "一般語" for row in rows))

    def test_tier_order_precedes_legacy_wordnet_order(self) -> None:
        validator = FakeValidator(
            {
                "甲": FakeResult(LexiconCode.ACCEPTED, "こう"),
                "乙": FakeResult(LexiconCode.ACCEPTED, "おつ"),
                "丙": FakeResult(LexiconCode.ACCEPTED, "へい"),
            }
        )
        rows = select_rows(
            {
                "甲": "00000001-n",
                "乙": "00000002-n",
                "丙": "00000003-n",
            },
            [
                tkg("甲", "こう", "core", position=1),
                tkg("乙", "おつ", "basic", position=2),
            ],
            validator=validator,  # type: ignore[arg-type]
        )

        selected = [row for row in rows if row[0] in {"甲", "乙", "丙"}]
        self.assertEqual(
            [(row[0], row[3]) for row in selected],
            [("乙", "basic"), ("甲", "core"), ("丙", "wordnet")],
        )

    def test_check_mode_detects_regeneration_drift(self) -> None:
        generated_rows = [
            ("林檎", "りんご", "curated", "curated"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wordnet = root / "wnjpn.db"
            tkg_index = root / "entries_index.json"
            output = root / "words.csv"
            connection = sqlite3.connect(wordnet)
            connection.close()
            tkg_index.write_text('{"entries":[]}', encoding="utf-8")

            with (
                patch("scripts.build_bot_data.verify_wordnet_hash"),
                patch("scripts.build_bot_data.verify_tkg_hash"),
                patch(
                    "scripts.build_bot_data.extract_sources",
                    return_value={},
                ),
                patch(
                    "scripts.build_bot_data.load_tkg_entries",
                    return_value=(),
                ),
                patch(
                    "scripts.build_bot_data.select_rows",
                    return_value=generated_rows,
                ),
            ):
                self.assertEqual(
                    build_bot_data(wordnet, tkg_index, output),
                    1,
                )
                self.assertEqual(
                    build_bot_data(
                        wordnet,
                        tkg_index,
                        output,
                        check=True,
                    ),
                    1,
                )
                output.write_text("drift\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    ValueError,
                    "does not match regenerated data",
                ):
                    build_bot_data(
                        wordnet,
                        tkg_index,
                        output,
                        check=True,
                    )


if __name__ == "__main__":
    unittest.main()
