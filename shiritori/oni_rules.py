"""Pure domain rules for the high-difficulty ``oni shiritori`` mode.

This module deliberately has no NiceGUI, database, authentication, or
Sudachi dependency.  A room coordinator can validate a human reading with
``OniConstraintSet.violations`` while the room runtime can pass the same
constraint set to ``filter_options`` for Bot candidates.

The checked-in Bot catalogue is used only by the caller as a conservative
source of known answers.  Human answers may still come from the wider lexicon:
the predicates in this module operate on readings, not catalogue membership.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import unicodedata
from typing import Final, Iterable, Sequence

from .bots import WordOption, final_kana


MINIMUM_FEASIBLE_CANDIDATES: Final = 3
ENDING_SEAL_WINDOW_SIZE: Final = 10

_ATTACHING_SMALL_KANA: Final = frozenset("ゃゅょぁぃぅぇぉゎ")
_READING_EXTRAS: Final = frozenset({"ー", "ゝ", "ゞ"})
_SPECIAL_MORAE: Final = frozenset({"っ", "ん", "ー", "ゝ", "ゞ"})

# These equivalences are intentionally narrower than spelling
# normalization.  They are the connection equivalences selected for this
# project and must not alter the displayed reading or duplicate key.
_MORA_EQUIVALENTS: Final = {
    "ぢ": "じ",
    "づ": "ず",
    "ゔ": "ぶ",
    "ゔぁ": "ば",
    "ゔぃ": "び",
    "ゔぇ": "べ",
    "ゔぉ": "ぼ",
}

_MORA_ALIASES_FOR_DISPLAY: Final = {
    "じ": "じ（ぢも可）",
    "ず": "ず（づも可）",
    "ぶ": "ぶ（ゔも可）",
    "ば": "ば（ゔぁも可）",
    "び": "び（ゔぃも可）",
    "べ": "べ（ゔぇも可）",
    "ぼ": "ぼ（ゔぉも可）",
}

_DAKUON: Final = frozenset(
    "がぎぐげござじずぜぞだぢづでどばびぶべぼゔ"
)
_HANDAKUON: Final = frozenset("ぱぴぷぺぽ")
_YOUON: Final = frozenset("ゃゅょ")
_SMALL_VOWELS: Final = frozenset("ぁぃぅぇぉゎ")


class OniRuleError(ValueError):
    """Base class for invalid rule data."""


class InsufficientCandidates(OniRuleError):
    """Raised when even a safely relaxed challenge has fewer than 3 answers."""


class SoundType(str, Enum):
    """A sound feature which one accepted reading must contain."""

    DAKUON = "dakuon"
    HANDAKUON = "handakuon"
    YOUON = "youon"
    SOKUON = "sokuon"
    LONG_VOWEL = "long_vowel"
    SMALL_VOWEL = "small_vowel"

    @property
    def description(self) -> str:
        return {
            SoundType.DAKUON: "濁音を含む",
            SoundType.HANDAKUON: "半濁音を含む",
            SoundType.YOUON: "小さい「ゃ・ゅ・ょ」を含む",
            SoundType.SOKUON: "小さい「っ」を含む",
            SoundType.LONG_VOWEL: "長音「ー」を含む",
            SoundType.SMALL_VOWEL: "小さい母音を含む",
        }[self]


class ConstraintCode(str, Enum):
    """Machine-readable reasons why a reading failed an Oni command."""

    MORA_COUNT = "mora_count"
    FORBIDDEN_KANA = "forbidden_kana"
    REQUIRED_KANA = "required_kana"
    REQUIRED_ENDING = "required_ending"
    CARRY_OVER_KANA = "carry_over_kana"
    SOUND_TYPE = "sound_type"
    REPEATED_KANA = "repeated_kana"
    SEALED_ENDING = "sealed_ending"


@dataclass(frozen=True, slots=True)
class ConstraintViolation:
    """One failed predicate suitable for server logic and UI feedback."""

    code: ConstraintCode
    message: str


def normalize_reading(reading: str) -> str:
    """Return a compact hiragana reading without importing the lexicon.

    Runtime readings normally arrive already normalized by the dictionary.
    Supporting NFKC, katakana, and surrounding whitespace here makes the
    pure predicates safer to use in tests and at integration boundaries.
    """

    if not isinstance(reading, str):
        raise OniRuleError("reading must be a string")
    value = unicodedata.normalize("NFKC", reading).strip()
    if not value:
        raise OniRuleError("reading must not be empty")

    converted: list[str] = []
    for character in value:
        codepoint = ord(character)
        if 0x30A1 <= codepoint <= 0x30F6:
            character = chr(codepoint - 0x60)
        elif character == "ヽ":
            character = "ゝ"
        elif character == "ヾ":
            character = "ゞ"
        converted.append(character)
    normalized = "".join(converted)
    if any(
        not (
            "\u3041" <= character <= "\u3096"
            or character in _READING_EXTRAS
        )
        for character in normalized
    ):
        raise OniRuleError("reading must contain only Japanese kana")
    return normalized


def mora_tokens(reading: str) -> tuple[str, ...]:
    """Split a reading into Japanese morae.

    Small ``ゃゅょ`` and small vowels attach to their preceding kana.
    ``っ``, ``ん``, and ``ー`` each count as one mora.  For example,
    ``キャット`` becomes ``("きゃ", "っ", "と")``.
    """

    normalized = normalize_reading(reading)
    tokens: list[str] = []
    for character in normalized:
        if (
            character in _ATTACHING_SMALL_KANA
            and tokens
            and tokens[-1] not in _SPECIAL_MORAE
        ):
            tokens[-1] += character
        else:
            tokens.append(character)
    return tuple(tokens)


def mora_count(reading: str) -> int:
    """Return the number of morae in one normalized reading."""

    return len(mora_tokens(reading))


def canonical_mora(mora: str) -> str:
    """Canonicalize only the project-approved equivalent morae."""

    tokens = mora_tokens(mora)
    if len(tokens) != 1:
        raise OniRuleError("a kana constraint must contain exactly one mora")
    return _MORA_EQUIVALENTS.get(tokens[0], tokens[0])


def canonical_mora_tokens(reading: str) -> tuple[str, ...]:
    """Return comparison tokens while preserving the original reading."""

    return tuple(
        _MORA_EQUIVALENTS.get(token, token)
        for token in mora_tokens(reading)
    )


def _canonical_ending(value: str) -> str:
    try:
        return final_kana(normalize_reading(value))
    except ValueError as error:
        raise OniRuleError("ending kana cannot be resolved") from error


def _display_mora(mora: str) -> str:
    return _MORA_ALIASES_FOR_DISPLAY.get(mora, mora)


def _has_sound_type(reading: str, sound_type: SoundType) -> bool:
    normalized = normalize_reading(reading)
    if sound_type is SoundType.DAKUON:
        return any(character in _DAKUON for character in normalized)
    if sound_type is SoundType.HANDAKUON:
        return any(character in _HANDAKUON for character in normalized)
    if sound_type is SoundType.YOUON:
        return any(character in _YOUON for character in normalized)
    if sound_type is SoundType.SOKUON:
        return "っ" in normalized
    if sound_type is SoundType.LONG_VOWEL:
        return "ー" in normalized
    if sound_type is SoundType.SMALL_VOWEL:
        return any(character in _SMALL_VOWELS for character in normalized)
    raise AssertionError(f"unsupported sound type: {sound_type!r}")


@dataclass(frozen=True, slots=True)
class EndingSealWindow:
    """The canonical endings of the most recent 10 successful words.

    Failed inputs, timeouts, and words ending in ``ん`` must not call
    :meth:`record_success`; this keeps the window based only on successful
    moves.  Duplicate endings remain sealed until their newest occurrence
    also leaves the rolling window.
    """

    endings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.endings) > ENDING_SEAL_WINDOW_SIZE:
            raise OniRuleError("ending seal window cannot exceed 10 entries")
        normalized = tuple(
            _canonical_ending(ending) for ending in self.endings
        )
        object.__setattr__(self, "endings", normalized)

    @property
    def sealed_endings(self) -> frozenset[str]:
        return frozenset(self.endings)

    def record_success(self, reading: str) -> EndingSealWindow:
        """Return a new window after one successful, non-terminal word."""

        ending = _canonical_ending(reading)
        if ending == "ん":
            raise OniRuleError("a word ending in ん is not a successful move")
        return EndingSealWindow(
            (*self.endings, ending)[-ENDING_SEAL_WINDOW_SIZE:]
        )

    def drop_oldest(self) -> EndingSealWindow:
        """Return a window without its oldest entry."""

        return EndingSealWindow(self.endings[1:])

    @classmethod
    def from_successful_readings(
        cls, readings: Iterable[str]
    ) -> EndingSealWindow:
        window = cls()
        for reading in readings:
            window = window.record_success(reading)
        return window


@dataclass(frozen=True, slots=True)
class OniConstraintSet:
    """All predicates which may be active for one Oni turn."""

    mora_count_required: int | None = None
    forbidden_kana: str | None = None
    required_kana: str | None = None
    required_ending: str | None = None
    carry_over_kana: str | None = None
    sound_type: SoundType | str | None = None
    no_repeated_kana: bool = False
    sealed_endings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mora_count_required is not None and (
            type(self.mora_count_required) is not int
            or self.mora_count_required < 1
        ):
            raise OniRuleError("mora count must be a positive integer")
        for field_name in (
            "forbidden_kana",
            "required_kana",
            "carry_over_kana",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    canonical_mora(value),
                )
        if self.required_ending is not None:
            object.__setattr__(
                self,
                "required_ending",
                _canonical_ending(self.required_ending),
            )
        if self.sound_type is not None:
            try:
                normalized_sound = SoundType(self.sound_type)
            except (TypeError, ValueError) as error:
                raise OniRuleError("unsupported sound type") from error
            object.__setattr__(self, "sound_type", normalized_sound)
        if type(self.no_repeated_kana) is not bool:
            raise OniRuleError("no_repeated_kana must be boolean")
        if len(self.sealed_endings) > ENDING_SEAL_WINDOW_SIZE:
            raise OniRuleError("sealed endings cannot exceed 10 entries")
        object.__setattr__(
            self,
            "sealed_endings",
            tuple(
                _canonical_ending(ending)
                for ending in self.sealed_endings
            ),
        )

    @property
    def sealed_ending_set(self) -> frozenset[str]:
        return frozenset(self.sealed_endings)

    def violations(self, reading: str) -> tuple[ConstraintViolation, ...]:
        """Return every failed predicate in stable display order."""

        normalized = normalize_reading(reading)
        tokens = canonical_mora_tokens(normalized)
        ending = _canonical_ending(normalized)
        failures: list[ConstraintViolation] = []

        if (
            self.mora_count_required is not None
            and len(tokens) != self.mora_count_required
        ):
            failures.append(
                ConstraintViolation(
                    ConstraintCode.MORA_COUNT,
                    (
                        f"読みを{self.mora_count_required}音にしてください"
                        f"（現在は{len(tokens)}音です）。"
                    ),
                )
            )
        if (
            self.forbidden_kana is not None
            and self.forbidden_kana in tokens
        ):
            failures.append(
                ConstraintViolation(
                    ConstraintCode.FORBIDDEN_KANA,
                    (
                        f"禁止かな「{_display_mora(self.forbidden_kana)}」"
                        "が含まれています。"
                    ),
                )
            )
        if (
            self.required_kana is not None
            and self.required_kana not in tokens
        ):
            failures.append(
                ConstraintViolation(
                    ConstraintCode.REQUIRED_KANA,
                    (
                        f"指定かな「{_display_mora(self.required_kana)}」"
                        "を含めてください。"
                    ),
                )
            )
        if (
            self.required_ending is not None
            and ending != self.required_ending
        ):
            failures.append(
                ConstraintViolation(
                    ConstraintCode.REQUIRED_ENDING,
                    (
                        f"末尾を「{_display_mora(self.required_ending)}」"
                        "にしてください。"
                    ),
                )
            )
        if (
            self.carry_over_kana is not None
            and self.carry_over_kana not in tokens
        ):
            failures.append(
                ConstraintViolation(
                    ConstraintCode.CARRY_OVER_KANA,
                    (
                        "前の単語から"
                        f"「{_display_mora(self.carry_over_kana)}」"
                        "を引き継いでください。"
                    ),
                )
            )
        if (
            isinstance(self.sound_type, SoundType)
            and not _has_sound_type(normalized, self.sound_type)
        ):
            failures.append(
                ConstraintViolation(
                    ConstraintCode.SOUND_TYPE,
                    f"音の条件「{self.sound_type.description}」を満たしません。",
                )
            )
        if self.no_repeated_kana and len(tokens) != len(set(tokens)):
            failures.append(
                ConstraintViolation(
                    ConstraintCode.REPEATED_KANA,
                    "同じかなを2回以上使うことはできません。",
                )
            )
        if ending in self.sealed_ending_set:
            failures.append(
                ConstraintViolation(
                    ConstraintCode.SEALED_ENDING,
                    (
                        f"末尾「{_display_mora(ending)}」は"
                        "直近10手で使われたため封印中です。"
                    ),
                )
            )
        return tuple(failures)

    def accepts(self, reading: str) -> bool:
        """Return whether a reading satisfies every active predicate."""

        return not self.violations(reading)

    def accepts_option(self, option: WordOption) -> bool:
        """Return whether one server-owned Bot option is allowed."""

        if not isinstance(option, WordOption):
            raise TypeError("option must be a WordOption")
        return self.accepts(option.reading)

    def filter_options(
        self, options: Iterable[WordOption]
    ) -> tuple[WordOption, ...]:
        """Filter Bot candidates through the same predicates as humans."""

        return tuple(
            option for option in options if self.accepts_option(option)
        )

    @property
    def descriptions(self) -> tuple[str, ...]:
        """Return concise Japanese command labels for the game UI."""

        labels: list[str] = []
        if self.mora_count_required is not None:
            labels.append(f"読みは{self.mora_count_required}音")
        if self.forbidden_kana is not None:
            labels.append(
                f"「{_display_mora(self.forbidden_kana)}」を含めない"
            )
        if self.required_kana is not None:
            labels.append(
                f"「{_display_mora(self.required_kana)}」を含む"
            )
        if self.required_ending is not None:
            labels.append(
                f"末尾は「{_display_mora(self.required_ending)}」"
            )
        if self.carry_over_kana is not None:
            labels.append(
                "前の単語から"
                f"「{_display_mora(self.carry_over_kana)}」を引き継ぐ"
            )
        if isinstance(self.sound_type, SoundType):
            labels.append(self.sound_type.description)
        if self.no_repeated_kana:
            labels.append("同じかなを2回使わない")
        if self.sealed_endings:
            displayed = "・".join(
                _display_mora(ending)
                for ending in dict.fromkeys(self.sealed_endings)
            )
            labels.append(f"直近10手の末尾を封印中：{displayed}")
        return tuple(labels)


class _ConstraintKind(str, Enum):
    FORBIDDEN = "forbidden"
    REQUIRED = "required"
    REQUIRED_ENDING = "required_ending"
    CARRY_OVER = "carry_over"
    SOUND_TYPE = "sound_type"
    NO_REPEATED = "no_repeated"


_KIND_ROTATION: Final = (
    _ConstraintKind.FORBIDDEN,
    _ConstraintKind.REQUIRED,
    _ConstraintKind.REQUIRED_ENDING,
    _ConstraintKind.CARRY_OVER,
    _ConstraintKind.SOUND_TYPE,
    _ConstraintKind.NO_REPEATED,
)


@dataclass(frozen=True, slots=True)
class _AtomicConstraint:
    kind: _ConstraintKind
    value: str


@dataclass(frozen=True, slots=True)
class GeneratedOniChallenge:
    """One deterministic, feasibility-checked command bundle."""

    constraints: OniConstraintSet
    candidates: tuple[WordOption, ...]
    minimum_candidates: int
    relaxed_seal_count: int = 0

    def __post_init__(self) -> None:
        if len(self.candidates) < self.minimum_candidates:
            raise InsufficientCandidates(
                "generated challenge does not meet its candidate minimum"
            )
        if any(
            option.ends_with_n
            or not self.constraints.accepts_option(option)
            for option in self.candidates
        ):
            raise OniRuleError(
                "generated candidates must be safe and satisfy all constraints"
            )

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)


def _stable_score(
    seed: int | str,
    turn_number: int,
    stage: str,
    *values: object,
) -> bytes:
    payload = "\x1f".join(
        (str(seed), str(turn_number), stage, *(str(value) for value in values))
    )
    return hashlib.sha256(payload.encode("utf-8")).digest()


def _sorted_unique_options(
    options: Iterable[WordOption],
) -> tuple[WordOption, ...]:
    unique: dict[tuple[str, str], WordOption] = {}
    for option in options:
        if not isinstance(option, WordOption):
            raise TypeError("options must contain only WordOption values")
        if option.ends_with_n:
            continue
        identity = (option.canonical_key, option.reading)
        previous = unique.get(identity)
        if previous is None or (
            option.rank,
            option.surface,
        ) < (
            previous.rank,
            previous.surface,
        ):
            unique[identity] = option
    return tuple(
        sorted(
            unique.values(),
            key=lambda option: (
                option.canonical_key,
                option.reading,
                option.rank,
                option.surface,
            ),
        )
    )


def _candidate_tokens(option: WordOption) -> tuple[str, ...]:
    return canonical_mora_tokens(option.reading)


def _atom_options(
    pool: Sequence[WordOption],
    *,
    previous_reading: str | None,
    minimum: int,
) -> tuple[_AtomicConstraint, ...]:
    facts = tuple(
        (
            frozenset(_candidate_tokens(option)),
            _canonical_ending(option.reading),
            frozenset(
                sound_type
                for sound_type in SoundType
                if _has_sound_type(option.reading, sound_type)
            ),
            (
                len(_candidate_tokens(option))
                == len(set(_candidate_tokens(option)))
            ),
        )
        for option in pool
    )
    token_counts = Counter(
        token
        for tokens, _ending, _sounds, _unique in facts
        for token in tokens
        if token not in _SPECIAL_MORAE
    )
    ending_counts = Counter(
        ending for _tokens, ending, _sounds, _unique in facts
    )
    sound_counts = Counter(
        sound_type
        for _tokens, _ending, sounds, _unique in facts
        for sound_type in sounds
    )
    atoms: list[_AtomicConstraint] = []

    def add_if_nontrivial(atom: _AtomicConstraint, accepted: int) -> None:
        rejected = len(pool) - accepted
        if accepted >= minimum and rejected >= minimum:
            atoms.append(atom)

    for token, count in sorted(token_counts.items()):
        add_if_nontrivial(
            _AtomicConstraint(_ConstraintKind.FORBIDDEN, token),
            len(pool) - count,
        )
        add_if_nontrivial(
            _AtomicConstraint(_ConstraintKind.REQUIRED, token),
            count,
        )

    for ending, count in sorted(ending_counts.items()):
        add_if_nontrivial(
            _AtomicConstraint(_ConstraintKind.REQUIRED_ENDING, ending),
            count,
        )

    if previous_reading is not None:
        carry_targets = {
            token
            for token in canonical_mora_tokens(previous_reading)
            if token not in _SPECIAL_MORAE
        }
        for token in sorted(carry_targets):
            add_if_nontrivial(
                _AtomicConstraint(_ConstraintKind.CARRY_OVER, token),
                token_counts.get(token, 0),
            )

    for sound_type in SoundType:
        add_if_nontrivial(
            _AtomicConstraint(_ConstraintKind.SOUND_TYPE, sound_type.value),
            sound_counts.get(sound_type, 0),
        )

    add_if_nontrivial(
        _AtomicConstraint(_ConstraintKind.NO_REPEATED, "true"),
        sum(unique for _tokens, _ending, _sounds, unique in facts),
    )
    return tuple(atoms)


def _with_atom(
    constraints: OniConstraintSet,
    atom: _AtomicConstraint,
) -> OniConstraintSet:
    if atom.kind is _ConstraintKind.FORBIDDEN:
        return replace(constraints, forbidden_kana=atom.value)
    if atom.kind is _ConstraintKind.REQUIRED:
        return replace(constraints, required_kana=atom.value)
    if atom.kind is _ConstraintKind.REQUIRED_ENDING:
        return replace(constraints, required_ending=atom.value)
    if atom.kind is _ConstraintKind.CARRY_OVER:
        return replace(constraints, carry_over_kana=atom.value)
    if atom.kind is _ConstraintKind.SOUND_TYPE:
        return replace(constraints, sound_type=SoundType(atom.value))
    if atom.kind is _ConstraintKind.NO_REPEATED:
        return replace(constraints, no_repeated_kana=True)
    raise AssertionError(f"unsupported constraint kind: {atom.kind!r}")


def _active_kinds(constraints: OniConstraintSet) -> frozenset[_ConstraintKind]:
    active: set[_ConstraintKind] = set()
    if constraints.forbidden_kana is not None:
        active.add(_ConstraintKind.FORBIDDEN)
    if constraints.required_kana is not None:
        active.add(_ConstraintKind.REQUIRED)
    if constraints.required_ending is not None:
        active.add(_ConstraintKind.REQUIRED_ENDING)
    if constraints.carry_over_kana is not None:
        active.add(_ConstraintKind.CARRY_OVER)
    if constraints.sound_type is not None:
        active.add(_ConstraintKind.SOUND_TYPE)
    if constraints.no_repeated_kana:
        active.add(_ConstraintKind.NO_REPEATED)
    return frozenset(active)


def _rotated_kinds(turn_number: int) -> tuple[_ConstraintKind, ...]:
    offset = (turn_number - 1) % len(_KIND_ROTATION)
    return (*_KIND_ROTATION[offset:], *_KIND_ROTATION[:offset])


def _pick_atom(
    atoms: Sequence[_AtomicConstraint],
    *,
    allowed_kinds: Sequence[_ConstraintKind],
    seed: int | str,
    turn_number: int,
    stage: str,
) -> _AtomicConstraint | None:
    for kind in allowed_kinds:
        matching = tuple(atom for atom in atoms if atom.kind is kind)
        if matching:
            return min(
                matching,
                key=lambda atom: _stable_score(
                    seed,
                    turn_number,
                    stage,
                    atom.kind.value,
                    atom.value,
                ),
            )
    return None


def generate_oni_challenge(
    options: Iterable[WordOption],
    *,
    previous_reading: str | None = None,
    seal_window: EndingSealWindow | None = None,
    seed: int | str = 0,
    turn_number: int = 1,
    extra_constraint_count: int = 1,
    include_mora_count: bool = True,
    minimum_candidates: int = MINIMUM_FEASIBLE_CANDIDATES,
) -> GeneratedOniChallenge:
    """Generate a deterministic command with at least 3 known answers.

    ``options`` should already be restricted by the expected starting kana and
    used-word set.  Words ending in ``ん`` are removed here as an additional
    safety rail.

    The optional rule families rotate by turn, so a feasible six-turn span
    naturally exposes forbidden kana, required kana, required ending,
    carry-over, sound type, and no-repetition.  The rolling ending seal is
    always active.  When all known answers are sealed, only the oldest seal
    entries needed to recover the minimum are relaxed and the count is
    reported to the caller for UI disclosure.
    """

    if type(turn_number) is not int or turn_number < 1:
        raise OniRuleError("turn_number must be a positive integer")
    if (
        type(extra_constraint_count) is not int
        or extra_constraint_count < 0
        or extra_constraint_count > len(_KIND_ROTATION)
    ):
        raise OniRuleError(
            "extra_constraint_count must be from 0 to 6"
        )
    if (
        type(minimum_candidates) is not int
        or minimum_candidates < MINIMUM_FEASIBLE_CANDIDATES
    ):
        raise OniRuleError("minimum_candidates must be at least 3")
    if type(include_mora_count) is not bool:
        raise OniRuleError("include_mora_count must be boolean")
    if previous_reading is not None:
        previous_reading = normalize_reading(previous_reading)

    safe_options = _sorted_unique_options(options)
    if len(safe_options) < minimum_candidates:
        raise InsufficientCandidates(
            f"only {len(safe_options)} safe candidates are available"
        )

    effective_window = seal_window or EndingSealWindow()
    relaxed_seal_count = 0
    while True:
        base_constraints = OniConstraintSet(
            sealed_endings=effective_window.endings
        )
        pool = base_constraints.filter_options(safe_options)
        if len(pool) >= minimum_candidates:
            break
        if not effective_window.endings:
            raise InsufficientCandidates(
                "ending seals leave fewer than 3 candidates"
            )
        effective_window = effective_window.drop_oldest()
        relaxed_seal_count += 1

    constraints = base_constraints
    rotated_kinds = _rotated_kinds(turn_number)

    if include_mora_count:
        length_counts = Counter(
            mora_count(option.reading) for option in pool
        )
        lengths = sorted(
            length
            for length, count in length_counts.items()
            if count >= minimum_candidates
        )
        if not lengths:
            raise InsufficientCandidates(
                "no mora length has at least 3 known candidates"
            )

        # Prefer a feasible pair of mora length and the turn's rotating
        # optional family.  This keeps the promised per-turn length limit
        # without frequently crowding out the more distinctive commands.
        paired: list[
            tuple[_ConstraintKind, int, _AtomicConstraint, tuple[WordOption, ...]]
        ] = []
        initial_atoms = _atom_options(
            pool,
            previous_reading=previous_reading,
            minimum=minimum_candidates,
        )
        for kind in rotated_kinds:
            for length in lengths:
                length_constraints = replace(
                    constraints,
                    mora_count_required=length,
                )
                length_pool = length_constraints.filter_options(pool)
                if len(length_pool) < minimum_candidates:
                    continue
                for atom in initial_atoms:
                    if atom.kind is not kind:
                        continue
                    combined = _with_atom(length_constraints, atom)
                    candidates = combined.filter_options(pool)
                    if len(candidates) >= minimum_candidates:
                        paired.append((kind, length, atom, candidates))
            if paired:
                break

        if paired and extra_constraint_count:
            _kind, _length, atom, candidates = min(
                paired,
                key=lambda item: _stable_score(
                    seed,
                    turn_number,
                    "mora-pair",
                    item[0].value,
                    item[1],
                    item[2].value,
                ),
            )
            constraints = _with_atom(
                replace(constraints, mora_count_required=_length),
                atom,
            )
            pool = candidates
        else:
            chosen_length = min(
                lengths,
                key=lambda length: _stable_score(
                    seed, turn_number, "mora", length
                ),
            )
            constraints = replace(
                constraints,
                mora_count_required=chosen_length,
            )
            pool = constraints.filter_options(pool)

    target_extra_count = extra_constraint_count
    while len(_active_kinds(constraints)) < target_extra_count:
        active = _active_kinds(constraints)
        atoms = tuple(
            atom
            for atom in _atom_options(
                pool,
                previous_reading=previous_reading,
                minimum=minimum_candidates,
            )
            if atom.kind not in active
        )
        if not atoms:
            break
        allowed = tuple(kind for kind in rotated_kinds if kind not in active)
        atom = _pick_atom(
            atoms,
            allowed_kinds=allowed,
            seed=seed,
            turn_number=turn_number,
            stage=f"extra-{len(active)}",
        )
        if atom is None:
            break
        proposed = _with_atom(constraints, atom)
        candidates = proposed.filter_options(pool)
        if len(candidates) < minimum_candidates:
            # _atom_options checks the same pool, so this is defensive.
            break
        constraints = proposed
        pool = candidates

    candidates = constraints.filter_options(safe_options)
    if len(candidates) < minimum_candidates:
        raise InsufficientCandidates(
            "constraint generation fell below the candidate minimum"
        )
    return GeneratedOniChallenge(
        constraints=constraints,
        candidates=candidates,
        minimum_candidates=minimum_candidates,
        relaxed_seal_count=relaxed_seal_count,
    )


__all__ = [
    "ConstraintCode",
    "ConstraintViolation",
    "ENDING_SEAL_WINDOW_SIZE",
    "EndingSealWindow",
    "GeneratedOniChallenge",
    "InsufficientCandidates",
    "MINIMUM_FEASIBLE_CANDIDATES",
    "OniConstraintSet",
    "OniRuleError",
    "SoundType",
    "canonical_mora",
    "canonical_mora_tokens",
    "generate_oni_challenge",
    "mora_count",
    "mora_tokens",
    "normalize_reading",
]
