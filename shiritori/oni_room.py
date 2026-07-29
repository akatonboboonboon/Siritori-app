"""Room-facing orchestration for the ``oni shiritori`` rule engine.

The pure predicates and generator live in :mod:`shiritori.oni_rules`.  This
module derives their inputs exclusively from an authoritative
``RoomSnapshot`` and the server-owned ``WordIndex``.  Reconnects, presence
updates, and other state-version-only writes therefore cannot reroll a turn's
command.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass
from hashlib import sha256
from typing import Callable, Final

from .bots import WordIndex, WordOption, final_kana
from .oni_rules import (
    EndingSealWindow,
    GeneratedOniChallenge,
    InsufficientCandidates,
    MINIMUM_FEASIBLE_CANDIDATES,
    OniConstraintSet,
    generate_oni_challenge,
    mora_count,
)
from .rooms import RoomRuleSet, RoomSnapshot


EARLY_TURN_LIMIT: Final = 4
EARLY_EXTRA_CONSTRAINTS: Final = 1
LATE_EXTRA_CONSTRAINTS: Final = 2
MAX_GENERATION_OPTIONS: Final = 96
MAX_CHALLENGE_CACHE_SIZE: Final = 128

WordIndexResolver = Callable[[RoomSnapshot], WordIndex]


@dataclass(frozen=True, slots=True)
class OniRoomChallenge:
    """The effective command and known server candidates for one room turn.

    ``degraded`` is true only when the current chain contains fewer than the
    promised three known safe Bot answers.  The room remains playable and the
    runtime remains safe in that rare exhaustion state.
    """

    constraints: OniConstraintSet
    candidates: tuple[WordOption, ...]
    minimum_candidates: int = MINIMUM_FEASIBLE_CANDIDATES
    relaxed_seal_count: int = 0
    degraded: bool = False

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)


class OniRoomRuleService:
    """Build deterministic Oni commands from persisted match state."""

    def __init__(self, word_index_resolver: WordIndexResolver) -> None:
        if not callable(word_index_resolver):
            raise TypeError("word_index_resolver must be callable")
        self._word_index_resolver = word_index_resolver
        self._cache: OrderedDict[
            tuple[object, ...], OniRoomChallenge
        ] = OrderedDict()

    def challenge_for(self, snapshot: RoomSnapshot) -> OniRoomChallenge:
        """Return the effective command for the snapshot's logical turn."""

        if not isinstance(snapshot, RoomSnapshot):
            raise TypeError("snapshot must be a RoomSnapshot")
        if snapshot.rule_set is not RoomRuleSet.ONI:
            raise ValueError("Oni challenges require an Oni room")

        cache_key = self._cache_key(snapshot)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            return cached

        index = self._word_index_resolver(snapshot)
        if not isinstance(index, WordIndex):
            raise TypeError("word_index_resolver must return a WordIndex")

        successes = self._successful_readings(snapshot)
        seal_window = EndingSealWindow.from_successful_readings(successes)
        logical_turn = len(successes) + 1
        extra_count = (
            EARLY_EXTRA_CONSTRAINTS
            if logical_turn <= EARLY_TURN_LIMIT
            else LATE_EXTRA_CONSTRAINTS
        )
        previous_reading = successes[-1] if successes else None
        # A failed turn is a real new command opportunity, while reconnects
        # and presence-only state_version changes are deliberately excluded.
        seed = (
            f"{snapshot.room_id}\x1f{logical_turn}"
            f"\x1f{len(snapshot.life_loss_events)}"
            f"\x1f{snapshot.expected_kana or '*'}"
        )

        effective_window = seal_window
        relaxed_seals = 0
        while True:
            generated = self._generate_for_window(
                snapshot,
                index,
                previous_reading=previous_reading,
                seal_window=effective_window,
                seed=seed,
                turn_number=logical_turn,
                extra_constraint_count=extra_count,
            )
            if generated is not None:
                challenge = self._from_generated(
                    generated,
                    relaxed_seal_count=(
                        relaxed_seals
                        + generated.relaxed_seal_count
                    ),
                )
                break
            if effective_window.endings:
                effective_window = effective_window.drop_oldest()
                relaxed_seals += 1
                continue
            options = self._legal_safe_options(
                snapshot,
                index,
                effective_window,
                limit=MAX_GENERATION_OPTIONS,
            )
            degraded = self._degraded_challenge(
                options, effective_window, seed
            )
            challenge = OniRoomChallenge(
                constraints=degraded.constraints,
                candidates=degraded.candidates,
                minimum_candidates=degraded.minimum_candidates,
                relaxed_seal_count=(
                    relaxed_seals + degraded.relaxed_seal_count
                ),
                degraded=True,
            )
            break
        self._remember(cache_key, challenge)
        return challenge

    def constraints_for(self, snapshot: RoomSnapshot) -> OniConstraintSet:
        """Return the predicate used by the authoritative coordinator."""

        return self.challenge_for(snapshot).constraints

    @staticmethod
    def _from_generated(
        generated: GeneratedOniChallenge,
        *,
        relaxed_seal_count: int | None = None,
        degraded: bool = False,
    ) -> OniRoomChallenge:
        return OniRoomChallenge(
            constraints=generated.constraints,
            candidates=generated.candidates,
            minimum_candidates=generated.minimum_candidates,
            relaxed_seal_count=(
                generated.relaxed_seal_count
                if relaxed_seal_count is None
                else relaxed_seal_count
            ),
            degraded=degraded,
        )

    def _generate_for_window(
        self,
        snapshot: RoomSnapshot,
        index: WordIndex,
        *,
        previous_reading: str | None,
        seal_window: EndingSealWindow,
        seed: str,
        turn_number: int,
        extra_constraint_count: int,
    ) -> GeneratedOniChallenge | None:
        """Try the fast rank sample, then stratify the full legal pool."""

        fast_options = self._legal_safe_options(
            snapshot,
            index,
            seal_window,
            limit=MAX_GENERATION_OPTIONS,
        )
        best = self._try_generate(
            fast_options,
            previous_reading=previous_reading,
            seal_window=seal_window,
            seed=seed,
            turn_number=turn_number,
            extra_constraint_count=extra_constraint_count,
        )
        if (
            best is not None
            and self._extra_constraint_count(best.constraints)
            >= extra_constraint_count
        ):
            return best

        # This slower path runs only when the rank sample cannot make the
        # base command or misses an optional command. Grouping by mora avoids
        # a mixed sample, while using the complete group prevents rare
        # features below the rank cap from being overlooked.
        full_options = self._legal_safe_options(
            snapshot,
            index,
            seal_window,
            limit=None,
        )
        if len(full_options) <= len(fast_options):
            return best

        groups: dict[int, list[WordOption]] = {}
        for option in full_options:
            groups.setdefault(mora_count(option.reading), []).append(option)
        ordered_groups = sorted(
            groups.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )
        for _length, group in ordered_groups:
            if len(group) < MINIMUM_FEASIBLE_CANDIDATES:
                continue
            generated = self._try_generate(
                tuple(group),
                previous_reading=previous_reading,
                seal_window=seal_window,
                seed=seed,
                turn_number=turn_number,
                extra_constraint_count=extra_constraint_count,
            )
            if generated is None:
                continue
            if (
                best is None
                or (
                    self._extra_constraint_count(generated.constraints),
                    generated.candidate_count,
                )
                > (
                    self._extra_constraint_count(best.constraints),
                    best.candidate_count,
                )
            ):
                best = generated
            if (
                self._extra_constraint_count(generated.constraints)
                >= extra_constraint_count
            ):
                return generated
        return best

    @staticmethod
    def _try_generate(
        options: tuple[WordOption, ...],
        *,
        previous_reading: str | None,
        seal_window: EndingSealWindow,
        seed: str,
        turn_number: int,
        extra_constraint_count: int,
    ) -> GeneratedOniChallenge | None:
        try:
            return generate_oni_challenge(
                options,
                previous_reading=previous_reading,
                seal_window=seal_window,
                seed=seed,
                turn_number=turn_number,
                extra_constraint_count=extra_constraint_count,
                include_mora_count=True,
                minimum_candidates=MINIMUM_FEASIBLE_CANDIDATES,
            )
        except InsufficientCandidates:
            return None

    @staticmethod
    def _extra_constraint_count(constraints: OniConstraintSet) -> int:
        return sum(
            (
                constraints.forbidden_kana is not None,
                constraints.required_kana is not None,
                constraints.required_ending is not None,
                constraints.carry_over_kana is not None,
                constraints.sound_type is not None,
                constraints.no_repeated_kana,
            )
        )

    def _legal_safe_options(
        self,
        snapshot: RoomSnapshot,
        index: WordIndex,
        seal_window: EndingSealWindow,
        *,
        limit: int | None,
    ) -> tuple[WordOption, ...]:
        if snapshot.expected_kana is None:
            return index.all_options(
                snapshot.used_canonical_keys,
                avoid_n=True,
                excluded_endings=seal_window.sealed_endings,
                limit=limit,
            )
        legal = index.legal_options(
            snapshot.expected_kana,
            snapshot.used_canonical_keys,
            avoid_n=True,
        )
        unsealed = tuple(
            option
            for option in legal
            if option.last_kana not in seal_window.sealed_endings
        )
        return unsealed if limit is None else unsealed[:limit]

    @staticmethod
    def _cache_key(snapshot: RoomSnapshot) -> tuple[object, ...]:
        return (
            snapshot.room_id,
            snapshot.rule_set.value,
            snapshot.theme_key,
            snapshot.expected_kana,
            snapshot.current_turn,
            tuple(
                (record.reading, record.canonical_key)
                for record in snapshot.history
            ),
            len(snapshot.life_loss_events),
        )

    def _remember(
        self,
        key: tuple[object, ...],
        challenge: OniRoomChallenge,
    ) -> None:
        self._cache[key] = challenge
        self._cache.move_to_end(key)
        while len(self._cache) > MAX_CHALLENGE_CACHE_SIZE:
            self._cache.popitem(last=False)

    @staticmethod
    def _successful_readings(snapshot: RoomSnapshot) -> tuple[str, ...]:
        # An ``ん`` word is appended to history for the result screen but is a
        # life-loss event, not a successful move.  Duplicates/timeouts are not
        # appended at all.
        return tuple(
            record.reading
            for record in snapshot.history
            if final_kana(record.reading) != "ん"
        )

    @staticmethod
    def _degraded_challenge(
        options: tuple[WordOption, ...],
        seal_window: EndingSealWindow,
        seed: str,
    ) -> OniRoomChallenge:
        """Keep exhausted chains safe without pretending three answers exist."""

        if not options:
            return OniRoomChallenge(
                constraints=OniConstraintSet(),
                candidates=(),
                degraded=True,
            )

        effective_window = seal_window
        relaxed = 0
        pool: tuple[WordOption, ...] = ()
        while True:
            seal_constraints = OniConstraintSet(
                sealed_endings=effective_window.endings
            )
            pool = seal_constraints.filter_options(options)
            if pool or not effective_window.endings:
                break
            effective_window = effective_window.drop_oldest()
            relaxed += 1

        # Even in degraded mode retain the defining per-turn mora command.
        # Pick the largest known group, then use a stable seed-derived tie
        # break so process restarts produce the same command.
        counts = Counter(mora_count(option.reading) for option in pool)
        best_count = max(counts.values())
        lengths = tuple(
            sorted(length for length, count in counts.items() if count == best_count)
        )
        digest_index = int.from_bytes(
            sha256(seed.encode("utf-8")).digest()[:8],
            "big",
        ) % len(lengths)
        selected_length = lengths[digest_index]
        constraints = OniConstraintSet(
            mora_count_required=selected_length,
            sealed_endings=effective_window.endings,
        )
        candidates = constraints.filter_options(options)
        return OniRoomChallenge(
            constraints=constraints,
            candidates=candidates,
            relaxed_seal_count=relaxed,
            degraded=True,
        )


__all__ = [
    "EARLY_EXTRA_CONSTRAINTS",
    "EARLY_TURN_LIMIT",
    "LATE_EXTRA_CONSTRAINTS",
    "MAX_CHALLENGE_CACHE_SIZE",
    "MAX_GENERATION_OPTIONS",
    "OniRoomChallenge",
    "OniRoomRuleService",
    "WordIndexResolver",
]
