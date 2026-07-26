"""Tests for the framework-independent score attack domain."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest

from shiritori.game_session import (
    DeadlinePolicy,
    GameSession,
    HistoryEntry,
    HistoryResult,
    SessionCode,
)
from shiritori.lexicon import (
    LexiconCandidate,
    LexiconCode,
    LexiconResult,
    normalize_surface,
)
from shiritori.score_attack import (
    SCORE_ATTACK_DURATION_SECONDS,
    SCORE_RULES_VERSION,
    ScoreAttackAlreadyStartedError,
    ScoreAttackFinishReason,
    ScoreAttackNotStartedError,
    ScoreAttackSession,
    ScoreAttackStatus,
    points_for_entry,
    score_history,
)


UTC = timezone.utc
START = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
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
) -> LexiconCandidate:
    return LexiconCandidate(
        surface=surface,
        reading=reading,
        lemma=surface,
        normalized_form=surface,
        part_of_speech=NOUN_POS,
        dictionary_id=0,
        word_id=word_id,
        canonical_key=reading,
    )


def accepted(
    surface: str,
    reading: str,
    *,
    word_id: int = 1,
) -> LexiconResult:
    return LexiconResult(
        code=LexiconCode.ACCEPTED,
        surface=surface,
        message="accepted",
        candidates=(
            candidate(surface, reading, word_id=word_id),
        ),
    )


def ambiguous(surface: str, *readings: str) -> LexiconResult:
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
        self.calls = 0

    def validate(self, raw_surface: str | None) -> LexiconResult:
        self.calls += 1
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
    "橋": ambiguous("橋", "はし", "きょう"),
}


def make_entry(
    reading: str,
    *,
    result: HistoryResult = HistoryResult.ACCEPTED,
    turn_number: int = 1,
) -> HistoryEntry:
    return HistoryEntry(
        surface=f"単語{turn_number}",
        reading=reading,
        canonical_key=reading,
        turn_number=turn_number,
        timestamp=START,
        result=result,
    )


class FixedDeadlinePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.lexicon = FakeLexicon(WORDS)

    def test_default_policy_remains_per_turn(self) -> None:
        game = GameSession(
            self.lexicon,  # type: ignore[arg-type]
            time_limit_seconds=30,
            clock=self.clock,
        )

        self.clock.advance(5)
        game.submit("林檎")

        self.assertIs(game.deadline_policy, DeadlinePolicy.PER_TURN)
        self.assertEqual(
            game.deadline_at,
            START + timedelta(seconds=35),
        )

    def test_fixed_policy_never_resets_after_an_accepted_word(self) -> None:
        game = GameSession(
            self.lexicon,  # type: ignore[arg-type]
            time_limit_seconds=180,
            deadline_policy=DeadlinePolicy.FIXED_MATCH,
            clock=self.clock,
        )
        original_deadline = game.deadline_at

        self.clock.advance(40)
        result = game.submit("林檎")

        self.assertEqual(result.code, SessionCode.ACCEPTED)
        self.assertEqual(game.deadline_at, original_deadline)
        self.assertEqual(game.remaining_seconds(), 140)

    def test_fixed_policy_requires_a_time_limit(self) -> None:
        with self.assertRaises(ValueError):
            GameSession(
                self.lexicon,  # type: ignore[arg-type]
                deadline_policy=DeadlinePolicy.FIXED_MATCH,
                clock=self.clock,
            )

    def test_fixed_snapshot_round_trips_without_extending_deadline(
        self,
    ) -> None:
        game = GameSession(
            self.lexicon,  # type: ignore[arg-type]
            time_limit_seconds=180,
            deadline_policy=DeadlinePolicy.FIXED_MATCH,
            clock=self.clock,
        )
        self.clock.advance(40)
        game.submit("林檎")

        restored = GameSession.from_snapshot(
            json.loads(json.dumps(game.to_snapshot(), ensure_ascii=False)),
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )

        self.assertIs(
            restored.deadline_policy,
            DeadlinePolicy.FIXED_MATCH,
        )
        self.assertEqual(
            restored.deadline_at,
            START + timedelta(seconds=180),
        )

    def test_fixed_snapshot_rejects_a_deadline_based_on_last_turn(
        self,
    ) -> None:
        game = GameSession(
            self.lexicon,  # type: ignore[arg-type]
            time_limit_seconds=180,
            deadline_policy=DeadlinePolicy.FIXED_MATCH,
            clock=self.clock,
        )
        self.clock.advance(40)
        game.submit("林檎")
        snapshot = game.to_snapshot()
        snapshot["deadline_at"] = (
            START + timedelta(seconds=220)
        ).isoformat()

        with self.assertRaises(ValueError):
            GameSession.from_snapshot(
                snapshot,
                self.lexicon,  # type: ignore[arg-type]
                clock=self.clock,
            )

    def test_version_one_snapshot_defaults_to_per_turn(self) -> None:
        game = GameSession(
            self.lexicon,  # type: ignore[arg-type]
            time_limit_seconds=30,
            clock=self.clock,
        )
        self.clock.advance(5)
        game.submit("林檎")
        legacy = game.to_snapshot()
        legacy["snapshot_version"] = 1
        del legacy["deadline_policy"]

        restored = GameSession.from_snapshot(
            legacy,
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )

        self.assertIs(restored.deadline_policy, DeadlinePolicy.PER_TURN)
        self.assertEqual(
            restored.deadline_at,
            START + timedelta(seconds=35),
        )


class ScoreRulesTests(unittest.TestCase):
    def test_rules_version_one_formula_and_caps(self) -> None:
        self.assertEqual(SCORE_RULES_VERSION, 1)
        self.assertEqual(
            points_for_entry(
                make_entry("あ" * 20),
                prior_scored_words=12,
            ),
            60,
        )
        self.assertEqual(
            points_for_entry(
                make_entry(
                    "ぱん",
                    result=HistoryResult.ENDS_WITH_N,
                ),
                prior_scored_words=12,
            ),
            0,
        )

    def test_score_is_derived_in_history_order(self) -> None:
        history = (
            make_entry("りんご", turn_number=1),
            make_entry("ごり", turn_number=2),
            make_entry(
                "ぱん",
                result=HistoryResult.ENDS_WITH_N,
                turn_number=3,
            ),
        )

        # 10 + 2*3 + 0, then 10 + 2*2 + 2; losing ん is zero.
        self.assertEqual(score_history(history), 32)

    def test_formula_rejects_coerced_or_negative_prior_count(self) -> None:
        for invalid in (-1, True, 1.5):
            with self.subTest(value=invalid):
                with self.assertRaises(ValueError):
                    points_for_entry(
                        make_entry("りんご"),
                        prior_scored_words=invalid,  # type: ignore[arg-type]
                    )


class ScoreAttackSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.lexicon = FakeLexicon(WORDS)
        self.attack = ScoreAttackSession(
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )

    def test_run_is_idle_until_explicit_start(self) -> None:
        self.assertIs(self.attack.status, ScoreAttackStatus.IDLE)
        self.assertIsNone(self.attack.started_at)
        self.assertIsNone(self.attack.deadline_at)
        self.assertIsNone(self.attack.remaining_seconds())
        self.assertEqual(self.attack.score, 0)
        self.assertIsNone(self.attack.expire_if_due())
        with self.assertRaises(ScoreAttackNotStartedError):
            self.attack.submit("林檎")

        self.clock.advance(25)
        self.attack.start()

        self.assertIs(self.attack.status, ScoreAttackStatus.ACTIVE)
        self.assertEqual(self.attack.started_at, START + timedelta(seconds=25))
        self.assertEqual(
            self.attack.deadline_at,
            START
            + timedelta(
                seconds=25 + SCORE_ATTACK_DURATION_SECONDS
            ),
        )
        with self.assertRaises(ScoreAttackAlreadyStartedError):
            self.attack.start()

    def test_safe_words_score_and_duplicate_finishes_without_points(
        self,
    ) -> None:
        self.attack.start()

        first = self.attack.submit("林檎")
        second = self.attack.submit("語尾")
        duplicate = self.attack.submit("りんご")

        self.assertEqual(first.code, SessionCode.ACCEPTED)
        self.assertEqual(second.code, SessionCode.ACCEPTED)
        self.assertEqual(duplicate.code, SessionCode.DUPLICATE)
        self.assertIs(self.attack.status, ScoreAttackStatus.FINISHED)
        self.assertIs(
            self.attack.finish_reason,
            ScoreAttackFinishReason.DUPLICATE,
        )
        self.assertEqual(self.attack.accepted_count, 2)
        self.assertEqual(self.attack.score, 32)
        self.assertEqual(len(self.attack.history), 2)

    def test_ends_with_n_is_a_zero_point_recorded_loss(self) -> None:
        self.attack.start()

        result = self.attack.submit("パン")

        self.assertEqual(result.code, SessionCode.ENDS_WITH_N)
        self.assertIs(
            self.attack.finish_reason,
            ScoreAttackFinishReason.ENDS_WITH_N,
        )
        self.assertEqual(len(self.attack.history), 1)
        self.assertEqual(self.attack.accepted_count, 0)
        self.assertEqual(self.attack.score, 0)
        self.assertEqual(self.attack.remaining_seconds(), 0)

    def test_invalid_and_unchained_words_never_score(self) -> None:
        self.attack.start()

        missing = self.attack.submit("存在しない")
        accepted_result = self.attack.submit("林檎")
        wrong_chain = self.attack.submit("パン")

        self.assertEqual(missing.code, SessionCode.LEXICON_REJECTED)
        self.assertEqual(accepted_result.code, SessionCode.ACCEPTED)
        self.assertEqual(wrong_chain.code, SessionCode.NOT_CHAINED)
        self.assertEqual(self.attack.accepted_count, 1)
        self.assertEqual(self.attack.score, 16)

    def test_reading_choice_keeps_the_fixed_server_clock(self) -> None:
        self.attack.start()
        deadline = self.attack.deadline_at
        self.clock.advance(100)

        pending = self.attack.submit("橋")
        invalid = self.attack.resolve_reading("ほし")
        resolved = self.attack.resolve_reading("はし")

        self.assertEqual(
            pending.code,
            SessionCode.READING_CHOICE_REQUIRED,
        )
        self.assertEqual(
            invalid.code,
            SessionCode.INVALID_READING_CHOICE,
        )
        self.assertEqual(resolved.code, SessionCode.ACCEPTED)
        self.assertEqual(self.attack.deadline_at, deadline)
        self.assertEqual(self.attack.score, 14)
        self.assertIsNone(self.attack.pending_reading)

    def test_exact_deadline_wins_before_dictionary_validation(self) -> None:
        self.attack.start()
        calls_before = self.lexicon.calls
        self.clock.advance(SCORE_ATTACK_DURATION_SECONDS)

        result = self.attack.submit("林檎")

        self.assertEqual(result.code, SessionCode.TIMED_OUT)
        self.assertEqual(self.lexicon.calls, calls_before)
        self.assertIs(
            self.attack.finish_reason,
            ScoreAttackFinishReason.TIMEOUT,
        )
        self.assertEqual(self.attack.score, 0)

    def test_delayed_timeout_uses_the_fixed_deadline_as_end_time(self) -> None:
        self.attack.start()
        deadline = self.attack.deadline_at
        self.clock.advance(SCORE_ATTACK_DURATION_SECONDS + 15)

        result = self.attack.expire_if_due()

        self.assertEqual(result.code, SessionCode.TIMED_OUT)
        self.assertIsNotNone(deadline)
        snapshot = self.attack.to_snapshot()
        game_snapshot = snapshot["game_session"]
        self.assertIsInstance(game_snapshot, dict)
        self.assertEqual(
            game_snapshot["ended_at"],
            deadline.isoformat(),
        )

    def test_pending_reading_expires_at_the_same_fixed_deadline(self) -> None:
        self.attack.start()
        self.clock.advance(170)
        self.attack.submit("橋")
        self.clock.advance(10)

        result = self.attack.resolve_reading("はし")

        self.assertEqual(result.code, SessionCode.TIMED_OUT)
        self.assertIsNone(self.attack.pending_reading)
        self.assertEqual(self.attack.score, 0)

    def test_snapshot_round_trip_preserves_score_and_deadline(self) -> None:
        self.attack.start()
        self.clock.advance(40)
        self.attack.submit("林檎")
        snapshot = json.loads(
            json.dumps(self.attack.to_snapshot(), ensure_ascii=False)
        )

        restored = ScoreAttackSession.from_snapshot(
            snapshot,
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )

        self.assertEqual(restored.to_snapshot(), snapshot)
        self.assertEqual(restored.score, 16)
        self.assertEqual(restored.accepted_count, 1)
        self.assertEqual(
            restored.deadline_at,
            START + timedelta(seconds=180),
        )

    def test_snapshot_rejects_client_score_or_timer_tampering(self) -> None:
        self.attack.start()
        self.attack.submit("林檎")
        snapshot = self.attack.to_snapshot()

        tampered_score = json.loads(
            json.dumps(snapshot, ensure_ascii=False)
        )
        tampered_score["score"] = 999_999
        with self.assertRaises(ValueError):
            ScoreAttackSession.from_snapshot(
                tampered_score,
                self.lexicon,  # type: ignore[arg-type]
                clock=self.clock,
            )

        tampered_policy = json.loads(
            json.dumps(snapshot, ensure_ascii=False)
        )
        tampered_policy["game_session"]["deadline_policy"] = "per_turn"
        with self.assertRaises(ValueError):
            ScoreAttackSession.from_snapshot(
                tampered_policy,
                self.lexicon,  # type: ignore[arg-type]
                clock=self.clock,
            )

    def test_restore_after_deadline_does_not_grant_more_time(self) -> None:
        self.attack.start()
        self.attack.submit("林檎")
        snapshot = self.attack.to_snapshot()
        self.clock.advance(181)

        restored = ScoreAttackSession.from_snapshot(
            snapshot,
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )
        result = restored.expire_if_due()

        self.assertEqual(result.code, SessionCode.TIMED_OUT)
        self.assertIs(
            restored.finish_reason,
            ScoreAttackFinishReason.TIMEOUT,
        )
        self.assertEqual(restored.score, 16)

    def test_idle_and_finished_snapshots_round_trip(self) -> None:
        idle_snapshot = self.attack.to_snapshot()
        restored_idle = ScoreAttackSession.from_snapshot(
            idle_snapshot,
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )
        self.assertIs(restored_idle.status, ScoreAttackStatus.IDLE)

        self.attack.start()
        self.attack.submit("パン")
        finished_snapshot = self.attack.to_snapshot()
        restored_finished = ScoreAttackSession.from_snapshot(
            finished_snapshot,
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )
        self.assertEqual(
            restored_finished.to_snapshot(),
            finished_snapshot,
        )


if __name__ == "__main__":
    unittest.main()
