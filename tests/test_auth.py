from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from argon2 import PasswordHasher
from sqlalchemy import select

from shiritori.auth import (
    AuthService,
    GENERIC_LOGIN_ERROR,
    InvalidCredentialsError,
    InvalidRegistrationError,
    InvalidSessionError,
    UsernameUnavailableError,
    session_token_hash,
)
from shiritori.database import Database
from shiritori.models import LoginSession, User


class AuthServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name, "auth.sqlite3")
        self.database = Database(
            f"sqlite+pysqlite:///{database_path.as_posix()}"
        )
        self.database.create_schema_for_testing()
        # Keep unit tests quick while exercising the same Argon2id algorithm.
        self.hasher = PasswordHasher(
            time_cost=1,
            memory_cost=8 * 1024,
            parallelism=1,
            hash_len=16,
            salt_len=16,
        )
        self.auth = AuthService(self.database, password_hasher=self.hasher)

    def tearDown(self) -> None:
        self.database.dispose()
        self.temporary_directory.cleanup()

    def test_registration_stores_argon2id_and_casefolded_unique_key(self) -> None:
        account = self.auth.register(
            "Player_One", "correct-horse-123", display_name="プレイヤー1"
        )

        with self.database.read_session() as session:
            user = session.get(User, account.id)
            self.assertIsNotNone(user)
            assert user is not None
            self.assertEqual(user.username_key, "player_one")
            self.assertTrue(user.password_hash.startswith("$argon2id$"))
            self.assertNotIn("correct-horse-123", user.password_hash)

        with self.assertRaises(UsernameUnavailableError):
            self.auth.register("ＰＬＡＹＥＲ＿ＯＮＥ", "another-password-456")

    def test_casefold_expansion_cannot_exceed_database_key_limit(self) -> None:
        expanding_username = "\u0390" * 22

        with self.assertRaises(InvalidRegistrationError):
            self.auth.register(expanding_username, "safe-password-123")
        with self.assertRaises(InvalidCredentialsError):
            self.auth.login(expanding_username, "safe-password-123")

    def test_display_names_resolve_bounded_unique_account_ids(self) -> None:
        alice = self.auth.register(
            "alice_names",
            "alice-display-password",
            display_name="ありす",
        )
        bob = self.auth.register(
            "bob_names",
            "bob-display-password",
            display_name="ボブ",
        )
        with self.database.transaction() as session:
            stored_bob = session.get(User, bob.id)
            assert stored_bob is not None
            stored_bob.display_name = None

        self.assertEqual(
            self.auth.display_names_for_user_ids(
                (alice.id, bob.id, alice.id, "0" * 36)
            ),
            {
                alice.id: "ありす",
                bob.id: "bob_names",
            },
        )
        self.assertEqual(self.auth.display_names_for_user_ids(()), {})

        invalid_sets: tuple[object, ...] = (
            ("",),
            (None,),
            ("x" * 37,),
            tuple(f"{index:036d}" for index in range(65)),
        )
        for invalid in invalid_sets:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.auth.display_names_for_user_ids(invalid)  # type: ignore[arg-type]

    def test_unknown_user_and_wrong_password_have_same_public_failure(self) -> None:
        self.auth.register("alice", "alice-password-123")

        messages = []
        for username, password in (
            ("alice", "definitely-wrong"),
            ("missing", "alice-password-123"),
            ("?", ""),
        ):
            with self.assertRaises(InvalidCredentialsError) as caught:
                self.auth.login(username, password)
            messages.append(str(caught.exception))

        self.assertEqual(messages, [GENERIC_LOGIN_ERROR] * 3)

    def test_login_returns_opaque_token_and_database_keeps_only_sha256(self) -> None:
        account = self.auth.register("bob", "bob-password-123")
        issued_at = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)
        issued = self.auth.login("BOB", "bob-password-123", now=issued_at)

        self.assertEqual(issued.account.id, account.id)
        self.assertTrue(issued.token.startswith("srt_"))
        with self.database.read_session() as session:
            stored = session.scalar(
                select(LoginSession).where(LoginSession.id == issued.session_id)
            )
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored.token_hash, session_token_hash(issued.token))
            self.assertNotEqual(stored.token_hash, issued.token)
            self.assertEqual(len(stored.token_hash), 64)

        principal = self.auth.authenticate_session(
            issued.token, now=issued_at + timedelta(minutes=5)
        )
        self.assertEqual(principal.account.id, account.id)
        self.assertEqual(principal.session_id, issued.session_id)

    def test_session_expiry_is_persisted_and_logout_is_idempotent(self) -> None:
        self.auth.register("carol", "carol-password-123")
        start = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)
        expiring = self.auth.login(
            "carol",
            "carol-password-123",
            now=start,
            ttl=timedelta(seconds=30),
        )
        with self.assertRaises(InvalidSessionError):
            self.auth.authenticate_session(
                expiring.token, now=start + timedelta(seconds=30)
            )

        with self.database.read_session() as session:
            stored = session.get(LoginSession, expiring.session_id)
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertIsNotNone(stored.revoked_at)

        active = self.auth.login(
            "carol", "carol-password-123", now=start + timedelta(minutes=1)
        )
        self.assertTrue(self.auth.logout(active.token))
        self.assertFalse(self.auth.logout(active.token))
        with self.assertRaises(InvalidSessionError):
            self.auth.authenticate_session(active.token)

    def test_explicit_zero_session_ttl_is_rejected(self) -> None:
        account = self.auth.register("shortttl", "short-ttl-password")
        with self.assertRaises(ValueError):
            self.auth.login(
                "shortttl", "short-ttl-password", ttl=timedelta(0)
            )
        with self.assertRaises(ValueError):
            self.auth.issue_session(account.id, ttl=timedelta(0))

    def test_password_change_revokes_existing_sessions(self) -> None:
        account = self.auth.register("dave", "old-password-123")
        issued = self.auth.login("dave", "old-password-123")

        self.auth.change_password(
            account.id, "old-password-123", "new-password-456"
        )

        with self.assertRaises(InvalidSessionError):
            self.auth.authenticate_session(issued.token)
        with self.assertRaises(InvalidCredentialsError):
            self.auth.login("dave", "old-password-123")
        replacement = self.auth.login("dave", "new-password-456")
        self.assertEqual(replacement.account.id, account.id)


if __name__ == "__main__":
    unittest.main()
