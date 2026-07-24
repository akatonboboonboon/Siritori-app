"""Build ranked theme candidates from Japanese WordNet for development.

This is an offline data-preparation tool, not application runtime code. It
extracts noun descendants from a local Japanese WordNet SQLite database,
validates each surface with the application's Sudachi lexicon rules, and uses
the Japanese Wikipedia Action API to rank the remaining words by the most
recent 30 days of pageviews.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Final, Iterator
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# Support both ``python -m scripts.rank_wordnet_themes`` and direct execution
# from the repository root.
PROJECT_ROOT: Final = Path(__file__).resolve().parent.parent
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))

from shiritori.lexicon import LexiconCode, get_default_validator


ROOTS: Final[dict[str, tuple[str, ...]]] = {
    "food": ("00021265-n", "07555863-n"),
    "animal": ("00015388-n",),
    "plant": ("00017222-n",),
    "sport": ("00523513-n",),
    "country": ("08544813-n",),
    "instrument": ("03800933-n",),
    "vehicle": ("04524313-n",),
    "fruit": ("13134947-n",),
    "vegetable": ("07707451-n",),
}

ALLOWED_SURFACE: Final = re.compile(
    r"^[\u3041-\u3096\u30a1-\u30fa\u3400-\u9fff々〆ヵヶー・]+$"
)
TAXON_SUFFIXES: Final = (
    "亜科",
    "亜属",
    "亜種",
    "亜目",
    "上科",
    "下目",
    "科",
    "属",
    "族",
    "類",
    "目",
)

WIKIPEDIA_API_URL: Final = "https://ja.wikipedia.org/w/api.php"
WIKIPEDIA_USER_AGENT: Final = (
    "SiritoriAppThemeBuilder/1.0 "
    "(https://github.com/akatonboboonboon/Siritori-app)"
)
WIKIPEDIA_BATCH_SIZE: Final = 10
PAGEVIEW_DAYS: Final = 30
HTTP_TIMEOUT_SECONDS: Final = 30
REQUEST_INTERVAL_SECONDS: Final = 0.1


def _placeholders(values: tuple[str, ...]) -> str:
    return ", ".join("?" for _ in values)


def extract_words(
    connection: sqlite3.Connection,
    roots: tuple[str, ...],
) -> dict[str, set[str]]:
    """Return Japanese noun surfaces and descendant synsets for the roots."""

    sql = f"""
        SELECT DISTINCT word.lemma, sense.synset
        FROM ancestor
        JOIN sense ON sense.synset = ancestor.synset1
        JOIN word ON word.wordid = sense.wordid
        WHERE ancestor.synset2 IN ({_placeholders(roots)})
          AND word.lang = 'jpn'
          AND word.pos = 'n'
        UNION
        SELECT DISTINCT word.lemma, sense.synset
        FROM sense
        JOIN word ON word.wordid = sense.wordid
        WHERE sense.synset IN ({_placeholders(roots)})
          AND word.lang = 'jpn'
          AND word.pos = 'n'
        ORDER BY 1
    """
    sources: dict[str, set[str]] = defaultdict(set)
    for surface, synset in connection.execute(sql, roots + roots):
        sources[str(surface)].add(str(synset))
    return sources


def prefilter(surface: str, theme_id: str) -> bool:
    """Apply deterministic character, length, and taxonomy filters."""

    if not 2 <= len(surface) <= 16:
        return False
    if not ALLOWED_SURFACE.fullmatch(surface):
        return False
    if theme_id in {"animal", "plant"} and surface.endswith(TAXON_SUFFIXES):
        return False
    return True


def validate_words(
    sources: dict[str, set[str]],
    theme_id: str,
) -> list[dict[str, object]]:
    """Keep only Sudachi-accepted surfaces having exactly one reading."""

    validator = get_default_validator()
    accepted: list[dict[str, object]] = []
    for surface, synsets in sources.items():
        if not prefilter(surface, theme_id):
            continue
        result = validator.validate(surface)
        if result.code is not LexiconCode.ACCEPTED:
            continue
        if len(result.readings) != 1:
            continue
        accepted.append(
            {
                "surface": surface,
                "reading": result.readings[0],
                "synsets": sorted(synsets),
            }
        )
    return accepted


def _chunks(values: list[str], size: int) -> Iterator[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _follow(mapping: dict[str, str], title: str) -> str:
    """Follow normalization and redirect mappings without risking a cycle."""

    seen: set[str] = set()
    while title in mapping and title not in seen:
        seen.add(title)
        title = mapping[title]
    return title


def _request_wikipedia(parameters: dict[str, object]) -> dict[str, object]:
    request = Request(
        f"{WIKIPEDIA_API_URL}?{urlencode(parameters)}",
        headers={
            "Accept": "application/json",
            "User-Agent": WIKIPEDIA_USER_AGENT,
        },
    )
    with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("Wikipedia returned a non-object JSON response")
    if "error" in payload:
        raise RuntimeError(f"Wikipedia API error: {payload['error']!r}")
    return payload


def _batch_pageviews(batch: list[str]) -> dict[str, int]:
    """Collect every continued response for one ten-title API query."""

    base_parameters: dict[str, object] = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "prop": "pageviews",
        "pvipdays": str(PAGEVIEW_DAYS),
        "redirects": "1",
        "titles": "|".join(batch),
    }
    title_mapping: dict[str, str] = {}
    pageviews_by_title: dict[str, dict[str, int]] = defaultdict(dict)
    continuation: dict[str, object] = {}
    seen_continuations: set[tuple[tuple[str, str], ...]] = set()

    while True:
        parameters = dict(base_parameters)
        parameters.update(continuation)
        payload = _request_wikipedia(parameters)
        query = payload.get("query", {})
        if not isinstance(query, dict):
            raise RuntimeError("Wikipedia response has an invalid query object")

        for group_name in ("normalized", "redirects"):
            group = query.get(group_name, ())
            if not isinstance(group, list):
                continue
            for item in group:
                if not isinstance(item, dict):
                    continue
                source = item.get("from")
                destination = item.get("to")
                if isinstance(source, str) and isinstance(destination, str):
                    title_mapping[source] = destination

        pages = query.get("pages", ())
        if not isinstance(pages, list):
            raise RuntimeError("Wikipedia response has an invalid pages array")
        for page in pages:
            if not isinstance(page, dict) or "missing" in page:
                continue
            title = page.get("title")
            daily_pageviews = page.get("pageviews", {})
            if not isinstance(title, str) or not isinstance(
                daily_pageviews, dict
            ):
                continue
            collected = pageviews_by_title[title]
            for day, value in daily_pageviews.items():
                if value is None:
                    collected[str(day)] = 0
                elif (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                ):
                    collected[str(day)] = int(value)

        next_continuation = payload.get("continue")
        time.sleep(REQUEST_INTERVAL_SECONDS)
        if next_continuation is None:
            break
        if not isinstance(next_continuation, dict):
            raise RuntimeError(
                "Wikipedia response has an invalid continuation object"
            )
        continuation_key = tuple(
            sorted(
                (str(key), str(value))
                for key, value in next_continuation.items()
            )
        )
        if continuation_key in seen_continuations:
            raise RuntimeError("Wikipedia repeated a continuation token")
        seen_continuations.add(continuation_key)
        continuation = dict(next_continuation)

    return {
        title: sum(
            pageviews_by_title.get(_follow(title_mapping, title), {}).values()
        )
        for title in batch
    }


def wikipedia_pageviews(titles: list[str]) -> dict[str, int]:
    """Fetch 30-day totals in polite ten-title batches."""

    scores: dict[str, int] = {}
    for batch_number, batch in enumerate(
        _chunks(titles, WIKIPEDIA_BATCH_SIZE),
        start=1,
    ):
        scores.update(_batch_pageviews(batch))
        if batch_number % 10 == 0:
            print(f"  Wikipedia batches: {batch_number}", flush=True)
    return scores


def rank_theme(
    connection: sqlite3.Connection,
    theme_id: str,
    roots: tuple[str, ...],
) -> list[dict[str, object]]:
    """Extract, validate, score, and rank one configured theme."""

    sources = extract_words(connection, roots)
    accepted = validate_words(sources, theme_id)
    print(
        f"{theme_id}: {len(sources)} source / {len(accepted)} valid",
        flush=True,
    )
    scores = wikipedia_pageviews(
        [str(item["surface"]) for item in accepted]
    )
    for item in accepted:
        item["pageviews"] = scores[str(item["surface"])]
    accepted.sort(
        key=lambda item: (
            -int(item["pageviews"]),
            len(str(item["surface"])),
            str(item["surface"]),
        )
    )
    return accepted


def _existing_file(value: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"file does not exist: {path}")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build ranked theme candidates from a Japanese WordNet database."
        )
    )
    parser.add_argument(
        "--wordnet-db",
        required=True,
        type=_existing_file,
        help="path to the local Japanese WordNet SQLite database",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="path for the generated ranked JSON",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    wordnet_db = args.wordnet_db.resolve()
    output_path = args.output.resolve()
    if wordnet_db == output_path:
        raise ValueError("--output must not overwrite --wordnet-db")

    ranked: dict[str, list[dict[str, object]]] = {}
    database_uri = f"{wordnet_db.as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        for theme_id, roots in ROOTS.items():
            ranked[theme_id] = rank_theme(connection, theme_id, roots)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(ranked, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
