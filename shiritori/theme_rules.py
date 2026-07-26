"""Shared, deterministic semantic rules for the sixteen built-in themes.

The roots are Japanese WordNet 1.1 synsets.  Offline builders follow the
database's transitive ``ancestor`` relation from a noun sense to these roots.
Runtime code never opens WordNet or performs network requests; it reads only
the checked-in theme labels generated into ``theme_data/word_themes.csv``.
"""

from __future__ import annotations

from typing import Final


LEGACY_THEME_IDS: Final = (
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
NEW_THEME_IDS: Final = (
    "person_job",
    "nature",
    "place_building",
    "body",
    "clothing",
    "daily_tools",
    "music",
)
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
THEME_IDS: Final = tuple(THEME_ROOTS)

# Compatibility roots are deliberately fixed independently of THEME_ROOTS.
# Adding a target root must not silently widen the other senses accepted for
# that theme.  Only the reviewed semantic relationships below can coexist.
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

# Edges describe reviewed hierarchical relationships, not arbitrary
# co-occurrence.  A connected chain is allowed, so food+plant+fruit+nature is
# valid through plant/fruit even though food and nature have no direct edge.
_BOTANICAL_LINK_PAIRS: Final = tuple(
    (left, right)
    for position, left in enumerate(LEGACY_THEME_IDS)
    if left in BOTANICAL_THEME_IDS
    for right in LEGACY_THEME_IDS[position + 1 :]
    if right in BOTANICAL_THEME_IDS
)
_HIERARCHY_LINK_PAIRS: Final = (
    ("nature", "animal"),
    ("nature", "plant"),
    ("nature", "fruit"),
    ("nature", "vegetable"),
    ("nature", "place_building"),
    ("place_building", "country"),
    ("music", "instrument"),
)
AUTOMATIC_MULTI_LABEL_LINKS: Final[frozenset[frozenset[str]]] = frozenset(
    frozenset((left, right))
    for left, right in (*_BOTANICAL_LINK_PAIRS, *_HIERARCHY_LINK_PAIRS)
)
PERSON_SYNSET: Final = "00007846-n"
PERSON_EXCLUDED_THEME_IDS: Final = frozenset({"animal", "nature"})
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
    "person_job": frozenset({"ご存じ", "交友", "右腕"}),
    "nature": frozenset({"母", "父", "赤ちゃん", "西", "バス"}),
    "place_building": frozenset({"可動", "ナンパ", "合宿", "泊まり"}),
    "body": frozenset({"ぼぼ", "ぐう", "縫合"}),
    "clothing": frozenset(
        {"防水", "シングル", "シートベルト", "出で立ち"}
    ),
    "daily_tools": frozenset(
        {"主事", "とじ込み", "下ろし", "ポースレン"}
    ),
    "music": frozenset(
        {"前文", "序説", "救世主", "器官", "三角形"}
    ),
}


__all__ = [
    "AUTOMATIC_MULTI_LABEL_LINKS",
    "BOTANICAL_COMPATIBLE_ROOTS",
    "BOTANICAL_THEME_IDS",
    "LEGACY_THEME_IDS",
    "NEW_THEME_IDS",
    "PERSON_EXCLUDED_THEME_IDS",
    "PERSON_SYNSET",
    "THEME_BLOCKLISTS",
    "THEME_COMPATIBLE_ROOTS",
    "THEME_IDS",
    "THEME_ROOTS",
]
