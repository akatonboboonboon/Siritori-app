"""Pure shiritori rules, independent from NiceGUI.

Separating the rules from the UI keeps the important state transitions easy
to read and test.  One ``GameState`` instance represents one browser game.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import unicodedata


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


class GameStatus(str, Enum):
    """Overall game status."""

    ACTIVE = "active"
    LOST_BY_N = "lost_by_n"
    LOST_BY_DUPLICATE = "lost_by_duplicate"


class TurnCode(str, Enum):
    """Machine-readable result of one input attempt."""

    ACCEPTED = "accepted"
    EMPTY = "empty"
    INVALID_CHARACTERS = "invalid_characters"
    TOO_SHORT = "too_short"
    SMALL_KANA_START = "small_kana_start"
    NOT_CHAINED = "not_chained"
    ENDS_WITH_N = "ends_with_n"
    DUPLICATE = "duplicate"
    GAME_ALREADY_OVER = "game_already_over"


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Outcome returned by :meth:`GameState.submit`."""

    code: TurnCode
    message: str
    word: str
    accepted: bool
    game_over: bool


def normalize_word(raw_word: str | None) -> str:
    """Normalize user input while preserving Japanese spelling.

    NFKC turns compatibility characters into their normal representation and
    NFC composes sequences such as ``か`` + combining dakuten into ``が``.
    Only leading and trailing whitespace is removed; whitespace inside a word
    is rejected by :func:`is_hiragana_only`.
    """

    if raw_word is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(raw_word))
    return unicodedata.normalize("NFC", normalized).strip()


def is_hiragana_only(word: str) -> bool:
    """Return whether ``word`` contains only ordinary hiragana letters.

    The accepted Unicode range U+3041..U+3096 includes small hiragana and
    voiced letters such as ``が`` and ``ゔ``.  The long sound mark ``ー`` is
    deliberately excluded so the optional "hiragana only" rule stays clear.
    """

    return bool(word) and all("\u3041" <= character <= "\u3096" for character in word)


def canonical_kana(character: str) -> str:
    """Return the canonical kana used only for shiritori connections."""

    if character in _VU_MORA_CHAIN_EQUIVALENTS:
        return _VU_MORA_CHAIN_EQUIVALENTS[character]
    expanded = _SMALL_TO_LARGE_KANA.get(character, character)
    return _DAKUON_CHAIN_EQUIVALENTS.get(expanded, expanded)


def _first_chain_kana(word: str) -> str:
    for alternate, canonical in _VU_MORA_CHAIN_EQUIVALENTS.items():
        if word.startswith(alternate):
            return canonical
    return canonical_kana(word[0])


def _ending_chain_kana(word: str) -> str:
    for alternate, canonical in _VU_MORA_CHAIN_EQUIVALENTS.items():
        if word.endswith(alternate):
            return canonical
    return canonical_kana(word[-1])


class GameState:
    """State and rules for a single game."""

    def __init__(self, start_word: str = "しりとり") -> None:
        normalized_start = normalize_word(start_word)
        if not is_hiragana_only(normalized_start):
            raise ValueError("start_word must contain hiragana only")
        if len(normalized_start) < 2:
            raise ValueError("start_word must contain at least two characters")
        if normalized_start[0] in _SMALL_TO_LARGE_KANA:
            raise ValueError("start_word cannot begin with a small kana")
        if _ending_chain_kana(normalized_start) == "ん":
            raise ValueError("start_word cannot end with ん")

        self._start_word = normalized_start
        self._history: list[str] = []
        self.status = GameStatus.ACTIVE
        self.reset()

    @property
    def history(self) -> tuple[str, ...]:
        """Accepted words, including the initial word."""

        return tuple(self._history)

    @property
    def current_word(self) -> str:
        """The most recently accepted word."""

        return self._history[-1]

    @property
    def expected_kana(self) -> str:
        """The kana that must begin the next word."""

        return _ending_chain_kana(self.current_word)

    @property
    def turn_count(self) -> int:
        """Number of accepted player turns, excluding the initial word."""

        return len(self._history) - 1

    @property
    def is_over(self) -> bool:
        return self.status is not GameStatus.ACTIVE

    def reset(self) -> None:
        """Restart the game; this works both during and after a game."""

        self._history = [self._start_word]
        self.status = GameStatus.ACTIVE

    def submit(self, raw_word: str | None) -> TurnResult:
        """Validate and, when valid, apply one word to the game."""

        word = normalize_word(raw_word)

        if self.is_over:
            return TurnResult(
                code=TurnCode.GAME_ALREADY_OVER,
                message="ゲームは終了しています。「もう一度」から再開してください。",
                word=word,
                accepted=False,
                game_over=True,
            )

        if not word:
            return TurnResult(
                code=TurnCode.EMPTY,
                message="ことばを入力してください。",
                word=word,
                accepted=False,
                game_over=False,
            )

        if not is_hiragana_only(word):
            return TurnResult(
                code=TurnCode.INVALID_CHARACTERS,
                message="ひらがなだけで入力してください。",
                word=word,
                accepted=False,
                game_over=False,
            )

        if len(word) < 2:
            return TurnResult(
                code=TurnCode.TOO_SHORT,
                message="2文字以上のことばを入力してください。",
                word=word,
                accepted=False,
                game_over=False,
            )

        if word[0] in _SMALL_TO_LARGE_KANA:
            return TurnResult(
                code=TurnCode.SMALL_KANA_START,
                message="小さい「ゃ・ゅ・ょ・っ」などからは始められません。",
                word=word,
                accepted=False,
                game_over=False,
            )

        if _first_chain_kana(word) != self.expected_kana:
            return TurnResult(
                code=TurnCode.NOT_CHAINED,
                message=f"「{self.expected_kana}」から始まることばを入力してください。",
                word=word,
                accepted=False,
                game_over=False,
            )

        if word in self._history:
            self.status = GameStatus.LOST_BY_DUPLICATE
            return TurnResult(
                code=TurnCode.DUPLICATE,
                message=f"「{word}」はすでに使われています。ゲーム終了です。",
                word=word,
                accepted=False,
                game_over=True,
            )

        self._history.append(word)

        if _ending_chain_kana(word) == "ん":
            self.status = GameStatus.LOST_BY_N
            return TurnResult(
                code=TurnCode.ENDS_WITH_N,
                message=f"「{word}」は「ん」で終わるため、ゲーム終了です。",
                word=word,
                accepted=True,
                game_over=True,
            )

        return TurnResult(
            code=TurnCode.ACCEPTED,
            message=f"「{word}」をつなぎました。次は「{self.expected_kana}」です。",
            word=word,
            accepted=True,
            game_over=False,
        )
