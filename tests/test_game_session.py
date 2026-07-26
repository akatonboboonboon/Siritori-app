"""Tests for the dictionary-backed game session."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest

from shiritori.game_session import (
    GameSession,
    HistoryResult,
    SessionCode,
    SessionStatus,
    canonical_kana,
    ending_chain_kana,
    first_chain_kana,
)
from shiritori.lexicon import (
    LexiconCandidate,
    LexiconCode,
    LexiconResult,
    normalize_surface,
)


UTC = timezone.utc
START = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
NOUN_POS = ("名詞", "普通名詞", "一般", "*", "*", "*")


class ManualClock:
    def __init__(self, current: datetime = START) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def candidate(
    surface: str,
    reading: str,
    *,
    word_id: int = 1,
    canonical_key: str | None = None,
) -> LexiconCandidate:
    return LexiconCandidate(
        surface=surface,
        reading=reading,
        lemma=surface,
        normalized_form=surface,
        part_of_speech=NOUN_POS,
        dictionary_id=0,
        word_id=word_id,
        canonical_key=canonical_key or reading,
    )


def accepted(
    surface: str,
    reading: str,
    *,
    word_id: int = 1,
    canonical_key: str | None = None,
) -> LexiconResult:
    item = candidate(
        surface,
        reading,
        word_id=word_id,
        canonical_key=canonical_key,
    )
    return LexiconResult(
        code=LexiconCode.ACCEPTED,
        surface=surface,
        message="accepted",
        candidates=(item,),
    )


def ambiguous(
    surface: str, *readings: str
) -> LexiconResult:
    return LexiconResult(
        code=LexiconCode.MULTIPLE_READINGS,
        surface=surface,
        message="choose",
        candidates=tuple(
            candidate(surface, reading, word_id=index)
            for index, reading in enumerate(readings, start=1)
        ),
    )


class FakeLexicon:
    def __init__(self, results: dict[str, LexiconResult]) -> None:
        self.results = results

    def validate(self, raw_surface: str | None) -> LexiconResult:
        surface = normalize_surface(raw_surface)
        return self.results.get(
            surface,
            LexiconResult(
                code=LexiconCode.NOT_IN_DICTIONARY,
                surface=surface,
                message="not found",
            ),
        )


WORDS = {
    "林檎": accepted("林檎", "りんご"),
    "りんご": accepted("りんご", "りんご", word_id=2),
    "語尾": accepted("語尾", "ごり", word_id=3),
    "ゴリラ": accepted("ゴリラ", "ごりら", word_id=4),
    "ラッパ": accepted("ラッパ", "らっぱ", word_id=5),
    "パン": accepted("パン", "ぱん", word_id=6),
    "列車": accepted("列車", "れっしゃ", word_id=7),
    "野菜": accepted("野菜", "やさい", word_id=8),
    "コーヒー": accepted("コーヒー", "こーひー", word_id=9),
    "椅子": accepted("椅子", "いす", word_id=10),
    "蟹": accepted("蟹", "かに", word_id=11),
    "日本": ambiguous("日本", "にほん", "にっぽん"),
    "橋": accepted("橋", "はし", word_id=14),
    "中継": accepted("中継", "しは", word_id=15),
    "箸": accepted("箸", "はし", word_id=16),
}


class GameSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.lexicon = FakeLexicon(WORDS)
        self.game = GameSession(
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )

    def test_starts_empty_and_accepts_any_valid_first_word(self) -> None:
        self.assertEqual(self.game.history, ())
        self.assertIsNone(self.game.current_entry)
        self.assertIsNone(self.game.expected_kana)
        self.assertEqual(self.game.turn_count, 0)

        result = self.game.submit("林檎")

        self.assertEqual(result.code, SessionCode.ACCEPTED)
        self.assertTrue(result.accepted)
        self.assertFalse(result.game_over)
        self.assertEqual(self.game.expected_kana, "ご")
        entry = self.game.history[0]
        self.assertEqual(entry.surface, "林檎")
        self.assertEqual(entry.reading, "りんご")
        self.assertEqual(entry.canonical_key, "りんご")
        self.assertEqual(entry.turn_number, 1)
        self.assertEqual(entry.timestamp, START)
        self.assertEqual(entry.result, HistoryResult.ACCEPTED)
        self.assertEqual(self.game.used_canonical_keys, frozenset({"りんご"}))

    def test_rejects_lexicon_failure_without_advancing(self) -> None:
        result = self.game.submit("存在しない")

        self.assertEqual(result.code, SessionCode.LEXICON_REJECTED)
        self.assertEqual(
            result.lexicon_result.code,
            LexiconCode.NOT_IN_DICTIONARY,
        )
        self.assertEqual(self.game.history, ())

    def test_chains_using_hiragana_readings_not_surfaces(self) -> None:
        self.game.submit("林檎")
        result = self.game.submit("ゴリラ")

        self.assertEqual(result.code, SessionCode.ACCEPTED)
        self.assertEqual(result.reading, "ごりら")
        self.assertEqual(self.game.expected_kana, "ら")

    def test_wrong_chain_does_not_advance(self) -> None:
        self.game.submit("林檎")

        result = self.game.submit("パン")

        self.assertEqual(result.code, SessionCode.NOT_CHAINED)
        self.assertFalse(result.accepted)
        self.assertEqual(self.game.turn_count, 1)
        self.assertEqual(self.game.expected_kana, "ご")

    def test_ambiguous_word_waits_for_an_explicit_choice(self) -> None:
        self.game.submit("蟹")

        result = self.game.submit("日本")

        self.assertEqual(
            result.code, SessionCode.READING_CHOICE_REQUIRED
        )
        self.assertEqual(result.reading_choices, ("にほん", "にっぽん"))
        self.assertEqual(self.game.turn_count, 1)
        self.assertIsNotNone(self.game.pending_reading)

        resolved = self.game.resolve_reading("ニッポン")

        self.assertEqual(resolved.code, SessionCode.ENDS_WITH_N)
        self.assertEqual(resolved.reading, "にっぽん")
        self.assertEqual(self.game.turn_count, 2)
        self.assertIsNone(self.game.pending_reading)

    def test_invalid_reading_choice_keeps_pending_state(self) -> None:
        self.game.submit("日本")

        result = self.game.resolve_reading("にちほん")

        self.assertEqual(
            result.code, SessionCode.INVALID_READING_CHOICE
        )
        self.assertEqual(self.game.turn_count, 0)
        self.assertIsNotNone(self.game.pending_reading)

    def test_reading_choice_that_does_not_chain_stays_pending(self) -> None:
        self.game.submit("林檎")
        self.game.submit("日本")

        result = self.game.resolve_reading("にほん")

        self.assertEqual(result.code, SessionCode.NOT_CHAINED)
        self.assertEqual(self.game.turn_count, 1)
        self.assertIsNotNone(self.game.pending_reading)

    def test_cancelled_reading_choice_does_not_advance(self) -> None:
        self.game.submit("日本")

        result = self.game.cancel_reading_choice()

        self.assertEqual(
            result.code, SessionCode.READING_CHOICE_CANCELLED
        )
        self.assertEqual(self.game.turn_count, 0)
        self.assertIsNone(self.game.pending_reading)

    def test_resolve_without_pending_choice_is_rejected(self) -> None:
        result = self.game.resolve_reading("にほん")
        self.assertEqual(
            result.code, SessionCode.NO_READING_CHOICE_PENDING
        )

    def test_kana_and_kanji_variants_are_duplicate_by_reading(self) -> None:
        self.game.submit("林檎")
        self.game.submit("語尾")

        result = self.game.submit("りんご")

        self.assertEqual(result.code, SessionCode.DUPLICATE)
        self.assertEqual(
            self.game.status, SessionStatus.LOST_BY_DUPLICATE
        )
        self.assertEqual(self.game.turn_count, 2)
        self.assertTrue(result.game_over)

    def test_homophones_are_duplicate_even_with_different_keys(self) -> None:
        custom_words = {
            **WORDS,
            "橋": accepted(
                "橋", "はし", word_id=21, canonical_key="bridge"
            ),
            "箸": accepted(
                "箸", "はし", word_id=22, canonical_key="chopsticks"
            ),
        }
        game = GameSession(
            FakeLexicon(custom_words),  # type: ignore[arg-type]
            clock=self.clock,
        )
        game.submit("橋")
        game.submit("中継")

        result = game.submit("箸")

        self.assertEqual(result.code, SessionCode.DUPLICATE)
        self.assertEqual(game.history[0].canonical_key, "はし")

    def test_word_ending_in_n_is_recorded_as_losing_entry(self) -> None:
        result = self.game.submit("パン")

        self.assertEqual(result.code, SessionCode.ENDS_WITH_N)
        self.assertTrue(result.accepted)
        self.assertTrue(result.game_over)
        self.assertEqual(self.game.status, SessionStatus.LOST_BY_N)
        self.assertEqual(len(self.game.history), 1)
        self.assertEqual(
            self.game.history[-1].result, HistoryResult.ENDS_WITH_N
        )

    def test_submit_after_game_over_is_rejected(self) -> None:
        self.game.submit("パン")

        result = self.game.submit("林檎")

        self.assertEqual(result.code, SessionCode.GAME_ALREADY_OVER)
        self.assertEqual(self.game.turn_count, 1)

    def test_small_final_kana_is_expanded_for_next_word(self) -> None:
        self.game.submit("列車")

        self.assertEqual(self.game.expected_kana, "や")
        result = self.game.submit("野菜")
        self.assertEqual(result.code, SessionCode.ACCEPTED)

    def test_long_vowel_uses_preceding_mora_vowel(self) -> None:
        self.game.submit("コーヒー")

        self.assertEqual(self.game.expected_kana, "い")
        result = self.game.submit("椅子")
        self.assertEqual(result.code, SessionCode.ACCEPTED)

    def test_kana_helpers_cover_small_and_repeated_long_marks(self) -> None:
        self.assertEqual(canonical_kana("ゃ"), "や")
        self.assertEqual(first_chain_kana("ゃく"), "や")
        self.assertEqual(ending_chain_kana("こーひーー"), "い")
        self.assertEqual(ending_chain_kana("きゃー"), "あ")
        with self.assertRaises(ValueError):
            ending_chain_kana("ーー")

    def test_reset_clears_all_match_state(self) -> None:
        self.game.submit("パン")
        self.clock.advance(10)

        self.game.reset()

        self.assertEqual(self.game.status, SessionStatus.ACTIVE)
        self.assertEqual(self.game.history, ())
        self.assertEqual(self.game.turn_count, 0)
        self.assertIsNone(self.game.ended_at)
        self.assertEqual(self.game.started_at, START + timedelta(seconds=10))


class DeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.lexicon = FakeLexicon(WORDS)
        self.game = GameSession(
            self.lexicon,  # type: ignore[arg-type]
            time_limit_seconds=10,
            clock=self.clock,
        )

    def test_rejects_time_limits_outside_approved_range(self) -> None:
        for limit in (0, 2, 181, True, 3.5):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    GameSession(
                        self.lexicon,  # type: ignore[arg-type]
                        time_limit_seconds=limit,  # type: ignore[arg-type]
                        clock=self.clock,
                    )

    def test_accepts_boundary_limits_and_unlimited(self) -> None:
        for limit in (None, 3, 180):
            with self.subTest(limit=limit):
                game = GameSession(
                    self.lexicon,  # type: ignore[arg-type]
                    time_limit_seconds=limit,
                    clock=self.clock,
                )
                expected = (
                    None
                    if limit is None
                    else START + timedelta(seconds=limit)
                )
                self.assertEqual(game.deadline_at, expected)

    def test_invalid_input_never_resets_deadline(self) -> None:
        original_deadline = self.game.deadline_at
        self.clock.advance(4)

        result = self.game.submit("存在しない")

        self.assertEqual(result.code, SessionCode.LEXICON_REJECTED)
        self.assertEqual(self.game.deadline_at, original_deadline)
        self.assertEqual(self.game.remaining_seconds(), 6)

    def test_pending_reading_does_not_reset_deadline(self) -> None:
        original_deadline = self.game.deadline_at
        self.clock.advance(4)

        result = self.game.submit("日本")

        self.assertEqual(
            result.code, SessionCode.READING_CHOICE_REQUIRED
        )
        self.assertEqual(self.game.deadline_at, original_deadline)

        self.clock.advance(2)
        resolved = self.game.resolve_reading("にほん")
        self.assertEqual(resolved.code, SessionCode.ENDS_WITH_N)

    def test_valid_word_starts_next_deadline_from_server_time(self) -> None:
        self.clock.advance(4)

        result = self.game.submit("林檎")

        self.assertEqual(result.code, SessionCode.ACCEPTED)
        self.assertEqual(
            self.game.deadline_at,
            START + timedelta(seconds=14),
        )

    def test_exact_deadline_times_out_before_validation(self) -> None:
        self.clock.advance(10)
        deadline = self.game.deadline_at

        result = self.game.submit("林檎")

        self.assertEqual(result.code, SessionCode.TIMED_OUT)
        self.assertEqual(self.game.status, SessionStatus.LOST_BY_TIMEOUT)
        self.assertEqual(self.game.history, ())
        self.assertEqual(self.game.ended_at, deadline)
        self.assertIsNone(self.game.deadline_at)

    def test_expire_if_due_is_idempotent(self) -> None:
        self.clock.advance(11)

        first = self.game.expire_if_due()
        second = self.game.expire_if_due()

        self.assertEqual(first.code, SessionCode.TIMED_OUT)
        self.assertIsNone(second)

    def test_timeout_clears_pending_choice(self) -> None:
        self.game.submit("日本")
        self.clock.advance(11)

        result = self.game.resolve_reading("にほん")

        self.assertEqual(result.code, SessionCode.TIMED_OUT)
        self.assertIsNone(self.game.pending_reading)

    def test_reset_restarts_deadline_from_current_server_time(self) -> None:
        old_deadline = self.game.deadline_at
        self.clock.advance(3)

        self.game.reset()

        self.assertNotEqual(self.game.deadline_at, old_deadline)
        self.assertEqual(
            self.game.deadline_at,
            START + timedelta(seconds=13),
        )


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.lexicon = FakeLexicon(WORDS)

    def test_round_trips_active_history_pending_and_deadline(self) -> None:
        game = GameSession(
            self.lexicon,  # type: ignore[arg-type]
            time_limit_seconds=30,
            clock=self.clock,
        )
        game.submit("蟹")
        self.clock.advance(7)
        game.submit("日本")
        snapshot = game.to_snapshot()

        encoded = json.dumps(snapshot, ensure_ascii=False)
        decoded = json.loads(encoded)
        restored = GameSession.from_snapshot(
            decoded,
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )

        self.assertEqual(restored.to_snapshot(), snapshot)
        self.assertEqual(restored.expected_kana, "に")
        self.assertEqual(
            restored.pending_reading.readings,
            ("にほん", "にっぽん"),
        )
        # Restoring does not grant a fresh 30 seconds.
        self.assertEqual(
            restored.deadline_at,
            START + timedelta(seconds=30),
        )

    def test_restored_pending_reading_can_be_resolved(self) -> None:
        game = GameSession(
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )
        game.submit("日本")
        restored = GameSession.from_snapshot(
            game.to_snapshot(),
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )

        result = restored.resolve_reading("にっぽん")

        self.assertEqual(result.code, SessionCode.ENDS_WITH_N)
        self.assertEqual(restored.history[0].surface, "日本")

    def test_round_trips_finished_game(self) -> None:
        game = GameSession(
            self.lexicon,  # type: ignore[arg-type]
            time_limit_seconds=10,
            clock=self.clock,
        )
        game.submit("パン")

        restored = GameSession.from_snapshot(
            game.to_snapshot(),
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )

        self.assertEqual(restored.status, SessionStatus.LOST_BY_N)
        self.assertEqual(
            restored.history[-1].result, HistoryResult.ENDS_WITH_N
        )
        self.assertIsNone(restored.deadline_at)

    def test_round_trips_duplicate_loss_without_duplicating_history(self) -> None:
        game = GameSession(
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )
        game.submit("林檎")
        game.submit("語尾")
        game.submit("りんご")

        restored = GameSession.from_snapshot(
            game.to_snapshot(),
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )

        self.assertEqual(
            restored.status, SessionStatus.LOST_BY_DUPLICATE
        )
        self.assertEqual(restored.turn_count, 2)
        self.assertEqual(
            restored.used_canonical_keys,
            frozenset({"りんご", "ごり"}),
        )

    def test_rejects_corrupt_or_unsupported_snapshots(self) -> None:
        game = GameSession(
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )
        snapshot = game.to_snapshot()

        corrupt_version = {**snapshot, "snapshot_version": 99}
        with self.assertRaises(ValueError):
            GameSession.from_snapshot(
                corrupt_version,
                self.lexicon,  # type: ignore[arg-type]
                clock=self.clock,
            )

        corrupt_history = {
            **snapshot,
            "history": [
                {
                    "surface": "林檎",
                    "reading": "りんご",
                    "canonical_key": "りんご",
                    "turn_number": 2,
                    "timestamp": START.isoformat(),
                    "result": "accepted",
                }
            ],
        }
        with self.assertRaises(ValueError):
            GameSession.from_snapshot(
                corrupt_history,
                self.lexicon,  # type: ignore[arg-type]
                clock=self.clock,
            )


    def _assert_snapshot_rejected(
        self, snapshot: dict[str, object]
    ) -> None:
        with self.assertRaises(ValueError):
            GameSession.from_snapshot(
                snapshot,
                self.lexicon,  # type: ignore[arg-type]
                clock=self.clock,
            )

    @staticmethod
    def _clone_snapshot(
        snapshot: dict[str, object],
    ) -> dict[str, object]:
        return json.loads(json.dumps(snapshot, ensure_ascii=False))

    def _chained_snapshot(self) -> dict[str, object]:
        game = GameSession(
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )
        game.submit("林檎")
        self.clock.advance(1)
        game.submit("ゴリラ")
        self.clock.advance(1)
        game.submit("ラッパ")
        return game.to_snapshot()

    def _pending_snapshot(self) -> dict[str, object]:
        game = GameSession(
            self.lexicon,  # type: ignore[arg-type]
            time_limit_seconds=30,
            clock=self.clock,
        )
        game.submit("蟹")
        self.clock.advance(7)
        game.submit("日本")
        return game.to_snapshot()

    def test_rejects_coerced_time_limit_values(self) -> None:
        snapshot = GameSession(
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        ).to_snapshot()

        for invalid_limit in ("30", 30.0, True):
            with self.subTest(limit=invalid_limit):
                corrupt = self._clone_snapshot(snapshot)
                corrupt["time_limit_seconds"] = invalid_limit
                self._assert_snapshot_rejected(corrupt)

    def test_rejects_noncanonical_or_unusable_history_readings(self) -> None:
        snapshot = self._chained_snapshot()

        corrupt_canonical = self._clone_snapshot(snapshot)
        corrupt_canonical["history"][0]["canonical_key"] = "別のキー"
        self._assert_snapshot_rejected(corrupt_canonical)

        for invalid_reading in ("リンゴ", "abc", "ーりんご", "りんごA"):
            with self.subTest(reading=invalid_reading):
                corrupt = self._clone_snapshot(snapshot)
                corrupt["history"][0]["reading"] = invalid_reading
                corrupt["history"][0]["canonical_key"] = invalid_reading
                self._assert_snapshot_rejected(corrupt)

    def test_rejects_history_which_does_not_chain(self) -> None:
        corrupt = self._clone_snapshot(self._chained_snapshot())
        corrupt["history"][1]["reading"] = "かに"
        corrupt["history"][1]["canonical_key"] = "かに"

        self._assert_snapshot_rejected(corrupt)

    def test_rejects_inconsistent_or_nonfinal_ends_with_n(self) -> None:
        snapshot = self._chained_snapshot()

        wrong_result = self._clone_snapshot(snapshot)
        wrong_result["history"][0]["result"] = "ends_with_n"
        self._assert_snapshot_rejected(wrong_result)

        hidden_n = self._clone_snapshot(snapshot)
        hidden_n["history"][0]["reading"] = "りん"
        hidden_n["history"][0]["canonical_key"] = "りん"
        hidden_n["history"][0]["result"] = "accepted"
        self._assert_snapshot_rejected(hidden_n)

        nonfinal_n = self._clone_snapshot(snapshot)
        nonfinal_n["history"][0]["reading"] = "りん"
        nonfinal_n["history"][0]["canonical_key"] = "りん"
        nonfinal_n["history"][0]["result"] = "ends_with_n"
        self._assert_snapshot_rejected(nonfinal_n)

    def test_rejects_timestamps_outside_session_timeline(self) -> None:
        snapshot = self._chained_snapshot()

        nonmonotonic = self._clone_snapshot(snapshot)
        nonmonotonic["history"][0]["timestamp"] = (
            START + timedelta(seconds=1)
        ).isoformat()
        nonmonotonic["history"][1]["timestamp"] = START.isoformat()
        self._assert_snapshot_rejected(nonmonotonic)

        before_start = self._clone_snapshot(snapshot)
        before_start["history"][0]["timestamp"] = (
            START - timedelta(seconds=1)
        ).isoformat()
        self._assert_snapshot_rejected(before_start)

        future_entry = self._clone_snapshot(snapshot)
        future_entry["history"][-1]["timestamp"] = (
            self.clock.current + timedelta(seconds=1)
        ).isoformat()
        self._assert_snapshot_rejected(future_entry)

        future_start = self._clone_snapshot(snapshot)
        future_start["started_at"] = (
            self.clock.current + timedelta(seconds=1)
        ).isoformat()
        self._assert_snapshot_rejected(future_start)

    def test_rejects_corrupt_pending_candidates_and_timeline(self) -> None:
        snapshot = self._pending_snapshot()

        mismatched_surface = self._clone_snapshot(snapshot)
        mismatched_surface["pending_reading"]["candidates"][0][
            "surface"
        ] = "別の単語"
        self._assert_snapshot_rejected(mismatched_surface)

        bad_candidate_key = self._clone_snapshot(snapshot)
        bad_candidate_key["pending_reading"]["candidates"][0][
            "canonical_key"
        ] = "別のキー"
        self._assert_snapshot_rejected(bad_candidate_key)

        bad_candidate_reading = self._clone_snapshot(snapshot)
        bad_candidate_reading["pending_reading"]["candidates"][0][
            "reading"
        ] = "not-kana"
        bad_candidate_reading["pending_reading"]["candidates"][0][
            "canonical_key"
        ] = "not-kana"
        self._assert_snapshot_rejected(bad_candidate_reading)

        coerced_candidate_id = self._clone_snapshot(snapshot)
        coerced_candidate_id["pending_reading"]["candidates"][0][
            "word_id"
        ] = "1"
        self._assert_snapshot_rejected(coerced_candidate_id)

        pending_before_history = self._clone_snapshot(snapshot)
        pending_before_history["pending_reading"]["submitted_at"] = (
            START - timedelta(seconds=1)
        ).isoformat()
        self._assert_snapshot_rejected(pending_before_history)

    def test_rejects_deadline_not_derived_from_current_turn(self) -> None:
        corrupt = self._clone_snapshot(self._pending_snapshot())
        corrupt["deadline_at"] = (
            START + timedelta(seconds=31)
        ).isoformat()

        self._assert_snapshot_rejected(corrupt)
if __name__ == "__main__":
    unittest.main()
