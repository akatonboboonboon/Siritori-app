from __future__ import annotations

import unittest

from shiritori.bots import WordOption
from shiritori.oni_rules import (
    ConstraintCode,
    EndingSealWindow,
    InsufficientCandidates,
    OniConstraintSet,
    OniRuleError,
    SoundType,
    canonical_mora_tokens,
    generate_oni_challenge,
    mora_count,
    mora_tokens,
)


def word(reading: str, *, rank: int = 1) -> WordOption:
    return WordOption(
        surface=reading,
        reading=reading,
        canonical_key=reading,
        rank=rank,
    )


def rich_option_pool() -> tuple[WordOption, ...]:
    readings = (
        "あかき",
        "あかく",
        "あかけ",
        "あさき",
        "あさく",
        "あさけ",
        "あたき",
        "あたく",
        "あたけ",
        "あがき",
        "あがく",
        "あがけ",
        "ああき",
        "ああく",
        "ああけ",
        "あーき",
        "あーく",
        "あーけ",
    )
    return tuple(word(reading, rank=index) for index, reading in enumerate(readings))


class MoraTests(unittest.TestCase):
    def test_tokenizer_normalizes_katakana_and_counts_small_kana(self) -> None:
        self.assertEqual(mora_tokens("キャット"), ("きゃ", "っ", "と"))
        self.assertEqual(mora_count("キャット"), 3)
        self.assertEqual(mora_count("コーヒー"), 4)
        self.assertEqual(mora_tokens("ティッシュ"), ("てぃ", "っ", "しゅ"))

    def test_project_equivalences_are_comparison_only(self) -> None:
        self.assertEqual(
            canonical_mora_tokens("ヴァぢづヴ"),
            ("ば", "じ", "ず", "ぶ"),
        )
        self.assertEqual(mora_tokens("ヴァぢづヴ"), ("ゔぁ", "ぢ", "づ", "ゔ"))

    def test_non_kana_reading_is_rejected(self) -> None:
        with self.assertRaises(OniRuleError):
            mora_tokens("abc")


class PredicateTests(unittest.TestCase):
    def test_forbidden_and_required_kana_share_aliases(self) -> None:
        forbidden = OniConstraintSet(forbidden_kana="づ")
        self.assertFalse(forbidden.accepts("すずめ"))
        self.assertEqual(
            forbidden.violations("すずめ")[0].code,
            ConstraintCode.FORBIDDEN_KANA,
        )

        required = OniConstraintSet(required_kana="ヴァ")
        self.assertTrue(required.accepts("ゔぁいおりん"))
        self.assertTrue(required.accepts("ばなな"))
        self.assertFalse(required.accepts("ぶどう"))

    def test_required_ending_uses_connection_canonicalization(self) -> None:
        constraints = OniConstraintSet(required_ending="ヴ")
        self.assertTrue(constraints.accepts("らゔ"))
        self.assertTrue(constraints.accepts("こぶ"))
        self.assertFalse(constraints.accepts("こま"))

    def test_carry_over_sound_types_and_descriptions(self) -> None:
        carry = OniConstraintSet(carry_over_kana="ぢ")
        self.assertTrue(carry.accepts("はなじ"))
        self.assertFalse(carry.accepts("はなび"))

        examples = {
            SoundType.DAKUON: "かぎ",
            SoundType.HANDAKUON: "かっぱ",
            SoundType.YOUON: "きゃく",
            SoundType.SOKUON: "きって",
            SoundType.LONG_VOWEL: "こーひー",
            SoundType.SMALL_VOWEL: "てぃー",
        }
        for sound_type, reading in examples.items():
            with self.subTest(sound_type=sound_type):
                constraints = OniConstraintSet(sound_type=sound_type)
                self.assertTrue(constraints.accepts(reading))
                self.assertTrue(constraints.descriptions)

    def test_no_repeated_kana_uses_canonical_morae(self) -> None:
        constraints = OniConstraintSet(no_repeated_kana=True)
        self.assertFalse(constraints.accepts("こころ"))
        self.assertFalse(constraints.accepts("ばゔぁ"))
        self.assertFalse(constraints.accepts("じぢ"))
        self.assertTrue(constraints.accepts("きゃく"))

    def test_all_failures_are_returned_in_stable_order(self) -> None:
        constraints = OniConstraintSet(
            mora_count_required=4,
            forbidden_kana="か",
            required_kana="さ",
            required_ending="き",
            carry_over_kana="た",
            sound_type=SoundType.LONG_VOWEL,
            no_repeated_kana=True,
            sealed_endings=("く",),
        )
        self.assertEqual(
            tuple(failure.code for failure in constraints.violations("かかく")),
            (
                ConstraintCode.MORA_COUNT,
                ConstraintCode.FORBIDDEN_KANA,
                ConstraintCode.REQUIRED_KANA,
                ConstraintCode.REQUIRED_ENDING,
                ConstraintCode.CARRY_OVER_KANA,
                ConstraintCode.SOUND_TYPE,
                ConstraintCode.REPEATED_KANA,
                ConstraintCode.SEALED_ENDING,
            ),
        )

    def test_filter_options_is_shared_server_predicate(self) -> None:
        constraints = OniConstraintSet(
            mora_count_required=3,
            required_kana="か",
        )
        options = (word("あかき"), word("あさき"), word("あかか"))
        self.assertEqual(
            constraints.filter_options(options),
            (options[0], options[2]),
        )


class EndingSealTests(unittest.TestCase):
    def test_only_the_latest_ten_successes_are_retained(self) -> None:
        readings = (
            "あか",
            "あき",
            "あく",
            "あけ",
            "あこ",
            "あさ",
            "あし",
            "あす",
            "あせ",
            "あそ",
            "あた",
        )
        window = EndingSealWindow.from_successful_readings(readings)

        self.assertEqual(len(window.endings), 10)
        self.assertNotIn("か", window.sealed_endings)
        self.assertIn("き", window.sealed_endings)
        self.assertIn("た", window.sealed_endings)

    def test_endings_use_dakuon_and_vu_equivalence(self) -> None:
        window = EndingSealWindow().record_success("あづ").record_success("らゔ")
        self.assertEqual(window.endings, ("ず", "ぶ"))
        constraints = OniConstraintSet(sealed_endings=window.endings)
        self.assertFalse(constraints.accepts("みず"))
        self.assertFalse(constraints.accepts("こぶ"))

    def test_n_is_not_a_successful_seal_entry(self) -> None:
        with self.assertRaises(OniRuleError):
            EndingSealWindow().record_success("みかん")


class GeneratorTests(unittest.TestCase):
    def test_generation_is_deterministic_and_order_independent(self) -> None:
        options = rich_option_pool()
        window = EndingSealWindow(("き",))
        first = generate_oni_challenge(
            options,
            previous_reading="かさ",
            seal_window=window,
            seed="match-1",
            turn_number=2,
        )
        second = generate_oni_challenge(
            reversed(options),
            previous_reading="かさ",
            seal_window=window,
            seed="match-1",
            turn_number=2,
        )

        self.assertEqual(first.constraints, second.constraints)
        self.assertEqual(first.candidates, second.candidates)
        self.assertGreaterEqual(first.candidate_count, 3)
        self.assertIsNotNone(first.constraints.mora_count_required)
        self.assertTrue(first.constraints.descriptions)
        self.assertTrue(
            all(
                first.constraints.accepts_option(option)
                for option in first.candidates
            )
        )

    def test_optional_families_rotate_when_each_is_feasible(self) -> None:
        options = rich_option_pool()
        expected_fields = (
            "forbidden_kana",
            "required_kana",
            "required_ending",
            "carry_over_kana",
            "sound_type",
            "no_repeated_kana",
        )
        for turn_number, field_name in enumerate(expected_fields, start=1):
            with self.subTest(turn=turn_number, field=field_name):
                challenge = generate_oni_challenge(
                    options,
                    previous_reading="かさ",
                    seed="rotation",
                    turn_number=turn_number,
                )
                self.assertTrue(
                    getattr(challenge.constraints, field_name),
                    challenge.constraints,
                )
                self.assertGreaterEqual(challenge.candidate_count, 3)

    def test_oldest_seals_are_relaxed_only_as_needed(self) -> None:
        options = (
            word("あかき"),
            word("あさき"),
            word("あたき"),
            word("あがき"),
            word("あなき"),
            word("あはき"),
        )
        challenge = generate_oni_challenge(
            options,
            seal_window=EndingSealWindow(("き",)),
            seed=1,
            turn_number=1,
            extra_constraint_count=0,
        )
        self.assertEqual(challenge.relaxed_seal_count, 1)
        self.assertEqual(challenge.constraints.sealed_endings, ())
        self.assertGreaterEqual(challenge.candidate_count, 3)

    def test_fewer_than_three_safe_options_is_rejected(self) -> None:
        with self.assertRaises(InsufficientCandidates):
            generate_oni_challenge((word("あか"), word("あき")))

        with self.assertRaises(OniRuleError):
            generate_oni_challenge(
                rich_option_pool(),
                minimum_candidates=2,
            )

    def test_terminal_n_options_do_not_count_toward_feasibility(self) -> None:
        with self.assertRaises(InsufficientCandidates):
            generate_oni_challenge(
                (
                    word("あか"),
                    word("あき"),
                    word("あん"),
                    word("あかん"),
                )
            )


if __name__ == "__main__":
    unittest.main()
