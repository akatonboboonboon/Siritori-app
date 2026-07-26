"""Tests for the deterministic, server-timed daily challenge domain."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import unittest

from shiritori.daily_challenge import (
    DAILY_CHALLENGE_DURATION_SECONDS,
    DAILY_CHALLENGE_RULES_VERSION,
    DailyChallengeCondition,
    DailyChallengeConfigurationError,
    DailyChallengeSession,
    challenge_date_at,
    daily_condition_for,
)
from shiritori.game_session import SessionCode
from shiritori.lexicon import LexiconCode, LexiconResult
from shiritori.score_attack import (
    ScoreAttackFinishReason,
    ScoreAttackStatus,
)
from tests.test_score_attack import (
    FakeLexicon,
    ManualClock,
    START,
    accepted,
    ambiguous,
)


DAY = date(2026, 7, 26)
CONDITION = DailyChallengeCondition.create(DAY, "林檎", "りんご")
WORDS = {
    "林檎": accepted("林檎", "りんご"),
    "語尾": accepted("語尾", "ごり"),
    "りんご": accepted("りんご", "りんご"),
    "語彙": ambiguous("語彙", "ごい", "かたり"),
    "ゴーン": accepted("ゴーン", "ごーん"),
}


class DailyConditionTests(unittest.TestCase):
    def test_jst_date_boundary_is_server_derived(self) -> None:
        before_midnight = datetime(
            2026,
            7,
            26,
            14,
            59,
            59,
            tzinfo=timezone.utc,
        )

        self.assertEqual(
            challenge_date_at(before_midnight),
            date(2026, 7, 26),
        )
        self.assertEqual(
            challenge_date_at(before_midnight + timedelta(seconds=1)),
            date(2026, 7, 27),
        )
        with self.assertRaises(ValueError):
            challenge_date_at(datetime(2026, 7, 26))

    def test_rules_v1_starting_vocabulary_is_frozen(self) -> None:
        condition = daily_condition_for(DAY)

        self.assertEqual(condition.rules_version, 1)
        self.assertEqual(
            condition.condition_key,
            daily_condition_for(DAY).condition_key,
        )

    def test_condition_is_stable_versioned_and_date_bound(self) -> None:
        first = daily_condition_for(DAY)
        second = daily_condition_for(DAY)
        following_conditions = {
            daily_condition_for(DAY + timedelta(days=offset)).condition_key
            for offset in range(1, 15)
        }

        self.assertEqual(first, second)
        self.assertEqual(first.rules_version, DAILY_CHALLENGE_RULES_VERSION)
        self.assertEqual(
            first.duration_seconds,
            DAILY_CHALLENGE_DURATION_SECONDS,
        )
        self.assertEqual(len(first.condition_key), 64)
        self.assertNotIn(first.condition_key, following_conditions)

    def test_condition_normalizes_reading_and_detects_tampering(self) -> None:
        condition = DailyChallengeCondition.create(
            DAY,
            "  リンゴ  ",
            "リンゴ",
        )
        snapshot = condition.to_snapshot()

        self.assertEqual(condition.start_surface, "リンゴ")
        self.assertEqual(condition.start_reading, "りんご")
        self.assertEqual(
            DailyChallengeCondition.from_snapshot(snapshot),
            condition,
        )

        snapshot["start_reading"] = "ごりら"
        with self.assertRaises(ValueError):
            DailyChallengeCondition.from_snapshot(snapshot)

    def test_condition_rejects_naive_types_and_ending_n(self) -> None:
        with self.assertRaises(ValueError):
            DailyChallengeCondition.create(  # type: ignore[arg-type]
                datetime(2026, 7, 26, tzinfo=timezone.utc),
                "林檎",
                "りんご",
            )
        with self.assertRaises(ValueError):
            DailyChallengeCondition.create(DAY, "パン", "ぱん")
        with self.assertRaises(ValueError):
            DailyChallengeCondition.create(
                DAY,
                "林檎",
                "りんご",
                duration_seconds=60,
            )


class DailyChallengeSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.lexicon = FakeLexicon(WORDS)
        self.daily = DailyChallengeSession(
            CONDITION,
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        )

    def test_server_seed_sets_first_kana_without_scoring(self) -> None:
        self.daily.start()
        nested = self.daily.to_snapshot()["score_attack"]

        self.assertIs(self.daily.status, ScoreAttackStatus.ACTIVE)
        self.assertEqual(self.daily.expected_kana, "ご")
        self.assertEqual(self.daily.history, ())
        self.assertEqual(self.daily.score, 0)
        self.assertEqual(self.daily.accepted_count, 0)
        self.assertEqual(
            self.daily.deadline_at,
            START + timedelta(seconds=DAILY_CHALLENGE_DURATION_SECONDS),
        )
        self.assertIsInstance(nested, dict)
        self.assertEqual(nested["accepted_count"], 1)

    def test_player_score_excludes_seed_and_uses_existing_rules(self) -> None:
        self.daily.start()

        accepted_result = self.daily.submit("語尾")
        duplicate_result = self.daily.submit("りんご")

        self.assertEqual(accepted_result.code, SessionCode.ACCEPTED)
        self.assertEqual(duplicate_result.code, SessionCode.DUPLICATE)
        self.assertIs(self.daily.status, ScoreAttackStatus.FINISHED)
        self.assertIs(
            self.daily.finish_reason,
            ScoreAttackFinishReason.DUPLICATE,
        )
        self.assertEqual(
            tuple(entry.surface for entry in self.daily.history),
            ("語尾",),
        )
        self.assertEqual(self.daily.accepted_count, 1)
        self.assertEqual(self.daily.score, 14)

    def test_reading_choices_and_fixed_deadline_are_reused(self) -> None:
        self.daily.start()
        original_deadline = self.daily.deadline_at
        self.clock.advance(100)

        pending = self.daily.submit("語彙")
        wrong = self.daily.resolve_reading("かたり")
        resolved = self.daily.resolve_reading("ごい")

        self.assertEqual(
            pending.code,
            SessionCode.READING_CHOICE_REQUIRED,
        )
        self.assertEqual(wrong.code, SessionCode.NOT_CHAINED)
        self.assertEqual(resolved.code, SessionCode.ACCEPTED)
        self.assertEqual(self.daily.deadline_at, original_deadline)
        self.assertEqual(self.daily.accepted_count, 1)

    def test_timeout_uses_persisted_deadline(self) -> None:
        self.daily.start()
        deadline = self.daily.deadline_at
        self.clock.advance(DAILY_CHALLENGE_DURATION_SECONDS + 20)

        result = self.daily.expire_if_due()

        self.assertEqual(result.code, SessionCode.TIMED_OUT)
        self.assertIs(
            self.daily.finish_reason,
            ScoreAttackFinishReason.TIMEOUT,
        )
        nested = self.daily.to_snapshot()["score_attack"]
        self.assertEqual(
            nested["game_session"]["ended_at"],
            deadline.isoformat(),
        )

    def test_snapshot_round_trip_and_projection_tampering(self) -> None:
        self.daily.start()
        self.daily.submit("語尾")
        snapshot = json.loads(
            json.dumps(self.daily.to_snapshot(), ensure_ascii=False)
        )

        restored = DailyChallengeSession.from_snapshot(
            snapshot,
            self.lexicon,  # type: ignore[arg-type]
            clock=self.clock,
            expected_condition=CONDITION,
        )

        self.assertEqual(restored.to_snapshot(), snapshot)
        self.assertEqual(restored.score, 14)
        self.assertEqual(restored.expected_kana, "り")

        snapshot["score"] = 999
        with self.assertRaises(ValueError):
            DailyChallengeSession.from_snapshot(
                snapshot,
                self.lexicon,  # type: ignore[arg-type]
                clock=self.clock,
                expected_condition=CONDITION,
            )

    def test_snapshot_rejects_changed_seed_or_condition(self) -> None:
        self.daily.start()
        snapshot = json.loads(
            json.dumps(self.daily.to_snapshot(), ensure_ascii=False)
        )
        snapshot["score_attack"]["game_session"]["history"][0][
            "surface"
        ] = "偽装"

        with self.assertRaises(ValueError):
            DailyChallengeSession.from_snapshot(
                snapshot,
                self.lexicon,  # type: ignore[arg-type]
                clock=self.clock,
                expected_condition=CONDITION,
            )

        other = DailyChallengeCondition.create(
            DAY + timedelta(days=1),
            "林檎",
            "りんご",
        )
        with self.assertRaises(ValueError):
            DailyChallengeSession.from_snapshot(
                self.daily.to_snapshot(),
                self.lexicon,  # type: ignore[arg-type]
                clock=self.clock,
                expected_condition=other,
            )

    def test_seed_can_resolve_one_pinned_ambiguous_reading(self) -> None:
        condition = DailyChallengeCondition.create(DAY, "橋", "はし")
        lexicon = FakeLexicon({"橋": ambiguous("橋", "はし", "きょう")})
        daily = DailyChallengeSession(
            condition,
            lexicon,  # type: ignore[arg-type]
            clock=self.clock,
        ).start()

        self.assertEqual(daily.expected_kana, "し")
        self.assertEqual(daily.history, ())

    def test_seed_fails_closed_when_dictionary_does_not_match(self) -> None:
        missing = FakeLexicon(
            {
                "林檎": LexiconResult(
                    code=LexiconCode.NOT_IN_DICTIONARY,
                    surface="林檎",
                    message="missing",
                )
            }
        )
        daily = DailyChallengeSession(
            CONDITION,
            missing,  # type: ignore[arg-type]
            clock=self.clock,
        )

        with self.assertRaises(DailyChallengeConfigurationError):
            daily.start()


if __name__ == "__main__":
    unittest.main()
