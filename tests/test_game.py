from __future__ import annotations

import unittest

from shiritori.game import (
    GameState,
    GameStatus,
    TurnCode,
    canonical_kana,
    is_hiragana_only,
    normalize_word,
)


class TextRuleTests(unittest.TestCase):
    def test_normalize_word_trims_and_composes_dakuten(self) -> None:
        self.assertEqual(normalize_word("  か\u3099くせい　"), "がくせい")

    def test_hiragana_only(self) -> None:
        self.assertTrue(is_hiragana_only("しりとり"))
        self.assertFalse(is_hiragana_only(""))
        self.assertFalse(is_hiragana_only("シリトリ"))
        self.assertFalse(is_hiragana_only("しり とり"))
        self.assertFalse(is_hiragana_only("らーめん"))

    def test_small_kana_is_canonicalized(self) -> None:
        self.assertEqual(canonical_kana("ゃ"), "や")
        self.assertEqual(canonical_kana("っ"), "つ")
        self.assertEqual(canonical_kana("り"), "り")


class GameStateTests(unittest.TestCase):
    def test_initial_state(self) -> None:
        game = GameState()
        self.assertEqual(game.current_word, "しりとり")
        self.assertEqual(game.expected_kana, "り")
        self.assertEqual(game.history, ("しりとり",))
        self.assertEqual(game.turn_count, 0)
        self.assertFalse(game.is_over)

    def test_valid_words_update_the_current_word(self) -> None:
        game = GameState()

        first = game.submit("りんご")
        second = game.submit("ごりら")

        self.assertEqual(first.code, TurnCode.ACCEPTED)
        self.assertEqual(second.code, TurnCode.ACCEPTED)
        self.assertEqual(game.current_word, "ごりら")
        self.assertEqual(game.history, ("しりとり", "りんご", "ごりら"))
        self.assertEqual(game.turn_count, 2)

    def test_mismatched_word_does_not_change_history(self) -> None:
        game = GameState()

        result = game.submit("すいか")

        self.assertEqual(result.code, TurnCode.NOT_CHAINED)
        self.assertFalse(result.accepted)
        self.assertEqual(game.history, ("しりとり",))
        self.assertFalse(game.is_over)

    def test_empty_non_hiragana_and_one_character_words_are_rejected(self) -> None:
        game = GameState()

        self.assertEqual(game.submit("   ").code, TurnCode.EMPTY)
        self.assertEqual(game.submit("リンゴ").code, TurnCode.INVALID_CHARACTERS)
        self.assertEqual(game.submit("り2").code, TurnCode.INVALID_CHARACTERS)
        self.assertEqual(game.submit("り").code, TurnCode.TOO_SHORT)
        self.assertEqual(game.history, ("しりとり",))

    def test_small_kana_cannot_start_a_word(self) -> None:
        game = GameState("はんにゃ")

        result = game.submit("ゃくそく")

        self.assertEqual(result.code, TurnCode.SMALL_KANA_START)
        self.assertEqual(game.expected_kana, "や")
        self.assertEqual(game.history, ("はんにゃ",))

    def test_small_final_kana_uses_full_sized_kana_for_next_word(self) -> None:
        game = GameState("はんにゃ")

        result = game.submit("やさい")

        self.assertEqual(result.code, TurnCode.ACCEPTED)
        self.assertEqual(game.expected_kana, "い")

    def test_word_ending_with_n_is_recorded_and_ends_the_game(self) -> None:
        game = GameState()

        result = game.submit("りぼん")

        self.assertEqual(result.code, TurnCode.ENDS_WITH_N)
        self.assertTrue(result.accepted)
        self.assertTrue(result.game_over)
        self.assertEqual(game.status, GameStatus.LOST_BY_N)
        self.assertEqual(game.current_word, "りぼん")
        self.assertEqual(game.history, ("しりとり", "りぼん"))

        blocked = game.submit("んま")
        self.assertEqual(blocked.code, TurnCode.GAME_ALREADY_OVER)

    def test_duplicate_word_ends_the_game_without_adding_it_twice(self) -> None:
        game = GameState()
        game.submit("りす")
        game.submit("すり")

        result = game.submit("りす")

        self.assertEqual(result.code, TurnCode.DUPLICATE)
        self.assertFalse(result.accepted)
        self.assertTrue(result.game_over)
        self.assertEqual(game.status, GameStatus.LOST_BY_DUPLICATE)
        self.assertEqual(game.history, ("しりとり", "りす", "すり"))
        self.assertEqual(game.current_word, "すり")

    def test_reset_works_during_and_after_game(self) -> None:
        game = GameState()
        game.submit("りす")
        game.reset()
        self.assertEqual(game.history, ("しりとり",))
        self.assertFalse(game.is_over)

        game.submit("りぼん")
        self.assertTrue(game.is_over)
        game.reset()

        self.assertEqual(game.status, GameStatus.ACTIVE)
        self.assertEqual(game.history, ("しりとり",))
        self.assertEqual(game.expected_kana, "り")

    def test_invalid_start_word_raises_value_error(self) -> None:
        for start_word in ("", "り", "リンゴ", "みかん", "ゃくそく"):
            with self.subTest(start_word=start_word):
                with self.assertRaises(ValueError):
                    GameState(start_word)


if __name__ == "__main__":
    unittest.main()
