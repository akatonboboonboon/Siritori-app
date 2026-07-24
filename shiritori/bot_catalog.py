"""Build a bot word index exclusively from server-validated surfaces.

The curated list is intentionally theme-neutral and modest: it makes Normal
and Hard bots usable while leaving final theme classification to the repository
owner.  Readings are never accepted as input.  They always come from
``LexiconValidator`` after exact dictionary lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Iterable, Protocol

from .bots import WordIndex, WordOption
from .lexicon import LexiconCode, LexiconResult, LexiconValidator
from .themes import ALL_THEME, ThemeDefinition


# Order doubles as a simple commonness rank.  Invalid or ambiguous entries are
# harmless: the builder validates and reports every one before constructing the
# immutable WordIndex.
DEFAULT_BOT_SURFACES: tuple[str, ...] = (
    "アイス",
    "苺",
    "椅子",
    "犬",
    "海",
    "映画",
    "絵本",
    "音楽",
    "柿",
    "傘",
    "カメラ",
    "学校",
    "キリン",
    "ギター",
    "狐",
    "靴",
    "クジラ",
    "熊",
    "車",
    "毛糸",
    "公園",
    "米",
    "コアラ",
    "コーヒー",
    "魚",
    "桜",
    "猿",
    "塩",
    "鹿",
    "写真",
    "新聞",
    "西瓜",
    "寿司",
    "蝉",
    "空",
    "太鼓",
    "卵",
    "狸",
    "地図",
    "机",
    "手紙",
    "時計",
    "トマト",
    "鳥",
    "長靴",
    "肉",
    "庭",
    "布",
    "猫",
    "鼠",
    "海苔",
    "花",
    "鋏",
    "飛行機",
    "葡萄",
    "船",
    "蛇",
    "帽子",
    "本",
    "枕",
    "マイク",
    "蜜柑",
    "虫",
    "眼鏡",
    "桃",
    "野菜",
    "山",
    "指輪",
    "雪",
    "洋服",
    "林檎",
    "料理",
    "留守",
    "檸檬",
    "蝋燭",
    "和紙",
    "ワニ",
    "ウサギ",
    "ズボン",
    "ゴマ",
    "ラジオ",
    "虹",
    "自転車",
)


class Validator(Protocol):
    def validate(self, raw_surface: str | None) -> LexiconResult:
        """Return an exact dictionary validation result."""


class CatalogSkipReason(str, Enum):
    INVALID_LEXICON = "invalid_lexicon"
    AMBIGUOUS_READING = "ambiguous_reading"
    NO_CANDIDATE = "no_candidate"
    DUPLICATE_CANONICAL_KEY = "duplicate_canonical_key"
    OUTSIDE_THEME = "outside_theme"


@dataclass(frozen=True, slots=True)
class CatalogDiagnostic:
    """Why one validated surface was not inserted into the bot index."""

    position: int
    surface: str
    reason: CatalogSkipReason
    lexicon_code: LexiconCode
    readings: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class BotCatalog:
    """A validated immutable set of options and its skip diagnostics."""

    index: WordIndex
    options: tuple[WordOption, ...]
    diagnostics: tuple[CatalogDiagnostic, ...]
    attempted_count: int

    @property
    def accepted_count(self) -> int:
        return len(self.options)

    @property
    def skipped_count(self) -> int:
        return len(self.diagnostics)


def _diagnostic(
    *,
    position: int,
    result: LexiconResult,
    reason: CatalogSkipReason,
    message: str | None = None,
) -> CatalogDiagnostic:
    return CatalogDiagnostic(
        position=position,
        surface=result.surface,
        reason=reason,
        lexicon_code=result.code,
        readings=result.readings,
        message=message or result.message,
    )


def build_bot_catalog(
    surfaces: Iterable[str] = DEFAULT_BOT_SURFACES,
    *,
    validator: Validator | None = None,
    theme: ThemeDefinition = ALL_THEME,
) -> BotCatalog:
    """Validate every surface, then build a :class:`WordIndex`.

    Ambiguous readings are skipped instead of guessed.  Duplicate canonical
    keys (normally kana/kanji variants or homophones under the project rules)
    keep the earliest-ranked surface.  The full, deterministic skip list is
    exposed for logging and data maintenance.
    """

    active_validator = validator or LexiconValidator()
    options: list[WordOption] = []
    diagnostics: list[CatalogDiagnostic] = []
    seen_canonical_keys: set[str] = set()
    attempted_count = 0

    for position, raw_surface in enumerate(surfaces):
        attempted_count += 1
        result = active_validator.validate(raw_surface)

        if result.code is LexiconCode.MULTIPLE_READINGS:
            diagnostics.append(
                _diagnostic(
                    position=position,
                    result=result,
                    reason=CatalogSkipReason.AMBIGUOUS_READING,
                    message="読みが複数あるためBot候補から除外しました。",
                )
            )
            continue
        if result.code is not LexiconCode.ACCEPTED:
            diagnostics.append(
                _diagnostic(
                    position=position,
                    result=result,
                    reason=CatalogSkipReason.INVALID_LEXICON,
                )
            )
            continue

        readings = tuple(
            dict.fromkeys(candidate.reading for candidate in result.candidates)
        )
        if len(readings) != 1 or not result.candidates:
            diagnostics.append(
                _diagnostic(
                    position=position,
                    result=result,
                    reason=(
                        CatalogSkipReason.AMBIGUOUS_READING
                        if len(readings) > 1
                        else CatalogSkipReason.NO_CANDIDATE
                    ),
                    message=(
                        "一意な辞書読みを取得できないため"
                        "Bot候補から除外しました。"
                    ),
                )
            )
            continue

        # All candidates share one reading here.  The tie-break only selects
        # dictionary metadata; it never resolves a semantic reading ambiguity.
        candidate = min(
            result.candidates,
            key=lambda item: (
                item.reading,
                item.canonical_key,
                item.dictionary_id,
                item.word_id,
                item.normalized_form,
                item.lemma,
            ),
        )
        if not theme.contains(result.surface, candidate.reading):
            diagnostics.append(
                _diagnostic(
                    position=position,
                    result=result,
                    reason=CatalogSkipReason.OUTSIDE_THEME,
                    message="選択テーマに含まれないため除外しました。",
                )
            )
            continue
        if candidate.canonical_key in seen_canonical_keys:
            diagnostics.append(
                _diagnostic(
                    position=position,
                    result=result,
                    reason=CatalogSkipReason.DUPLICATE_CANONICAL_KEY,
                    message=(
                        "同じ正規化キーの候補が先に登録されているため"
                        "除外しました。"
                    ),
                )
            )
            continue

        seen_canonical_keys.add(candidate.canonical_key)
        options.append(
            WordOption(
                surface=result.surface,
                reading=candidate.reading,
                canonical_key=candidate.canonical_key,
                rank=position,
            )
        )

    frozen_options = tuple(options)
    return BotCatalog(
        index=WordIndex(frozen_options),
        options=frozen_options,
        diagnostics=tuple(diagnostics),
        attempted_count=attempted_count,
    )


@lru_cache(maxsize=1)
def get_default_bot_catalog() -> BotCatalog:
    """Build and cache the server's default validated catalog."""

    return build_bot_catalog()


def get_default_word_index() -> WordIndex:
    """Convenience accessor for room/solo services."""

    return get_default_bot_catalog().index
