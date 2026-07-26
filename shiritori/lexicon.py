"""Sudachi辞書を使った、しりとり用の実在語判定。

このモジュールはゲーム状態やNiceGUIから独立させている。入力表記に一致する
辞書項目だけを採用し、形態素解析で複数語へ分割できるだけの文字列は実在語と
みなさない。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
import unicodedata

from sudachipy import Dictionary


MAX_SURFACE_LENGTH = 30

_ALLOWED_MARKS = frozenset({"々", "〆", "ー", "・"})
_INVALID_EDGE_MARKS = frozenset(
    {"々", "〆", "ー", "・", "ゝ", "ゞ", "ヽ", "ヾ"}
)
_ALLOWED_NOUN_CATEGORIES = frozenset({"普通名詞", "固有名詞"})


class LexiconCode(str, Enum):
    """辞書判定結果を表す機械可読コード。"""

    ACCEPTED = "accepted"
    MULTIPLE_READINGS = "multiple_readings"
    EMPTY = "empty"
    TOO_LONG = "too_long"
    INTERNAL_WHITESPACE = "internal_whitespace"
    INVALID_CHARACTERS = "invalid_characters"
    SINGLE_HIRAGANA = "single_hiragana"
    SINGLE_KATAKANA = "single_katakana"
    NOT_IN_DICTIONARY = "not_in_dictionary"
    UNSUPPORTED_PART_OF_SPEECH = "unsupported_part_of_speech"
    NO_USABLE_READING = "no_usable_reading"


_MESSAGES = {
    LexiconCode.ACCEPTED: "辞書に登録されている単語です。",
    LexiconCode.MULTIPLE_READINGS: (
        "読みが複数あります。しりとりで使う読みを選んでください。"
    ),
    LexiconCode.EMPTY: "単語を入力してください。",
    LexiconCode.TOO_LONG: f"単語は{MAX_SURFACE_LENGTH}文字以内で入力してください。",
    LexiconCode.INTERNAL_WHITESPACE: "単語の途中に空白を入れることはできません。",
    LexiconCode.INVALID_CHARACTERS: (
        "ひらがな・カタカナ・漢字で入力してください。"
    ),
    LexiconCode.SINGLE_HIRAGANA: "ひらがな1文字だけの単語は使用できません。",
    LexiconCode.SINGLE_KATAKANA: "カタカナ1文字だけの単語は使用できません。",
    LexiconCode.NOT_IN_DICTIONARY: "辞書に登録されていない単語です。",
    LexiconCode.UNSUPPORTED_PART_OF_SPEECH: (
        "しりとりで使用できる名詞ではありません。"
    ),
    LexiconCode.NO_USABLE_READING: "この単語の読みを取得できませんでした。",
}


@dataclass(frozen=True, slots=True)
class LexiconCandidate:
    """しりとりに利用できる1件の辞書項目。"""

    surface: str
    reading: str
    lemma: str
    normalized_form: str
    part_of_speech: tuple[str, str, str, str, str, str]
    dictionary_id: int
    word_id: int
    canonical_key: str


@dataclass(frozen=True, slots=True)
class LexiconResult:
    """ユーザーが入力した表記を検証した結果。"""

    code: LexiconCode
    surface: str
    message: str
    candidates: tuple[LexiconCandidate, ...] = ()

    @property
    def accepted(self) -> bool:
        """追加の選択なしで確定できる場合に真を返す。"""

        return self.code is LexiconCode.ACCEPTED

    @property
    def is_dictionary_word(self) -> bool:
        """許可された辞書項目が1件以上見つかった場合に真を返す。"""

        return self.code in {
            LexiconCode.ACCEPTED,
            LexiconCode.MULTIPLE_READINGS,
        }

    @property
    def requires_reading_choice(self) -> bool:
        return self.code is LexiconCode.MULTIPLE_READINGS

    @property
    def readings(self) -> tuple[str, ...]:
        """重複を除いたひらがなの読みを辞書順で返す。"""

        return tuple(
            dict.fromkeys(candidate.reading for candidate in self.candidates)
        )

    def candidates_for_reading(
        self, reading: str
    ) -> tuple[LexiconCandidate, ...]:
        """画面で選ばれた読みに対応する候補だけを返す。"""

        normalized_reading = katakana_to_hiragana(
            normalize_surface(reading)
        )
        return tuple(
            candidate
            for candidate in self.candidates
            if candidate.reading == normalized_reading
        )


def normalize_surface(raw_surface: str | None) -> str:
    """互換文字を正規化し、結合文字を合成して前後の空白を除く。"""

    if raw_surface is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(raw_surface))
    return unicodedata.normalize("NFC", normalized).strip()


def _is_hiragana(character: str) -> bool:
    return "\u3041" <= character <= "\u3096" or character in {"ゝ", "ゞ"}


def _is_katakana(character: str) -> bool:
    return (
        "\u30a1" <= character <= "\u30fa"
        or character in {"ヽ", "ヾ"}
        or "\u31f0" <= character <= "\u31ff"
    )


def _is_han(character: str) -> bool:
    name = unicodedata.name(character, "")
    return name.startswith("CJK UNIFIED IDEOGRAPH") or name.startswith(
        "CJK COMPATIBILITY IDEOGRAPH"
    )


def _is_allowed_surface_character(character: str) -> bool:
    return (
        _is_hiragana(character)
        or _is_katakana(character)
        or _is_han(character)
        or character in _ALLOWED_MARKS
    )


def _has_valid_surface_characters(surface: str) -> bool:
    if not all(_is_allowed_surface_character(char) for char in surface):
        return False
    if surface[0] in _INVALID_EDGE_MARKS:
        return False
    if surface[-1] == "・":
        return False
    return True


def katakana_to_hiragana(text: str) -> str:
    """一般的な全角カタカナを、追加依存なしでひらがなへ変換する。"""

    converted: list[str] = []
    for character in text:
        codepoint = ord(character)
        if 0x30A1 <= codepoint <= 0x30F6:
            converted.append(chr(codepoint - 0x60))
        elif character == "ヽ":
            converted.append("ゝ")
        elif character == "ヾ":
            converted.append("ゞ")
        else:
            converted.append(character)
    return unicodedata.normalize("NFC", "".join(converted))


def _is_usable_reading(reading: str) -> bool:
    if not reading or reading[0] in _INVALID_EDGE_MARKS:
        return False
    return all(
        _is_hiragana(character) or character == "ー"
        for character in reading
    )


def _is_allowed_noun(
    part_of_speech: tuple[str, str, str, str, str, str],
) -> bool:
    return (
        len(part_of_speech) >= 2
        and part_of_speech[0] == "名詞"
        and part_of_speech[1] in _ALLOWED_NOUN_CATEGORIES
    )


def _result(
    code: LexiconCode,
    surface: str,
    candidates: tuple[LexiconCandidate, ...] = (),
) -> LexiconResult:
    return LexiconResult(
        code=code,
        surface=surface,
        message=_MESSAGES[code],
        candidates=candidates,
    )


class LexiconValidator:
    """入力表記をSudachi full辞書の完全一致項目で検証する。"""

    def __init__(self, dictionary: Dictionary | None = None) -> None:
        self._dictionary = (
            dictionary if dictionary is not None else Dictionary(dict="full")
        )

    def validate(self, raw_surface: str | None) -> LexiconResult:
        surface = normalize_surface(raw_surface)

        if not surface:
            return _result(LexiconCode.EMPTY, surface)
        if len(surface) > MAX_SURFACE_LENGTH:
            return _result(LexiconCode.TOO_LONG, surface)
        if any(character.isspace() for character in surface):
            return _result(LexiconCode.INTERNAL_WHITESPACE, surface)
        if not _has_valid_surface_characters(surface):
            return _result(LexiconCode.INVALID_CHARACTERS, surface)
        if len(surface) == 1 and _is_hiragana(surface):
            return _result(LexiconCode.SINGLE_HIRAGANA, surface)
        if len(surface) == 1 and _is_katakana(surface):
            return _result(LexiconCode.SINGLE_KATAKANA, surface)

        entries = tuple(self._dictionary.lookup(surface))
        if not entries:
            return _result(LexiconCode.NOT_IN_DICTIONARY, surface)

        allowed_entries = []
        for entry in entries:
            part_of_speech = tuple(entry.part_of_speech())
            if _is_allowed_noun(part_of_speech):
                allowed_entries.append((entry, part_of_speech))

        if not allowed_entries:
            return _result(
                LexiconCode.UNSUPPORTED_PART_OF_SPEECH,
                surface,
            )

        candidates: list[LexiconCandidate] = []
        for entry, part_of_speech in allowed_entries:
            reading = katakana_to_hiragana(entry.reading_form())
            if not _is_usable_reading(reading):
                continue

            normalized_form = (
                entry.normalized_form()
                or entry.dictionary_form()
                or surface
            )
            lemma = entry.dictionary_form() or normalized_form
            candidates.append(
                LexiconCandidate(
                    surface=surface,
                    reading=reading,
                    lemma=lemma,
                    normalized_form=normalized_form,
                    part_of_speech=part_of_speech,
                    dictionary_id=entry.dictionary_id(),
                    word_id=entry.word_id(),
                    # Spoken shiritori treats kana/kanji variants and
                    # homophones as the same used word.
                    canonical_key=reading,
                )
            )

        if not candidates:
            return _result(LexiconCode.NO_USABLE_READING, surface)

        candidates.sort(
            key=lambda candidate: (
                candidate.part_of_speech[1] != "普通名詞",
                candidate.reading,
                candidate.word_id,
            )
        )
        frozen_candidates = tuple(candidates)
        unique_readings = {
            candidate.reading for candidate in frozen_candidates
        }
        code = (
            LexiconCode.ACCEPTED
            if len(unique_readings) == 1
            else LexiconCode.MULTIPLE_READINGS
        )
        return _result(code, surface, frozen_candidates)


@lru_cache(maxsize=1)
def get_default_validator() -> LexiconValidator:
    """アプリ内で共有する読み取り専用の辞書判定器を返す。"""

    return LexiconValidator()


def validate_word(raw_surface: str | None) -> LexiconResult:
    """共有辞書を使う簡潔な呼び出し口。"""

    return get_default_validator().validate(raw_surface)