"""Build the checked-in general Bot vocabulary from Japanese WordNet.

This is an offline development tool. Runtime Bot turns read only the generated
CSV and never contact WordNet, Sudachi's package index, or another website.
Every emitted surface is validated by the exact same Sudachi rules used for
player input, and only unambiguous common-noun readings are retained.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from pathlib import Path
import re
import sqlite3
import sys
from typing import Final


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))

from shiritori.bot_catalog import DEFAULT_BOT_SURFACES
from shiritori.lexicon import LexiconCode, LexiconValidator


WORDNET_DATABASE_SHA256: Final = (
    "a8e749c4a356bf93d0b5de505bca8b21e13746f5728f76819728e8b4c3305a12"
)
CSV_HEADER: Final = ("surface", "reading", "source_ref")
MAX_SURFACE_LENGTH: Final = 16
ALLOWED_SURFACE: Final = re.compile(
    r"^[\u3041-\u3096\u30a1-\u30fa\u3400-\u9fff々〆ヵヶー・]+$"
)
INVALID_EDGE_MARKS: Final = frozenset({"々", "〆", "ー", "・"})


def verify_wordnet_hash(path: Path) -> None:
    """Reject a database other than the recorded Japanese WordNet release."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest().lower() != WORDNET_DATABASE_SHA256:
        raise ValueError("Japanese WordNet SHA-256 does not match the record")


def prefilter(surface: str) -> bool:
    """Apply cheap deterministic checks before consulting Sudachi."""

    return (
        1 <= len(surface) <= MAX_SURFACE_LENGTH
        and ALLOWED_SURFACE.fullmatch(surface) is not None
        and surface[0] not in INVALID_EDGE_MARKS
        and not surface.endswith("・")
    )


def extract_sources(
    connection: sqlite3.Connection,
) -> dict[str, str]:
    """Return each Japanese noun and one stable provenance synset."""

    rows = connection.execute(
        """
        SELECT word.lemma, min(sense.synset)
        FROM word
        JOIN sense ON sense.wordid = word.wordid
        WHERE word.lang = 'jpn' AND word.pos = 'n'
        GROUP BY word.lemma
        ORDER BY word.lemma
        """
    )
    return {
        str(surface): str(synset)
        for surface, synset in rows
        if prefilter(str(surface))
    }


def select_rows(
    sources: dict[str, str],
    *,
    validator: LexiconValidator | None = None,
) -> list[tuple[str, str, str]]:
    """Validate and rank a deterministic, reading-unique Bot vocabulary."""

    active_validator = validator or LexiconValidator()
    preferred = tuple(dict.fromkeys(DEFAULT_BOT_SURFACES))
    preferred_set = frozenset(preferred)
    surfaces = list(preferred)
    surfaces.extend(
        sorted(
            (
                surface
                for surface in sources
                if surface not in preferred_set
            ),
            key=lambda surface: (len(surface), surface),
        )
    )

    accepted: list[tuple[str, str, str]] = []
    seen_readings: set[str] = set()
    for surface in surfaces:
        result = active_validator.validate(surface)
        if result.code is not LexiconCode.ACCEPTED:
            continue
        common_candidates = tuple(
            candidate
            for candidate in result.candidates
            if candidate.part_of_speech[1] == "普通名詞"
        )
        readings = {
            candidate.reading for candidate in common_candidates
        }
        if len(readings) != 1:
            continue
        reading = next(iter(readings))
        if reading in seen_readings:
            continue
        if surface in preferred_set:
            source_ref = "curated"
        else:
            source_ref = f"wnja:{sources[surface]}"
        seen_readings.add(reading)
        accepted.append((surface, reading, source_ref))
    return accepted


def build_bot_data(wordnet_db: Path, output: Path) -> int:
    """Verify WordNet, generate the CSV, and return its accepted row count."""

    verify_wordnet_hash(wordnet_db)
    database_uri = f"{wordnet_db.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        rows = select_rows(extract_sources(connection))

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(CSV_HEADER)
        writer.writerows(rows)
    return len(rows)


def _existing_file(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"file does not exist: {path}")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the deterministic general Bot vocabulary."
    )
    parser.add_argument(
        "--wordnet-db",
        required=True,
        type=_existing_file,
        help="path to the extracted Japanese WordNet SQLite database",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="destination CSV path",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    count = build_bot_data(args.wordnet_db, args.output)
    print(f"Bot words: {count}")


if __name__ == "__main__":
    main()
