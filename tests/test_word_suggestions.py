"""Tests for review-only user dictionary suggestions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib import import_module
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from argon2 import PasswordHasher
from sqlalchemy import func, inspect, select

from shiritori.auth import AuthService
from shiritori.database import Database
from shiritori.models import User, WordSuggestion
from shiritori.word_suggestions import (
    WordSuggestionPendingLimitError,
    WordSuggestionService,
    WordSuggestionUserUnavailableError,
    WordSuggestionValidationError,
)


UTC = timezone.utc
BASE_TIME = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self) -> None:
        self.current = BASE_TIME

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int = 1) -> None:
        self.current += timedelta(seconds=seconds)


class WordSuggestionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        path = Path(self.temporary_directory.name, "suggestions.sqlite3")
        self.database = Database(
            f"sqlite+pysqlite:///{path.as_posix()}"
        )
        self.database.create_schema_for_testing()
        hasher = PasswordHasher(
            time_cost=1,
            memory_cost=8 * 1024,
            parallelism=1,
            hash_len=16,
            salt_len=16,
        )
        auth = AuthService(self.database, password_hasher=hasher)
        self.alice = auth.register(
            "suggestion-alice",
            "alice-password-123",
            display_name="Alice",
        )
        self.bob = auth.register(
            "suggestion-bob",
            "bob-password-123",
            display_name="Bob",
        )
        self.clock = MutableClock()
        self.service = WordSuggestionService(
            self.database,
            clock=self.clock,
        )

    def tearDown(self) -> None:
        self.database.dispose()
        self.temporary_directory.cleanup()

    def test_submit_normalizes_and_returns_ui_safe_pending_view(self) -> None:
        result = self.service.submit(
            self.alice.id,
            "  ﾕｰﾘﾝﾁｰ  ",
            "  ゆーりんちー  ",
            "  食べ物テーマで使用したい  ",
        )

        self.assertFalse(result.replayed)
        self.assertEqual(result.suggestion.surface, "ユーリンチー")
        self.assertEqual(result.suggestion.reading, "ゆーりんちー")
        self.assertEqual(
            result.suggestion.note,
            "食べ物テーマで使用したい",
        )
        self.assertEqual(result.suggestion.status, "pending")
        self.assertEqual(result.suggestion.created_at, BASE_TIME)
        self.assertIsNone(result.suggestion.reviewed_at)
        self.assertNotIn(
            "id",
            result.suggestion.__dataclass_fields__,
        )
        self.assertNotIn(
            "user_id",
            result.suggestion.__dataclass_fields__,
        )

        with self.database.read_session() as session:
            stored = session.scalar(select(WordSuggestion))
            self.assertIsNotNone(stored)
            self.assertEqual(stored.status, "pending")
            self.assertIsNone(stored.reviewed_at)

    def test_exact_normalized_duplicate_is_idempotent(self) -> None:
        first = self.service.submit(
            self.alice.id,
            "佃煮",
            "つくだに",
            "最初の補足",
        )
        self.clock.advance()
        duplicate = self.service.submit(
            self.alice.id,
            "  佃煮  ",
            "  つくだに  ",
            "後から異なる補足",
        )

        self.assertFalse(first.replayed)
        self.assertTrue(duplicate.replayed)
        self.assertEqual(duplicate.suggestion, first.suggestion)
        self.assertEqual(duplicate.suggestion.note, "最初の補足")
        with self.database.read_session() as session:
            count = session.scalar(
                select(func.count()).select_from(WordSuggestion)
            )
        self.assertEqual(count, 1)

    def test_same_word_with_another_reading_or_user_is_distinct(self) -> None:
        self.service.submit(self.alice.id, "日本", "にほん")
        self.clock.advance()
        self.service.submit(self.alice.id, "日本", "にっぽん")
        self.clock.advance()
        self.service.submit(self.bob.id, "日本", "にほん")

        self.assertEqual(len(self.service.list_mine(self.alice.id)), 2)
        self.assertEqual(len(self.service.list_mine(self.bob.id)), 1)

    def test_list_is_own_only_newest_first_and_bounded(self) -> None:
        self.service.submit(self.alice.id, "蚊", "か")
        self.clock.advance()
        self.service.submit(self.bob.id, "油淋鶏", "ゆーりんちー")
        self.clock.advance()
        self.service.submit(self.alice.id, "湯豆腐", "ゆどうふ")

        own = self.service.list_mine(self.alice.id)
        self.assertEqual(
            tuple(item.surface for item in own),
            ("湯豆腐", "蚊"),
        )
        self.assertEqual(
            tuple(item.surface for item in self.service.list_mine(
                self.alice.id,
                limit=1,
            )),
            ("湯豆腐",),
        )
        for invalid in (0, 101, True):
            with self.subTest(limit=invalid):
                with self.assertRaises(ValueError):
                    self.service.list_mine(
                        self.alice.id,
                        limit=invalid,
                    )

    def test_pending_cap_does_not_block_replay_and_frees_after_review(self) -> None:
        service = WordSuggestionService(
            self.database,
            max_pending_per_user=2,
            clock=self.clock,
        )
        first = service.submit(self.alice.id, "佃煮", "つくだに")
        self.clock.advance()
        service.submit(self.alice.id, "湯豆腐", "ゆどうふ")

        with self.assertRaises(WordSuggestionPendingLimitError) as raised:
            service.submit(self.alice.id, "油淋鶏", "ゆーりんちー")
        self.assertEqual(raised.exception.limit, 2)
        self.assertTrue(
            service.submit(
                self.alice.id,
                "佃煮",
                "つくだに",
            ).replayed
        )

        with self.database.transaction() as session:
            stored = session.scalar(
                select(WordSuggestion).where(
                    WordSuggestion.user_id == self.alice.id,
                    WordSuggestion.surface == first.suggestion.surface,
                )
            )
            stored.status = "approved"
            stored.reviewed_at = self.clock.current
            stored.updated_at = self.clock.current

        created = service.submit(
            self.alice.id,
            "油淋鶏",
            "ゆーりんちー",
        )
        self.assertFalse(created.replayed)

    def test_missing_and_disabled_users_fail_for_submit_and_list(self) -> None:
        missing = str(uuid4())
        for operation in (
            lambda: self.service.submit(missing, "林檎", "りんご"),
            lambda: self.service.list_mine(missing),
        ):
            with self.assertRaises(WordSuggestionUserUnavailableError):
                operation()

        with self.database.transaction() as session:
            session.get(User, self.alice.id).disabled_at = BASE_TIME
        for operation in (
            lambda: self.service.submit(
                self.alice.id,
                "林檎",
                "りんご",
            ),
            lambda: self.service.list_mine(self.alice.id),
        ):
            with self.assertRaises(WordSuggestionUserUnavailableError):
                operation()

    def test_surface_and_reading_reject_non_word_content(self) -> None:
        invalid_surfaces = (
            None,
            "",
            "あ",
            "ユー リン",
            "🍎",
            "apple",
            "りんご1",
            "・りんご",
            "りんご・",
            "あ" * 31,
        )
        for surface in invalid_surfaces:
            with self.subTest(surface=surface):
                with self.assertRaises(WordSuggestionValidationError):
                    self.service.submit(
                        self.alice.id,
                        surface,
                        "りんご",
                    )

        invalid_readings = (
            None,
            "",
            "リンゴ",
            "りん ご",
            "🍎",
            "apple",
            "ーりんご",
            "りんご。",
            "あ" * 61,
        )
        for reading in invalid_readings:
            with self.subTest(reading=reading):
                with self.assertRaises(WordSuggestionValidationError):
                    self.service.submit(
                        self.alice.id,
                        "林檎",
                        reading,
                    )

    def test_non_string_validation_uses_stable_field_keys(self) -> None:
        cases = (
            (
                "surface",
                lambda: self.service.submit(
                    self.alice.id, 123, "りんご"
                ),
            ),
            (
                "reading",
                lambda: self.service.submit(
                    self.alice.id, "林檎", 123
                ),
            ),
            (
                "note",
                lambda: self.service.submit(
                    self.alice.id, "林檎", "りんご", 123
                ),
            ),
        )
        for expected_field, operation in cases:
            with self.subTest(field=expected_field):
                with self.assertRaises(
                    WordSuggestionValidationError
                ) as raised:
                    operation()
                self.assertEqual(raised.exception.field, expected_field)

    def test_note_is_optional_short_and_single_line(self) -> None:
        empty = self.service.submit(
            self.alice.id,
            "林檎",
            "りんご",
            "   ",
        )
        self.assertIsNone(empty.suggestion.note)

        for note in (
            "あ" * 201,
            "一行目\n二行目",
            "一行目\u2028二行目",
            "一段落目\u2029二段落目",
            "補足\u200b",
        ):
            with self.subTest(note=note[:10]):
                with self.assertRaises(
                    WordSuggestionValidationError
                ) as raised:
                    self.service.submit(
                        self.alice.id,
                        "蜜柑",
                        "みかん",
                        note,
                    )
                self.assertEqual(raised.exception.field, "note")


class WordSuggestionMigrationTests(unittest.TestCase):
    def test_sqlite_upgrade_check_and_downgrade(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "suggestions-migration.sqlite3")
            url = f"sqlite+pysqlite:///{path.as_posix()}"
            previous = os.environ.get("DIRECT_DATABASE_URL")
            os.environ["DIRECT_DATABASE_URL"] = url
            try:
                config = Config(str(root / "alembic.ini"))
                command.upgrade(config, "head")
                command.check(config)
                database = Database(url)
                try:
                    schema = inspect(database.engine)
                    self.assertIn(
                        "word_suggestions",
                        schema.get_table_names(),
                    )
                    self.assertEqual(
                        {
                            column["name"]
                            for column in schema.get_columns(
                                "word_suggestions"
                            )
                        },
                        {
                            "id",
                            "user_id",
                            "surface",
                            "reading",
                            "note",
                            "status",
                            "created_at",
                            "updated_at",
                            "reviewed_at",
                        },
                    )
                    indexes = {
                        index["name"]
                        for index in schema.get_indexes(
                            "word_suggestions"
                        )
                    }
                    self.assertTrue(
                        {
                            "ix_word_suggestions_user_created",
                            "ix_word_suggestions_review_queue",
                        }.issubset(indexes)
                    )
                finally:
                    database.dispose()

                command.downgrade(config, "0005_room_current_game")
                downgraded = Database(url)
                try:
                    schema = inspect(downgraded.engine)
                    self.assertNotIn(
                        "word_suggestions",
                        schema.get_table_names(),
                    )
                    self.assertIn("rooms", schema.get_table_names())
                finally:
                    downgraded.dispose()
            finally:
                if previous is None:
                    os.environ.pop("DIRECT_DATABASE_URL", None)
                else:
                    os.environ["DIRECT_DATABASE_URL"] = previous

    def test_postgresql_round_trip_ddl_compiles(self) -> None:
        migration = import_module(
            "migrations.versions.0006_word_suggestions"
        )
        output = StringIO()
        context = MigrationContext.configure(
            url="postgresql://",
            opts={"as_sql": True, "output_buffer": output},
        )
        operations = Operations(context)
        original_op = migration.op
        migration.op = operations
        try:
            migration.upgrade()
            migration.downgrade()
        finally:
            migration.op = original_op

        ddl = output.getvalue().lower()
        self.assertIn("create table word_suggestions", ddl)
        self.assertIn(
            "fk_word_suggestions_user_id_users",
            ddl,
        )
        self.assertIn(
            "uq_word_suggestions_user_surface_reading",
            ddl,
        )
        self.assertIn(
            "ix_word_suggestions_review_queue",
            ddl,
        )
        self.assertIn("drop table word_suggestions", ddl)


if __name__ == "__main__":
    unittest.main()
