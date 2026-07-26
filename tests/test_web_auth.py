from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
import time
from types import SimpleNamespace
import unittest

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse

from shiritori.auth import Account, SessionPrincipal
from shiritori.lobby import LobbyStateError
from shiritori.rooms import (
    RoomMode,
    Role,
    RoomReaction,
    RoomStatus,
    SeatController,
    TurnRecord,
    create_room_snapshot,
)
from shiritori.settings import Settings
from shiritori.web_auth import (
    CsrfProtector,
    LoginAttemptLimiter,
    PasswordWorkLimiter,
    _DeadlinePresentation,
    _MatchResultPresentation,
    _SnapshotEffect,
    _VersionedFeedback,
    _auth_rate_limit_keys,
    _invite_rate_limit_keys,
    _can_surrender,
    _deadline_presentation,
    _feedback_for_version,
    _match_result_presentation,
    _post_match_lobby_destination,
    _reaction_sender_label,
    _read_form,
    _room_invite_url,
    _room_listing_summary,
    _room_timer_text,
    _safe_next,
    _same_origin,
    _session_principal_matches_user,
    _snapshot_effect,
    _sound_cue_script,
    _set_session_cookie,
    _solo_difficulty_options,
    _word_suggestion_status_label,
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

    def test_session_principal_must_still_own_the_play_page(self) -> None:
        now = datetime(2026, 7, 26, 0, 0, tzinfo=timezone.utc)
        principal = SessionPrincipal(
            account=Account(
                id="alice",
                username="alice",
                display_name="Alice",
                created_at=now,
            ),
            session_id="session-alice",
            expires_at=now + timedelta(hours=1),
        )

        self.assertTrue(
            _session_principal_matches_user(principal, "alice")
        )
        self.assertFalse(
            _session_principal_matches_user(principal, "bob")
        )
        self.assertFalse(_session_principal_matches_user(None, "alice"))

    def test_word_suggestion_status_labels_are_japanese(self) -> None:
        self.assertEqual(
            _word_suggestion_status_label("pending"), "審査待ち"
        )
        self.assertEqual(
            _word_suggestion_status_label("approved"), "承認済み"
        )
        self.assertEqual(
            _word_suggestion_status_label("rejected"), "見送り"
        )
        self.assertEqual(_word_suggestion_status_label("other"), "確認中")

    def test_public_room_summary_exposes_settings_but_not_user_ids(self) -> None:
        room = SimpleNamespace(
            players=(
                SimpleNamespace(user_id="secret-alice-id"),
                SimpleNamespace(user_id="secret-bob-id"),
            ),
            max_players=4,
            turn_seconds=30,
            fill_empty_seats_with_bots=True,
            allow_spectators=False,
        )

        summary = _room_listing_summary(room)

        self.assertEqual(
            summary,
            "対戦参加 2/4人・30秒・不足分はNormal Bot・観戦不可",
        )
        self.assertNotIn("secret-alice-id", summary)
        self.assertNotIn("secret-bob-id", summary)
        self.assertEqual(_room_timer_text(None), "無制限")

    def test_invite_url_is_absolute_and_same_origin(self) -> None:
        request = request_with_headers(
            ("host", "siritori.example"),
            scheme="https",
        )

        self.assertEqual(
            _room_invite_url(request, "ABC234"),
            "https://siritori.example/join/ABC234",
        )

    def test_surrender_is_available_to_temporary_bot_seat_owner(self) -> None:
        snapshot = create_room_snapshot(
            "game",
            ("alice", "bob", "carol"),
            mode=RoomMode.PVP,
        )
        temporary_bot = replace(
            snapshot.players[0],
            controller=SeatController.BOT,
        )
        taken_over = replace(
            snapshot,
            players=(temporary_bot, *snapshot.players[1:]),
        )

        self.assertTrue(_can_surrender(taken_over, "alice"))
        self.assertFalse(_can_surrender(taken_over, "spectator"))

        eliminated = replace(
            taken_over,
            current_turn=1,
            eliminated_seats=(0,),
        )
        self.assertFalse(_can_surrender(eliminated, "alice"))

        finished = replace(
            taken_over,
            status=RoomStatus.FINISHED,
            current_turn=2,
            eliminated_seats=(0, 1),
            losing_seat=0,
            end_reason="surrender",
        )
        self.assertFalse(_can_surrender(finished, "alice"))

    def test_post_match_destination_follows_newer_active_round(self) -> None:
        room = SimpleNamespace(room_code="ABC234")

        class StartedNextRound:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, str]] = []

            def return_to_waiting(
                self,
                user_id: str,
                game_id: str,
            ) -> object:
                self.calls.append(("return", user_id, game_id))
                raise LobbyStateError("game is not the room's current round")

            def open_room_for_game(
                self,
                user_id: str,
                game_id: str,
            ) -> object:
                self.calls.append(("lookup", user_id, game_id))
                return room

        lobby = StartedNextRound()
        self.assertIs(
            _post_match_lobby_destination(
                lobby,  # type: ignore[arg-type]
                "watcher",
                "finished-round",
            ),
            room,
        )
        self.assertEqual(
            lobby.calls,
            [
                ("return", "watcher", "finished-round"),
                ("lookup", "watcher", "finished-round"),
            ],
        )


class GameResultUiHelperTests(unittest.TestCase):
    def test_result_card_is_personalized_without_private_ids(self) -> None:
        now = datetime(2026, 7, 26, 1, 0, tzinfo=timezone.utc)
        active = create_room_snapshot(
            "result-game",
            ("secret-alice-id", "secret-bob-id"),
            mode=RoomMode.PVP,
            spectators=("secret-watcher-id",),
            now=now,
            seat_picker=lambda _count: 0,
        )
        record = TurnRecord(
            surface="りんご",
            reading="りんご",
            canonical_key="りんご",
            seat_index=0,
            actor_user_id="secret-alice-id",
            by_bot=False,
            submitted_at=now,
        )
        finished = replace(
            active,
            status=RoomStatus.FINISHED,
            state_version=3,
            current_turn=0,
            eliminated_seats=(1,),
            history=(record,),
            losing_seat=1,
            end_reason="timeout",
            deadline_at=None,
        )

        winner = _match_result_presentation(
            finished, "secret-alice-id"
        )
        loser = _match_result_presentation(
            finished, "secret-bob-id"
        )
        spectator = _match_result_presentation(
            finished, "secret-watcher-id"
        )

        self.assertEqual(
            winner,
            _MatchResultPresentation(
                title="勝利！",
                tone="victory",
                outcome="あなたが最後まで勝ち残りました。",
                accepted_word_count=1,
                end_reason="時間切れ",
                round_summary="2人で開始・1人脱落",
                last_word="りんご（りんご）",
            ),
        )
        self.assertEqual(loser.title, "今回は敗北")
        self.assertEqual(loser.tone, "defeat")
        self.assertEqual(loser.outcome, "プレイヤー1の勝ちです。")
        self.assertEqual(spectator.title, "対局終了")
        self.assertEqual(spectator.tone, "neutral")
        self.assertEqual(
            spectator.outcome,
            "プレイヤー1の勝ちです。",
        )
        combined = f"{winner!r}{loser!r}{spectator!r}"
        self.assertNotIn("secret-alice-id", combined)
        self.assertNotIn("secret-bob-id", combined)
        self.assertNotIn("secret-watcher-id", combined)

    def test_solo_result_names_a_permanent_bot(self) -> None:
        active = create_room_snapshot(
            "solo-result",
            ("private-user-id",),
            mode=RoomMode.SOLO_BOT,
            permanent_bot_count=1,
            seat_picker=lambda _count: 0,
        )
        finished = replace(
            active,
            status=RoomStatus.FINISHED,
            state_version=1,
            current_turn=1,
            eliminated_seats=(0,),
            losing_seat=0,
            end_reason="ends_with_n",
            deadline_at=None,
        )

        result = _match_result_presentation(
            finished, "private-user-id"
        )

        self.assertEqual(result.title, "今回は敗北")
        self.assertEqual(result.outcome, "Bot 2の勝ちです。")
        self.assertEqual(result.round_summary, "あなたとBot 1体で対戦")
        self.assertEqual(result.last_word, "なし")
        self.assertNotIn("private-user-id", repr(result))

    def test_result_card_rejects_an_active_snapshot(self) -> None:
        active = create_room_snapshot(
            "active-game",
            ("alice", "bob"),
            mode=RoomMode.PVP,
            seat_picker=lambda _count: 0,
        )

        with self.assertRaises(ValueError):
            _match_result_presentation(active, "alice")

    def test_snapshot_effect_only_fires_for_new_versions(self) -> None:
        now = datetime(2026, 7, 26, 1, 0, tzinfo=timezone.utc)
        initial = create_room_snapshot(
            "effect-game",
            ("alice", "bob", "carol"),
            mode=RoomMode.PVP,
            now=now,
            seat_picker=lambda _count: 0,
        )
        record = TurnRecord(
            surface="すいか",
            reading="すいか",
            canonical_key="すいか",
            seat_index=0,
            actor_user_id="alice",
            by_bot=False,
            submitted_at=now,
        )
        accepted = replace(
            initial,
            state_version=1,
            history=(record,),
            expected_kana="か",
        )
        eliminated = replace(
            accepted,
            state_version=2,
            eliminated_seats=(1,),
            losing_seat=1,
            end_reason="timeout",
        )
        finished = replace(
            eliminated,
            status=RoomStatus.FINISHED,
            state_version=3,
            current_turn=0,
            eliminated_seats=(1, 2),
            losing_seat=2,
            end_reason="ends_with_n",
            expected_kana=None,
        )

        self.assertIsNone(_snapshot_effect(None, initial, "alice"))
        self.assertEqual(
            _snapshot_effect(initial, accepted, "alice"),
            _SnapshotEffect("accepted", "accepted"),
        )
        self.assertIsNone(
            _snapshot_effect(accepted, accepted, "alice")
        )
        self.assertEqual(
            _snapshot_effect(accepted, eliminated, "alice"),
            _SnapshotEffect("elimination", "elimination"),
        )
        self.assertEqual(
            _snapshot_effect(eliminated, finished, "alice"),
            _SnapshotEffect("finish", "victory"),
        )
        self.assertEqual(
            _snapshot_effect(eliminated, finished, "spectator"),
            _SnapshotEffect("finish", "finish"),
        )

    def test_reaction_sender_labels_never_expose_user_ids(self) -> None:
        now = datetime(2026, 7, 26, 2, 0, tzinfo=timezone.utc)
        active = create_room_snapshot(
            "reaction-labels",
            ("private-alice-id", "private-bob-id"),
            mode=RoomMode.PVP,
            spectators=("private-watcher-id",),
            now=now,
            seat_picker=lambda _count: 0,
        )
        player_reaction = RoomReaction(
            emoji="👍",
            sender_user_id="private-alice-id",
            sender_role=Role.PLAYER,
            sent_at=now,
        )
        watcher_reaction = RoomReaction(
            emoji="👏",
            sender_user_id="private-watcher-id",
            sender_role=Role.SPECTATOR,
            sent_at=now,
        )
        eliminated_reaction = RoomReaction(
            emoji="🔥",
            sender_user_id="private-alice-id",
            sender_role=Role.SPECTATOR,
            sent_at=now,
        )
        finished = replace(
            active,
            status=RoomStatus.FINISHED,
            current_turn=1,
            eliminated_seats=(0,),
            losing_seat=0,
            end_reason="surrender",
        )

        labels = (
            _reaction_sender_label(
                active, player_reaction, "private-alice-id"
            ),
            _reaction_sender_label(
                active, player_reaction, "private-bob-id"
            ),
            _reaction_sender_label(
                active, watcher_reaction, "private-bob-id"
            ),
            _reaction_sender_label(
                finished, eliminated_reaction, "private-bob-id"
            ),
        )

        self.assertEqual(
            labels,
            ("あなた", "プレイヤー1", "観戦者", "プレイヤー1（観戦中）"),
        )
        self.assertFalse(any("private-" in label for label in labels))

    def test_sound_cues_are_generated_and_fail_closed(self) -> None:
        script = _sound_cue_script("accepted")

        self.assertIn("AudioContext", script)
        self.assertIn("createOscillator", script)
        self.assertIn("catch(_error)", script)
        self.assertNotIn("http://", script)
        self.assertNotIn("https://", script)
        with self.assertRaises(ValueError):
            _sound_cue_script("not-a-cue")

    def test_game_effect_css_respects_reduced_motion(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "shiritori" / "web_auth.py").read_text(
            encoding="utf-8"
        )
        css = (root / "assets" / "platform.css").read_text(
            encoding="utf-8"
        )

        self.assertIn('app.storage.user', source)
        self.assertIn('game_sound_muted', source)
        self.assertIn('game_reduced_motion', source)
        self.assertIn('delay_seconds=5.0', source)
        self.assertIn('@media (prefers-reduced-motion: reduce)', css)
        self.assertIn('.match-result-card', css)
        self.assertIn('.game-effect--error', css)
        self.assertIn('rooms.send_reaction(game_id, user_id, emoji)', source)
        self.assertIn('event.kind is RoomEventKind.REACTION', source)
        self.assertIn('for emoji in SUPPORTED_REACTIONS', source)
        self.assertIn('while len(reaction_bubbles) > 4', source)
        self.assertIn('.reaction-bubble--enter', css)
        self.assertIn('.reaction-panel', css)

    def test_word_suggestion_page_is_protected_and_intentional(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "shiritori" / "web_auth.py").read_text(
            encoding="utf-8"
        )
        main_source = (root / "main.py").read_text(encoding="utf-8")
        page_source = source[
            source.index('@ui.page("/word-suggestions")'):
            source.index('@ui.page("/join/{room_code}")')
        ]

        self.assertIn(
            "word_suggestions: WordSuggestionService | None = None",
            source,
        )
        self.assertIn(
            '"/login?next=/word-suggestions"',
            page_source,
        )
        self.assertIn(
            "fresh_principal = await principal_for(request)",
            page_source,
        )
        self.assertIn(
            "_session_principal_matches_user(",
            page_source,
        )
        self.assertIn("if busy:", page_source)
        self.assertIn("word_suggestions.submit,", page_source)
        self.assertIn("word_suggestions.list_mine,", page_source)
        self.assertIn(
            "except WordSuggestionValidationError as error:",
            page_source,
        )
        self.assertIn(
            "except WordSuggestionPendingLimitError as error:",
            page_source,
        )
        self.assertIn("maxlength=30", page_source)
        self.assertIn("maxlength=60", page_source)
        self.assertIn("maxlength=200", page_source)
        self.assertIn("autocomplete=off required", page_source)
        self.assertIn(
            "同じ単語と読みはすでに申請済みです。",
            page_source,
        )
        self.assertNotIn("request.query_params", page_source)
        self.assertNotIn("suggestion.id", page_source)
        self.assertNotIn("suggestion.user_id", page_source)
        self.assertIn(
            'ui.link("単語追加リクエスト", "/word-suggestions")',
            source,
        )
        self.assertIn(
            '"辞書にない単語を申請",',
            source,
        )
        self.assertIn(
            "WORD_SUGGESTIONS = SERVICES.word_suggestions",
            main_source,
        )
        self.assertIn('"/word-suggestions",', main_source)
        self.assertIn(
            "word_suggestions=WORD_SUGGESTIONS,",
            main_source,
        )

    def test_reaction_bubble_uses_parent_live_region_only(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "shiritori" / "web_auth.py").read_text(
            encoding="utf-8"
        )
        reaction_source = source[
            source.index("def show_room_reaction("):
            source.index("async def release_reaction_buttons(")
        ]

        self.assertIn("aria-label='{sender}が", reaction_source)
        self.assertNotIn("role='status'", reaction_source)


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

    def test_invite_rate_keys_separate_accounts_and_ips(self) -> None:
        first = request_with_headers(
            ("host", "example.test"), client_host="192.0.2.1"
        )
        second = request_with_headers(
            ("host", "example.test"), client_host="192.0.2.2"
        )

        first_ip, first_account = _invite_rate_limit_keys(
            first, "account-a"
        )
        second_ip, same_account = _invite_rate_limit_keys(
            second, "account-a"
        )
        _, other_account = _invite_rate_limit_keys(
            first, "account-b"
        )

        self.assertNotEqual(first_ip, second_ip)
        self.assertEqual(first_account, same_account)
        self.assertNotEqual(first_account, other_account)

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
