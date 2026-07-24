"""Build the checked-in Japanese WordNet theme CSV files.

The input JSON is expected to map each theme id to an already ranked list of
objects with ``surface``, ``reading``, ``synsets``, and ``pageviews`` fields.
Rows stay in that input order.  This script only applies the repository's
fixed acceptance rules and writes deterministic UTF-8 CSV files.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Final


THEME_LIMITS: Final[dict[str, int]] = {
    "food": 120,
    "animal": 120,
    "plant": 100,
    "sport": 60,
    "country": 80,
    "instrument": 70,
    "vehicle": 100,
    "fruit": 60,
    "vegetable": 55,
}

THEME_BLOCKLISTS: Final[dict[str, frozenset[str]]] = {
    "food": frozenset(
        {
            # The user keeps these entries explicitly in user_themes.py.
            "林檎",
            "蜜柑",
            "西瓜",
            "おっぱい",
            "弥助",
            "ウサギ",
            "キジ",
            "雌鳥",
            "ニワトリ",
            "マンボウ",
            "カバ",
            "甲殻類",
            "エンパイア",
        }
    ),
    "animal": frozenset({"エチオピア", "吸血鬼", "ミッキーマウス", "動物"}),
    "plant": frozenset(
        {"将棋", "雷魚", "花王", "チーズ", "コーラ", "高粱酒"}
    ),
    "sport": frozenset({"戦い", "戦闘"}),
    "country": frozenset(
        {
            "ソビエト社会主義共和国連邦",
            "ユーゴスラビア",
            "ドイツ民主共和国",
            "ビルマ",
            "越南",
            "カンプチア",
            "スワジランド",
        }
    ),
    "instrument": frozenset({"真鍮", "ペット", "三角形", "音叉"}),
    "vehicle": frozenset(
        {"キャット", "海賊", "馬力", "装甲", "仏頂面", "弾道弾", "キャタピラ"}
    ),
    "fruit": frozenset(
        {
            "トウモロコシ",
            "エンパイア",
            "ダイズ",
            "南京豆",
            "ラッカセイ",
            "エノキ",
            "亜麻仁",
            "ヒヨコマメ",
            "ササゲ",
            "クミン",
            "ニワトコ",
            "蓖麻子",
            "グリーンピース",
            "蜀黍",
            "トチノキ",
            "扁豆",
        }
    ),
    "vegetable": frozenset(
        {"フライドポテト", "マッシュポテト", "ベークドポテト"}
    ),
}

CSV_HEADER: Final = ("surface", "reading", "source_ref")


def _positive_pageviews(value: object) -> bool:
    """Return whether a JSON pageview value is numeric and above zero."""

    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
    )


def select_rows(
    theme_id: str,
    ranked_rows: object,
) -> list[tuple[str, str, str]]:
    """Apply the fixed selection policy to one theme's ranked rows."""

    if theme_id not in THEME_LIMITS:
        raise ValueError(f"unsupported theme: {theme_id}")
    if not isinstance(ranked_rows, list):
        raise ValueError(f"{theme_id}: ranked rows must be a JSON array")

    accepted: list[tuple[str, str, str]] = []
    seen_readings: set[str] = set()
    seen_synset_keys: set[str] = set()
    blocklist = THEME_BLOCKLISTS[theme_id]

    for index, raw_row in enumerate(ranked_rows):
        if not isinstance(raw_row, dict):
            raise ValueError(f"{theme_id}[{index}]: row must be an object")
        if not _positive_pageviews(raw_row.get("pageviews")):
            continue

        surface = raw_row.get("surface")
        reading = raw_row.get("reading")
        synsets = raw_row.get("synsets")
        if not isinstance(surface, str) or not surface:
            raise ValueError(f"{theme_id}[{index}]: surface must be non-empty")
        if not isinstance(reading, str) or not reading:
            raise ValueError(f"{theme_id}[{index}]: reading must be non-empty")
        if (
            not isinstance(synsets, list)
            or not synsets
            or not all(isinstance(synset, str) and synset for synset in synsets)
        ):
            raise ValueError(
                f"{theme_id}[{index}]: synsets must be non-empty strings"
            )

        synset_key = "|".join(synsets)
        if (
            surface in blocklist
            or reading in seen_readings
            or synset_key in seen_synset_keys
        ):
            continue

        accepted.append((surface, reading, f"wnja:{synset_key}"))
        seen_readings.add(reading)
        seen_synset_keys.add(synset_key)
        if len(accepted) == THEME_LIMITS[theme_id]:
            break

    return accepted


def build_theme_data(ranked_json: Path, output_dir: Path) -> dict[str, int]:
    """Read the ranked JSON and write every configured theme CSV."""

    with ranked_json.open("r", encoding="utf-8") as stream:
        ranked_data = json.load(stream)
    if not isinstance(ranked_data, dict):
        raise ValueError("ranked JSON root must be an object")

    output_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for theme_id in THEME_LIMITS:
        if theme_id not in ranked_data:
            raise ValueError(f"ranked JSON is missing theme: {theme_id}")
        rows = select_rows(theme_id, ranked_data[theme_id])
        output_path = output_dir / f"{theme_id}.csv"
        with output_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(CSV_HEADER)
            writer.writerows(rows)
        counts[theme_id] = len(rows)
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build deterministic shiritori theme CSV files."
    )
    parser.add_argument(
        "--ranked-json",
        required=True,
        type=Path,
        help="ranked source JSON containing all configured themes",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="directory where {theme}.csv files are written",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    counts = build_theme_data(args.ranked_json, args.output_dir)
    for theme_id, count in counts.items():
        print(f"{theme_id}: {count}")


if __name__ == "__main__":
    main()
