from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from shiritori.bot_data import load_bot_word_options
from shiritori.theme_data import (
    AUTO_THEME_SOURCE_REF,
    WORD_THEME_DATA_HEADER,
    ThemeDataFormatError,
    load_theme_entries,
    load_theme_rows,
    load_word_theme_rows,
)
from shiritori.themes import ThemeEntry


class ThemeDataLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.temporary_directory.name, "theme.csv")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_text(self, content: str) -> None:
        self.csv_path.write_text(content, encoding="utf-8", newline="")

    def test_loads_and_normalizes_valid_utf8_rows(self) -> None:
        self.write_text(
            "surface,reading,source_ref\r\n"
            "林檎,リンゴ,wikidata:Q89\r\n"
            "蜜柑,みかん,wikidata:Q13191\r\n"
        )

        rows = load_theme_rows(self.csv_path)

        self.assertEqual(
            tuple((row.surface, row.reading, row.source_ref) for row in rows),
            (
                ("林檎", "りんご", "wikidata:Q89"),
                ("蜜柑", "みかん", "wikidata:Q13191"),
            ),
        )
        self.assertEqual(
            load_theme_entries(self.csv_path),
            (
                ThemeEntry("林檎", "りんご"),
                ThemeEntry("蜜柑", "みかん"),
            ),
        )

    def test_rejects_empty_or_non_exact_header(self) -> None:
        invalid_documents = (
            "",
            "reading,surface,source_ref\n",
            "surface,reading\n",
            "surface,reading,source_ref,notes\n",
        )

        for document in invalid_documents:
            with self.subTest(document=document):
                self.write_text(document)
                with self.assertRaises(ThemeDataFormatError):
                    load_theme_rows(self.csv_path)

    def test_rejects_blank_values(self) -> None:
        invalid_rows = (
            ",りんご,wikidata:Q89",
            "林檎, ,wikidata:Q89",
            "林檎,りんご,",
        )

        for row in invalid_rows:
            with self.subTest(row=row):
                self.write_text(
                    "surface,reading,source_ref\n"
                    f"{row}\n"
                )
                with self.assertRaises(ThemeDataFormatError):
                    load_theme_rows(self.csv_path)

    def test_rejects_missing_and_extra_row_columns(self) -> None:
        for row in (
            "林檎,りんご",
            "林檎,りんご,wikidata:Q89,extra",
            "",
        ):
            with self.subTest(row=row):
                self.write_text(
                    "surface,reading,source_ref\n"
                    f"{row}\n"
                )
                with self.assertRaises(ThemeDataFormatError):
                    load_theme_rows(self.csv_path)

    def test_rejects_duplicate_normalized_theme_entry(self) -> None:
        self.write_text(
            "surface,reading,source_ref\n"
            "林檎,りんご,wikidata:Q89\n"
            "林檎,リンゴ,manual\n"
        )

        with self.assertRaisesRegex(
            ThemeDataFormatError,
            "duplicate theme entry",
        ):
            load_theme_rows(self.csv_path)

    def test_rejects_non_utf8_input(self) -> None:
        self.csv_path.write_bytes(
            b"surface,reading,source_ref\n\xff,\xff,source\n"
        )

        with self.assertRaises(UnicodeDecodeError):
            load_theme_rows(self.csv_path)


AUTO_PAIR = ("\u718a", "\u304f\u307e")
REVIEWED_ONLY_PAIR = (
    "\u30e6\u30fc\u30ea\u30f3\u30c1\u30fc",
    "\u3086\u30fc\u308a\u3093\u3061\u30fc",
)


class WordThemeDataLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.csv_path = Path(
            self.temporary_directory.name,
            "word_themes.csv",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_text(self, *rows: str) -> None:
        document = "\n".join(
            (",".join(WORD_THEME_DATA_HEADER), *rows, "")
        )
        self.csv_path.write_text(
            document,
            encoding="utf-8",
            newline="",
        )

    def test_accepts_auto_exact_pair_and_reviewed_only_pair(self) -> None:
        self.write_text(
            (
                f"{AUTO_PAIR[0]},{AUTO_PAIR[1]},animal,auto,"
                f"{AUTO_THEME_SOURCE_REF}"
            ),
            (
                f"{REVIEWED_ONLY_PAIR[0]},{REVIEWED_ONLY_PAIR[1]},"
                "food,reviewed,manual:user-food-v1"
            ),
        )

        rows = load_word_theme_rows(self.csv_path)

        self.assertEqual(
            tuple(
                (
                    row.surface,
                    row.reading,
                    row.theme_ids,
                    row.source_kind,
                    row.source_ref,
                )
                for row in rows
            ),
            (
                (
                    *AUTO_PAIR,
                    frozenset({"animal"}),
                    "auto",
                    AUTO_THEME_SOURCE_REF,
                ),
                (
                    *REVIEWED_ONLY_PAIR,
                    frozenset({"food"}),
                    "reviewed",
                    "manual:user-food-v1",
                ),
            ),
        )
        bot_pairs = {
            (option.surface, option.reading)
            for option in load_bot_word_options()
        }
        self.assertIn(AUTO_PAIR, bot_pairs)
        self.assertNotIn(REVIEWED_ONLY_PAIR, bot_pairs)

    def test_rejects_automatic_pair_missing_from_general_vocabulary(
        self,
    ) -> None:
        self.write_text(
            (
                "\u672a\u767b\u9332\u8a9e,"
                "\u307f\u3068\u3046\u308d\u304f\u3054,"
                f"food,auto,{AUTO_THEME_SOURCE_REF}"
            )
        )

        with self.assertRaisesRegex(
            ThemeDataFormatError,
            "automatic pair is absent",
        ):
            load_word_theme_rows(self.csv_path)

    def test_rejects_unknown_source_kind_or_source_ref(self) -> None:
        invalid_rows = (
            (
                f"{AUTO_PAIR[0]},{AUTO_PAIR[1]},animal,derived,"
                f"{AUTO_THEME_SOURCE_REF}"
            ),
            (
                f"{AUTO_PAIR[0]},{AUTO_PAIR[1]},animal,auto,"
                "wnja:00015388-n"
            ),
            (
                f"{REVIEWED_ONLY_PAIR[0]},{REVIEWED_ONLY_PAIR[1]},"
                "food,reviewed,manual:UPPER"
            ),
        )

        for row in invalid_rows:
            with self.subTest(row=row):
                self.write_text(row)
                with self.assertRaises(ThemeDataFormatError):
                    load_word_theme_rows(self.csv_path)

    def test_rejects_malformed_unknown_or_noncanonical_theme_ids(
        self,
    ) -> None:
        invalid_theme_ids = (
            "animal||food",
            "future",
            "plant|food",
            "animal|animal",
        )

        for theme_ids in invalid_theme_ids:
            with self.subTest(theme_ids=theme_ids):
                self.write_text(
                    (
                        f"{AUTO_PAIR[0]},{AUTO_PAIR[1]},{theme_ids},"
                        f"auto,{AUTO_THEME_SOURCE_REF}"
                    )
                )
                with self.assertRaises(ThemeDataFormatError):
                    load_word_theme_rows(self.csv_path)

    def test_requires_exact_five_column_header(self) -> None:
        self.csv_path.write_text(
            "surface,reading,theme_ids,source_kind,source_ref,extra\n",
            encoding="utf-8",
            newline="",
        )

        with self.assertRaisesRegex(
            ThemeDataFormatError,
            "header must be exactly",
        ):
            load_word_theme_rows(self.csv_path)

if __name__ == "__main__":
    unittest.main()
