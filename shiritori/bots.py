"""Framework-independent bot strategies for shiritori.

The strategies consume already validated dictionary entries.  They deliberately
do not know about NiceGUI, a database, or a particular game-state class, which
makes them safe to run from a room worker and straightforward to unit test.

All three production difficulties share a validated immutable word index.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import Final, Iterable, Mapping, Protocol, Sequence, runtime_checkable


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

_DAKUON_CHAIN_EQUIVALENTS = {
    "ぢ": "じ",
    "づ": "ず",
    "ゔ": "ぶ",
}

_VU_MORA_CHAIN_EQUIVALENTS = {
    "ゔぁ": "ば",
    "ゔぃ": "び",
    "ゔぇ": "べ",
    "ゔぉ": "ぼ",
}

_VOWEL_BY_KANA = {
    **{kana: "あ" for kana in "あかがさざただなはばぱまゃやらゎわ"},
    **{kana: "い" for kana in "いきぎしじちぢにひびぴみりゐ"},
    **{kana: "う" for kana in "うくぐすずつづぬふぶぷむゅゆるゔ"},
    **{kana: "え" for kana in "えけげせぜてでねへべぺめれゑ"},
    **{kana: "お" for kana in "おこごそぞとどのほぼぽもょよろを"},
}


def canonical_kana(character: str) -> str:
    """Return the canonical kana used only for shiritori connections."""

    if character in _VU_MORA_CHAIN_EQUIVALENTS:
        return _VU_MORA_CHAIN_EQUIVALENTS[character]
    expanded = _SMALL_TO_LARGE_KANA.get(character, character)
    return _DAKUON_CHAIN_EQUIVALENTS.get(expanded, expanded)


def first_kana(reading: str) -> str:
    """Return a reading's effective first kana."""

    if not reading:
        raise ValueError("reading must not be empty")
    for alternate, canonical in _VU_MORA_CHAIN_EQUIVALENTS.items():
        if reading.startswith(alternate):
            return canonical
    return canonical_kana(reading[0])


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
        for alternate, canonical in _VU_MORA_CHAIN_EQUIVALENTS.items():
            if reading.endswith(alternate):
                return canonical
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
        return first_kana(self.reading)

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
        object.__setattr__(
            self,
            "expected_kana",
            canonical_kana(self.expected_kana),
        )


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
        self._all_options: tuple[WordOption, ...] = tuple(
            sorted(
                (
                    option
                    for bucket in frozen.values()
                    for option in bucket
                ),
                key=lambda option: (
                    option.rank,
                    option.reading,
                    option.canonical_key,
                    option.surface,
                ),
            )
        )
        safe_counts: dict[str, int] = {}
        safe_counts_by_key: dict[str, dict[str, int]] = {}
        for kana, bucket in frozen.items():
            for option in bucket:
                if option.ends_with_n:
                    continue
                safe_counts[kana] = safe_counts.get(kana, 0) + 1
                locations = safe_counts_by_key.setdefault(
                    option.canonical_key, {}
                )
                locations[kana] = locations.get(kana, 0) + 1
        self._safe_counts: Mapping[str, int] = MappingProxyType(safe_counts)
        self._safe_counts_by_key: Mapping[
            str, Mapping[str, int]
        ] = MappingProxyType(
            {
                key: MappingProxyType(locations)
                for key, locations in safe_counts_by_key.items()
            }
        )

    def starting_with(self, kana: str) -> tuple[WordOption, ...]:
        return self._by_first.get(canonical_kana(kana), ())

    def all_options(
        self,
        used_canonical_keys: frozenset[str] | set[str] = frozenset(),
        *,
        avoid_n: bool = False,
        excluded_endings: frozenset[str] | set[str] = frozenset(),
        limit: int | None = None,
    ) -> tuple[WordOption, ...]:
        """Return every server-owned option after the common legal filters."""

        if limit is not None and (
            type(limit) is not int or limit < 1
        ):
            raise ValueError("limit must be a positive integer or None")
        if any(
            not isinstance(ending, str) or not ending
            for ending in excluded_endings
        ):
            raise ValueError("excluded endings must be non-empty strings")
        excluded = frozenset(
            canonical_kana(ending) for ending in excluded_endings
        )

        selected: list[WordOption] = []
        for option in self._all_options:
            if option.canonical_key in used_canonical_keys:
                continue
            if avoid_n and option.ends_with_n:
                continue
            if option.last_kana in excluded:
                continue
            selected.append(option)
            if limit is not None and len(selected) >= limit:
                break
        return tuple(selected)

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

    def available_safe_counts(
        self,
        used_canonical_keys: frozenset[str] | set[str] = frozenset(),
    ) -> Mapping[str, int]:
        """Count safe options by first kana after removing used keys."""

        counts = dict(self._safe_counts)
        for key in used_canonical_keys:
            for kana, amount in self._safe_counts_by_key.get(
                key, {}
            ).items():
                counts[kana] = counts.get(kana, 0) - amount
        return MappingProxyType(counts)

    def safe_count_for_key(
        self,
        kana: str,
        canonical_key: str,
    ) -> int:
        """Return safe options removed from one bucket by excluding a key."""

        return self._safe_counts_by_key.get(canonical_key, {}).get(
            canonical_kana(kana),
            0,
        )

    def safe_locations_for_key(
        self,
        canonical_key: str,
    ) -> Mapping[str, int]:
        """Return safe first-kana bucket counts occupied by one key."""

        return self._safe_counts_by_key.get(canonical_key, {})

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


class EasyBot(_SeededStrategy):
    """Choose a deterministic pseudo-random legal word, including risky ones."""

    def choose(
        self,
        context: BotContext,
        words: WordIndex,
    ) -> WordOption | None:
        legal = words.legal_options(
            context.expected_kana,
            context.used_canonical_keys,
        )
        if not legal:
            return None
        return min(
            legal,
            key=lambda option: self._tie_break(option, context),
        )


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


HARD_CANDIDATE_LIMIT: Final = 8


class HardBot(_SeededStrategy):
    """Look two turns ahead and avoid replies that can immediately trap the bot."""

    def choose(self, context: BotContext, words: WordIndex) -> WordOption | None:
        legal = words.legal_options(
            context.expected_kana,
            context.used_canonical_keys,
        )
        return self.choose_from_candidates(context, words, legal)

    def choose_from_candidates(
        self,
        context: BotContext,
        words: WordIndex,
        allowed_candidates: Sequence[WordOption],
    ) -> WordOption | None:
        """Choose only from ``allowed_candidates`` using the full index ahead.

        Oni mode uses this entry point: the current command restricts the
        playable candidates, while opponent-reply evaluation still sees the
        complete server dictionary for the next turn.
        """

        server_legal = frozenset(
            words.legal_options(
                context.expected_kana,
                context.used_canonical_keys,
            )
        )
        legal = tuple(
            sorted(
                (
                    option
                    for option in allowed_candidates
                    if option in server_legal
                ),
                key=lambda option: (
                    option.rank,
                    option.reading,
                    option.canonical_key,
                    option.surface,
                ),
            )
        )
        if not legal:
            return None
        candidates = self._safe_or_all(legal)

        # If every legal word ends with ``ん``, the game is already lost.
        # Keep the fallback deterministic instead of evaluating an irrelevant
        # continuation after the terminal move.
        if all(option.ends_with_n for option in candidates):
            return min(
                candidates,
                key=lambda option: (
                    option.rank,
                    self._tie_break(option, context),
                ),
            )

        available_safe_counts = words.available_safe_counts(
            context.used_canonical_keys
        )
        forced_wins = tuple(
            option
            for option in candidates
            if max(
                available_safe_counts.get(
                    canonical_kana(option.last_kana), 0
                )
                - words.safe_count_for_key(
                    option.last_kana,
                    option.canonical_key,
                ),
                0,
            )
            == 0
        )
        if forced_wins:
            return min(
                forced_wins,
                key=lambda option: (
                    option.rank,
                    self._tie_break(option, context),
                ),
            )

        # Commonness is a safety rail, not merely the last tactical tie-break.
        # Search every option for an immediate win above, then limit the more
        # expensive two-ply comparison to the most natural offline-ranked
        # choices. This avoids obscure tactical suffixes dominating play.
        candidates = tuple(candidates[:HARD_CANDIDATE_LIMIT])
        continuation_cache: dict[
            tuple[
                str,
                tuple[tuple[str, int], ...],
                str,
            ],
            tuple[int, int, int],
        ] = {}

        def continuation_metrics(
            option: WordOption,
        ) -> tuple[int, int, int]:
            unavailable = set(context.used_canonical_keys)
            unavailable.add(option.canonical_key)
            opponent_replies = words.legal_options(
                option.last_kana,
                unavailable,
                avoid_n=True,
            )

            if not opponent_replies:
                return (0, 0, 0)

            def counter_count(reply: WordOption) -> int:
                reply_kana = reply.last_kana
                count = available_safe_counts.get(
                    canonical_kana(reply_kana), 0
                )
                for excluded_key in {
                    option.canonical_key,
                    reply.canonical_key,
                }:
                    count -= words.safe_count_for_key(
                        reply_kana,
                        excluded_key,
                    )
                return max(count, 0)

            worst_counter_count = min(
                counter_count(reply)
                for reply in opponent_replies
            )
            return (
                1,
                -worst_counter_count,
                len(opponent_replies),
            )

        def lookahead_score(option: WordOption) -> tuple[object, ...]:
            locations = tuple(
                words.safe_locations_for_key(
                    option.canonical_key
                ).items()
            )
            removes_specific_reply = (
                option.canonical_key
                if words.safe_count_for_key(
                    option.last_kana,
                    option.canonical_key,
                )
                else ""
            )
            cache_key = (
                canonical_kana(option.last_kana),
                locations,
                removes_specific_reply,
            )
            metrics = continuation_cache.get(cache_key)
            if metrics is None:
                metrics = continuation_metrics(option)
                continuation_cache[cache_key] = metrics
            return (
                *metrics,
                option.rank,
                self._tie_break(option, context),
            )

        return min(
            candidates,
            key=lookahead_score,
        )
