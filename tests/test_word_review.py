from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from shiritori.database import Database
from shiritori.models import (
    ApprovedWord,
    User,
    WordSuggestion,
    WordSuggestionReview,
    new_id,
)
from shiritori.word_review import (
    ApprovedWordCatalog,
    ReviewDecision,
    WordReviewAuthorizationError,
    WordReviewConfigurationError,
    WordReviewConflictError,
    WordReviewNotFoundError,
    WordReviewService,
    WordReviewValidationError,
    _recent_reviews_statement,
    admin_username_keys_from_value,
)


BASE_TIME = datetime(2026, 7, 26, 1, 0, tzinfo=timezone.utc)


class WordReviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(
            self.temporary_directory.name,
            "word-review.sqlite3",
        )
        self.database = Database(
            f"sqlite+pysqlite:///{database_path.as_posix()}"
        )
        self.database.create_schema_for_testing()
        self.admin = self._create_user("Admin", "管理者")
        self.alice = self._create_user("alice", "ありす")
        self.bob = self._create_user("bob", "ぼぶ")
        self.carol = self._create_user("carol", "きゃろる")
        self.catalog = ApprovedWordCatalog(self.database)
        self.service = WordReviewService(
            self.database,
            {"admin"},
            self.catalog,
            clock=lambda: BASE_TIME + timedelta(hours=2),
        )

    def tearDown(self) -> None:
        self.database.dispose()
        self.temporary_directory.cleanup()

    def _create_user(
        self,
        username: str,
        display_name: str,
    ) -> User:
        with self.database.transaction() as session:
            user = User(
                id=new_id(),
                username=username,
                username_key=username.casefold(),
                display_name=display_name,
                password_hash="$argon2id$test-only",
                created_at=BASE_TIME,
                updated_at=BASE_TIME,
            )
            session.add(user)
            session.flush()
            return user

    def _suggest(
        self,
        user: User,
        surface: str = "佃煮",
        reading: str = "つくだに",
        *,
        note: str | None = None,
        minutes: int = 0,
    ) -> WordSuggestion:
        created_at = BASE_TIME + timedelta(minutes=minutes)
        with self.database.transaction() as session:
            suggestion = WordSuggestion(
                id=new_id(),
                user_id=user.id,
                surface=surface,
                reading=reading,
                note=note,
                status="pending",
                created_at=created_at,
                updated_at=created_at,
                reviewed_at=None,
            )
            session.add(suggestion)
            session.flush()
            return suggestion

    def test_admin_environment_parser_normalizes_and_rejects_empty_items(
        self,
    ) -> None:
        self.assertEqual(
            admin_username_keys_from_value(" Admin,ＡＬＩＣＥ "),
            frozenset({"admin", "alice"}),
        )
        self.assertEqual(
            admin_username_keys_from_value(" "),
            frozenset(),
        )
        with self.assertRaises(WordReviewConfigurationError):
            admin_username_keys_from_value("admin,,alice")

    def test_configured_admin_must_already_exist(self) -> None:
        self.assertEqual(
            self.service.validate_configured_admins(),
            (self.admin.id,),
        )
        missing = WordReviewService(
            self.database,
            {"not-registered"},
            self.catalog,
        )
        with self.assertRaises(WordReviewConfigurationError):
            missing.validate_configured_admins()

    def test_authorization_is_refreshed_from_database(self) -> None:
        self.assertTrue(self.service.is_admin(self.admin.id))
        self.assertFalse(self.service.is_admin(self.alice.id))
        self.assertFalse(self.service.is_admin("bad id " * 10))

        with self.database.transaction() as session:
            session.get(User, self.admin.id).disabled_at = BASE_TIME

        self.assertFalse(self.service.is_admin(self.admin.id))
        with self.assertRaises(WordReviewAuthorizationError):
            self.service.list_pending_groups(self.admin.id)

    def test_pending_queue_groups_duplicates_and_keeps_review_hints(
        self,
    ) -> None:
        self._suggest(
            self.alice,
            note="郷土料理の名前",
            minutes=0,
        )
        self._suggest(
            self.bob,
            note="辞書掲載あり",
            minutes=3,
        )
        self._suggest(
            self.carol,
            surface="油淋鶏",
            reading="ゆーりんちー",
            minutes=1,
        )

        groups = self.service.list_pending_groups(self.admin.id)

        self.assertEqual(
            [(group.surface, group.submission_count) for group in groups],
            [("佃煮", 2), ("油淋鶏", 1)],
        )
        self.assertEqual(
            [detail.submitter_display_name for detail in groups[0].submissions],
            ["ありす", "ぼぶ"],
        )
        self.assertEqual(
            [detail.note for detail in groups[0].submissions],
            ["郷土料理の名前", "辞書掲載あり"],
        )
        self.assertEqual(groups[0].first_submitted_at, BASE_TIME)
        self.assertEqual(
            groups[0].last_submitted_at,
            BASE_TIME + timedelta(minutes=3),
        )

    def test_non_admin_cannot_list_or_forge_a_review(self) -> None:
        suggestion = self._suggest(self.alice)

        with self.assertRaises(WordReviewAuthorizationError):
            self.service.list_pending_groups(self.bob.id)
        with self.assertRaises(WordReviewAuthorizationError):
            self.service.review_group(
                self.bob.id,
                "佃煮",
                "つくだに",
                "approved",
            )

        with self.database.read_session() as session:
            stored = session.get(WordSuggestion, suggestion.id)
            self.assertEqual(stored.status, "pending")
            self.assertIsNone(
                session.get(WordSuggestionReview, suggestion.id)
            )
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(ApprovedWord)
                ),
                0,
            )

    def test_approval_is_atomic_grouped_audited_and_immediately_playable(
        self,
    ) -> None:
        first = self._suggest(self.alice, note="最初", minutes=0)
        second = self._suggest(self.bob, note="重複", minutes=1)

        result = self.service.review_group(
            self.admin.id,
            "佃煮",
            "つくだに",
            ReviewDecision.APPROVED,
            "実在する名詞を確認",
        )

        self.assertEqual(result.reviewed_count, 2)
        self.assertFalse(result.replayed)
        self.assertTrue(result.approved_word_added)
        with self.database.read_session() as session:
            suggestions = tuple(
                session.scalars(
                    select(WordSuggestion)
                    .where(WordSuggestion.id.in_((first.id, second.id)))
                    .order_by(WordSuggestion.id)
                )
            )
            self.assertTrue(
                all(item.status == "approved" for item in suggestions)
            )
            self.assertTrue(
                all(item.reviewed_at is not None for item in suggestions)
            )
            reviews = tuple(
                session.scalars(
                    select(WordSuggestionReview).order_by(
                        WordSuggestionReview.suggestion_id
                    )
                )
            )
            self.assertEqual(len(reviews), 2)
            self.assertTrue(
                all(
                    review.reviewer_user_id == self.admin.id
                    and review.decision == "approved"
                    and review.review_note == "実在する名詞を確認"
                    for review in reviews
                )
            )
            approved = session.scalar(select(ApprovedWord))
            self.assertEqual(
                (approved.surface, approved.reading),
                ("佃煮", "つくだに"),
            )
            self.assertEqual(
                approved.approved_by_user_id,
                self.admin.id,
            )
            self.assertEqual(approved.source_suggestion_id, first.id)

        self.assertEqual(
            [
                (entry.surface, entry.reading)
                for entry in self.catalog.lookup("佃煮")
            ],
            [("佃煮", "つくだに")],
        )
        recent = self.service.list_recent_reviews(self.admin.id)
        self.assertEqual(len(recent), 2)
        self.assertTrue(
            all(
                item.status == "approved"
                and item.reviewer_display_name == "管理者"
                for item in recent
            )
        )

    def test_same_decision_is_idempotent_and_opposite_decision_conflicts(
        self,
    ) -> None:
        suggestion = self._suggest(self.alice)
        first = self.service.review_group(
            self.admin.id,
            "佃煮",
            "つくだに",
            "approved",
            "最初の監査メモ",
        )
        replay = self.service.review_group(
            self.admin.id,
            "佃煮",
            "つくだに",
            "approved",
            "上書きしないメモ",
        )

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.reviewed_count, 0)
        self.assertFalse(replay.approved_word_added)
        with self.database.read_session() as session:
            review = session.get(WordSuggestionReview, suggestion.id)
            self.assertEqual(review.review_note, "最初の監査メモ")
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(ApprovedWord)
                ),
                1,
            )

        with self.assertRaises(WordReviewConflictError):
            self.service.review_group(
                self.admin.id,
                "佃煮",
                "つくだに",
                "rejected",
            )

    def test_new_duplicate_can_join_an_existing_approved_group(self) -> None:
        self._suggest(self.alice)
        first = self.service.review_group(
            self.admin.id,
            "佃煮",
            "つくだに",
            "approved",
        )
        late = self._suggest(self.carol, minutes=10)

        second = self.service.review_group(
            self.admin.id,
            "佃煮",
            "つくだに",
            "approved",
        )

        self.assertTrue(first.approved_word_added)
        self.assertEqual(second.reviewed_count, 1)
        self.assertFalse(second.approved_word_added)
        with self.database.read_session() as session:
            self.assertEqual(
                session.get(WordSuggestion, late.id).status,
                "approved",
            )
            self.assertIsNotNone(
                session.get(WordSuggestionReview, late.id)
            )
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(ApprovedWord)
                ),
                1,
            )

    def test_rejection_is_final_audited_and_never_updates_catalog(self) -> None:
        suggestion = self._suggest(self.alice)

        result = self.service.review_group(
            self.admin.id,
            "佃煮",
            "つくだに",
            "rejected",
            "固有の作品内用語",
        )
        replay = self.service.review_group(
            self.admin.id,
            "佃煮",
            "つくだに",
            "rejected",
        )

        self.assertEqual(result.decision, ReviewDecision.REJECTED)
        self.assertTrue(replay.replayed)
        self.assertEqual(self.catalog.lookup("佃煮"), ())
        with self.database.read_session() as session:
            self.assertEqual(
                session.get(WordSuggestion, suggestion.id).status,
                "rejected",
            )
            review = session.get(WordSuggestionReview, suggestion.id)
            self.assertEqual(review.decision, "rejected")
            self.assertEqual(review.review_note, "固有の作品内用語")
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(ApprovedWord)
                ),
                0,
            )

        with self.assertRaises(WordReviewConflictError):
            self.service.review_group(
                self.admin.id,
                "佃煮",
                "つくだに",
                "approved",
            )

    def test_exact_group_and_review_input_are_validated(self) -> None:
        self._suggest(self.alice)

        with self.assertRaises(WordReviewNotFoundError):
            self.service.review_group(
                self.admin.id,
                "油淋鶏",
                "ゆーりんちー",
                "approved",
            )
        with self.assertRaises(WordReviewValidationError) as decision_error:
            self.service.review_group(
                self.admin.id,
                "佃煮",
                "つくだに",
                "maybe",
            )
        self.assertEqual(decision_error.exception.field, "decision")
        with self.assertRaises(WordReviewValidationError) as note_error:
            self.service.review_group(
                self.admin.id,
                "佃煮",
                "つくだに",
                "approved",
                "改行\n不可",
            )
        self.assertEqual(note_error.exception.field, "review_note")

    def test_different_reading_is_a_separate_group(self) -> None:
        self._suggest(self.alice, surface="上手", reading="じょうず")
        other = self._suggest(
            self.bob,
            surface="上手",
            reading="うわて",
        )

        self.service.review_group(
            self.admin.id,
            "上手",
            "じょうず",
            "approved",
        )

        with self.database.read_session() as session:
            self.assertEqual(
                session.get(WordSuggestion, other.id).status,
                "pending",
            )
        groups = self.service.list_pending_groups(self.admin.id)
        self.assertEqual(
            [(group.surface, group.reading) for group in groups],
            [("上手", "うわて")],
        )

    def test_recent_review_query_joins_audit_before_reviewer_on_postgres(
        self,
    ) -> None:
        statement = _recent_reviews_statement(50)
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        from_clause = sql[sql.index("FROM word_suggestions") :]
        audit_join = from_clause.index(
            "JOIN word_suggestion_reviews ON"
        )
        reviewer_join = from_clause.index(
            "word_suggestion_reviews.reviewer_user_id"
        )

        self.assertLess(audit_join, reviewer_join)
        self.assertIn(
            "word_suggestion_reviews.suggestion_id = word_suggestions.id",
            from_clause,
        )


if __name__ == "__main__":
    unittest.main()
