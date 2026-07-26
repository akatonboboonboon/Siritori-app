from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock
import time
import unittest

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse

from shiritori.settings import Settings
from shiritori.web_auth import (
    CsrfProtector,
    LoginAttemptLimiter,
    PasswordWorkLimiter,
    _DeadlinePresentation,
    _VersionedFeedback,
    _auth_rate_limit_keys,
    _deadline_presentation,
    _feedback_for_version,
    _read_form,
    _safe_next,
    _same_origin,
    _set_session_cookie,
    _solo_difficulty_options,
)


def request_with_headers(
    *headers: tuple[str, str],
    scheme: str = "https",
    client_host: str = "127.0.0.1",
) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": scheme,
            "server": ("example.test", 443),
            "path": "/auth/login",
            "query_string": b"",
            "headers": [
                (name.lower().encode(), value.encode()) for name, value in headers
            ],
            "client": (client_host, 12345),
        }
    )


def streaming_form_request(
    chunks: list[bytes], *, content_length: str | None = None
) -> tuple[Request, dict[str, int]]:
    headers = [(b"content-type", b"application/x-www-form-urlencoded")]
    if content_length is not None:
        headers.append((b"content-length", content_length.encode("ascii")))
    state = {"receive_calls": 0}
    frames = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ] or [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        call = state["receive_calls"]
        state["receive_calls"] += 1
        if call < len(frames):
            return frames[call]
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("example.test", 443),
            "path": "/auth/login",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
        },
        receive,
    )
    return request, state


class CsrfProtectorTests(unittest.TestCase):
    def test_token_is_subject_bound_and_expires(self) -> None:
        protector = CsrfProtector("s" * 32, lifetime_seconds=120)
        token = protector.issue("login", now=100)

        self.assertTrue(protector.verify(token, "login", now=220))
        self.assertFalse(protector.verify(token, "logout", now=101))
        self.assertFalse(protector.verify(token, "login", now=221))
        self.assertFalse(protector.verify("broken", "login", now=101))


class GameUiHelperTests(unittest.TestCase):
    def test_deadline_presentation_uses_ceil_and_warning_thresholds(self) -> None:
        now = datetime(2026, 7, 26, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(
            _deadline_presentation(None, now=now),
            _DeadlinePresentation(
                text="残り時間: 無制限",
                level="normal",
                expired=False,
            ),
        )
        self.assertEqual(
            _deadline_presentation(
                now + timedelta(seconds=10, milliseconds=1),
                now=now,
            ),
            _DeadlinePresentation(
                text="残り時間: 11秒",
                level="normal",
                expired=False,
            ),
        )
        self.assertEqual(
            _deadline_presentation(
                now + timedelta(seconds=9, milliseconds=1),
                now=now,
            ),
            _DeadlinePresentation(
                text="残り時間: 10秒",
                level="warning",
                expired=False,
            ),
        )
        self.assertEqual(
            _deadline_presentation(
                now + timedelta(seconds=4, milliseconds=1),
                now=now,
            ),
            _DeadlinePresentation(
                text="残り時間: 5秒",
                level="danger",
                expired=False,
            ),
        )
        self.assertEqual(
            _deadline_presentation(now, now=now),
            _DeadlinePresentation(
                text="残り時間: 0秒",
                level="danger",
                expired=True,
            ),
        )

    def test_transient_feedback_is_bound_to_one_state_version(self) -> None:
        feedback = _VersionedFeedback(7, "辞書にない単語です。")

        self.assertEqual(
            _feedback_for_version(feedback, 7),
            "辞書にない単語です。",
        )
        self.assertIsNone(_feedback_for_version(feedback, 8))
        self.assertIsNone(_feedback_for_version(None, 7))

    def test_solo_ui_exposes_all_three_bot_difficulties(self) -> None:
        self.assertEqual(
            _solo_difficulty_options(),
            {
                "easy": "やさしい",
                "normal": "ふつう",
                "hard": "むずかしい",
            },
        )


class LoginAttemptLimiterTests(unittest.TestCase):
    def test_limit_is_bounded_per_key_and_resets(self) -> None:
        limiter = LoginAttemptLimiter(attempts=2, window_seconds=10)

        self.assertTrue(limiter.allow("a", now=0))
        self.assertTrue(limiter.allow("a", now=1))
        self.assertFalse(limiter.allow("a", now=2))
        self.assertTrue(limiter.allow("b", now=2))
        self.assertTrue(limiter.allow("a", now=11))
        limiter.reset("a")
        self.assertTrue(limiter.allow("a", now=11))

    def test_key_storage_has_a_hard_lru_cap(self) -> None:
        limiter = LoginAttemptLimiter(
            attempts=2, window_seconds=60, max_keys=3
        )

        for index in range(20):
            self.assertTrue(limiter.allow(f"key-{index}", now=float(index)))

        self.assertEqual(limiter.tracked_key_count, 3)

    def test_concurrent_calls_cannot_exceed_attempt_limit(self) -> None:
        limiter = LoginAttemptLimiter(
            attempts=4, window_seconds=60, max_keys=8
        )
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(
                pool.map(
                    lambda _: limiter.allow("shared", now=1.0),
                    range(32),
                )
            )

        self.assertEqual(sum(results), 4)
        self.assertEqual(limiter.tracked_key_count, 1)


class PasswordWorkLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_argon2_work_has_a_small_concurrency_ceiling(self) -> None:
        limiter = PasswordWorkLimiter(max_concurrency=2)
        state_lock = Lock()
        active = 0
        peak = 0

        def work(value: int) -> int:
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with state_lock:
                active -= 1
            return value

        results = await asyncio.gather(
            *(limiter.run(work, value) for value in range(8))
        )

        self.assertEqual(results, list(range(8)))
        self.assertEqual(peak, 2)


class FormReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_content_length_rejects_before_reading_body(self) -> None:
        request, state = streaming_form_request(
            [b"ignored"], content_length="8193"
        )

        with self.assertRaises(HTTPException) as caught:
            await _read_form(request)

        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(state["receive_calls"], 0)

    async def test_stream_stops_as_soon_as_body_exceeds_limit(self) -> None:
        request, state = streaming_form_request(
            [b"a" * 4096, b"b" * 4097, b"never-read"]
        )

        with self.assertRaises(HTTPException) as caught:
            await _read_form(request)

        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(state["receive_calls"], 2)

    async def test_invalid_length_and_too_many_fields_are_bad_requests(self) -> None:
        invalid_length, _ = streaming_form_request(
            [b"a=b"], content_length="not-a-number"
        )
        with self.assertRaises(HTTPException) as length_error:
            await _read_form(invalid_length)
        self.assertEqual(length_error.exception.status_code, 400)

        encoded = "&".join(f"field{index}=x" for index in range(13)).encode()
        too_many_fields, _ = streaming_form_request([encoded])
        with self.assertRaises(HTTPException) as fields_error:
            await _read_form(too_many_fields)
        self.assertEqual(fields_error.exception.status_code, 400)

    async def test_valid_streamed_form_is_parsed(self) -> None:
        request, _ = streaming_form_request(
            [b"username=Ali", b"ce&password=safe-password"]
        )

        values = await _read_form(request)

        self.assertEqual(values["username"], "Alice")
        self.assertEqual(values["password"], "safe-password")


class RequestSecurityTests(unittest.TestCase):
    def test_same_origin_uses_host_and_render_forwarded_proto(self) -> None:
        request = request_with_headers(
            ("host", "siritori.example"),
            ("origin", "https://siritori.example"),
            ("x-forwarded-host", "attacker.example"),
            ("x-forwarded-proto", "https"),
        )
        self.assertTrue(_same_origin(request))

        forged_forwarded_host = request_with_headers(
            ("host", "internal:10000"),
            ("origin", "https://siritori.example"),
            ("x-forwarded-host", "siritori.example"),
            ("x-forwarded-proto", "https"),
        )
        self.assertFalse(_same_origin(forged_forwarded_host))

        hostile = request_with_headers(
            ("host", "siritori.example"),
            ("origin", "https://attacker.example"),
        )
        self.assertFalse(_same_origin(hostile))

    def test_rate_keys_share_nfkc_casefolded_account_but_not_ip(self) -> None:
        first = request_with_headers(
            ("host", "example.test"), client_host="192.0.2.1"
        )
        second = request_with_headers(
            ("host", "example.test"), client_host="192.0.2.2"
        )

        first_ip, first_account = _auth_rate_limit_keys(
            first, "  ＰＬＡＹＥＲ  "
        )
        second_ip, second_account = _auth_rate_limit_keys(
            second, "player"
        )

        self.assertNotEqual(first_ip, second_ip)
        self.assertEqual(first_account, second_account)
        self.assertLessEqual(len(first_account), 72)

    def test_next_path_cannot_be_an_open_redirect(self) -> None:
        self.assertEqual(_safe_next("/rooms/ABC"), "/rooms/ABC")
        self.assertEqual(_safe_next("//attacker.example"), "/lobby")
        self.assertEqual(_safe_next("/\\attacker.example"), "/lobby")
        self.assertEqual(_safe_next("https://attacker.example"), "/lobby")
        self.assertEqual(_safe_next("/ok\r\nLocation: x"), "/lobby")

    def test_production_cookie_is_secure_httponly_and_lax(self) -> None:
        settings = Settings.from_environment(
            {
                "APP_ENV": "production",
                "DATABASE_URL": "postgresql://pooled",
                "DIRECT_DATABASE_URL": "postgresql://direct",
                "NICEGUI_STORAGE_SECRET": "N7!ceGUI-Storage_2026:aB3dE5fG8hJ",
                "SESSION_SECRET": "Sess10n-Key_2026:Zx9Yw8Vu7Ts6Rq5P",
            }
        )
        response = RedirectResponse("/lobby")
        _set_session_cookie(
            response,
            "opaque-token",
            datetime.now(timezone.utc) + timedelta(hours=1),
            settings,
        )

        cookie = response.headers["set-cookie"]
        self.assertIn("siritori_session=opaque-token", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertIn("Path=/", cookie)


if __name__ == "__main__":
    unittest.main()
