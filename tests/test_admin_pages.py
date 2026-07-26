from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from nicegui.elements.button import Button
from nicegui.elements.textarea import Textarea
from nicegui.slot import Slot
from nicegui.testing import user_simulation

from shiritori.admin_pages import (
    _admin_datetime,
    _review_error_message,
    register_admin_pages,
)
from shiritori.auth import (
    Account,
    InvalidSessionError,
    SessionPrincipal,
)
from shiritori.settings import Settings
from shiritori.word_review import (
    PendingSuggestionDetail,
    PendingSuggestionGroup,
    ReviewDecision,
    ReviewedSuggestionView,
    WordReviewConflictError,
    WordReviewNotFoundError,
    WordReviewResult,
    WordReviewValidationError,
)


NOW = datetime(2026, 7, 26, 3, 15, tzinfo=timezone.utc)


def test_settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        direct_database_url="sqlite+pysqlite:///:memory:",
        nicegui_storage_secret="test-storage-secret",
        session_secret="test-session-secret",
    )


def principal(user_id: str = "admin-user") -> SessionPrincipal:
    return SessionPrincipal(
        account=Account(
            id=user_id,
            username="あかとんぼ",
            display_name="あかとんぼ",
            created_at=NOW,
        ),
        session_id="session-1",
        expires_at=NOW + timedelta(days=1),
    )


class FakeAuth:
    def __init__(self, session_principal: SessionPrincipal) -> None:
        self.principal = session_principal
        self.calls = 0

    def authenticate_session(self, token: str) -> SessionPrincipal:
        self.calls += 1
        if token != "valid-session":
            raise InvalidSessionError()
        return self.principal


class FakeWordReview:
    def __init__(self, *, admin: bool = True) -> None:
        self.admin = admin
        self.admin_checks = 0
        self.pending_calls = 0
        self.recent_calls = 0
        self.review_calls: list[tuple[str, str, str, str, str | None]] = []
        self.pending = (
            PendingSuggestionGroup(
                surface="油淋鶏",
                reading="ゆーりんちー",
                submission_count=2,
                first_submitted_at=NOW - timedelta(hours=2),
                last_submitted_at=NOW - timedelta(hours=1),
                submissions=(
                    PendingSuggestionDetail(
                        submitter_display_name="申請者A",
                        note="中華料理の名前です",
                        created_at=NOW - timedelta(hours=2),
                    ),
                    PendingSuggestionDetail(
                        submitter_display_name="申請者B",
                        note=None,
                        created_at=NOW - timedelta(hours=1),
                    ),
                ),
            ),
        )
        self.recent: tuple[ReviewedSuggestionView, ...] = ()

    def is_admin(self, user_id: str) -> bool:
        self.admin_checks += 1
        return self.admin and user_id == "admin-user"

    def list_pending_groups(
        self,
        reviewer_user_id: str,
        *,
        limit: int = 100,
    ) -> tuple[PendingSuggestionGroup, ...]:
        self.pending_calls += 1
        if not self.is_admin(reviewer_user_id):
            raise AssertionError("non-admin queue read")
        return self.pending

    def list_recent_reviews(
        self,
        reviewer_user_id: str,
        *,
        limit: int = 50,
    ) -> tuple[ReviewedSuggestionView, ...]:
        self.recent_calls += 1
        if not self.is_admin(reviewer_user_id):
            raise AssertionError("non-admin audit read")
        return self.recent

    def review_group(
        self,
        reviewer_user_id: str,
        surface: str,
        reading: str,
        decision: ReviewDecision,
        review_note: str | None = None,
    ) -> WordReviewResult:
        if not self.is_admin(reviewer_user_id):
            raise AssertionError("non-admin review")
        self.review_calls.append(
            (
                reviewer_user_id,
                surface,
                reading,
                decision.value,
                review_note,
            )
        )
        self.pending = ()
        self.recent = (
            ReviewedSuggestionView(
                surface=surface,
                reading=reading,
                status=decision.value,
                review_note=review_note,
                reviewed_at=NOW,
                reviewer_display_name="あかとんぼ",
                submitter_display_name="申請者A",
            ),
        )
        return WordReviewResult(
            surface=surface,
            reading=reading,
            decision=decision,
            reviewed_count=2,
            replayed=False,
            approved_word_added=decision is ReviewDecision.APPROVED,
        )


class AdminPageHelperTests(unittest.TestCase):
    def test_datetime_is_presented_in_japan_time(self) -> None:
        self.assertEqual(_admin_datetime(NOW), "2026/07/26 12:15")
        self.assertEqual(
            _admin_datetime(NOW.replace(tzinfo=None)),
            "2026/07/26 12:15",
        )

    def test_known_failures_have_safe_actionable_messages(self) -> None:
        self.assertIn(
            "すでに保存",
            _review_error_message(WordReviewConflictError()),
        )
        self.assertIn(
            "すでに処理",
            _review_error_message(WordReviewNotFoundError()),
        )
        self.assertIn(
            "200文字以内",
            _review_error_message(
                WordReviewValidationError("review_note", "raw detail")
            ),
        )
        self.assertNotIn(
            "raw detail",
            _review_error_message(
                WordReviewValidationError("review_note", "raw detail")
            ),
        )

    def test_admin_css_preserves_mobile_layout_and_touch_targets(self) -> None:
        css = (
            Path(__file__).parent.parent / "assets" / "admin_pages.css"
        ).read_text(encoding="utf-8")
        self.assertIn("@media (max-width: 820px)", css)
        self.assertIn("@media (max-width: 520px)", css)
        self.assertIn("min-height: 44px", css)


class AdminPageSimulationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        Slot.stacks.clear()

    async def test_anonymous_user_is_sent_to_login(self) -> None:
        auth = FakeAuth(principal())
        review = FakeWordReview()
        async with user_simulation() as user:
            register_admin_pages(
                auth=auth,
                settings=test_settings(),
                word_review=review,
            )
            response = await user.http_client.get(
                "/admin/word-suggestions",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/login?next=/admin/word-suggestions",
        )
        self.assertEqual(review.admin_checks, 0)

    async def test_non_admin_is_returned_to_lobby_without_queue_data(
        self,
    ) -> None:
        auth = FakeAuth(principal())
        review = FakeWordReview(admin=False)
        async with user_simulation() as user:
            register_admin_pages(
                auth=auth,
                settings=test_settings(),
                word_review=review,
            )
            user.http_client.cookies.set(
                "siritori_session",
                "valid-session",
            )
            response = await user.http_client.get(
                "/admin/word-suggestions",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/lobby")
        self.assertEqual(review.pending_calls, 0)
        self.assertEqual(review.recent_calls, 0)

    async def test_admin_can_review_group_after_fresh_authorization(
        self,
    ) -> None:
        auth = FakeAuth(principal())
        review = FakeWordReview()
        async with user_simulation() as user:
            register_admin_pages(
                auth=auth,
                settings=test_settings(),
                word_review=review,
            )
            user.http_client.cookies.set(
                "siritori_session",
                "valid-session",
            )
            await user.open("/admin/word-suggestions")
            await user.should_see("単語申請の審査")
            await user.should_see("油淋鶏")
            await user.should_see("2件の申請")
            await user.should_see("申請者A")
            await user.should_see("中華料理の名前です")
            await user.should_not_see("admin-user")

            user.find(kind=Textarea).type("料理名として確認")
            user.find(
                kind=Button,
                content="承認",
            ).click()
            await user.should_see("「油淋鶏」を承認しました（2件）。")
            await user.should_see("現在、審査待ちの申請はありません。")
            await user.should_see("承認：油淋鶏（ゆーりんちー）")

        self.assertEqual(
            review.review_calls,
            [
                (
                    "admin-user",
                    "油淋鶏",
                    "ゆーりんちー",
                    "approved",
                    "料理名として確認",
                )
            ],
        )
        self.assertGreaterEqual(auth.calls, 2)
        self.assertGreaterEqual(review.admin_checks, 4)


if __name__ == "__main__":
    unittest.main()
