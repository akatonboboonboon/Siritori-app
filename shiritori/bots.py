"""Framework-independent bot strategies for shiritori.

The strategies consume already validated dictionary entries.  They deliberately
do not know about NiceGUI, a database, or a particular game-state class, which
makes them safe to run from a room worker and straightforward to unit test.

``EasyBot`` is intentionally not provided.  The repository owner can implement
the small :class:`BotStrategy` protocol as an independent contribution.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import Iterable, Mapping, Protocol, Sequence, runtime_checkable


_SMALL_TO_LARGE_KANA = {
    "ぁ": "あ",
    "ぃ": "い",
    "ぅ": "う",
    "ぇ": "え",
    "ぉ": "お",
    "っ": "つ",
    "ゃ": "や",
    "ゅ": "ゆ",
    "ょ": "よ",
    "ゎ": "わ",
    "ゕ": "か",
    "ゖ": "け",
}

_VOWEL_BY_KANA = {
    **{kana: "あ" for kana in "あかがさざただなはばぱまゃやらゎわ"},
    **{kana: "い" for kana in "いきぎしじちぢにひびぴみりゐ"},
    **{kana: "う" for kana in "うくぐすずつづぬふぶぷむゅゆるゔ"},
    **{kana: "え" for kana in "えけげせぜてでねへべぺめれゑ"},
    **{kana: "お" for kana in "おこごそぞとどのほぼぽもょよろを"},
}


def canonical_kana(character: str) -> str:
    """Return the full-sized hiragana used for chaining."""

    return _SMALL_TO_LARGE_KANA.get(character, character)


def final_kana(reading: str) -> str:
    """Return a reading's effective final kana.

    A prolonged sound mark uses the preceding kana's vowel (``コーヒー`` is
    therefore followed by an ``い`` word).  The dictionary layer supplies
    hiragana readings, so this function treats an unresolvable mark as invalid.
    """

    if not reading:
        raise ValueError("reading must not be empty")
    final = reading[-1]
    if final != "ー":
        return canonical_kana(final)
    for character in reversed(reading[:-1]):
        if character == "ー":
            continue
        vowel = _VOWEL_BY_KANA.get(canonical_kana(character))
        if vowel is None:
            break
        return vowel
    raise ValueError(f"cannot resolve prolonged sound mark in {reading!r}")


@dataclass(frozen=True, slots=True)
class WordOption:
    """One dictionary-approved word available to a bot.

    Smaller ``rank`` values mean more common or otherwise preferred words.
    ``canonical_key`` must be the same key used by the game to detect reuse.
    """

    surface: str
    reading: str
    canonical_key: str
    rank: int = 1_000_000

    def __post_init__(self) -> None:
        if not self.surface or not self.reading or not self.canonical_key:
            raise ValueError("surface, reading, and canonical_key are required")
        if self.rank < 0:
            raise ValueError("rank must be non-negative")

    @property
    def first_kana(self) -> str:
        return canonical_kana(self.reading[0])

    @property
    def last_kana(self) -> str:
        return final_kana(self.reading)

    @property
    def ends_with_n(self) -> bool:
        return self.last_kana == "ん"


@dataclass(frozen=True, slots=True)
class BotContext:
    """The minimum immutable state needed to choose a word."""

    expected_kana: str
    used_canonical_keys: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.expected_kana:
            raise ValueError("expected_kana is required")


@runtime_checkable
class BotStrategy(Protocol):
    """Extension point for every bot difficulty, including user-owned Easy."""

    def choose(self, context: BotContext, words: "WordIndex") -> WordOption | None:
        """Choose a legal option, or return ``None`` when no move exists."""


class WordIndex:
    """Immutable start-kana index used by all strategies.

    Building the index is linear in the dictionary slice.  Looking up the legal
    candidates for a turn scans only the matching first-kana bucket.
    """

    def __init__(self, options: Iterable[WordOption]) -> None:
        buckets: dict[str, list[WordOption]] = {}
        seen: set[tuple[str, str]] = set()
        for option in options:
            identity = (option.canonical_key, option.reading)
            if identity in seen:
                continue
            seen.add(identity)
            buckets.setdefault(option.first_kana, []).append(option)

        frozen = {
            kana: tuple(
                sorted(
                    bucket,
                    key=lambda option: (
                        option.rank,
                        option.reading,
                        option.canonical_key,
                        option.surface,
                    ),
                )
            )
            for kana, bucket in buckets.items()
        }
        self._by_first: Mapping[str, tuple[WordOption, ...]] = MappingProxyType(
            frozen
        )

    def starting_with(self, kana: str) -> tuple[WordOption, ...]:
        return self._by_first.get(canonical_kana(kana), ())

    def legal_options(
        self,
        expected_kana: str,
        used_canonical_keys: frozenset[str] | set[str] = frozenset(),
        *,
        avoid_n: bool = False,
    ) -> tuple[WordOption, ...]:
        return tuple(
            option
            for option in self.starting_with(expected_kana)
            if option.canonical_key not in used_canonical_keys
            and (not avoid_n or not option.ends_with_n)
        )

    def reply_count(
        self,
        option: WordOption,
        used_canonical_keys: frozenset[str] | set[str],
    ) -> int:
        """Count safe opponent replies after ``option`` is played."""

        if option.ends_with_n:
            return 0
        unavailable = set(used_canonical_keys)
        unavailable.add(option.canonical_key)
        return len(
            self.legal_options(
                option.last_kana,
                unavailable,
                avoid_n=True,
            )
        )


class _SeededStrategy:
    def __init__(self, *, seed: int | str = 0) -> None:
        self._seed = str(seed)

    def _tie_break(self, option: WordOption, context: BotContext) -> bytes:
        used = "\x1f".join(sorted(context.used_canonical_keys))
        value = (
            f"{self._seed}\x1e{context.expected_kana}\x1e{used}"
            f"\x1e{option.canonical_key}\x1e{option.reading}"
        )
        return hashlib.sha256(value.encode("utf-8")).digest()

    @staticmethod
    def _safe_or_all(options: Sequence[WordOption]) -> Sequence[WordOption]:
        safe = tuple(option for option in options if not option.ends_with_n)
        return safe or options


class NormalBot(_SeededStrategy):
    """Prefer common words while avoiding an immediate ``ん`` loss."""

    def choose(self, context: BotContext, words: WordIndex) -> WordOption | None:
        legal = words.legal_options(
            context.expected_kana,
            context.used_canonical_keys,
        )
        if not legal:
            return None
        candidates = self._safe_or_all(legal)
        return min(
            candidates,
            key=lambda option: (
                option.rank,
                self._tie_break(option, context),
            ),
        )


class HardBot(_SeededStrategy):
    """Choose the move that leaves the opponent the fewest safe replies."""

    def choose(self, context: BotContext, words: WordIndex) -> WordOption | None:
        legal = words.legal_options(
            context.expected_kana,
            context.used_canonical_keys,
        )
        if not legal:
            return None
        candidates = self._safe_or_all(legal)
        return min(
            candidates,
            key=lambda option: (
                words.reply_count(option, context.used_canonical_keys),
                option.rank,
                self._tie_break(option, context),
            ),
        )
