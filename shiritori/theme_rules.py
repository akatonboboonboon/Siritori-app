"""Shared, deterministic semantic rules for the nine built-in themes.

The roots are Japanese WordNet 1.1 synsets.  Offline builders follow the
database's transitive ``ancestor`` relation from a noun sense to these roots.
Runtime code never opens WordNet or performs network requests; it reads only
the checked-in theme labels generated into ``theme_data/word_themes.csv``.
"""

from __future__ import annotations

from typing import Final


THEME_ROOTS: Final[dict[str, tuple[str, ...]]] = {
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
THEME_IDS: Final = tuple(THEME_ROOTS)

# Compatibility roots are deliberately fixed independently of THEME_ROOTS.
# Adding a new target root in the future must not silently widen which other
# noun senses are considered compatible with an existing theme.  The four
# botanical/food themes may coexist; every other theme is compatible only with
# its own current semantic root.
BOTANICAL_THEME_IDS: Final = frozenset(
    {"food", "plant", "fruit", "vegetable"}
)
BOTANICAL_COMPATIBLE_ROOTS: Final = (
    "00021265-n",
    "07555863-n",
    "00017222-n",
    "13134947-n",
    "07707451-n",
)
THEME_COMPATIBLE_ROOTS: Final[dict[str, tuple[str, ...]]] = {
    "food": BOTANICAL_COMPATIBLE_ROOTS,
    "animal": ("00015388-n",),
    "plant": BOTANICAL_COMPATIBLE_ROOTS,
    "sport": ("00523513-n",),
    "country": ("08544813-n",),
    "instrument": ("03800933-n",),
    "vehicle": ("04524313-n",),
    "fruit": BOTANICAL_COMPATIBLE_ROOTS,
    "vegetable": BOTANICAL_COMPATIBLE_ROOTS,
}
PERSON_SYNSET: Final = "00007846-n"

# These are reviewed Japanese WordNet polysemy or taxonomy mismatches.  A
# surface is blocked only from the named theme; it may still receive another
# independently supported label.
THEME_BLOCKLISTS: Final[dict[str, frozenset[str]]] = {
    "food": frozenset(
        {
            # User-authored reviewed rows add these food memberships back.
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
    "animal": frozenset(
        {"エチオピア", "吸血鬼", "ミッキーマウス", "動物"}
    ),
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
        {
            "キャット",
            "海賊",
            "馬力",
            "装甲",
            "仏頂面",
            "弾道弾",
            "キャタピラ",
        }
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


__all__ = [
    "BOTANICAL_COMPATIBLE_ROOTS",
    "BOTANICAL_THEME_IDS",
    "PERSON_SYNSET",
    "THEME_BLOCKLISTS",
    "THEME_COMPATIBLE_ROOTS",
    "THEME_IDS",
    "THEME_ROOTS",
]
