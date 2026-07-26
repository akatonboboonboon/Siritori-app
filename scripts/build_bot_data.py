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
from shiritori.theme_data import (
    AUTO_THEME_SOURCE_REF,
    REVIEWED_THEME_DATA_PATH,
    THEME_DATA_DIRECTORY,
    THEME_SEPARATOR,
    WORD_THEME_DATA_HEADER,
    WORD_THEME_DATA_PATH,
    load_reviewed_theme_rows,
    load_theme_rows,
)
from shiritori.theme_rules import (
    BOTANICAL_THEME_IDS,
    PERSON_SYNSET,
    THEME_BLOCKLISTS,
    THEME_COMPATIBLE_ROOTS,
    THEME_IDS,
    THEME_ROOTS,
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
WORDNET_SOURCE_REF: Final = re.compile(r"^wnja:\d{8}-[nvars]$")
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


@dataclass(frozen=True, slots=True)
class ReviewedThemeSeed:
    """One reviewed legacy membership used only during offline generation."""

    surface: str
    reading: str
    source_ref: str
    theme_ids: frozenset[str]


BotCsvRow = tuple[str, str, str, str]
ThemeMembershipCsvRow = tuple[str, str, str, str, str]


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


def _legacy_wordnet_refs(source_ref: str) -> tuple[str, ...]:
    """Expand ``wnja:a|b`` legacy notation to fully qualified refs."""

    if not source_ref.startswith("wnja:"):
        raise ValueError(f"invalid legacy WordNet source_ref: {source_ref}")
    values = tuple(
        f"wnja:{value}"
        for value in source_ref.removeprefix("wnja:").split("|")
    )
    if any(WORDNET_SOURCE_REF.fullmatch(value) is None for value in values):
        raise ValueError(f"invalid legacy WordNet source_ref: {source_ref}")
    return values


def load_reviewed_theme_seeds(
    theme_data_directory: Path = THEME_DATA_DIRECTORY,
) -> tuple[ReviewedThemeSeed, ...]:
    """Load all legacy and explicit reviewed pairs as build-time seeds."""

    by_pair: dict[tuple[str, str], dict[str, object]] = {}
    for theme_id in THEME_IDS:
        for row in load_theme_rows(theme_data_directory / f"{theme_id}.csv"):
            if row.surface in THEME_BLOCKLISTS[theme_id]:
                raise ValueError(
                    f"reviewed seed reintroduces blocked {theme_id} word: "
                    f"{row.surface}"
                )
            key = (row.surface, row.reading)
            aggregate = by_pair.setdefault(
                key,
                {"source_refs": set(), "theme_ids": set()},
            )
            source_refs = aggregate["source_refs"]
            theme_ids = aggregate["theme_ids"]
            assert isinstance(source_refs, set)
            assert isinstance(theme_ids, set)
            source_refs.update(_legacy_wordnet_refs(row.source_ref))
            theme_ids.add(theme_id)

    additions_path = theme_data_directory / REVIEWED_THEME_DATA_PATH.name
    for row in load_reviewed_theme_rows(additions_path):
        key = (row.surface, row.reading)
        aggregate = by_pair.setdefault(
            key,
            {"source_refs": set(), "theme_ids": set()},
        )
        source_refs = aggregate["source_refs"]
        theme_ids = aggregate["theme_ids"]
        assert isinstance(source_refs, set)
        assert isinstance(theme_ids, set)
        source_refs.update(row.source_ref.split(THEME_SEPARATOR))
        theme_ids.update(row.theme_ids)

    seeds: list[ReviewedThemeSeed] = []
    for (surface, reading), aggregate in by_pair.items():
        source_refs = aggregate["source_refs"]
        theme_ids = aggregate["theme_ids"]
        assert isinstance(source_refs, set)
        assert isinstance(theme_ids, set)
        if not source_refs:
            raise ValueError(
                f"reviewed theme seed lacks provenance: {surface}"
            )
        seeds.append(
            ReviewedThemeSeed(
                surface=surface,
                reading=reading,
                source_ref=THEME_SEPARATOR.join(sorted(source_refs)),
                theme_ids=frozenset(theme_ids),
            )
        )
    return tuple(
        sorted(
            seeds,
            key=lambda seed: (len(seed.surface), seed.surface, seed.reading),
        )
    )


def validate_reviewed_theme_seeds(
    seeds: Iterable[ReviewedThemeSeed],
    *,
    validator: LexiconValidator,
) -> tuple[ReviewedThemeSeed, ...]:
    """Require every reviewed exact reading, including aliases, in Sudachi."""

    frozen_seeds = tuple(seeds)
    for seed in frozen_seeds:
        result = validator.validate(seed.surface)
        if not result.candidates_for_reading(seed.reading):
            raise ValueError(
                "reviewed theme seed no longer matches Sudachi: "
                f"{seed.surface}/{seed.reading}"
            )
    return frozen_seeds


def merge_reviewed_theme_rows(
    rows: Iterable[BotCsvRow],
    seeds: Iterable[ReviewedThemeSeed],
    *,
    validator: LexiconValidator | None = None,
) -> list[BotCsvRow]:
    """Add conflict-free WordNet reviewed pairs to the general vocabulary.

    All reviewed pairs remain in the unified theme mapping.  This narrower
    merge adds only pairs that preserve the general vocabulary's one-surface,
    one-reading invariant and that have WordNet provenance.
    """

    merged = list(rows)
    seen_pairs = {(row[0], row[1]) for row in merged}
    seen_surfaces = {row[0] for row in merged}
    seen_readings = {row[1] for row in merged}
    active_validator = validator or LexiconValidator()
    frozen_seeds = validate_reviewed_theme_seeds(
        seeds,
        validator=active_validator,
    )

    for seed in frozen_seeds:
        pair = (seed.surface, seed.reading)
        if pair in seen_pairs:
            continue
        if seed.surface in seen_surfaces or seed.reading in seen_readings:
            continue
        wordnet_ref = next(
            (
                source_ref
                for source_ref in seed.source_ref.split(THEME_SEPARATOR)
                if WORDNET_SOURCE_REF.fullmatch(source_ref) is not None
            ),
            None,
        )
        if wordnet_ref is None:
            continue
        merged.append(
            (seed.surface, seed.reading, wordnet_ref, "wordnet")
        )
        seen_pairs.add(pair)
        seen_surfaces.add(seed.surface)
        seen_readings.add(seed.reading)
    return merged

def classify_theme_memberships(
    connection: sqlite3.Connection,
    rows: Iterable[BotCsvRow],
    seeds: Iterable[ReviewedThemeSeed],
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Classify exact pairs with fixed, per-theme sense compatibility.

    A surface receives an automatic theme ``T`` only when at least one noun
    sense reaches a target root for ``T`` and every noun sense reaches one of
    the separately pinned compatibility roots for ``T``.  Compatibility roots
    intentionally do not expand when target roots change.  Reviewed exact-pair
    seeds are unioned only after all automatic review gates.
    """

    frozen_rows = tuple(rows)
    connection.execute("DROP TABLE IF EXISTS temp.selected_bot_surface")
    connection.execute("DROP TABLE IF EXISTS temp.selected_theme_root")
    connection.execute("DROP TABLE IF EXISTS temp.selected_compatible_root")
    connection.execute(
        "CREATE TEMP TABLE selected_bot_surface("
        "surface TEXT PRIMARY KEY)"
    )
    connection.executemany(
        "INSERT INTO selected_bot_surface(surface) VALUES (?)",
        ((row[0],) for row in frozen_rows),
    )
    connection.execute(
        "CREATE TEMP TABLE selected_theme_root("
        "theme_id TEXT NOT NULL, root TEXT NOT NULL, "
        "PRIMARY KEY(theme_id, root))"
    )
    connection.executemany(
        "INSERT INTO selected_theme_root(theme_id, root) VALUES (?, ?)",
        (
            (theme_id, root)
            for theme_id, roots in THEME_ROOTS.items()
            for root in roots
        ),
    )
    connection.execute(
        "CREATE TEMP TABLE selected_compatible_root("
        "theme_id TEXT NOT NULL, root TEXT NOT NULL, "
        "PRIMARY KEY(theme_id, root))"
    )
    connection.executemany(
        "INSERT INTO selected_compatible_root(theme_id, root) VALUES (?, ?)",
        (
            (theme_id, root)
            for theme_id, roots in THEME_COMPATIBLE_ROOTS.items()
            for root in roots
        ),
    )

    senses_by_surface: dict[str, set[str]] = {}
    for surface, synset in connection.execute(
        """
        SELECT DISTINCT selected_bot_surface.surface, sense.synset
        FROM selected_bot_surface
        JOIN word ON word.lemma = selected_bot_surface.surface
        JOIN sense ON sense.wordid = word.wordid
        WHERE word.lang = 'jpn' AND word.pos = 'n'
        ORDER BY 1, 2
        """
    ):
        senses_by_surface.setdefault(str(surface), set()).add(str(synset))

    def sense_theme_matches(table_name: str) -> dict[tuple[str, str], set[str]]:
        if table_name not in {
            "selected_theme_root",
            "selected_compatible_root",
        }:
            raise ValueError(f"unsupported root table: {table_name}")
        matches: dict[tuple[str, str], set[str]] = {}
        query = f"""
            SELECT DISTINCT selected_bot_surface.surface,
                            sense.synset,
                            {table_name}.theme_id
            FROM selected_bot_surface
            JOIN word ON word.lemma = selected_bot_surface.surface
            JOIN sense ON sense.wordid = word.wordid
            JOIN ancestor ON ancestor.synset1 = sense.synset
            JOIN {table_name} ON {table_name}.root = ancestor.synset2
            WHERE word.lang = 'jpn' AND word.pos = 'n'
            UNION
            SELECT DISTINCT selected_bot_surface.surface,
                            sense.synset,
                            {table_name}.theme_id
            FROM selected_bot_surface
            JOIN word ON word.lemma = selected_bot_surface.surface
            JOIN sense ON sense.wordid = word.wordid
            JOIN {table_name} ON {table_name}.root = sense.synset
            WHERE word.lang = 'jpn' AND word.pos = 'n'
            ORDER BY 1, 2, 3
        """
        for surface, synset, theme_id in connection.execute(query):
            key = (str(surface), str(synset))
            matches.setdefault(key, set()).add(str(theme_id))
        return matches

    targets_by_sense = sense_theme_matches("selected_theme_root")
    compatible_by_sense = sense_theme_matches("selected_compatible_root")
    person_senses = {
        (str(surface), str(synset))
        for surface, synset in connection.execute(
            """
            SELECT DISTINCT selected_bot_surface.surface, sense.synset
            FROM selected_bot_surface
            JOIN word ON word.lemma = selected_bot_surface.surface
            JOIN sense ON sense.wordid = word.wordid
            JOIN ancestor ON ancestor.synset1 = sense.synset
            WHERE word.lang = 'jpn' AND word.pos = 'n'
              AND ancestor.synset2 = ?
            UNION
            SELECT DISTINCT selected_bot_surface.surface, sense.synset
            FROM selected_bot_surface
            JOIN word ON word.lemma = selected_bot_surface.surface
            JOIN sense ON sense.wordid = word.wordid
            WHERE word.lang = 'jpn' AND word.pos = 'n'
              AND sense.synset = ?
            ORDER BY 1, 2
            """,
            (PERSON_SYNSET, PERSON_SYNSET),
        )
    }

    direct_by_surface: dict[str, set[str]] = {}
    for surface, senses in senses_by_surface.items():
        automatic_theme_ids: set[str] = set()
        for theme_id in THEME_IDS:
            if surface in THEME_BLOCKLISTS[theme_id]:
                continue
            if not any(
                theme_id in targets_by_sense.get((surface, synset), ())
                for synset in senses
            ):
                continue
            if theme_id == "animal" and any(
                (surface, synset) in person_senses for synset in senses
            ):
                continue
            if all(
                theme_id in compatible_by_sense.get((surface, synset), ())
                for synset in senses
            ):
                automatic_theme_ids.add(theme_id)

        # Only the reviewed botanical family can be emitted automatically as
        # a multi-label union.  Every other cross-family union needs review.
        if (
            len(automatic_theme_ids) > 1
            and not automatic_theme_ids <= BOTANICAL_THEME_IDS
        ):
            automatic_theme_ids.clear()
        if automatic_theme_ids:
            direct_by_surface[surface] = automatic_theme_ids

    frozen_seeds = tuple(seeds)
    reviewed_by_pair = {
        (seed.surface, seed.reading): seed.theme_ids
        for seed in frozen_seeds
    }
    memberships: dict[tuple[str, str], tuple[str, ...]] = {}
    for surface, reading, _source_ref, _tier in frozen_rows:
        theme_ids = set(direct_by_surface.get(surface, ()))
        theme_ids.update(reviewed_by_pair.get((surface, reading), ()))
        memberships[(surface, reading)] = tuple(
            theme_id for theme_id in THEME_IDS if theme_id in theme_ids
        )
    for seed in frozen_seeds:
        pair = (seed.surface, seed.reading)
        if pair not in memberships:
            memberships[pair] = tuple(
                theme_id
                for theme_id in THEME_IDS
                if theme_id in seed.theme_ids
            )
    return memberships


def theme_membership_rows(
    memberships: dict[tuple[str, str], tuple[str, ...]],
    seeds: Iterable[ReviewedThemeSeed],
) -> list[ThemeMembershipCsvRow]:
    """Return tagged exact pairs with their generation provenance."""

    reviewed_by_pair = {
        (seed.surface, seed.reading): seed.source_ref
        for seed in seeds
    }
    rows: list[ThemeMembershipCsvRow] = []
    for (surface, reading), theme_ids in memberships.items():
        if not theme_ids:
            continue
        reviewed_source_ref = reviewed_by_pair.get((surface, reading))
        rows.append(
            (
                surface,
                reading,
                THEME_SEPARATOR.join(theme_ids),
                "reviewed" if reviewed_source_ref else "auto",
                reviewed_source_ref or AUTO_THEME_SOURCE_REF,
            )
        )
    return rows

def render_rows(rows: Iterable[BotCsvRow]) -> str:
    """Render general Bot rows exactly as the checked-in CSV."""

    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    writer.writerows(rows)
    return stream.getvalue()


def render_theme_memberships(
    rows: Iterable[ThemeMembershipCsvRow],
) -> str:
    """Render the sparse exact-pair multi-label mapping."""

    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(WORD_THEME_DATA_HEADER)
    writer.writerows(rows)
    return stream.getvalue()


def build_bot_data(
    wordnet_db: Path,
    tkg_index: Path,
    output: Path,
    *,
    theme_output: Path,
    theme_data_directory: Path = THEME_DATA_DIRECTORY,
    check: bool = False,
) -> int:
    """Generate the general vocabulary plus its sparse multi-label map."""

    verify_wordnet_hash(wordnet_db)
    verify_tkg_hash(tkg_index)
    seeds = load_reviewed_theme_seeds(theme_data_directory)
    validator = LexiconValidator()
    database_uri = f"{wordnet_db.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as connection:
        sources = extract_sources(connection)
        base_rows = select_rows(
            sources,
            load_tkg_entries(tkg_index),
            validator=validator,
        )
        rows = merge_reviewed_theme_rows(
            base_rows,
            seeds,
            validator=validator,
        )
        memberships = classify_theme_memberships(
            connection,
            rows,
            seeds,
        )
    rendered = render_rows(rows)
    rendered_themes = render_theme_memberships(
        theme_membership_rows(memberships, seeds)
    )

    if output.resolve() == theme_output.resolve():
        raise ValueError("Bot output and theme output must be different files")
    if check:
        if not output.is_file() or output.read_text(encoding="utf-8") != rendered:
            raise ValueError("checked-in Bot CSV does not match regenerated data")
        if (
            not theme_output.is_file()
            or theme_output.read_text(encoding="utf-8") != rendered_themes
        ):
            raise ValueError(
                "checked-in theme CSV does not match regenerated data"
            )
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8", newline="")
        theme_output.parent.mkdir(parents=True, exist_ok=True)
        theme_output.write_text(
            rendered_themes,
            encoding="utf-8",
            newline="",
        )
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
        "--theme-output",
        type=Path,
        default=WORD_THEME_DATA_PATH,
        help="destination for sparse exact-pair theme memberships",
    )
    parser.add_argument(
        "--theme-data-directory",
        type=Path,
        default=THEME_DATA_DIRECTORY,
        help="reviewed build-time seed directory for the nine legacy CSVs",
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
        theme_output=args.theme_output,
        theme_data_directory=args.theme_data_directory,
        check=args.check,
    )
    action = "verified" if args.check else "written"
    print(f"Bot words: {count} ({action})")


if __name__ == "__main__":
    main()
