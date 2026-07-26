from __future__ import annotations

import unittest

from shiritori.bots import (
    BotContext,
    BotStrategy,
    EasyBot,
    HARD_CANDIDATE_LIMIT,
    HardBot,
    NormalBot,
    WordIndex,
    WordOption,
    final_kana,
)


def word(
    surface: str,
    reading: str,
    *,
    rank: int,
    canonical_key: str | None = None,
) -> WordOption:
    return WordOption(
        surface=surface,
        reading=reading,
        canonical_key=canonical_key or reading,
        rank=rank,
    )


class WordIndexTests(unittest.TestCase):
    def test_index_filters_used_words_and_resolves_long_vowel(self) -> None:
        coffee = word("コーヒー", "こーひー", rank=1)
        index = WordIndex(
            [
                coffee,
                word("林檎", "りんご", rank=2),
                word("リンゴ", "りんご", rank=3),
            ]
        )

        self.assertEqual(final_kana(coffee.reading), "い")
        self.assertEqual(
            index.legal_options("り", frozenset({"りんご"})),
            (),
        )

    def test_strategy_protocol_is_easy_to_implement(self) -> None:
        class FirstLegal:
            def choose(
                self, context: BotContext, words: WordIndex
            ) -> WordOption | None:
                options = words.legal_options(
                    context.expected_kana,
                    context.used_canonical_keys,
                )
                return options[0] if options else None

        self.assertIsInstance(FirstLegal(), BotStrategy)


class EasyBotTests(unittest.TestCase):
    def test_choice_is_deterministic_and_ignores_rank(self) -> None:
        index = WordIndex(
            [
                word("林檎", "りんご", rank=1),
                word("リス", "りす", rank=999),
                word("リボン", "りぼん", rank=2),
            ]
        )
        context = BotContext("り")

        first = EasyBot(seed=123).choose(context, index)
        second = EasyBot(seed=123).choose(context, index)

        self.assertEqual(first, second)
        self.assertIn(first, index.starting_with("り"))

    def test_returns_none_without_a_legal_option(self) -> None:
        selected = EasyBot().choose(
            BotContext("り", frozenset({"りす"})),
            WordIndex([word("リス", "りす", rank=1)]),
        )
        self.assertIsNone(selected)


class NormalBotTests(unittest.TestCase):
    def test_prefers_best_ranked_safe_word(self) -> None:
        index = WordIndex(
            [
                word("リボン", "りぼん", rank=1),
                word("林檎", "りんご", rank=4),
                word("リス", "りす", rank=2),
            ]
        )

        selected = NormalBot(seed=7).choose(BotContext("り"), index)

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.reading, "りす")

    def test_uses_terminal_word_only_when_no_safe_move_exists(self) -> None:
        terminal = word("リボン", "りぼん", rank=1)
        selected = NormalBot().choose(
            BotContext("り"),
            WordIndex([terminal]),
        )
        self.assertEqual(selected, terminal)

    def test_seeded_tie_break_is_deterministic(self) -> None:
        index = WordIndex(
            [
                word("栗鼠", "りす", rank=1),
                word("料理", "りょうり", rank=1),
            ]
        )
        context = BotContext("り")

        first = NormalBot(seed=123).choose(context, index)
        second = NormalBot(seed=123).choose(context, index)

        self.assertEqual(first, second)


class HardBotTests(unittest.TestCase):
    def test_minimizes_opponent_legal_replies(self) -> None:
        risu = word("リス", "りす", rank=1)
        ringo = word("林檎", "りんご", rank=50)
        index = WordIndex(
            [
                risu,
                ringo,
                word("スイカ", "すいか", rank=1),
                word("相撲", "すもう", rank=2),
                word("ゴリラ", "ごりら", rank=1),
            ]
        )

        selected = HardBot(seed=9).choose(BotContext("り"), index)

        self.assertEqual(selected, ringo)

    def test_prioritizes_forced_win_over_mobile_continuation(self) -> None:
        forced_win = word("朝", "あさ", rank=99)
        mobile = word("赤", "あか", rank=1)
        index = WordIndex(
            [
                forced_win,
                mobile,
                word("柿", "かき", rank=1),
                word("狐", "きつね", rank=1),
            ]
        )

        self.assertEqual(index.reply_count(forced_win, frozenset()), 0)
        self.assertEqual(index.reply_count(mobile, frozenset()), 1)

        selected = HardBot(seed=9).choose(BotContext("あ"), index)

        self.assertEqual(selected, forced_win)

    def test_forced_win_outside_natural_beam_is_still_selected(self) -> None:
        common = [
            word(
                f"common-{position}",
                "あ" + ("い" * position) + "か",
                rank=position,
            )
            for position in range(HARD_CANDIDATE_LIMIT)
        ]
        forced_win = word("forced", "あさ", rank=999)
        reply = word("reply", "かき", rank=1)
        index = WordIndex([*common, forced_win, reply])

        self.assertNotIn(
            forced_win,
            index.legal_options("あ", avoid_n=True)[
                :HARD_CANDIDATE_LIMIT
            ],
        )
        self.assertEqual(index.reply_count(forced_win, frozenset()), 0)

        selected = HardBot(seed=9).choose(BotContext("あ"), index)

        self.assertEqual(selected, forced_win)

    def test_avoids_two_ply_trap_despite_more_immediate_replies(self) -> None:
        trapped = word("赤", "あか", rank=1)
        resilient = word("朝", "あさ", rank=99)
        index = WordIndex(
            [
                trapped,
                resilient,
                word("柿", "かき", rank=1),
                word("酒", "さけ", rank=1),
                word("刺身", "さしみ", rank=2),
                word("煙", "けむり", rank=1),
                word("水", "みず", rank=1),
            ]
        )

        self.assertEqual(index.reply_count(trapped, frozenset()), 1)
        self.assertEqual(index.reply_count(resilient, frozenset()), 2)

        selected = HardBot(seed=9).choose(BotContext("あ"), index)

        self.assertEqual(selected, resilient)

    def test_reply_count_excludes_used_keys_and_candidate_itself(self) -> None:
        risu = word("リス", "りす", rank=1)
        ringo = word("林檎", "りんご", rank=10)
        used_reply = word("ゴリラ", "ごりら", rank=1)
        free_reply = word("ゴマ", "ごま", rank=2)
        index = WordIndex(
            [
                risu,
                ringo,
                used_reply,
                free_reply,
                word("スイカ", "すいか", rank=1),
                word("相撲", "すもう", rank=2),
            ]
        )

        selected = HardBot().choose(
            BotContext("り", frozenset({"ごま"})),
            index,
        )

        self.assertEqual(selected, ringo)
        self.assertEqual(
            index.reply_count(ringo, frozenset({"ごま"})),
            1,
        )

        looping = word("リハビリ", "りはびり", rank=1)
        self.assertEqual(
            WordIndex([looping]).reply_count(looping, frozenset()),
            0,
        )

    def test_preindexed_safe_counts_match_legal_options(self) -> None:
        options = [
            word("林檎", "りんご", rank=1),
            word("リス", "りす", rank=2),
            word("リボン", "りぼん", rank=3),
            word("ゴマ", "ごま", rank=4),
        ]
        index = WordIndex(options)
        used = frozenset({"りす"})
        counts = index.available_safe_counts(used)

        for kana in ("り", "ご"):
            expected = len(
                index.legal_options(kana, used, avoid_n=True)
            )
            self.assertEqual(counts.get(kana, 0), expected)

    def test_returns_none_without_a_legal_option(self) -> None:
        selected = HardBot().choose(
            BotContext("り", frozenset({"りす"})),
            WordIndex([word("リス", "りす", rank=1)]),
        )
        self.assertIsNone(selected)


if __name__ == "__main__":
    unittest.main()
