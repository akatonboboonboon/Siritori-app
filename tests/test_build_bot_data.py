from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import scripts.build_bot_data as build_bot_data_module
from scripts.build_bot_data import (
    ReviewedThemeSeed,
    TKG_COMMIT,
    TKG_INDEX_SHA256,
    TkgEntry,
    _retain_hierarchical_automatic_themes,
    build_bot_data,
    classify_theme_memberships,
    index_tkg_entries,
    load_reviewed_theme_seeds,
    load_tkg_entries,
    merge_reviewed_theme_rows,
    select_rows,
)
from shiritori.lexicon import LexiconCode
from shiritori.theme_rules import (
    LEGACY_THEME_IDS,
    PERSON_SYNSET,
    THEME_COMPATIBLE_ROOTS,
    THEME_IDS,
    THEME_ROOTS,
)


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
        self.calls: list[str] = []

    def validate(self, surface: str) -> FakeResult:
        self.calls.append(surface)
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


def reviewed_seed(
    surface: str,
    reading: str,
    theme_id: str,
    *,
    source_ref: str = "wnja:00000001-n",
) -> ReviewedThemeSeed:
    return ReviewedThemeSeed(
        surface=surface,
        reading=reading,
        source_ref=source_ref,
        theme_ids=frozenset({theme_id}),
    )


def wordnet_fixture() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE word(
            wordid INTEGER PRIMARY KEY,
            lemma TEXT NOT NULL,
            lang TEXT NOT NULL,
            pos TEXT NOT NULL
        );
        CREATE TABLE sense(
            wordid INTEGER NOT NULL,
            synset TEXT NOT NULL
        );
        CREATE TABLE ancestor(
            synset1 TEXT NOT NULL,
            synset2 TEXT NOT NULL
        );
        """
    )
    return connection


def add_noun_senses(
    connection: sqlite3.Connection,
    surface: str,
    *senses: str,
) -> None:
    word_id = connection.execute(
        "SELECT COALESCE(MAX(wordid), 0) + 1 FROM word"
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO word(wordid, lemma, lang, pos) VALUES (?, ?, 'jpn', 'n')",
        (word_id, surface),
    )
    connection.executemany(
        "INSERT INTO sense(wordid, synset) VALUES (?, ?)",
        ((word_id, sense) for sense in senses),
    )


def add_ancestors(
    connection: sqlite3.Connection,
    sense: str,
    *roots: str,
) -> None:
    connection.executemany(
        "INSERT INTO ancestor(synset1, synset2) VALUES (?, ?)",
        ((sense, root) for root in roots),
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

    def test_theme_classifier_applies_fixed_sense_compatibility(self) -> None:
        connection = wordnet_fixture()
        rows = [
            ("food_term", "food_reading", "wnja:00000001-n", "wordnet"),
            ("unthemed", "unthemed_reading", "wnja:00000002-n", "wordnet"),
            ("mixed_senses", "mixed_reading", "wnja:00000003-n", "wordnet"),
            ("person_animal", "person_reading", "wnja:00000004-n", "wordnet"),
            ("botanical", "botanical_reading", "wnja:00000005-n", "wordnet"),
            ("cross_family", "cross_reading", "wnja:00000006-n", "wordnet"),
            ("reviewed", "reviewed_reading", "wnja:00000007-n", "wordnet"),
        ]

        add_noun_senses(connection, "food_term", "food-sense")
        add_ancestors(connection, "food-sense", THEME_ROOTS["food"][0])

        add_noun_senses(connection, "unthemed", "unthemed-sense")

        add_noun_senses(
            connection,
            "mixed_senses",
            "food-only-sense",
            "animal-only-sense",
        )
        add_ancestors(
            connection,
            "food-only-sense",
            THEME_ROOTS["food"][0],
        )
        add_ancestors(
            connection,
            "animal-only-sense",
            THEME_ROOTS["animal"][0],
        )

        add_noun_senses(connection, "person_animal", "person-animal-sense")
        add_ancestors(
            connection,
            "person-animal-sense",
            THEME_ROOTS["animal"][0],
            PERSON_SYNSET,
        )

        add_noun_senses(connection, "botanical", "botanical-sense")
        add_ancestors(
            connection,
            "botanical-sense",
            THEME_ROOTS["food"][0],
            THEME_ROOTS["plant"][0],
            THEME_ROOTS["fruit"][0],
            THEME_ROOTS["vegetable"][0],
        )

        add_noun_senses(connection, "cross_family", "cross-family-sense")
        add_ancestors(
            connection,
            "cross-family-sense",
            THEME_ROOTS["animal"][0],
            THEME_ROOTS["vehicle"][0],
        )

        seeds = (
            reviewed_seed("reviewed", "reviewed_reading", "sport"),
            reviewed_seed("reviewed_only", "reviewed_only_reading", "country"),
        )
        memberships = classify_theme_memberships(connection, rows, seeds)

        self.assertEqual(
            memberships[("food_term", "food_reading")],
            ("food",),
        )
        self.assertEqual(
            memberships[("unthemed", "unthemed_reading")],
            (),
        )
        self.assertEqual(
            memberships[("mixed_senses", "mixed_reading")],
            (),
        )
        self.assertEqual(
            memberships[("person_animal", "person_reading")],
            ("person_job",),
        )
        self.assertEqual(
            memberships[("botanical", "botanical_reading")],
            ("food", "plant", "fruit", "vegetable", "nature"),
        )
        self.assertEqual(
            memberships[("cross_family", "cross_reading")],
            (),
        )
        self.assertEqual(
            memberships[("reviewed", "reviewed_reading")],
            ("sport",),
        )
        self.assertEqual(
            memberships[("reviewed_only", "reviewed_only_reading")],
            ("country",),
        )

    def test_hierarchy_gate_uses_only_reviewed_relationships(self) -> None:
        cases = (
            (
                {"food", "plant", "fruit", "nature"},
                frozenset(),
                {"food", "plant", "fruit", "nature"},
            ),
            (
                {"food", "nature"},
                frozenset(),
                {"food"},
            ),
            (
                {"person_job", "body"},
                frozenset(),
                set(),
            ),
            (
                {"country", "place_building", "nature"},
                frozenset(),
                {"country", "place_building", "nature"},
            ),
            (
                {"instrument", "music"},
                frozenset(),
                {"instrument", "music"},
            ),
            (
                {"animal", "country", "nature", "place_building"},
                frozenset(),
                set(),
            ),
            (
                {"nature"},
                frozenset({"sport", "vegetable"}),
                {"nature"},
            ),
            (
                {"nature"},
                frozenset({"vehicle"}),
                set(),
            ),
            (
                {"body", "person_job"},
                frozenset({"body"}),
                set(),
            ),
        )

        for eligible, reviewed, expected in cases:
            with self.subTest(eligible=eligible, reviewed=reviewed):
                self.assertEqual(
                    _retain_hierarchical_automatic_themes(
                        set(eligible),
                        reviewed,
                    ),
                    expected,
                )

    def test_legacy_seed_loader_does_not_probe_new_theme_csvs(self) -> None:
        with (
            patch(
                "scripts.build_bot_data.load_theme_rows",
                return_value=(),
            ) as load_legacy_rows,
            patch(
                "scripts.build_bot_data.load_reviewed_theme_rows",
                return_value=(),
            ),
        ):
            self.assertEqual(
                load_reviewed_theme_seeds(Path("theme-data")),
                (),
            )

        self.assertEqual(
            tuple(
                call.args[0].stem
                for call in load_legacy_rows.call_args_list
            ),
            LEGACY_THEME_IDS,
        )

    def test_future_target_root_does_not_widen_compatibility(self) -> None:
        connection = wordnet_fixture()
        add_noun_senses(
            connection,
            "future_sport",
            "current-sport-sense",
            "future-sport-sense",
        )
        future_root = "99999999-n"
        add_ancestors(
            connection,
            "current-sport-sense",
            THEME_ROOTS["sport"][0],
        )
        add_ancestors(connection, "future-sport-sense", future_root)
        widened_targets = dict(THEME_ROOTS)
        widened_targets["sport"] = (
            *THEME_ROOTS["sport"],
            future_root,
        )

        self.assertNotIn(future_root, THEME_COMPATIBLE_ROOTS["sport"])
        with patch.object(
            build_bot_data_module,
            "THEME_ROOTS",
            widened_targets,
        ):
            memberships = classify_theme_memberships(
                connection,
                [
                    (
                        "future_sport",
                        "future_reading",
                        "wnja:00000001-n",
                        "wordnet",
                    )
                ],
                (),
            )

        self.assertEqual(
            memberships[("future_sport", "future_reading")],
            (),
        )

    def test_merge_validates_even_a_conflicting_exact_reading(self) -> None:
        rows = [("existing", "existing_reading", "curated", "curated")]
        seed = reviewed_seed("existing", "other_reading", "food")
        validator = FakeValidator(
            {
                "existing": FakeResult(
                    LexiconCode.ACCEPTED,
                    "existing_reading",
                )
            }
        )

        with self.assertRaisesRegex(
            ValueError,
            "no longer matches Sudachi",
        ):
            merge_reviewed_theme_rows(
                rows,
                (seed,),
                validator=validator,  # type: ignore[arg-type]
            )

        self.assertEqual(validator.calls, ["existing"])

    def test_merge_skips_conflicts_but_theme_map_retains_reviewed_pairs(
        self,
    ) -> None:
        rows = [("existing", "existing_reading", "curated", "curated")]
        seeds = (
            reviewed_seed("existing", "existing_reading", "food"),
            reviewed_seed("existing", "other_reading", "animal"),
            reviewed_seed("other_surface", "existing_reading", "plant"),
            reviewed_seed("addition", "addition_reading", "vehicle"),
            reviewed_seed(
                "manual_only",
                "manual_reading",
                "country",
                source_ref="manual:test-review",
            ),
        )
        validator = FakeValidator(
            {
                "existing": FakeResult(
                    LexiconCode.ACCEPTED,
                    "existing_reading",
                    "other_reading",
                ),
                "other_surface": FakeResult(
                    LexiconCode.ACCEPTED,
                    "existing_reading",
                ),
                "addition": FakeResult(
                    LexiconCode.ACCEPTED,
                    "addition_reading",
                ),
                "manual_only": FakeResult(
                    LexiconCode.ACCEPTED,
                    "manual_reading",
                ),
            }
        )

        merged = merge_reviewed_theme_rows(
            rows,
            seeds,
            validator=validator,  # type: ignore[arg-type]
        )

        self.assertEqual(
            validator.calls,
            [seed.surface for seed in seeds],
        )
        self.assertEqual(
            merged,
            [
                ("existing", "existing_reading", "curated", "curated"),
                (
                    "addition",
                    "addition_reading",
                    "wnja:00000001-n",
                    "wordnet",
                ),
            ],
        )

        connection = wordnet_fixture()
        memberships = classify_theme_memberships(
            connection,
            merged,
            seeds,
        )
        for seed in seeds:
            with self.subTest(pair=(seed.surface, seed.reading)):
                expected = tuple(
                    theme_id
                    for theme_id in THEME_IDS
                    if theme_id in seed.theme_ids
                )
                self.assertEqual(
                    memberships[(seed.surface, seed.reading)],
                    expected,
                )
    def test_check_mode_detects_drift_in_either_generated_file(self) -> None:
        generated_rows = [
            ("generated", "generated_reading", "curated", "curated"),
        ]
        generated_memberships = {
            ("generated", "generated_reading"): ("food",)
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wordnet = root / "wnjpn.db"
            tkg_index = root / "entries_index.json"
            output = root / "words.csv"
            theme_output = root / "word_themes.csv"
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
                patch(
                    "scripts.build_bot_data.load_reviewed_theme_seeds",
                    return_value=(),
                ),
                patch(
                    "scripts.build_bot_data.merge_reviewed_theme_rows",
                    return_value=generated_rows,
                ),
                patch(
                    "scripts.build_bot_data.classify_theme_memberships",
                    return_value=generated_memberships,
                ),
                patch(
                    "scripts.build_bot_data.LexiconValidator",
                    return_value=object(),
                ),
            ):
                self.assertEqual(
                    build_bot_data(
                        wordnet,
                        tkg_index,
                        output,
                        theme_output=theme_output,
                    ),
                    1,
                )
                self.assertEqual(
                    build_bot_data(
                        wordnet,
                        tkg_index,
                        output,
                        theme_output=theme_output,
                        check=True,
                    ),
                    1,
                )

                output.write_text("drift\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    ValueError,
                    "Bot CSV does not match regenerated data",
                ):
                    build_bot_data(
                        wordnet,
                        tkg_index,
                        output,
                        theme_output=theme_output,
                        check=True,
                    )

                build_bot_data(
                    wordnet,
                    tkg_index,
                    output,
                    theme_output=theme_output,
                )
                theme_output.write_text("drift\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    ValueError,
                    "theme CSV does not match regenerated data",
                ):
                    build_bot_data(
                        wordnet,
                        tkg_index,
                        output,
                        theme_output=theme_output,
                        check=True,
                    )

if __name__ == "__main__":
    unittest.main()
