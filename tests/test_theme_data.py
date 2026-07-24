from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from shiritori.theme_data import (
    ThemeDataFormatError,
    load_theme_entries,
    load_theme_rows,
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


if __name__ == "__main__":
    unittest.main()
