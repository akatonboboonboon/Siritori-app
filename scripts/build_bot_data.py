"""Build the checked-in general Bot vocabulary from pinned offline data.

Japanese WordNet supplies the broad noun candidate set.  The TKG
Japanese-English Learner's Dictionary supplies only a coarse commonness tier
and selected-reading hints.  Every emitted row is still accepted by the exact
Sudachi rules used for player input; TKG is never an existence authority.

This is an offline development tool.  Runtime Bot turns read only the generated
CSV and never contact WordNet, TKG, Sudachi's package index, or another website.
"""

from __future__ import annotations

import argparse
import csv
from contextlib import closing
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Final, Iterable


PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))

from shiritori.bot_catalog import DEFAULT_BOT_SURFACES
from shiritori.lexicon import (
    LexiconCode,
    LexiconResult,
    LexiconValidator,
    katakana_to_hiragana,
    normalize_surface,
)


WORDNET_DATABASE_SHA256: Final = (
    "a8e749c4a356bf93d0b5de505bca8b21e13746f5728f76819728e8b4c3305a12"
)
TKG_INDEX_SHA256: Final = (
    "cd7a5d73465118ea484c9809c09df61e6419c4d34689b5a78aca3a4cf36b8a4b"
)
TKG_COMMIT: Final = "9dd2e89ef86212d249c013d77f843d59b110330c"
TKG_INDEX_URL: Final = (
    "https://raw.githubusercontent.com/tkgally/je-dict-1/"
    f"{TKG_COMMIT}/entries_index.json"
)
CSV_HEADER: Final = (
    "surface",
    "reading",
    "source_ref",
    "commonness_tier",
)
MAX_SURFACE_LENGTH: Final = 16
ALLOWED_SURFACE: Final = re.compile(
    r"^[\u3041-\u3096\u30a1-\u30fa\u3400-\u9fff々〆ヵヶー・]+$"
)
INVALID_EDGE_MARKS: Final = frozenset({"々", "〆", "ー", "・"})
COMMON_NOUN_CATEGORY: Final = "普通名詞"
TKG_TIERS: Final = ("basic", "core", "general")
COMMONNESS_TIERS: Final = (
    "curated",
    "basic",
    "core",
    "general",
    "wordnet",
)
TIER_ORDER: Final = {
    tier: position for position, tier in enumerate(COMMONNESS_TIERS)
}


@dataclass(frozen=True, slots=True)
class TkgEntry:
    """One normalized noun hint from the pinned TKG index."""

    entry_id: str
    surface: str
    reading: str
    tier: str
    position: int


@dataclass(frozen=True, slots=True)
class RankedRow:
    """One validated candidate before canonical duplicate removal."""

    surface: str
    reading: str
    source_ref: str
    commonness_tier: str
    stable_rank: int

    def as_csv_row(self) -> tuple[str, str, str, str]:
        return (
            self.surface,
            self.reading,
            self.source_ref,
            self.commonness_tier,
        )


def _verify_hash(path: Path, expected: str, label: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest().lower() != expected.lower():
        raise ValueError(f"{label} SHA-256 does not match the record")


def verify_wordnet_hash(path: Path) -> None:
    """Reject a database other than the recorded Japanese WordNet release."""

    _verify_hash(path, WORDNET_DATABASE_SHA256, "Japanese WordNet")


def verify_tkg_hash(path: Path) -> None:
    """Reject a TKG index other than the recorded commit."""

    _verify_hash(path, TKG_INDEX_SHA256, "TKG entries_index.json")


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


def load_tkg_entries(path: Path) -> tuple[TkgEntry, ...]:
    """Load normalized noun hints; malformed or unranked rows are ignored."""

    with path.open(encoding="utf-8") as stream:
        payload = json.load(stream)
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("TKG index does not contain an entries list")

    entries: list[TkgEntry] = []
    for position, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            continue
        tier = str(raw_entry.get("vocabulary_tier", "")).lower()
        pos_tags = raw_entry.get("pos_tags")
        if (
            tier not in TKG_TIERS
            or not isinstance(pos_tags, list)
            or "noun" not in pos_tags
        ):
            continue
        surface = normalize_surface(raw_entry.get("headword"))
        reading = katakana_to_hiragana(
            normalize_surface(raw_entry.get("reading"))
        )
        entry_id = str(raw_entry.get("id", "")).strip()
        if not surface or not reading or not entry_id:
            continue
        entries.append(
            TkgEntry(
                entry_id=entry_id,
                surface=surface,
                reading=reading,
                tier=tier,
                position=position,
            )
        )
    return tuple(entries)


def index_tkg_entries(
    entries: Iterable[TkgEntry],
) -> dict[str, tuple[TkgEntry, ...]]:
    """Resolve duplicate pairs to their strongest tier and group by surface."""

    best_by_pair: dict[tuple[str, str], TkgEntry] = {}
    for entry in entries:
        key = (entry.surface, entry.reading)
        current = best_by_pair.get(key)
        if current is None or (
            TIER_ORDER[entry.tier],
            entry.position,
            entry.entry_id,
        ) < (
            TIER_ORDER[current.tier],
            current.position,
            current.entry_id,
        ):
            best_by_pair[key] = entry

    grouped: dict[str, list[TkgEntry]] = {}
    for entry in best_by_pair.values():
        grouped.setdefault(entry.surface, []).append(entry)
    return {
        surface: tuple(
            sorted(
                surface_entries,
                key=lambda entry: (
                    TIER_ORDER[entry.tier],
                    entry.position,
                    entry.entry_id,
                    entry.reading,
                ),
            )
        )
        for surface, surface_entries in grouped.items()
    }


def _unambiguous_common_reading(
    result: LexiconResult,
) -> str | None:
    """Return the legacy WordNet reading, without guessing ambiguity."""

    if result.code is not LexiconCode.ACCEPTED:
        return None
    readings = {
        candidate.reading
        for candidate in result.candidates
        if len(candidate.part_of_speech) >= 2
        and candidate.part_of_speech[1] == COMMON_NOUN_CATEGORY
    }
    if len(readings) != 1:
        return None
    return next(iter(readings))


def _selected_tkg_entry(
    result: LexiconResult,
    entries: Iterable[TkgEntry],
    *,
    allowed_tiers: frozenset[str] = frozenset(TKG_TIERS),
) -> TkgEntry | None:
    """Return the best TKG hint whose reading is a Sudachi candidate."""

    if not result.is_dictionary_word:
        return None
    for entry in entries:
        if (
            entry.tier in allowed_tiers
            and result.candidates_for_reading(entry.reading)
        ):
            return entry
    return None


def select_rows(
    sources: dict[str, str],
    tkg_entries: Iterable[TkgEntry] = (),
    *,
    validator: LexiconValidator | None = None,
) -> list[tuple[str, str, str, str]]:
    """Validate, tier, and rank a deterministic reading-unique vocabulary."""

    active_validator = validator or LexiconValidator()
    preferred = tuple(dict.fromkeys(DEFAULT_BOT_SURFACES))
    preferred_set = frozenset(preferred)
    wordnet_surfaces = sorted(
        (
            surface
            for surface in sources
            if surface not in preferred_set
        ),
        key=lambda surface: (len(surface), surface),
    )
    surfaces = [*preferred, *wordnet_surfaces]
    tkg_by_surface = index_tkg_entries(tkg_entries)

    ranked: list[RankedRow] = []
    selected_surfaces: set[str] = set()
    for stable_rank, surface in enumerate(surfaces):
        result = active_validator.validate(surface)
        if surface in preferred_set:
            reading = _unambiguous_common_reading(result)
            if reading is None:
                tkg_entry = _selected_tkg_entry(
                    result,
                    tkg_by_surface.get(surface, ()),
                )
                reading = (
                    tkg_entry.reading
                    if tkg_entry is not None
                    else None
                )
            if reading is None:
                continue
            ranked.append(
                RankedRow(
                    surface=surface,
                    reading=reading,
                    source_ref="curated",
                    commonness_tier="curated",
                    stable_rank=stable_rank,
                )
            )
            selected_surfaces.add(surface)
            continue

        tkg_entry = _selected_tkg_entry(
            result,
            tkg_by_surface.get(surface, ()),
        )
        if tkg_entry is not None:
            reading = tkg_entry.reading
            commonness_tier = tkg_entry.tier
        else:
            reading = _unambiguous_common_reading(result)
            commonness_tier = "wordnet"
        if reading is None:
            continue
        ranked.append(
            RankedRow(
                surface=surface,
                reading=reading,
                source_ref=f"wnja:{sources[surface]}",
                commonness_tier=commonness_tier,
                stable_rank=stable_rank,
            )
        )
        selected_surfaces.add(surface)

    supplement_tiers = frozenset({"basic", "core"})
    supplement_base = len(surfaces)
    for surface, entries in tkg_by_surface.items():
        if surface in selected_surfaces or surface in preferred_set:
            continue
        result = active_validator.validate(surface)
        entry = _selected_tkg_entry(
            result,
            entries,
            allowed_tiers=supplement_tiers,
        )
        if entry is None:
            continue
        ranked.append(
            RankedRow(
                surface=surface,
                reading=entry.reading,
                source_ref=f"tkg:{entry.entry_id}",
                commonness_tier=entry.tier,
                stable_rank=supplement_base + entry.position,
            )
        )

    ranked.sort(
        key=lambda row: (
            TIER_ORDER[row.commonness_tier],
            row.stable_rank,
            row.surface,
            row.reading,
            row.source_ref,
        )
    )
    accepted: list[tuple[str, str, str, str]] = []
    seen_surfaces: set[str] = set()
    seen_readings: set[str] = set()
    for row in ranked:
        if row.surface in seen_surfaces or row.reading in seen_readings:
            continue
        seen_surfaces.add(row.surface)
        seen_readings.add(row.reading)
        accepted.append(row.as_csv_row())
    return accepted


def render_rows(rows: Iterable[tuple[str, str, str, str]]) -> str:
    """Render generated rows exactly as the checked-in CSV."""

    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    writer.writerows(rows)
    return stream.getvalue()


def build_bot_data(
    wordnet_db: Path,
    tkg_index: Path,
    output: Path,
    *,
    check: bool = False,
) -> int:
    """Verify both inputs, generate the CSV, and return its row count."""

    verify_wordnet_hash(wordnet_db)
    verify_tkg_hash(tkg_index)
    database_uri = f"{wordnet_db.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as connection:
        sources = extract_sources(connection)
    rows = select_rows(sources, load_tkg_entries(tkg_index))
    rendered = render_rows(rows)

    if check:
        if not output.is_file() or output.read_text(encoding="utf-8") != rendered:
            raise ValueError("checked-in Bot CSV does not match regenerated data")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8", newline="")
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
        "--tkg-index",
        required=True,
        type=_existing_file,
        help="path to the pinned TKG entries_index.json",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="destination CSV path",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail instead of writing when output differs from regenerated data",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    count = build_bot_data(
        args.wordnet_db,
        args.tkg_index,
        args.output,
        check=args.check,
    )
    action = "verified" if args.check else "written"
    print(f"Bot words: {count} ({action})")


if __name__ == "__main__":
    main()
