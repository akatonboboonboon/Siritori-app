from __future__ import annotations

import hashlib
import unittest

from shiritori.theme_data import load_word_theme_rows
from shiritori.theme_rules import (
    AUTOMATIC_MULTI_LABEL_LINKS,
    LEGACY_THEME_IDS,
    NEW_THEME_IDS,
    PERSON_EXCLUDED_THEME_IDS,
    THEME_BLOCKLISTS,
    THEME_COMPATIBLE_ROOTS,
    THEME_IDS,
    THEME_ROOTS,
)


EXPECTED_LEGACY_THEME_IDS = (
    "food",
    "animal",
    "plant",
    "sport",
    "country",
    "instrument",
    "vehicle",
    "fruit",
    "vegetable",
)
EXPECTED_NEW_THEME_IDS = (
    "person_job",
    "nature",
    "place_building",
    "body",
    "clothing",
    "daily_tools",
    "music",
)
EXPECTED_NEW_ROOTS = {
    "person_job": ("00007846-n", "09632518-n", "00582388-n"),
    "nature": (
        "00015388-n",
        "00017222-n",
        "09287968-n",
        "09225146-n",
        "11425580-n",
        "11524662-n",
        "09239740-n",
    ),
    "place_building": (
        "08574314-n",
        "09287968-n",
        "09225146-n",
        "02913152-n",
    ),
    "body": ("05220461-n",),
    "clothing": ("03051540-n",),
    "daily_tools": (
        "03563967-n",
        "03405265-n",
        "03528263-n",
        "04516672-n",
    ),
    "music": ("07020895-n", "07037465-n", "03800933-n"),
}
EXPECTED_HIERARCHY_LINKS = frozenset(
    {
        frozenset(("food", "plant")),
        frozenset(("food", "fruit")),
        frozenset(("food", "vegetable")),
        frozenset(("plant", "fruit")),
        frozenset(("plant", "vegetable")),
        frozenset(("fruit", "vegetable")),
        frozenset(("nature", "animal")),
        frozenset(("nature", "plant")),
        frozenset(("nature", "fruit")),
        frozenset(("nature", "vegetable")),
        frozenset(("nature", "place_building")),
        frozenset(("place_building", "country")),
        frozenset(("music", "instrument")),
    }
)
LEGACY_PROJECTION_SHA256 = (
    "ac252ed28cdcb30215e06f6da0053f3907483a6d945af9cac2f5cd184465f32f"
)


class Theme16ConfigurationTests(unittest.TestCase):
    def test_ids_roots_compatibility_and_blocklists_are_complete(self) -> None:
        self.assertEqual(LEGACY_THEME_IDS, EXPECTED_LEGACY_THEME_IDS)
        self.assertEqual(NEW_THEME_IDS, EXPECTED_NEW_THEME_IDS)
        self.assertEqual(
            THEME_IDS,
            EXPECTED_LEGACY_THEME_IDS + EXPECTED_NEW_THEME_IDS,
        )
        self.assertEqual(tuple(THEME_ROOTS), THEME_IDS)
        self.assertEqual(set(THEME_COMPATIBLE_ROOTS), set(THEME_IDS))
        self.assertEqual(set(THEME_BLOCKLISTS), set(THEME_IDS))
        self.assertEqual(
            PERSON_EXCLUDED_THEME_IDS,
            frozenset({"animal", "nature"}),
        )

        for theme_id, roots in EXPECTED_NEW_ROOTS.items():
            with self.subTest(theme_id=theme_id):
                self.assertEqual(THEME_ROOTS[theme_id], roots)
                self.assertEqual(THEME_COMPATIBLE_ROOTS[theme_id], roots)

    def test_hierarchy_graph_has_only_reviewed_links(self) -> None:
        self.assertEqual(
            AUTOMATIC_MULTI_LABEL_LINKS,
            EXPECTED_HIERARCHY_LINKS,
        )
        self.assertEqual(len(AUTOMATIC_MULTI_LABEL_LINKS), 13)
        self.assertTrue(
            all(len(link) == 2 for link in AUTOMATIC_MULTI_LABEL_LINKS)
        )

    def test_old_nine_projection_is_byte_stable(self) -> None:
        projected_rows: list[tuple[str, str, str]] = []
        for row in load_word_theme_rows():
            theme_ids = tuple(
                theme_id
                for theme_id in LEGACY_THEME_IDS
                if theme_id in row.theme_ids
            )
            if theme_ids:
                projected_rows.append(
                    (
                        row.surface,
                        row.reading,
                        "|".join(theme_ids),
                    )
                )

        payload = "".join(
            f"{surface}\0{reading}\0{theme_ids}\n"
            for surface, reading, theme_ids in sorted(projected_rows)
        ).encode("utf-8")

        self.assertEqual(len(projected_rows), 2_872)
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            LEGACY_PROJECTION_SHA256,
        )


if __name__ == "__main__":
    unittest.main()
