"""NiceGUI/FastAPI authentication edge with secure opaque cookies."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import hmac
from html import escape
import logging
import math
from pathlib import Path
import secrets
from threading import Lock
import time
from typing import TypeVar
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from fastapi import HTTPException, Request
from nicegui import app, ui
from starlette.responses import RedirectResponse

from .auth import (
    AuthService,
    InvalidCredentialsError,
    InvalidRegistrationError,
    InvalidSessionError,
    SessionPrincipal,
    UsernameUnavailableError,
    canonicalize_username,
)
from .bots import canonical_kana, final_kana
from .game_session import SessionCode
from .database import GameRepository
from .lobby import (
    LobbyAuthorizationError,
    LobbyCapacityError,
    LobbyError,
    LobbyNameConflict,
    LobbyRevisionConflict,
    LobbyRoomSnapshot,
    LobbyService,
    LobbyStateError,
)
from .models import RoomRole, RoomStatus as StoredRoomStatus
from .onboarding import (
    OnboardingService,
    OnboardingUserUnavailableError,
)
from .room_runtime import RoomRuntimeCapabilityError
from .rooms import (
    LifeLossRecord,
    LexiconRoomService,
    ReactionRateLimitError,
    SUPPORTED_REACTIONS,
    Role,
    RoomCoordinator,
    RoomError,
    RoomEvent,
    RoomReaction,
    RoomEventKind,
    RoomSnapshot,
    RoomMode,
    RoomRuleSet,
    RoomStatus,
    RoomVersionConflict,
    SeatController,
    TurnRecord,
    WordSubmissionStatus,
)
from .score_attack import ScoreAttackSession, ScoreAttackStatus
from .score_attack_persistence import (
    SQLAlchemyScoreAttackService,
    ScoreAttackActiveRunExistsError,
    ScoreAttackPersistenceError,
    ScoreAttackRunView,
    StaleScoreAttackStateError,
)
from .settings import Settings
from .solo import SoloGameAuthorizationError, SoloGameService
from .statistics import StatisticsRepository
from .word_review import WordReviewService
from .word_suggestions import (
    WordSuggestionPendingLimitError,
    WordSuggestionService,
    WordSuggestionUserUnavailableError,
    WordSuggestionValidationError,
    WordSuggestionView,
)


_PLATFORM_CSS = (
    Path(__file__).parent.parent / "assets" / "platform.css"
).read_text(encoding="utf-8")
_MAX_FORM_BYTES = 8_192
_PasswordResult = TypeVar("_PasswordResult")
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuthWebServices:
    auth: AuthService
    games: GameRepository
    settings: Settings
    solo: SoloGameService | None = None
    rooms: RoomCoordinator | None = None
    room_words: LexiconRoomService | None = None
    lobby: LobbyService | None = None
    statistics: StatisticsRepository | None = None
    score_attack: SQLAlchemyScoreAttackService | None = None
    word_suggestions: WordSuggestionService | None = None
    word_review: WordReviewService | None = None
    onboarding: OnboardingService | None = None
    oni_rules: object | None = None


@dataclass(frozen=True, slots=True)
class _DeadlinePresentation:
    text: str
    level: str
    expired: bool


@dataclass(frozen=True, slots=True)
class _VersionedFeedback:
    state_version: int | None
    message: str


@dataclass(frozen=True, slots=True)
class _MatchResultPresentation:
    """Non-sensitive copy for the finished-match result card."""

    title: str
    tone: str
    outcome: str
    accepted_word_count: int
    end_reason: str
    round_summary: str
    last_word: str
    life_loss_history: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _OniChallengePresentation:
    """Non-sensitive Oni command data safe to show in the game UI."""

    commands: tuple[str, ...]
    sealed_endings: tuple[str, ...]
    candidate_count: int | None
    relaxed_seal_count: int


@dataclass(frozen=True, slots=True)
class _SnapshotEffect:
    """One visual/audio cue emitted for an authoritative state transition."""

    kind: str
    sound: str


_END_REASON_TEXT = {
    "ends_with_n": "「ん」で終わった",
    "duplicate": "同じ読みを使った",
    "timeout": "時間切れ",
    "no_legal_move": "出せる単語がなくなった",
    "surrender": "降参",
    "disconnect": "接続が戻らなかった",
}
_SOUND_CUE_NOTES = {
    "accepted": ((659, 0.00, 0.08), (880, 0.07, 0.10)),
    "your_turn": (
        (659, 0.00, 0.62),
        (1318, 0.00, 0.42),
        (1977, 0.00, 0.28),
    ),
    "error": ((247, 0.00, 0.12), (196, 0.09, 0.14)),
    "elimination": ((392, 0.00, 0.10), (294, 0.09, 0.14)),
    "victory": (
        (523, 0.00, 0.10),
        (659, 0.09, 0.10),
        (784, 0.18, 0.16),
    ),
    "finish": ((440, 0.00, 0.11), (349, 0.10, 0.16)),
}
_DEFAULT_SOUND_PEAK = 0.035
_YOUR_TURN_SOUND_PEAK = _DEFAULT_SOUND_PEAK * 1.5



def _public_seat_label(
    snapshot: RoomSnapshot,
    seat_index: int,
    user_id: str,
) -> str:
    """Describe a seat without returning an account identifier."""

    seat = snapshot.players[seat_index]
    if seat.owner_user_id == user_id:
        return "あなた"
    if (
        seat.owner_user_id is None
        and seat.controller is SeatController.BOT
    ):
        return f"Bot {seat_index + 1}"
    return f"プレイヤー{seat_index + 1}"


def _turn_seat_label(
    snapshot: RoomSnapshot,
    user_id: str,
    display_names: Mapping[str, str],
) -> str:
    """Return a readable current-turn label without exposing account IDs."""

    return _seat_display_label(
        snapshot,
        snapshot.current_turn,
        user_id,
        display_names,
    )


def _seat_display_label(
    snapshot: RoomSnapshot,
    seat_index: int,
    user_id: str,
    display_names: Mapping[str, str],
) -> str:
    """Return one public seat label for turn, life, and result displays."""

    seat = snapshot.players[seat_index]
    owner_id = seat.owner_user_id
    display_name = (
        display_names.get(owner_id)
        if owner_id is not None
        else None
    )
    if seat.controller is SeatController.BOT:
        if owner_id == user_id:
            own_label = (
                f"あなた（{display_name}）" if display_name else "あなた"
            )
            return f"{own_label}の代行Bot"
        if display_name:
            return f"{display_name}の代行Bot"
        return f"Bot {seat.index + 1}"
    if owner_id == user_id:
        return f"あなた（{display_name}）" if display_name else "あなた"
    return display_name or f"プレイヤー{seat.index + 1}"


def _seat_life_text(
    snapshot: RoomSnapshot,
    seat_index: int,
    user_id: str,
    display_names: Mapping[str, str],
) -> str:
    """Describe one player's remaining lives without exposing an account ID."""

    seat = snapshot.players[seat_index]
    label = _seat_display_label(
        snapshot,
        seat_index,
        user_id,
        display_names,
    )
    remaining = snapshot.remaining_lives[seat_index]
    if remaining <= 0 or seat_index in snapshot.eliminated_seats:
        elimination = next(
            (
                event
                for event in reversed(snapshot.life_loss_events)
                if event.seat_index == seat_index and event.eliminated
            ),
            None,
        )
        if elimination is None:
            state = "脱落・観戦中"
        else:
            reason = _END_REASON_TEXT.get(
                elimination.reason,
                "対局ルールに違反した",
            )
            word = _life_loss_word_text(elimination)
            state = (
                f"脱落（{reason}／{word}）・観戦中"
            )
    elif (
        snapshot.status is RoomStatus.ACTIVE
        and snapshot.current_turn == seat_index
    ):
        state = "現在の手番"
    elif seat.controller is SeatController.BOT:
        state = "Botが代行中"
    else:
        state = "待機中"
    return (
        f"{label}｜ライフ {remaining}/{snapshot.lives_per_player}"
        f"｜{state}"
    )


def _life_loss_word_text(event: LifeLossRecord) -> str:
    """Return the attempted word or a clear server-event fallback."""

    if not event.surface:
        return "使用単語なし"
    if event.reading and event.reading != event.surface:
        return f"{event.surface}（よみ：{event.reading}）"
    return event.surface


def _life_loss_event_text(
    snapshot: RoomSnapshot,
    event: LifeLossRecord,
    user_id: str,
    display_names: Mapping[str, str],
) -> str:
    """Build a public, complete explanation of one life-loss event."""

    actor = _seat_display_label(
        snapshot,
        event.seat_index,
        user_id,
        display_names,
    )
    reason = _END_REASON_TEXT.get(event.reason, "対局ルールに違反した")
    outcome = "脱落" if event.eliminated else "ライフ減少"
    return (
        f"{actor}｜{_life_loss_word_text(event)}｜{reason}｜"
        f"残りライフ {event.remaining_lives}/{snapshot.lives_per_player}"
        f"｜{outcome}"
    )


def _reaction_sender_label(
    snapshot: RoomSnapshot,
    reaction: RoomReaction,
    user_id: str,
) -> str:
    """Return a public reaction sender label without leaking account IDs."""

    if reaction.sender_user_id == user_id:
        return "あなた"
    seat = snapshot.seat_for_user(reaction.sender_user_id)
    if seat is None:
        return "観戦者"
    if reaction.sender_role is Role.SPECTATOR:
        return f"プレイヤー{seat.index + 1}（観戦中）"
    return f"プレイヤー{seat.index + 1}"

def _match_result_presentation(
    snapshot: RoomSnapshot,
    user_id: str,
    display_names: Mapping[str, str] | None = None,
) -> _MatchResultPresentation:
    """Build the finished result card entirely from public seat labels."""

    if snapshot.status is not RoomStatus.FINISHED:
        raise ValueError("a match result requires a finished snapshot")

    own_seat = snapshot.seat_for_user(user_id)
    winner_indexes = snapshot.active_seat_indexes
    if len(winner_indexes) == 1:
        winner_index = winner_indexes[0]
        winner = _public_seat_label(snapshot, winner_index, user_id)
        if own_seat is not None and own_seat.index == winner_index:
            title = "勝利！"
            tone = "victory"
            outcome = "あなたが最後まで勝ち残りました。"
        elif own_seat is not None:
            title = "今回は敗北"
            tone = "defeat"
            outcome = f"{winner}の勝ちです。"
        else:
            title = "対局終了"
            tone = "neutral"
            outcome = f"{winner}の勝ちです。"
    else:
        title = "対局終了"
        tone = "neutral"
        outcome = "勝者は確定しませんでした。"

    if snapshot.mode is RoomMode.SOLO_BOT:
        round_summary = (
            f"あなたとBot {len(snapshot.players) - 1}体で対戦"
        )
    else:
        round_summary = (
            f"{len(snapshot.players)}人で開始・"
            f"{len(snapshot.eliminated_seats)}人脱落"
        )
    last_word = (
        f"{snapshot.history[-1].surface}"
        f"（{snapshot.history[-1].reading}）"
        if snapshot.history
        else "なし"
    )
    public_names = display_names or {}
    return _MatchResultPresentation(
        title=title,
        tone=tone,
        outcome=outcome,
        accepted_word_count=sum(
            final_kana(record.reading) != "ん"
            for record in snapshot.history
        ),
        end_reason=_END_REASON_TEXT.get(
            snapshot.end_reason,
            "対局終了条件を満たした",
        ),
        round_summary=round_summary,
        last_word=last_word,
        life_loss_history=tuple(
            _life_loss_event_text(
                snapshot,
                event,
                user_id,
                public_names,
            )
            for event in snapshot.life_loss_events
        ),
    )


def _result_share_text(result: _MatchResultPresentation) -> str:
    """Return a private-ID-free, URL-free plain-text result."""

    lines = [
        "しりとり対局結果",
        result.title,
        result.outcome,
        f"成立したことば: {result.accepted_word_count}語",
        f"終了理由: {result.end_reason}",
        f"対戦概要: {result.round_summary}",
        f"最後のことば: {result.last_word}",
    ]
    if result.life_loss_history:
        lines.append("ライフ損失履歴:")
        lines.extend(
            f"- {event_text}"
            for event_text in result.life_loss_history
        )
    return "\n".join(lines)


def _result_share_script() -> str:
    """Share from the trusted click itself, then fall back to copying."""

    return """
(event) => {
  const button = event && event.currentTarget;
  const source = document.getElementById('siritori-result-share-payload');
  const status = document.getElementById('siritori-result-share-status');
  const text = source ? source.textContent.trim() : '';
  const setStatus = (message) => {
    if (status) status.textContent = message;
  };
  const copyResult = async () => {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch (_clipboardError) {
        // A denied Clipboard API must still reach the legacy fallback.
      }
    }
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    let copied = false;
    try {
      copied = document.execCommand('copy');
    } finally {
      area.remove();
    }
    if (!copied) throw new Error('copy unavailable');
  };
  void (async () => {
    if (!text) {
      setStatus('共有できる対局結果がありません。');
      return;
    }
    if (button) button.disabled = true;
    try {
      if (typeof navigator.share === 'function') {
        try {
          await navigator.share({title: 'しりとり対局結果', text});
          setStatus('対局結果を共有しました。');
          return;
        } catch (error) {
          if (error && error.name === 'AbortError') {
            setStatus('共有をキャンセルしました。');
            return;
          }
        }
      }
      await copyResult();
      setStatus('対局結果をコピーしました。');
    } catch (_error) {
      setStatus('共有またはコピーを利用できませんでした。');
    } finally {
      if (button) button.disabled = false;
    }
  })();
}
""".strip()


def _has_user_human_turn(snapshot: RoomSnapshot, user_id: str) -> bool:
    seat = snapshot.seat_for_user(user_id)
    return (
        snapshot.status is RoomStatus.ACTIVE
        and seat is not None
        and snapshot.current_turn == seat.index
        and seat.controller is SeatController.HUMAN
    )


def _snapshot_effect(
    previous: RoomSnapshot | None,
    current: RoomSnapshot,
    user_id: str,
) -> _SnapshotEffect | None:
    """Return at most one cue for a new snapshot; polling is intentionally mute."""

    if (
        previous is None
        or previous.room_id != current.room_id
        or current.state_version <= previous.state_version
    ):
        return None
    if (
        previous.status is not RoomStatus.FINISHED
        and current.status is RoomStatus.FINISHED
    ):
        result = _match_result_presentation(current, user_id)
        sound = "victory" if result.tone == "victory" else "finish"
        return _SnapshotEffect("finish", sound)
    effect: _SnapshotEffect | None = None
    if len(current.life_loss_events) > len(previous.life_loss_events):
        latest_loss = current.life_loss_events[-1]
        effect = _SnapshotEffect(
            "elimination" if latest_loss.eliminated else "life_loss",
            "elimination",
        )
    elif len(current.history) > len(previous.history):
        effect = _SnapshotEffect("accepted", "accepted")
    if effect is None:
        return None
    if (
        not _has_user_human_turn(previous, user_id)
        and _has_user_human_turn(current, user_id)
    ):
        return _SnapshotEffect(effect.kind, "your_turn")
    return effect


def _sound_cue_script(cue: str) -> str:
    """Return a self-contained Web Audio cue which fails closed."""

    try:
        notes = _SOUND_CUE_NOTES[cue]
    except KeyError as error:
        raise ValueError("unknown sound cue") from error
    peak_gain = _DEFAULT_SOUND_PEAK
    if cue == "your_turn":
        peak_gain = _YOUR_TURN_SOUND_PEAK / math.sqrt(len(notes))
    encoded_notes = ",".join(
        f"[{frequency},{delay:.2f},{duration:.2f}]"
        for frequency, delay, duration in notes
    )
    return (
        "(async()=>{try{"
        "const Audio=window.AudioContext||window.webkitAudioContext;"
        "if(!Audio){return false;}"
        "const context=window.__siritoriAudioContext||"
        "(window.__siritoriAudioContext=new Audio());"
        "if(context.state==='suspended'){await context.resume();}"
        f"const notes=[{encoded_notes}];"
        "for(const [frequency,delay,duration] of notes){"
        "const start=context.currentTime+delay;"
        "const oscillator=context.createOscillator();"
        "const gain=context.createGain();"
        "oscillator.type='sine';"
        "oscillator.frequency.setValueAtTime(frequency,start);"
        "gain.gain.setValueAtTime(0.0001,start);"
        f"gain.gain.exponentialRampToValueAtTime({peak_gain:.4f},start+0.01);"
        "gain.gain.exponentialRampToValueAtTime(0.0001,start+duration);"
        "oscillator.connect(gain);gain.connect(context.destination);"
        "oscillator.start(start);oscillator.stop(start+duration+0.02);"
        "}return true;}catch(_error){return false;}})()"
    )


def _latest_word_text(history: Sequence[TurnRecord]) -> str:
    """Return the latest recorded word without exposing actor identifiers."""

    if not history:
        return "まだありません（好きな単語から）"
    latest = history[-1]
    if latest.surface == latest.reading:
        return latest.surface
    return f"{latest.surface}（よみ：{latest.reading}）"


def _oni_challenge_presentation(
    rule_service: object | None,
    snapshot: RoomSnapshot,
) -> _OniChallengePresentation | None:
    """Return safe current commands without exposing candidate words."""

    if (
        snapshot.rule_set is not RoomRuleSet.ONI
        or snapshot.status is RoomStatus.FINISHED
    ):
        return None
    empty = _OniChallengePresentation((), (), None, 0)
    if rule_service is None:
        return empty
    resolver = getattr(rule_service, "challenge_for", None)
    if not callable(resolver):
        return empty
    try:
        challenge = resolver(snapshot)
        if challenge is None:
            return empty
        constraints = getattr(challenge, "constraints", None)
        raw_descriptions = getattr(constraints, "descriptions", ())
        commands = tuple(
            label
            for item in raw_descriptions
            if (label := str(item).strip())
            and not label.startswith("直近10手の末尾を封印中")
        )
        sealed_endings = tuple(
            dict.fromkeys(
                ending
                for item in getattr(constraints, "sealed_endings", ())
                if (ending := str(item).strip())
            )
        )
        raw_count = getattr(challenge, "candidate_count", None)
        candidate_count = (
            raw_count
            if type(raw_count) is int and raw_count >= 0
            else None
        )
        raw_relaxed = getattr(challenge, "relaxed_seal_count", 0)
        relaxed_seal_count = (
            raw_relaxed
            if type(raw_relaxed) is int and raw_relaxed >= 0
            else 0
        )
    except Exception:
        # A presentation failure must never block an authoritative game.
        LOGGER.debug(
            "failed to prepare Oni command presentation", exc_info=True
        )
        return empty
    return _OniChallengePresentation(
        commands=commands,
        sealed_endings=sealed_endings,
        candidate_count=candidate_count,
        relaxed_seal_count=relaxed_seal_count,
    )


def _history_was_appended(
    previous: tuple[object, ...] | None,
    current: tuple[object, ...],
) -> bool:
    """Return whether current strictly extends an already-rendered history."""

    return (
        previous is not None
        and len(current) > len(previous)
        and current[:len(previous)] == previous
    )


def _history_scroll_script(*, reduced_motion: bool) -> str:
    """Return a fail-closed script which scrolls only the history region."""

    if type(reduced_motion) is not bool:
        raise ValueError("reduced_motion must be a bool")
    reduced = "true" if reduced_motion else "false"
    return (
        "(()=>{try{"
        "const history=document.getElementById('siritori-game-history');"
        "if(!history){return false;}"
        f"const reduced={reduced}||"
        "window.matchMedia('(prefers-reduced-motion: reduce)').matches;"
        "const scroll=()=>{try{history.scrollTo({"
        "top:history.scrollHeight,behavior:reduced?'auto':'smooth'"
        "});}catch(_scrollError){history.scrollTop=history.scrollHeight;}};"
        "requestAnimationFrame(()=>requestAnimationFrame(scroll));"
        "return true;}catch(_error){return false;}})()"
    )


def _deadline_presentation(
    deadline_at: datetime | None,
    *,
    now: datetime | None = None,
) -> _DeadlinePresentation:
    """Return a stable whole-second countdown and its visual urgency."""

    if deadline_at is None:
        return _DeadlinePresentation(
            text="残り時間: 無制限",
            level="normal",
            expired=False,
        )
    checked_at = datetime.now(timezone.utc) if now is None else now
    seconds = max(
        0,
        math.ceil((deadline_at - checked_at).total_seconds()),
    )
    level = (
        "danger"
        if seconds <= 5
        else "warning"
        if seconds <= 10
        else "normal"
    )
    return _DeadlinePresentation(
        text=f"残り時間: {seconds}秒",
        level=level,
        expired=deadline_at <= checked_at,
    )


def _feedback_for_version(
    feedback: _VersionedFeedback | None,
    state_version: int,
) -> str | None:
    """Keep transient feedback only while its authoritative state is current."""

    if feedback is None or feedback.state_version != state_version:
        return None
    return feedback.message


def _session_principal_matches_user(
    principal: SessionPrincipal | None,
    expected_user_id: str,
) -> bool:
    """Return whether a freshly authenticated session still owns the page."""

    return (
        principal is not None
        and principal.account.id == expected_user_id
    )


def _word_suggestion_status_label(status: str) -> str:
    """Return a Japanese label without trusting persisted display text."""

    return {
        "pending": "審査待ち",
        "approved": "承認済み",
        "rejected": "見送り",
    }.get(str(status), "確認中")


def _solo_difficulty_options() -> dict[str, str]:
    """Return every Bot difficulty exposed by the application service."""

    return {
        "easy": "やさしい",
        "normal": "ふつう",
        "hard": "むずかしい",
    }


def _life_count_options() -> dict[int, str]:
    """Return every supported initial life count."""

    return {
        count: f"{count}個"
        for count in range(1, 6)
    }


def _validate_lives_per_player(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 5:
        raise ValueError("lives_per_player must be from 1 to 5")
    return value


def _room_timer_text(turn_seconds: int | None) -> str:
    """Return the compact Japanese timer label shared by room screens."""

    return "無制限" if turn_seconds is None else f"{turn_seconds}秒"


def _room_timer_value(turn_seconds: int | None) -> str:
    """Return the select value for one validated room timer."""

    if turn_seconds is None:
        return "unlimited"
    if type(turn_seconds) is not int or not 3 <= turn_seconds <= 180:
        raise ValueError("invalid room timer")
    return str(turn_seconds)


def _parse_room_timer_value(value: object) -> int | None:
    """Parse the room timer values shared by create and waiting screens."""

    if value == "unlimited":
        return None
    if isinstance(value, str) and value.isdigit():
        seconds = int(value)
        if 3 <= seconds <= 180:
            return seconds
    raise ValueError("invalid room timer")


def _room_listing_summary(room: object) -> str:
    """Describe only the non-sensitive fields allowed in public listings."""

    players = getattr(room, "players")
    player_count = len(players)
    max_players = int(getattr(room, "max_players"))
    timer = _room_timer_text(getattr(room, "turn_seconds"))
    lives = _validate_lives_per_player(getattr(room, "lives_per_player"))
    bot_fill = (
        "不足分はNormal Bot"
        if bool(getattr(room, "fill_empty_seats_with_bots"))
        else "Bot補充なし"
    )
    spectator = (
        "観戦可"
        if bool(getattr(room, "allow_spectators"))
        else "観戦不可"
    )
    return (
        f"対戦参加 {player_count}/{max_players}人・"
        f"{timer}・ライフ{lives}・{bot_fill}・{spectator}"
    )


def _room_invite_url(request: Request, room_code: str) -> str:
    """Build an absolute same-origin invite URL for clipboard sharing."""

    return f"{str(request.base_url).rstrip('/')}/join/{room_code}"


def _can_surrender(snapshot: RoomSnapshot, user_id: str) -> bool:
    """Return whether the authenticated owner still has an active seat."""

    seat = snapshot.seat_for_user(user_id)
    return (
        snapshot.status is RoomStatus.ACTIVE
        and seat is not None
        and seat.owner_user_id == user_id
        and seat.index not in snapshot.eliminated_seats
    )


def _room_closed_message(reason: str | None) -> str:
    """Explain why an active room closed without exposing private state."""

    if reason == "inactive":
        return (
            "30分間動きがなかったため、部屋を自動削除しました。"
            "ロビーへ戻ります。"
        )
    return "部屋が終了しました。ロビーへ戻ります。"


def _post_match_lobby_destination(
    lobby: LobbyService,
    user_id: str,
    finished_game_id: str,
) -> LobbyRoomSnapshot:
    """Return the safe destination even if a newer round already started."""

    try:
        return lobby.return_to_waiting(user_id, finished_game_id)
    except LobbyStateError:
        # Finishing a PvP game already reopens its lobby atomically. A delayed
        # browser callback may arrive after another tab has started the next
        # round; resolve the still-authorized room without replaying mutation.
        return lobby.open_room_for_game(user_id, finished_game_id)


class CsrfProtector:
    """Small stateless signed-token helper for native HTML forms."""

    def __init__(self, secret: str, *, lifetime_seconds: int = 7_200) -> None:
        if len(secret) < 32:
            raise ValueError("CSRF secret must contain at least 32 characters")
        if lifetime_seconds < 60:
            raise ValueError("CSRF token lifetime must be at least 60 seconds")
        self._secret = secret.encode("utf-8")
        self.lifetime_seconds = lifetime_seconds

    def issue(self, subject: str, *, now: int | None = None) -> str:
        issued_at = int(time.time()) if now is None else int(now)
        nonce = secrets.token_urlsafe(18)
        payload = f"{issued_at}.{nonce}.{subject}"
        signature = hmac.new(
            self._secret, payload.encode("utf-8"), sha256
        ).hexdigest()
        return f"{issued_at}.{nonce}.{signature}"

    def verify(
        self, token: str, subject: str, *, now: int | None = None
    ) -> bool:
        try:
            issued_text, nonce, signature = str(token).split(".", 2)
            issued_at = int(issued_text)
        except (TypeError, ValueError):
            return False
        checked_at = int(time.time()) if now is None else int(now)
        if issued_at > checked_at + 60:
            return False
        if checked_at - issued_at > self.lifetime_seconds:
            return False
        payload = f"{issued_at}.{nonce}.{subject}"
        expected = hmac.new(
            self._secret, payload.encode("utf-8"), sha256
        ).hexdigest()
        return hmac.compare_digest(signature, expected)


class LoginAttemptLimiter:
    """Thread-safe, TTL/LRU-bounded in-process attempt limiter."""

    def __init__(
        self,
        *,
        attempts: int = 5,
        window_seconds: int = 60,
        max_keys: int = 4_096,
    ) -> None:
        if attempts < 1 or window_seconds < 1 or max_keys < 1:
            raise ValueError(
                "attempt limit, window, and max_keys must be positive"
            )
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = Lock()

    @staticmethod
    def _bounded_key(key: str) -> str:
        value = str(key)
        if len(value) <= 256:
            return value
        return "sha256:" + sha256(value.encode("utf-8")).hexdigest()

    def _prune_expired_lru(self, cutoff: float) -> None:
        while self._events:
            oldest_key = next(iter(self._events))
            events = self._events[oldest_key]
            while events and events[0] <= cutoff:
                events.popleft()
            if events:
                break
            self._events.popitem(last=False)

    def allow(self, key: str, *, now: float | None = None) -> bool:
        checked_at = time.monotonic() if now is None else now
        cutoff = checked_at - self.window_seconds
        bounded_key = self._bounded_key(key)
        with self._lock:
            self._prune_expired_lru(cutoff)
            events = self._events.get(bounded_key)
            if events is not None:
                while events and events[0] <= cutoff:
                    events.popleft()
                if not events:
                    self._events.pop(bounded_key, None)
                    events = None
            if events is not None and len(events) >= self.attempts:
                return False
            if events is None:
                while len(self._events) >= self.max_keys:
                    self._events.popitem(last=False)
                events = deque()
                self._events[bounded_key] = events
            events.append(checked_at)
            self._events.move_to_end(bounded_key)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(self._bounded_key(key), None)

    @property
    def tracked_key_count(self) -> int:
        with self._lock:
            return len(self._events)


class PasswordWorkLimiter:
    """Bound concurrent CPU/memory-heavy password hash operations."""

    def __init__(self, *, max_concurrency: int = 2) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def run(
        self,
        operation: Callable[..., _PasswordResult],
        /,
        *args,
        **kwargs,
    ) -> _PasswordResult:
        async with self._semaphore:
            return await asyncio.to_thread(operation, *args, **kwargs)

async def _read_form(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if media_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="unsupported form encoding")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        stripped_length = content_length.strip()
        if not stripped_length.isascii() or not stripped_length.isdigit():
            raise HTTPException(status_code=400, detail="invalid content length")
        if int(stripped_length) > _MAX_FORM_BYTES:
            raise HTTPException(status_code=413, detail="form is too large")

    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > _MAX_FORM_BYTES - len(body):
            raise HTTPException(status_code=413, detail="form is too large")
        body.extend(chunk)
    try:
        decoded = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise HTTPException(
            status_code=400, detail="invalid form encoding"
        ) from error
    try:
        values = parse_qs(
            decoded,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=12,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail="invalid form fields"
        ) from error
    return {name: entries[-1] for name, entries in values.items() if entries}

def _same_origin(request: Request) -> bool:
    source = request.headers.get("origin")
    if not source:
        referer = request.headers.get("referer")
        if referer:
            parts = urlsplit(referer)
            source = f"{parts.scheme}://{parts.netloc}"
    if not source:
        return False

    # Render preserves the public Host header and sets X-Forwarded-Proto. Do
    # not trust X-Forwarded-Host: a client-supplied value could redefine the
    # authority against which Origin is checked.
    source_parts = urlsplit(source)
    host = request.headers.get("host", "").strip()
    if not host or "," in host:
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto")
    scheme = (
        forwarded_proto.split(",", 1)[0].strip()
        if forwarded_proto
        else request.url.scheme
    ).casefold()
    if scheme not in {"http", "https"}:
        return False
    if (
        source_parts.scheme.casefold() != scheme
        or source_parts.username is not None
        or source_parts.password is not None
        or source_parts.path not in {"", "/"}
        or source_parts.query
        or source_parts.fragment
    ):
        return False
    target_parts = urlsplit(f"{scheme}://{host}")
    try:
        source_port = source_parts.port or (443 if scheme == "https" else 80)
        target_port = target_parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        return False
    return (
        source_parts.hostname is not None
        and target_parts.hostname is not None
        and source_parts.hostname.casefold() == target_parts.hostname.casefold()
        and source_port == target_port
    )


def _auth_rate_limit_keys(
    request: Request, username: str
) -> tuple[str, str]:
    """Return fixed-size IP and canonical-account bucket keys."""

    client_host = request.client.host if request.client else "unknown"
    ip_identity = str(client_host).strip().casefold() or "unknown"
    _, username_key = canonicalize_username(username)
    ip_key = "ip:" + sha256(ip_identity.encode("utf-8")).hexdigest()
    account_key = (
        "account:" + sha256(username_key.encode("utf-8")).hexdigest()
    )
    return ip_key, account_key


def _invite_rate_limit_keys(
    request: Request, account_id: str
) -> tuple[str, str]:
    """Return independent fixed-size buckets for invite-code lookups."""

    client_host = request.client.host if request.client else "unknown"
    ip_identity = str(client_host).strip().casefold() or "unknown"
    account_identity = str(account_id).strip() or "unknown"
    return (
        "invite-ip:" + sha256(ip_identity.encode("utf-8")).hexdigest(),
        "invite-account:"
        + sha256(account_identity.encode("utf-8")).hexdigest(),
    )


def _safe_next(value: str | None, *, default: str = "/lobby") -> str:
    candidate = str(value or "")
    parts = urlsplit(candidate)
    if (
        candidate.startswith("/")
        and not candidate.startswith("//")
        and "\\" not in candidate
        and "\r" not in candidate
        and "\n" not in candidate
        and "\x00" not in candidate
        and not parts.scheme
        and not parts.netloc
    ):
        return candidate
    return default


def _tutorial_return_path(value: str | None) -> str:
    """Keep tutorial completion on a safe, non-recursive internal route."""

    target = _safe_next(value)
    parts = urlsplit(target)
    if parts.path == "/tutorial":
        nested_values = parse_qs(parts.query).get("next", ())
        nested = _safe_next(
            nested_values[0] if nested_values else None
        )
        if urlsplit(nested).path == "/tutorial":
            return "/lobby"
        return nested
    return target


def _tutorial_url(next_path: str | None) -> str:
    return "/tutorial?" + urlencode(
        {"next": _tutorial_return_path(next_path)}
    )


def _error_redirect(path: str, code: str, next_path: str) -> RedirectResponse:
    query = urlencode({"error": code, "next": _safe_next(next_path)})
    return RedirectResponse(f"{path}?{query}", status_code=303)


def _set_session_cookie(
    response: RedirectResponse,
    token: str,
    expires_at: datetime,
    settings: Settings,
) -> None:
    now = datetime.now(timezone.utc)
    expiry = (
        expires_at.replace(tzinfo=timezone.utc)
        if expires_at.tzinfo is None
        else expires_at.astimezone(timezone.utc)
    )
    max_age = max(1, int((expiry - now).total_seconds()))
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=max_age,
        expires=expiry,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _error_message(
    error_code: str | None, messages: Mapping[str, str]
) -> str | None:
    if error_code is None:
        return None
    return messages.get(error_code, "処理を完了できませんでした。もう一度お試しください。")


def _page_shell() -> None:
    ui.add_css(_PLATFORM_CSS)
    ui.add_head_html(
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
    )


async def session_principal_from_request(
    request: Request,
    auth: AuthService,
    settings: Settings,
) -> SessionPrincipal | None:
    """Resolve the DB-backed principal for a custom protected NiceGUI page."""

    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    try:
        return await asyncio.to_thread(auth.authenticate_session, token)
    except InvalidSessionError:
        return None


def _native_form(
    *,
    action: str,
    csrf_token: str,
    next_path: str,
    register: bool,
) -> str:
    display_field = (
        """
        <label class="native-field">表示名（任意）
          <input name="display_name" maxlength="40" autocomplete="name">
        </label>
        """
        if register
        else ""
    )
    submit_text = "アカウントを作る" if register else "ログイン"
    autocomplete = "new-password" if register else "current-password"
    return f"""
      <form class="native-form" action="{escape(action)}" method="post">
        <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
        <input type="hidden" name="next" value="{escape(next_path)}">
        <label class="native-field">ユーザー名
          <input name="username" minlength="3" maxlength="32"
                 autocomplete="username" required autofocus>
        </label>
        {display_field}
        <label class="native-field">パスワード
          <input name="password" type="password" minlength="10" maxlength="128"
                 autocomplete="{autocomplete}" required>
        </label>
        <button class="native-submit" type="submit">{submit_text}</button>
      </form>
    """


def register_auth_pages(
    services: AuthWebServices,
    *,
    limiter: LoginAttemptLimiter | None = None,
    ip_limiter: LoginAttemptLimiter | None = None,
    password_work_limiter: PasswordWorkLimiter | None = None,
) -> None:
    """Register authentication endpoints and minimal protected shells."""

    auth = services.auth
    games = services.games
    settings = services.settings
    solo = services.solo
    csrf = CsrfProtector(settings.session_secret)
    rooms = services.rooms
    room_words = services.room_words
    lobby = services.lobby
    statistics = services.statistics
    score_attack = services.score_attack
    word_suggestions = services.word_suggestions
    word_review = services.word_review
    onboarding = services.onboarding
    oni_rules = services.oni_rules
    account_attempts = limiter or LoginAttemptLimiter()
    ip_attempts = ip_limiter or LoginAttemptLimiter(attempts=20)
    password_work = password_work_limiter or PasswordWorkLimiter()
    invite_account_attempts = LoginAttemptLimiter(
        attempts=30, window_seconds=60
    )
    invite_ip_attempts = LoginAttemptLimiter(attempts=60, window_seconds=60)

    def consume_auth_attempt(
        request: Request, username: str
    ) -> tuple[bool, str]:
        ip_key, account_key = _auth_rate_limit_keys(request, username)
        # Evaluate both buckets on every request so account/IP behavior does
        # not disclose which one reached its limit.
        ip_allowed = ip_attempts.allow(ip_key)
        account_allowed = account_attempts.allow(account_key)
        return ip_allowed and account_allowed, account_key

    def consume_invite_attempt(request: Request, account_id: str) -> bool:
        ip_key, account_key = _invite_rate_limit_keys(
            request, account_id
        )
        # Always consume both buckets so the response does not reveal which
        # identity reached its limit.
        ip_allowed = invite_ip_attempts.allow(ip_key)
        account_allowed = invite_account_attempts.allow(account_key)
        return ip_allowed and account_allowed

    async def principal_for(request: Request):
        return await session_principal_from_request(
            request, auth, settings
        )

    async def authenticated_destination(
        user_id: str,
        next_path: str | None,
    ) -> str:
        target = _tutorial_return_path(next_path)
        if onboarding is None:
            return target
        try:
            needs_tutorial = await asyncio.to_thread(
                onboarding.needs_tutorial,
                user_id,
            )
        except OnboardingUserUnavailableError:
            raise
        except Exception:
            LOGGER.exception("failed to load onboarding progress")
            return target
        if needs_tutorial:
            return _tutorial_url(target)
        return target

    @ui.page("/login")
    async def login_page(request: Request):
        if await principal_for(request) is not None:
            return RedirectResponse("/lobby", status_code=303)
        _page_shell()
        next_path = _safe_next(request.query_params.get("next"))
        message = _error_message(
            request.query_params.get("error"),
            {
                "credentials": "ユーザー名またはパスワードが正しくありません。",
                "rate": "試行回数が多すぎます。少し待ってからお試しください。",
                "csrf": "フォームの有効期限が切れました。もう一度お試しください。",
            },
        )
        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("auth-card"):
                ui.label("ログイン").classes("auth-title")
                ui.label("保存した対局とオンライン部屋を利用できます。").classes(
                    "auth-copy"
                )
                if message:
                    ui.label(message).classes("auth-error").props(
                        "role='alert'"
                    )
                ui.html(
                    _native_form(
                        action="/auth/login",
                        csrf_token=csrf.issue("anonymous-login"),
                        next_path=next_path,
                        register=False,
                    )
                )
                with ui.row().classes("auth-links"):
                    ui.link("まず1人で試す", "/").classes("platform-link")
                    ui.link("新規登録", f"/register?next={next_path}").classes(
                        "platform-link"
                    )

    @ui.page("/register")
    async def register_page(request: Request):
        if await principal_for(request) is not None:
            return RedirectResponse("/lobby", status_code=303)
        _page_shell()
        next_path = _safe_next(request.query_params.get("next"))
        message = _error_message(
            request.query_params.get("error"),
            {
                "invalid": "入力条件を確認してください。パスワードは10文字以上です。",
                "unavailable": "そのユーザー名は使用できません。",
                "rate": "試行回数が多すぎます。少し待ってからお試しください。",
                "csrf": "フォームの有効期限が切れました。もう一度お試しください。",
            },
        )
        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("auth-card"):
                ui.label("新規登録").classes("auth-title")
                ui.label(
                    "ユーザー名は3〜32文字、パスワードは10〜128文字です。"
                ).classes("auth-copy")
                if message:
                    ui.label(message).classes("auth-error").props(
                        "role='alert'"
                    )
                ui.html(
                    _native_form(
                        action="/auth/register",
                        csrf_token=csrf.issue("anonymous-register"),
                        next_path=next_path,
                        register=True,
                    )
                )
                ui.link("ログインへ戻る", f"/login?next={next_path}").classes(
                    "platform-link"
                )

    @app.post("/auth/login")
    async def login_action(request: Request):
        form = await _read_form(request)
        next_path = _safe_next(form.get("next"))
        if (
            not _same_origin(request)
            or not csrf.verify(
                form.get("csrf_token", ""), "anonymous-login"
            )
        ):
            return _error_redirect("/login", "csrf", next_path)

        username = form.get("username", "")
        allowed, account_key = consume_auth_attempt(request, username)
        if not allowed:
            return _error_redirect("/login", "rate", next_path)
        try:
            issued = await password_work.run(
                auth.login, username, form.get("password", "")
            )
        except (InvalidCredentialsError, InvalidRegistrationError):
            return _error_redirect("/login", "credentials", next_path)
        # A valid login clears only its canonical account bucket. Clearing the
        # IP-wide bucket would let one successful account bypass that limit.
        account_attempts.reset(account_key)
        try:
            destination = await authenticated_destination(
                issued.account.id,
                next_path,
            )
        except OnboardingUserUnavailableError:
            await asyncio.to_thread(auth.logout, issued.token)
            return _error_redirect(
                "/login", "credentials", next_path
            )
        response = RedirectResponse(destination, status_code=303)
        _set_session_cookie(
            response, issued.token, issued.expires_at, settings
        )
        return response

    @app.post("/auth/register")
    async def register_action(request: Request):
        form = await _read_form(request)
        next_path = _safe_next(form.get("next"))
        if (
            not _same_origin(request)
            or not csrf.verify(
                form.get("csrf_token", ""), "anonymous-register"
            )
        ):
            return _error_redirect("/register", "csrf", next_path)
        username = form.get("username", "")
        allowed, account_key = consume_auth_attempt(request, username)
        if not allowed:
            return _error_redirect("/register", "rate", next_path)
        try:
            account = await password_work.run(
                auth.register,
                username,
                form.get("password", ""),
                display_name=form.get("display_name") or None,
            )
            issued = await asyncio.to_thread(
                auth.issue_session, account.id
            )
        except InvalidRegistrationError:
            return _error_redirect("/register", "invalid", next_path)
        except UsernameUnavailableError:
            return _error_redirect("/register", "unavailable", next_path)
        account_attempts.reset(account_key)
        try:
            destination = await authenticated_destination(
                issued.account.id,
                next_path,
            )
        except OnboardingUserUnavailableError:
            await asyncio.to_thread(auth.logout, issued.token)
            return _error_redirect(
                "/login", "credentials", next_path
            )
        response = RedirectResponse(destination, status_code=303)
        _set_session_cookie(
            response, issued.token, issued.expires_at, settings
        )
        return response

    @ui.page("/tutorial")
    async def tutorial_page(request: Request):
        next_path = _tutorial_return_path(
            request.query_params.get("next")
        )
        tutorial_path = _tutorial_url(next_path)
        principal = await principal_for(request)
        if principal is None:
            login_query = urlencode({"next": tutorial_path})
            return RedirectResponse(
                f"/login?{login_query}",
                status_code=303,
            )
        if onboarding is None:
            return RedirectResponse(next_path, status_code=303)

        _page_shell()
        user_id = principal.account.id
        step_index = 0
        busy = False
        steps = (
            (
                "しりとりの基本",
                "読みの最後の文字から、次の単語をつなぎます。",
                (
                    "先攻は辞書にある好きな単語から始められます。",
                    "漢字やカタカナも使えます。読みは自動で確認されます。",
                    "「ん」で終わる単語や、一度使った読みは使えません。",
                ),
            ),
            (
                "遊び方を選ぶ",
                "1人でも、友だちとも遊べます。",
                (
                    "Bot戦はBot数・難易度・制限時間を選べます。",
                    "対人戦は部屋を作るか、参加コードから入ります。",
                    "手番と次の文字を確認してから単語を送ります。",
                ),
            ),
            (
                "対戦と観戦",
                "途中からでも同じ部屋で楽しめます。",
                (
                    "観戦可能な部屋では、対戦中でも観戦できます。",
                    "脱落後は観戦に回り、残った人へ手番が続きます。",
                    "対戦中も観戦中もリアクションを送れます。",
                ),
            ),
            (
                "記録を楽しむ",
                "遊んだ結果はアカウントに保存されます。",
                (
                    "戦績・ランキング・鬼しりとりを楽しめます。",
                    "結果は共有でき、同じ設定で再戦もできます。",
                    "辞書にない単語は追加リクエストから申請できます。",
                ),
            ),
        )

        def render_step(*, focus: bool) -> None:
            title, summary, points = steps[step_index]
            step_count.set_text(
                f"{step_index + 1} / {len(steps)}"
            )
            progress.set_value((step_index + 1) / len(steps))
            progress.props(
                f"aria-valuenow={step_index + 1} "
                f"aria-valuemax={len(steps)}"
            )
            step_title.set_text(title)
            step_summary.set_text(summary)
            step_body.clear()
            items = "".join(
                f"<li>{escape(point)}</li>" for point in points
            )
            with step_body:
                ui.html(
                    f"<ul class='tutorial-points'>{items}</ul>"
                )
            back_button.set_visibility(step_index > 0)
            next_button.set_visibility(
                step_index < len(steps) - 1
            )
            skip_button.set_visibility(
                step_index < len(steps) - 1
            )
            start_button.set_visibility(
                step_index == len(steps) - 1
            )
            if focus:
                step_title.run_method("focus")

        def previous_step() -> None:
            nonlocal step_index
            if busy or step_index <= 0:
                return
            step_index -= 1
            render_step(focus=True)

        def next_step() -> None:
            nonlocal step_index
            if busy or step_index >= len(steps) - 1:
                return
            step_index += 1
            render_step(focus=True)

        async def finish_tutorial(
            _event: object | None = None,
        ) -> None:
            nonlocal busy
            if busy:
                return
            busy = True
            for button in (
                back_button,
                next_button,
                skip_button,
                start_button,
            ):
                button.disable()
            tutorial_error.set_text("")
            tutorial_error.set_visibility(False)
            try:
                current_principal = await principal_for(request)
                if not _session_principal_matches_user(
                    current_principal,
                    user_id,
                ):
                    login_query = urlencode(
                        {"next": tutorial_path}
                    )
                    ui.navigate.to(f"/login?{login_query}")
                    return
                await asyncio.to_thread(
                    onboarding.complete,
                    user_id,
                )
            except OnboardingUserUnavailableError:
                login_query = urlencode({"next": tutorial_path})
                ui.navigate.to(f"/login?{login_query}")
                return
            except Exception:
                LOGGER.exception(
                    "failed to complete account tutorial"
                )
                tutorial_error.set_text(
                    "完了状態を保存できませんでした。"
                    "少し待ってからもう一度お試しください。"
                )
                tutorial_error.set_visibility(True)
            else:
                ui.navigate.to(next_path)
                return
            finally:
                busy = False
                for button in (
                    back_button,
                    next_button,
                    skip_button,
                    start_button,
                ):
                    button.enable()

        with ui.element("main").classes("platform-shell"):
            with ui.column().classes(
                "tutorial-card"
            ):
                with ui.row().classes(
                    "tutorial-heading w-full items-start "
                    "justify-between gap-3"
                ):
                    with ui.column().classes("min-w-0 gap-1"):
                        ui.label("遊び方ガイド").classes(
                            "auth-title"
                        ).props(
                            "role='heading' aria-level='1'"
                        )
                        ui.label(
                            "4つのポイントを確認すると、すぐに遊べます。"
                        ).classes("platform-muted")
                    step_count = ui.label("").classes(
                        "tutorial-step-count"
                    ).props(
                        "role='status' aria-live='polite' "
                        "aria-atomic='true'"
                    )
                progress = ui.linear_progress(
                    value=0.25,
                ).classes("tutorial-progress w-full").props(
                    "aria-label='チュートリアルの進み具合' "
                    "aria-valuemin=1 aria-valuemax=4"
                )
                with ui.element("section").classes(
                    "tutorial-step w-full"
                ).props(
                    "role='group' "
                    "aria-labelledby='tutorial-step-title'"
                ):
                    step_title = ui.label("").classes(
                        "tutorial-step-title"
                    ).props(
                        "id='tutorial-step-title' tabindex='-1' "
                        "role='heading' aria-level='2'"
                    )
                    step_summary = ui.label("").classes(
                        "tutorial-step-summary"
                    )
                    step_body = ui.column().classes("w-full")
                tutorial_error = ui.label("").classes(
                    "auth-error"
                ).props(
                    "role='alert' aria-live='assertive'"
                )
                tutorial_error.set_visibility(False)
                with ui.row().classes(
                    "tutorial-actions w-full items-center gap-2"
                ):
                    back_button = ui.button(
                        "戻る",
                        icon="arrow_back",
                        on_click=previous_step,
                    ).props("outline no-caps")
                    skip_button = ui.button(
                        "スキップ",
                        on_click=finish_tutorial,
                    ).props("flat no-caps")
                    next_button = ui.button(
                        "次へ",
                        icon="arrow_forward",
                        on_click=next_step,
                    ).props("unelevated no-caps")
                    start_button = ui.button(
                        "始める",
                        icon="play_arrow",
                        on_click=finish_tutorial,
                    ).props("unelevated no-caps")
        render_step(focus=False)

    @ui.page("/lobby")
    async def lobby_page(request: Request):
        principal = await principal_for(request)
        if principal is None:
            return RedirectResponse("/login?next=/lobby", status_code=303)
        if onboarding is not None:
            try:
                needs_tutorial = await asyncio.to_thread(
                    onboarding.needs_tutorial,
                    principal.account.id,
                )
            except OnboardingUserUnavailableError:
                return RedirectResponse(
                    "/login?next=/lobby",
                    status_code=303,
                )
            except Exception:
                LOGGER.exception(
                    "failed to load onboarding progress in lobby"
                )
            else:
                if needs_tutorial:
                    return RedirectResponse(
                        _tutorial_url("/lobby"),
                        status_code=303,
                    )
        show_admin_review = False
        if word_review is not None:
            try:
                show_admin_review = await asyncio.to_thread(
                    word_review.is_admin,
                    principal.account.id,
                )
            except Exception:
                LOGGER.exception(
                    "failed to load word-review authorization in lobby"
                )
        _page_shell()
        logout_token = csrf.issue(principal.session_id)
        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("platform-wrap"):
                with ui.element("header").classes("platform-header"):
                    with ui.column():
                        ui.label(
                            f"{principal.account.display_name} さん"
                        ).classes("auth-title")
                        ui.label("オンライン対戦ロビー").classes(
                            "platform-muted"
                        )
                    ui.html(
                        f"""
                        <form class="logout-form" action="/auth/logout" method="post">
                          <input type="hidden" name="csrf_token"
                                 value="{escape(logout_token)}">
                          <button class="logout-button" type="submit">ログアウト</button>
                        </form>
                        """
                    )
                with ui.row().classes("platform-nav"):
                    ui.link("戦績", "/stats").classes("platform-link")
                    ui.link("ランキング", "/rankings").classes("platform-link")
                    ui.link("スコアアタック", "/score-attack").classes(
                        "platform-link"
                    )
                    ui.link(
                        "鬼しりとり", "/oni-shiritori"
                    ).classes("platform-link")
                    ui.link("単語追加リクエスト", "/word-suggestions").classes(
                        "platform-link"
                    )
                    if show_admin_review:
                        ui.link(
                            "単語審査", "/admin/word-suggestions"
                        ).classes("platform-link")
                    ui.link(
                        "遊び方", _tutorial_url("/lobby")
                    ).classes("platform-link")
                with ui.element("section").classes("dashboard-grid"):
                    with ui.column().classes("dashboard-card"):
                        ui.label("部屋を作る・参加する").classes("aside-title")
                        room_name_input = ui.input(
                            label="部屋名",
                            value=f"{principal.account.display_name}の部屋",
                        ).props("outlined maxlength=64").classes("w-full")
                        room_players_select = ui.select(
                            options={
                                number: f"{number}人"
                                for number in range(2, 9)
                            },
                            value=2,
                            label="最大人数",
                        ).props("outlined options-dense").classes("w-full")
                        room_lives_select = ui.select(
                            options=_life_count_options(),
                            value=1,
                            label="ライフ数",
                        ).props("outlined options-dense").classes("w-full")
                        room_timer_select = ui.select(
                            options={
                                "unlimited": "無制限",
                                "3": "3秒",
                                "10": "10秒",
                                "30": "30秒",
                                "60": "1分",
                                "180": "3分",
                            },
                            value="unlimited",
                            label="1手の制限時間",
                        ).props("outlined options-dense").classes("w-full")
                        spectator_switch = ui.switch(
                            "観戦を許可する", value=True
                        )
                        public_switch = ui.switch(
                            "公開部屋としてロビーに表示する",
                            value=False,
                        )
                        ui.label(
                            "非公開でも、招待URLを知っている人は参加できます。"
                        ).classes("platform-muted room-setting-help")
                        bot_fill_switch = ui.switch(
                            "不足人数をNormal Botで補う",
                            value=False,
                        )
                        room_code_input = ui.input(
                            label="参加コード",
                            placeholder="例: ABCD234567",
                        ).props(
                            "outlined maxlength=12 autocomplete=off"
                        ).classes("w-full")
                        room_error = ui.label("").classes(
                            "platform-muted"
                        ).props("role='alert' aria-live='assertive'")
                        room_busy = False

                        def room_turn_seconds() -> int | None:
                            return _parse_room_timer_value(room_timer_select.value)

                        async def create_room() -> None:
                            nonlocal room_busy
                            if room_busy:
                                return
                            room_busy = True
                            create_room_button.disable()
                            room_error.set_text("")
                            try:
                                current_principal = await principal_for(
                                    request
                                )
                                if current_principal is None:
                                    ui.navigate.to(
                                        "/login?next=/lobby"
                                    )
                                    return
                                if lobby is None:
                                    raise RuntimeError(
                                        "lobby service is unavailable"
                                    )
                                max_players = room_players_select.value
                                if (
                                    type(max_players) is not int
                                    or not 2 <= max_players <= 8
                                ):
                                    raise ValueError("invalid player count")
                                lives_per_player = (
                                    _validate_lives_per_player(room_lives_select.value)
                                )
                                room = await asyncio.to_thread(
                                    lobby.create_pvp_room,
                                    current_principal.account.id,
                                    name=str(
                                        room_name_input.value or ""
                                    ),
                                    max_players=max_players,
                                    lives_per_player=lives_per_player,
                                    allow_spectators=bool(
                                        spectator_switch.value
                                    ),
                                    is_public=bool(public_switch.value),
                                    fill_empty_seats_with_bots=bool(
                                        bot_fill_switch.value
                                    ),
                                    turn_seconds=room_turn_seconds(),
                                )
                            except LobbyNameConflict:
                                room_error.set_text(
                                    "同名の部屋が既にあります。"
                                )
                            except (LobbyError, TypeError, ValueError):
                                LOGGER.exception("invalid room setup")
                                room_error.set_text(
                                    "部屋の設定を確認してください。"
                                )
                            except Exception:
                                LOGGER.exception("failed to create room")
                                room_error.set_text(
                                    "部屋を作成できませんでした。"
                                )
                            else:
                                ui.navigate.to(
                                    f"/room/{room.room_code}"
                                )
                            finally:
                                room_busy = False
                                create_room_button.enable()

                        async def join_room(
                            *,
                            as_spectator: bool,
                        ) -> None:
                            nonlocal room_busy
                            if room_busy:
                                return
                            room_busy = True
                            room_error.set_text("")
                            try:
                                current_principal = await principal_for(
                                    request
                                )
                                if current_principal is None:
                                    ui.navigate.to(
                                        "/login?next=/lobby"
                                    )
                                    return
                                if lobby is None:
                                    raise RuntimeError(
                                        "lobby service is unavailable"
                                    )
                                if not consume_invite_attempt(
                                    request, current_principal.account.id
                                ):
                                    room_error.set_text(
                                        "確認回数が多すぎます。少し待ってください。"
                                    )
                                    return
                                join_method = (
                                    lobby.join_as_spectator
                                    if as_spectator
                                    else lobby.join_as_player
                                )
                                room = await asyncio.to_thread(
                                    join_method,
                                    current_principal.account.id,
                                    str(room_code_input.value or ""),
                                )
                            except (LobbyError, TypeError, ValueError):
                                LOGGER.exception("failed to join room")
                                room_error.set_text(
                                    "参加コードまたは部屋の状態を確認してください。"
                                )
                            except Exception:
                                LOGGER.exception(
                                    "unexpected room join failure"
                                )
                                room_error.set_text(
                                    "部屋へ参加できませんでした。"
                                )
                            else:
                                ui.navigate.to(
                                    f"/room/{room.room_code}"
                                )
                            finally:
                                room_busy = False

                        create_room_button = ui.button(
                            "部屋を作る",
                            icon="add",
                            on_click=create_room,
                        ).props("unelevated no-caps").classes("w-full")
                        with ui.row().classes("w-full gap-2"):
                            ui.button(
                                "対戦参加",
                                on_click=lambda: join_room(
                                    as_spectator=False
                                ),
                            ).props("outline no-caps").classes("grow")
                            ui.button(
                                "観戦参加",
                                on_click=lambda: join_room(
                                    as_spectator=True
                                ),
                            ).props("outline no-caps").classes("grow")
                    with ui.column().classes("dashboard-card"):
                        ui.label("保存したBot戦").classes("aside-title")
                        ui.link("保存一覧を開く", "/saved-games").classes(
                            "platform-link"
                        )
                    with ui.column().classes("dashboard-card"):
                        ui.label("1人でBot戦").classes("aside-title")
                        ui.label(
                            "Botの数・難易度・制限時間を選びます。"
                        ).classes("platform-muted")
                        bot_count_select = ui.select(
                            options={
                                number: f"{number}体"
                                for number in range(1, 8)
                            },
                            value=1,
                            label="Botの数",
                        ).props("outlined options-dense").classes("w-full")
                        solo_lives_select = ui.select(
                            options=_life_count_options(),
                            value=1,
                            label="ライフ数",
                        ).props("outlined options-dense").classes("w-full")
                        difficulty_select = ui.select(
                            options=_solo_difficulty_options(),
                            value="normal",
                            label="難易度",
                        ).props("outlined options-dense").classes("w-full")
                        timer_select = ui.select(
                            options={
                                "unlimited": "無制限",
                                "3": "3秒",
                                "10": "10秒",
                                "30": "30秒",
                                "60": "1分",
                                "180": "3分",
                            },
                            value="unlimited",
                            label="1手の制限時間",
                        ).props("outlined options-dense").classes("w-full")
                        create_error = ui.label("").classes(
                            "platform-muted"
                        ).props("role='alert' aria-live='assertive'")
                        creating = False

                        async def create_solo_game() -> None:
                            nonlocal creating
                            if creating:
                                return
                            creating = True
                            create_button.disable()
                            create_error.set_text("")
                            try:
                                current_principal = await principal_for(
                                    request
                                )
                                if current_principal is None:
                                    ui.navigate.to(
                                        "/login?next=/lobby"
                                    )
                                    return
                                if solo is None:
                                    raise RuntimeError(
                                        "solo service is unavailable"
                                    )
                                bot_count = bot_count_select.value
                                difficulty = difficulty_select.value
                                timer_value = timer_select.value
                                lives_per_player = (
                                    _validate_lives_per_player(solo_lives_select.value)
                                )
                                if (
                                    type(bot_count) is not int
                                    or not 1 <= bot_count <= 7
                                ):
                                    raise ValueError("invalid bot count")
                                if (
                                    difficulty
                                    not in _solo_difficulty_options()
                                ):
                                    raise ValueError("invalid difficulty")
                                if timer_value == "unlimited":
                                    turn_seconds = None
                                elif (
                                    isinstance(timer_value, str)
                                    and timer_value.isdigit()
                                ):
                                    turn_seconds = int(timer_value)
                                    if not 3 <= turn_seconds <= 180:
                                        raise ValueError("invalid timer")
                                else:
                                    raise ValueError("invalid timer")
                                snapshot = await solo.create(
                                    current_principal.account.id,
                                    bot_count=bot_count,
                                    bot_difficulty=difficulty,
                                    turn_seconds=turn_seconds,
                                    lives_per_player=lives_per_player,
                                )
                            except (
                                TypeError,
                                ValueError,
                                KeyError,
                                RoomRuntimeCapabilityError,
                            ):
                                LOGGER.exception("invalid solo setup")
                                create_error.set_text(
                                    "設定を確認してください。"
                                )
                            except Exception:
                                LOGGER.exception(
                                    "failed to create solo game"
                                )
                                create_error.set_text(
                                    "Bot戦を開始できませんでした。"
                                )
                            else:
                                ui.navigate.to(
                                    f"/play/{snapshot.room_id}"
                                )
                            finally:
                                creating = False
                                create_button.enable()

                        create_button = ui.button(
                            "Bot戦を始める",
                            icon="play_arrow",
                            on_click=create_solo_game,
                        ).props("unelevated no-caps").classes("w-full")

                with ui.column().classes(
                    "dashboard-card public-room-list"
                ):
                    with ui.row().classes(
                        "w-full items-center justify-between gap-3"
                    ):
                        ui.label("公開中の部屋").classes("aside-title")
                        public_room_status = ui.label("").classes(
                            "platform-muted"
                        ).props("role='status' aria-live='polite'")
                    ui.label(
                        "参加したい部屋を選ぶと、内容を確認してから入れます。"
                    ).classes("platform-muted")
                    public_room_box = ui.column().classes(
                        "w-full gap-3"
                    )
                    public_refreshing = False

                    async def refresh_public_rooms() -> None:
                        nonlocal public_refreshing
                        if public_refreshing:
                            return
                        public_refreshing = True
                        try:
                            if lobby is None:
                                raise RuntimeError(
                                    "lobby service is unavailable"
                                )
                            listed_rooms = await asyncio.to_thread(
                                lobby.list_public_rooms,
                                limit=50,
                            )
                        except Exception:
                            LOGGER.exception(
                                "failed to list public waiting rooms"
                            )
                            public_room_status.set_text(
                                "公開部屋を取得できませんでした。"
                            )
                        else:
                            public_room_box.clear()
                            public_room_status.set_text(
                                f"{len(listed_rooms)}件"
                            )
                            with public_room_box:
                                if not listed_rooms:
                                    ui.label(
                                        "現在、参加できる公開部屋はありません。"
                                    ).classes("platform-muted")
                                for listed_room in listed_rooms:
                                    with ui.row().classes(
                                        "public-room-card w-full "
                                        "items-center justify-between gap-3"
                                    ):
                                        with ui.column().classes(
                                            "min-w-0 gap-1"
                                        ):
                                            ui.label(
                                                (
                                                    "対戦中・観戦受付中"
                                                    if listed_room.status
                                                    is StoredRoomStatus.ACTIVE
                                                    else "参加者募集中"
                                                )
                                            ).classes(
                                                "public-room-status"
                                            )
                                            ui.label(
                                                listed_room.name
                                            ).classes("aside-title")
                                            ui.label(
                                                _room_listing_summary(
                                                    listed_room
                                                )
                                            ).classes("platform-muted")
                                        ui.link(
                                            "部屋を見る",
                                            f"/join/{listed_room.room_code}",
                                        ).classes("platform-link")
                        finally:
                            public_refreshing = False

                await refresh_public_rooms()
                ui.timer(4.0, refresh_public_rooms)

    @ui.page("/stats")
    async def statistics_page(request: Request):
        principal = await principal_for(request)
        if principal is None:
            return RedirectResponse("/login?next=/stats", status_code=303)
        if statistics is None:
            return RedirectResponse("/lobby", status_code=303)

        user_id = principal.account.id
        try:
            summary, recent_matches, score_best = await asyncio.gather(
                asyncio.to_thread(statistics.get_user_summary, user_id),
                asyncio.to_thread(
                    statistics.list_recent_matches,
                    user_id,
                    limit=20,
                ),
                asyncio.to_thread(
                    statistics.get_score_attack_personal_best,
                    user_id,
                ),
            )
        except Exception:
            LOGGER.exception("failed to load match statistics")
            return RedirectResponse("/lobby", status_code=303)

        _page_shell()
        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("platform-wrap"):
                with ui.row().classes("platform-nav"):
                    ui.link("← ロビー", "/lobby").classes("platform-link")
                    ui.link("ランキング", "/rankings").classes(
                        "platform-link"
                    )
                    ui.link("スコアアタック", "/score-attack").classes(
                        "platform-link"
                    )
                ui.label("あなたの戦績").classes("auth-title")
                ui.label(
                    "終了した対戦は、同じ操作が再送されても一度だけ記録されます。"
                ).classes("platform-muted")
                with ui.element("section").classes("stats-grid"):
                    metrics = (
                        ("対戦数", summary.games_played),
                        ("勝ち", summary.wins),
                        ("負け", summary.losses),
                        ("勝率", f"{summary.win_rate * 100:.1f}%"),
                        ("対人戦の勝ち", summary.pvp_wins),
                        ("Bot戦の勝ち", summary.solo_wins),
                        ("使った単語", summary.accepted_words),
                        ("引き分け", summary.draws),
                        (
                            "最高スコア",
                            score_best.score
                            if score_best is not None else 0,
                        ),
                        (
                            "最高スコアの単語数",
                            score_best.accepted_count
                            if score_best is not None else 0,
                        ),
                    )
                    for label, value in metrics:
                        with ui.column().classes("stat-card"):
                            ui.label(str(value)).classes("stat-value")
                            ui.label(label).classes("platform-muted")

                with ui.column().classes("dashboard-card w-full"):
                    ui.label("ランキング公開設定").classes("aside-title")
                    ui.label(
                        "オンにした場合だけ、表示名と対人戦の成績がランキングに載ります。"
                    ).classes("platform-muted")
                    visibility_busy = False
                    confirmed_visibility = summary.leaderboard_visible

                    async def update_visibility(event) -> None:
                        nonlocal visibility_busy, confirmed_visibility
                        if visibility_busy:
                            visibility_switch.set_value(
                                confirmed_visibility
                            )
                            return
                        visibility_busy = True
                        visibility_switch.disable()
                        desired = bool(event.value)
                        try:
                            current_principal = await principal_for(request)
                            if not _session_principal_matches_user(
                                current_principal, user_id
                            ):
                                ui.navigate.to("/login?next=/stats")
                                return
                            confirmed_visibility = await asyncio.to_thread(
                                statistics.set_leaderboard_visibility,
                                user_id,
                                desired,
                            )
                            visibility_switch.set_value(
                                confirmed_visibility
                            )
                        except Exception:
                            LOGGER.exception(
                                "failed to update leaderboard visibility"
                            )
                            visibility_switch.set_value(
                                confirmed_visibility
                            )
                            ui.notify(
                                "公開設定を保存できませんでした。再読み込みしてください。",
                                type="negative",
                            )
                        else:
                            ui.notify("公開設定を保存しました。", type="positive")
                        finally:
                            visibility_busy = False
                            visibility_switch.enable()

                    visibility_switch = ui.switch(
                        "ランキングに自分の成績を表示する",
                        value=summary.leaderboard_visible,
                        on_change=update_visibility,
                    )

                with ui.column().classes("dashboard-card w-full"):
                    ui.label("最近の対戦").classes("aside-title")
                    if not recent_matches:
                        ui.label(
                            "まだ記録された対戦はありません。"
                        ).classes("platform-muted")
                    for match in recent_matches:
                        mode = (
                            "対人戦"
                            if match.mode == "multiplayer"
                            else "Bot戦"
                        )
                        result = {
                            "win": "勝ち",
                            "loss": "負け",
                            "draw": "引き分け",
                        }.get(match.result, match.result)
                        placement = (
                            f"{match.placement}位"
                            if match.placement is not None
                            else "順位なし"
                        )
                        with ui.row().classes(
                            "recent-match-row w-full "
                            "items-center justify-between gap-3"
                        ):
                            with ui.column().classes("min-w-0 gap-1"):
                                ui.label(
                                    f"{mode}・{result}・{placement}"
                                ).classes("aside-title")
                                ui.label(
                                    f"{match.player_count}人戦・"
                                    f"{match.move_count}語"
                                ).classes("platform-muted")
                            ui.label(
                                match.finished_at.strftime("%Y-%m-%d")
                            ).classes("platform-muted")

    @ui.page("/rankings")
    async def rankings_page(request: Request):
        principal = await principal_for(request)
        if principal is None:
            return RedirectResponse(
                "/login?next=/rankings", status_code=303
            )
        if statistics is None:
            return RedirectResponse("/lobby", status_code=303)
        try:
            pvp_entries, score_entries = await asyncio.gather(
                asyncio.to_thread(
                    statistics.list_pvp_win_leaderboard,
                    limit=50,
                ),
                asyncio.to_thread(
                    statistics.list_score_attack_leaderboard,
                    limit=50,
                ),
            )
        except Exception:
            LOGGER.exception("failed to load leaderboard")
            pvp_entries = ()
            score_entries = ()

        _page_shell()
        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("platform-wrap"):
                with ui.row().classes("platform-nav"):
                    ui.link("← ロビー", "/lobby").classes("platform-link")
                    ui.link("自分の戦績", "/stats").classes("platform-link")
                    ui.link("スコアアタック", "/score-attack").classes(
                        "platform-link"
                    )
                ui.label("ランキング").classes("auth-title")
                ui.label(
                    "公開を選んだプレイヤーだけを表示します。"
                ).classes("platform-muted")
                with ui.column().classes("dashboard-card ranking-list w-full"):
                    ui.label("3分スコアアタック").classes("aside-title")
                    if not score_entries:
                        ui.label(
                            "公開中のスコアはまだありません。"
                        ).classes("platform-muted")
                    for entry in score_entries:
                        with ui.row().classes(
                            "ranking-row w-full items-center gap-3"
                        ):
                            ui.label(f"{entry.rank}位").classes(
                                "ranking-position"
                            )
                            with ui.column().classes(
                                "ranking-player min-w-0 gap-1"
                            ):
                                ui.label(entry.display_name).classes(
                                    "aside-title"
                                )
                                ui.label(
                                    f"{entry.accepted_count}語成功"
                                ).classes("platform-muted")
                            ui.label(f"{entry.score}点").classes(
                                "stat-value"
                            )
                with ui.column().classes("dashboard-card ranking-list w-full"):
                    ui.label("対人戦 勝利数").classes("aside-title")
                    if not pvp_entries:
                        ui.label(
                            "公開中の戦績はまだありません。"
                        ).classes("platform-muted")
                    for entry in pvp_entries:
                        with ui.row().classes(
                            "ranking-row w-full items-center gap-3"
                        ):
                            ui.label(f"{entry.rank}位").classes(
                                "ranking-position"
                            )
                            with ui.column().classes(
                                "ranking-player min-w-0 gap-1"
                            ):
                                ui.label(entry.display_name).classes(
                                    "aside-title"
                                )
                                ui.label(
                                    f"{entry.games_played}戦・"
                                    f"勝率 {entry.win_rate * 100:.1f}%"
                                ).classes("platform-muted")
                            ui.label(f"{entry.wins}勝").classes("stat-value")

    @ui.page("/score-attack")
    async def score_attack_page(request: Request):
        principal = await principal_for(request)
        if principal is None:
            return RedirectResponse(
                "/login?next=/score-attack", status_code=303
            )
        if score_attack is None:
            return RedirectResponse("/lobby", status_code=303)

        user_id = principal.account.id
        try:
            current_run = await asyncio.to_thread(
                score_attack.resume_active,
                user_id,
            )
        except Exception:
            LOGGER.exception("failed to resume score attack")
            current_run = None

        _page_shell()
        busy = False
        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("platform-wrap"):
                with ui.row().classes("platform-nav"):
                    ui.link("← ロビー", "/lobby").classes("platform-link")
                    ui.link("自分の戦績", "/stats").classes("platform-link")
                    ui.link("ランキング", "/rankings").classes(
                        "platform-link"
                    )
                ui.label("3分スコアアタック").classes("auth-title")
                ui.label(
                    "辞書にある単語を3分間でつなぎ、自己ベストを目指します。"
                ).classes("platform-muted")

                with ui.column().classes(
                    "dashboard-card score-rules-card w-full"
                ):
                    ui.label("ルール").classes("aside-title")
                    ui.label(
                        "最初の単語は自由です。重複、「ん」で終わる単語、"
                        "または時間切れで終了します。"
                    ).classes("platform-muted")
                    ui.label(
                        "1語の得点 = 10 + 読みの長さ×2（最大30点）"
                        " + 連鎖ボーナス（最大20点）"
                    ).classes("platform-muted")

                with ui.column().classes(
                    "dashboard-card score-attack-card w-full"
                ):
                    with ui.row().classes(
                        "score-summary w-full items-center gap-3"
                    ):
                        with ui.column().classes("score-metric"):
                            score_label = ui.label("0").classes(
                                "score-value"
                            )
                            ui.label("スコア").classes("platform-muted")
                        with ui.column().classes("score-metric"):
                            count_label = ui.label("0").classes(
                                "stat-value"
                            )
                            ui.label("成功した単語").classes(
                                "platform-muted"
                            )
                        deadline_label = ui.label(
                            "開始前"
                        ).classes("deadline-label deadline--normal")

                    expected_label = ui.label(
                        "開始ボタンを押すと3分の計測が始まります。"
                    ).classes("aside-title")
                    feedback_label = ui.label("").classes(
                        "platform-muted"
                    ).props("role='status' aria-live='polite'")

                    start_button = ui.button(
                        "3分スコアアタックを開始",
                        icon="timer",
                    ).props("unelevated no-caps").classes(
                        "score-start-button"
                    )

                    with ui.column().classes(
                        "score-active-panel w-full gap-3"
                    ) as active_panel:
                        word_input = ui.input(
                            label="単語",
                            placeholder="漢字・ひらがな・カタカナ",
                        ).props(
                            "outlined maxlength=30 autocomplete=off"
                        ).classes("w-full")
                        submit_button = ui.button(
                            "送信",
                            icon="send",
                        ).props("unelevated no-caps").classes("w-full")
                        reading_box = ui.column().classes(
                            "score-reading-box w-full gap-2"
                        )

                    with ui.column().classes(
                        "score-finished-panel w-full gap-2"
                    ) as finished_panel:
                        finish_label = ui.label("").classes("aside-title")
                        ui.label(
                            "結果は保存済みです。ランキング公開は戦績画面で選べます。"
                        ).classes("platform-muted")

                    ui.separator()
                    ui.label("単語履歴").classes("aside-title")
                    history_box = ui.column().classes(
                        "game-history score-history w-full gap-2"
                    )

                def restored_attack(
                    run: ScoreAttackRunView | None,
                ) -> ScoreAttackSession | None:
                    if run is None:
                        return None
                    try:
                        return ScoreAttackSession.from_snapshot(run.snapshot)
                    except ValueError:
                        LOGGER.exception(
                            "score attack snapshot failed UI validation"
                        )
                        return None

                def update_deadline(
                    run: ScoreAttackRunView | None,
                ) -> bool:
                    if (
                        run is None
                        or run.status
                        != ScoreAttackStatus.ACTIVE.value
                    ):
                        return False
                    deadline = _deadline_presentation(run.deadline_at)
                    deadline_label.set_text(deadline.text)
                    deadline_label.classes(
                        add=f"deadline--{deadline.level}",
                        remove=(
                            "deadline--normal deadline--warning "
                            "deadline--danger"
                        ),
                    )
                    return deadline.expired

                def render_history(
                    attack: ScoreAttackSession | None,
                ) -> None:
                    history_box.clear()
                    with history_box:
                        if attack is None or not attack.history:
                            ui.label(
                                "まだ単語はありません。"
                            ).classes("platform-muted")
                            return
                        for entry in reversed(attack.history):
                            with ui.row().classes(
                                "game-history-row w-full "
                                "items-center justify-between gap-3"
                            ):
                                with ui.column().classes("min-w-0 gap-1"):
                                    ui.label(entry.surface).classes(
                                        "aside-title"
                                    )
                                    ui.label(entry.reading).classes(
                                        "platform-muted"
                                    )
                                result_text = (
                                    "終了語"
                                    if entry.result.value == "ends_with_n"
                                    else f"{entry.turn_number}語目"
                                )
                                ui.label(result_text).classes(
                                    "platform-muted"
                                )

                async def choose_reading(reading: str) -> None:
                    nonlocal busy, current_run
                    if (
                        busy
                        or current_run is None
                        or current_run.status
                        != ScoreAttackStatus.ACTIVE.value
                    ):
                        return
                    busy = True
                    submit_button.disable()
                    try:
                        if not await score_page_session_valid():
                            return
                        outcome = await asyncio.to_thread(
                            score_attack.resolve_reading,
                            user_id=user_id,
                            run_id=current_run.id,
                            reading=reading,
                            expected_version=current_run.state_version,
                        )
                        render_run(
                            outcome.run,
                            outcome.result.message
                            if outcome.result is not None
                            else None,
                        )
                    except StaleScoreAttackStateError:
                        await refresh_after_conflict()
                    except ScoreAttackPersistenceError:
                        LOGGER.exception(
                            "score attack reading choice failed"
                        )
                        feedback_label.set_text(
                            "読みを確定できませんでした。再読み込みしてください。"
                        )
                    finally:
                        busy = False
                        if (
                            current_run is not None
                            and current_run.status
                            == ScoreAttackStatus.ACTIVE.value
                        ):
                            submit_button.enable()

                def render_reading_choices(
                    attack: ScoreAttackSession | None,
                ) -> None:
                    reading_box.clear()
                    if attack is None or attack.pending_reading is None:
                        return
                    with reading_box:
                        ui.label("読みを選んでください").classes(
                            "aside-title"
                        )
                        for reading in attack.pending_reading.readings:
                            ui.button(
                                reading,
                                on_click=lambda selected=reading: (
                                    choose_reading(selected)
                                ),
                            ).props("outline no-caps").classes("w-full")

                def render_run(
                    run: ScoreAttackRunView | None,
                    message: str | None = None,
                ) -> None:
                    nonlocal current_run
                    current_run = run
                    attack = restored_attack(run)
                    score_label.set_text(str(run.score if run else 0))
                    count_label.set_text(
                        str(run.accepted_count if run else 0)
                    )
                    render_history(attack)
                    render_reading_choices(attack)
                    is_active = (
                        run is not None
                        and run.status == ScoreAttackStatus.ACTIVE.value
                        and attack is not None
                    )
                    active_panel.set_visibility(is_active)
                    finished_panel.set_visibility(
                        run is not None and not is_active
                    )
                    start_button.set_visibility(not is_active)

                    if run is None:
                        start_button.set_text("3分スコアアタックを開始")
                        deadline_label.set_text("開始前")
                        expected_label.set_text(
                            "開始ボタンを押すと3分の計測が始まります。"
                        )
                        feedback_label.set_text(message or "")
                        return
                    if is_active and attack is not None:
                        start_button.set_text("3分スコアアタックを開始")
                        update_deadline(run)
                        expected_label.set_text(
                            "最初は好きな単語から"
                            if attack.expected_kana is None
                            else f"「{attack.expected_kana}」から始めてください"
                        )
                        feedback_label.set_text(
                            message or "時計はサーバー側で進んでいます。"
                        )
                        word_input.enable()
                        submit_button.enable()
                        return

                    deadline_label.set_text("終了")
                    deadline_label.classes(
                        add="deadline--normal",
                        remove="deadline--warning deadline--danger",
                    )
                    reason = {
                        "timeout": "3分が経過しました。",
                        "ends_with_n": "「ん」で終わる単語を入力しました。",
                        "duplicate": "同じ読みの単語を使いました。",
                    }.get(run.finish_reason, "終了しました。")
                    finish_label.set_text(
                        f"{reason} 最終スコアは {run.score} 点です。"
                    )
                    feedback_label.set_text(message or reason)
                    start_button.set_text("もう一度挑戦")

                async def score_page_session_valid() -> bool:
                    current_principal = await principal_for(request)
                    if _session_principal_matches_user(
                        current_principal, user_id
                    ):
                        return True
                    score_timer.deactivate()
                    ui.navigate.to("/login?next=/score-attack")
                    return False

                async def refresh_after_conflict() -> None:
                    nonlocal current_run
                    if current_run is None:
                        return
                    try:
                        latest = await asyncio.to_thread(
                            score_attack.get,
                            user_id,
                            current_run.id,
                        )
                    except Exception:
                        LOGGER.exception(
                            "failed to refresh stale score attack"
                        )
                        ui.navigate.to("/score-attack")
                        return
                    render_run(
                        latest,
                        "別の画面で行われた操作を反映しました。",
                    )

                async def start_run() -> None:
                    nonlocal busy, current_run
                    if busy:
                        return
                    busy = True
                    start_button.disable()
                    try:
                        if not await score_page_session_valid():
                            return
                        try:
                            started = await asyncio.to_thread(
                                score_attack.start,
                                user_id,
                            )
                        except ScoreAttackActiveRunExistsError:
                            started = await asyncio.to_thread(
                                score_attack.resume_active,
                                user_id,
                            )
                            if started is None:
                                raise
                        render_run(
                            started,
                            "開始しました。最初の単語は自由です。",
                        )
                        word_input.set_value("")
                        word_input.run_method("focus")
                    except ScoreAttackPersistenceError:
                        LOGGER.exception("failed to start score attack")
                        feedback_label.set_text(
                            "開始できませんでした。再読み込みしてください。"
                        )
                    finally:
                        busy = False
                        start_button.enable()

                async def submit_word() -> None:
                    nonlocal busy, current_run
                    if (
                        busy
                        or current_run is None
                        or current_run.status
                        != ScoreAttackStatus.ACTIVE.value
                    ):
                        return
                    busy = True
                    submit_button.disable()
                    try:
                        if not await score_page_session_valid():
                            return
                        outcome = await asyncio.to_thread(
                            score_attack.submit,
                            user_id=user_id,
                            run_id=current_run.id,
                            surface=str(word_input.value or ""),
                            expected_version=current_run.state_version,
                        )
                        result = outcome.result
                        render_run(
                            outcome.run,
                            result.message if result is not None else None,
                        )
                        if (
                            result is not None
                            and result.code
                            not in {
                                SessionCode.LEXICON_REJECTED,
                                SessionCode.NOT_CHAINED,
                                SessionCode.INVALID_LEXICON_RESULT,
                            }
                        ):
                            word_input.set_value("")
                        if (
                            outcome.run.status
                            == ScoreAttackStatus.ACTIVE.value
                        ):
                            word_input.run_method("focus")
                    except StaleScoreAttackStateError:
                        await refresh_after_conflict()
                    except ScoreAttackPersistenceError:
                        LOGGER.exception("score attack submission failed")
                        feedback_label.set_text(
                            "送信できませんでした。再読み込みしてください。"
                        )
                    finally:
                        busy = False
                        if (
                            current_run is not None
                            and current_run.status
                            == ScoreAttackStatus.ACTIVE.value
                        ):
                            submit_button.enable()

                async def tick_score_attack() -> None:
                    nonlocal busy, current_run
                    if (
                        busy
                        or current_run is None
                        or current_run.status
                        != ScoreAttackStatus.ACTIVE.value
                    ):
                        return
                    if not await score_page_session_valid():
                        return
                    if not update_deadline(current_run):
                        return
                    busy = True
                    submit_button.disable()
                    try:
                        outcome = await asyncio.to_thread(
                            score_attack.expire,
                            user_id=user_id,
                            run_id=current_run.id,
                            expected_version=current_run.state_version,
                        )
                        render_run(
                            outcome.run,
                            outcome.result.message
                            if outcome.result is not None
                            else None,
                        )
                    except StaleScoreAttackStateError:
                        await refresh_after_conflict()
                    except ScoreAttackPersistenceError:
                        LOGGER.exception("score attack timeout failed")
                    finally:
                        busy = False

                start_button.on("click", start_run)
                submit_button.on("click", submit_word)
                word_input.on("keydown.enter", submit_word)
                render_run(current_run)
                score_timer = ui.timer(0.5, tick_score_attack)

    @ui.page("/word-suggestions")
    async def word_suggestions_page(request: Request):
        principal = await principal_for(request)
        if principal is None:
            return RedirectResponse(
                "/login?next=/word-suggestions",
                status_code=303,
            )
        if word_suggestions is None:
            return RedirectResponse("/lobby", status_code=303)

        user_id = principal.account.id
        initial_error: str | None = None
        try:
            initial_suggestions = await asyncio.to_thread(
                word_suggestions.list_mine,
                user_id,
                limit=50,
            )
        except WordSuggestionUserUnavailableError:
            return RedirectResponse(
                "/login?next=/word-suggestions",
                status_code=303,
            )
        except Exception:
            LOGGER.exception("failed to load word suggestions")
            initial_suggestions = ()
            initial_error = (
                "申請履歴を読み込めませんでした。"
                "少し待ってから再読み込みしてください。"
            )

        _page_shell()
        client = ui.context.client
        busy = False

        def set_feedback(message: str, *, error: bool = False) -> None:
            feedback_label.set_text(message)
            feedback_label.classes(
                add="auth-error" if error else "platform-muted",
                remove="platform-muted" if error else "auth-error",
            )

        def render_suggestions(
            suggestions: tuple[WordSuggestionView, ...],
        ) -> None:
            suggestions_box.clear()
            with suggestions_box:
                if not suggestions:
                    ui.label(
                        "申請履歴はまだありません。"
                    ).classes("platform-muted")
                    return
                for suggestion in suggestions:
                    with ui.column().classes(
                        "public-room-card w-full gap-1"
                    ):
                        with ui.row().classes(
                            "w-full items-center justify-between gap-2"
                        ):
                            ui.label(suggestion.surface).classes(
                                "aside-title"
                            )
                            ui.label(
                                _word_suggestion_status_label(
                                    suggestion.status
                                )
                            ).classes("platform-muted")
                        ui.label(
                            f"読み: {suggestion.reading}"
                        ).classes("platform-muted")
                        if suggestion.note:
                            ui.label(
                                f"補足: {suggestion.note}"
                            ).classes("platform-muted")
                        created_text = suggestion.created_at.astimezone(
                            timezone.utc
                        ).strftime("%Y-%m-%d %H:%M UTC")
                        ui.label(
                            f"申請日時: {created_text}"
                        ).classes("platform-muted")

        async def submit_suggestion(
            _event: object | None = None,
        ) -> None:
            nonlocal busy
            if busy:
                return
            busy = True
            submit_button.disable()
            set_feedback("申請内容を確認しています。")
            try:
                fresh_principal = await principal_for(request)
                if not _session_principal_matches_user(
                    fresh_principal,
                    user_id,
                ):
                    set_feedback(
                        "ログイン状態を確認できませんでした。"
                        "もう一度ログインしてください。",
                        error=True,
                    )
                    ui.navigate.to(
                        "/login?next=/word-suggestions"
                    )
                    return

                result = await asyncio.to_thread(
                    word_suggestions.submit,
                    user_id,
                    surface_input.value,
                    reading_input.value,
                    note_input.value or None,
                )
                latest = await asyncio.to_thread(
                    word_suggestions.list_mine,
                    user_id,
                    limit=50,
                )
            except WordSuggestionValidationError as error:
                field_label = {
                    "surface": "単語",
                    "reading": "読み",
                    "note": "補足",
                }.get(error.field, "入力")
                set_feedback(
                    f"{field_label}: {error}",
                    error=True,
                )
            except WordSuggestionPendingLimitError as error:
                set_feedback(str(error), error=True)
            except WordSuggestionUserUnavailableError as error:
                set_feedback(str(error), error=True)
            except Exception:
                LOGGER.exception("word suggestion submission failed")
                set_feedback(
                    "申請を保存できませんでした。"
                    "少し待ってからもう一度お試しください。",
                    error=True,
                )
            else:
                render_suggestions(latest)
                surface_input.set_value("")
                reading_input.set_value("")
                note_input.set_value("")
                if result.replayed:
                    set_feedback(
                        "同じ単語と読みはすでに申請済みです。"
                        "既存の申請を履歴に表示しました。"
                    )
                else:
                    set_feedback(
                        "申請を受け付けました。"
                        "審査後に結果が更新されます。"
                    )
            finally:
                busy = False
                if not client.is_deleted:
                    submit_button.enable()

        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("platform-wrap"):
                with ui.element("header").classes("platform-header"):
                    with ui.column():
                        ui.label("単語追加リクエスト").classes(
                            "auth-title"
                        )
                        ui.label(
                            "辞書にない実在する単語を審査へ送れます。"
                        ).classes("platform-muted")
                    ui.link("← ロビー", "/lobby").classes(
                        "platform-link"
                    )
                ui.label(
                    "申請した単語は自動では追加されません。"
                    "確認後の辞書更新をお待ちください。"
                ).classes("auth-copy")
                with ui.element("section").classes("dashboard-grid"):
                    with ui.column().classes("dashboard-card"):
                        ui.label("新しく申請する").classes("aside-title")
                        # Gameplay failures are never copied here. The user
                        # must intentionally provide a word and its reading.
                        surface_input = ui.input(
                            label="申請する単語",
                            placeholder="例: 佃煮",
                        ).props(
                            "outlined clearable maxlength=30 "
                            "autocomplete=off required"
                        ).classes("w-full")
                        reading_input = ui.input(
                            label="ひらがなの読み",
                            placeholder="例: つくだに",
                        ).props(
                            "outlined clearable maxlength=60 "
                            "autocomplete=off required"
                        ).classes("w-full")
                        note_input = ui.textarea(
                            label="補足（任意）",
                            placeholder="辞書掲載例や用途など",
                        ).props(
                            "outlined maxlength=200 autogrow"
                        ).classes("w-full")
                        submit_button = ui.button(
                            "審査を依頼する",
                            icon="send",
                            on_click=submit_suggestion,
                        ).props(
                            "unelevated no-caps"
                        ).classes("w-full")
                        feedback_label = ui.label(
                            initial_error or (
                                "単語と読みを入力してください。"
                            )
                        ).classes(
                            "auth-error"
                            if initial_error
                            else "platform-muted"
                        ).props(
                            "role='status' aria-live='polite'"
                        )
                    with ui.column().classes("dashboard-card"):
                        ui.label("あなたの申請履歴").classes("aside-title")
                        suggestions_box = ui.column().classes(
                            "w-full gap-2"
                        )

        render_suggestions(initial_suggestions)

    @ui.page("/join/{room_code}")
    async def room_invite_page(room_code: str, request: Request):
        principal = await principal_for(request)
        if principal is None:
            next_path = f"/join/{room_code}"
            return RedirectResponse(
                f"/login?{urlencode({'next': next_path})}",
                status_code=303,
            )
        if lobby is None:
            return RedirectResponse("/lobby", status_code=303)

        invite_rate_limited = not consume_invite_attempt(
            request, principal.account.id
        )
        if invite_rate_limited:
            initial_room = None
        else:
            try:
                initial_room = await asyncio.to_thread(
                    lobby.get_room, room_code
                )
            except (LobbyError, TypeError, ValueError):
                initial_room = None

        if (
            initial_room is not None
            and initial_room.member_for(principal.account.id) is not None
        ):
            return RedirectResponse(
                f"/room/{initial_room.room_code}",
                status_code=303,
            )
        if (
            initial_room is not None
            and initial_room.status is StoredRoomStatus.ACTIVE
            and not initial_room.allow_spectators
        ):
            initial_room = None

        _page_shell()
        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("platform-wrap"):
                ui.link("← ロビーへ", "/lobby").classes("platform-link")
                with ui.column().classes(
                    "dashboard-card room-invite-card"
                ):
                    ui.label("部屋への招待").classes("auth-title")
                    if initial_room is None:
                        unavailable_message = (
                            "招待URLの確認回数が多すぎます。"
                            "少し待ってからお試しください。"
                            if invite_rate_limited
                            else "この招待URLは無効か、部屋が削除されています。"
                        )
                        ui.label(unavailable_message).classes(
                            "auth-error"
                        ).props(
                            "role='alert' aria-live='assertive'"
                        )
                        return
                    visibility = (
                        "公開部屋"
                        if initial_room.is_public
                        else "招待URL限定の非公開部屋"
                    )
                    ui.label(initial_room.name).classes("aside-title")
                    ui.label(
                        f"{visibility}・"
                        f"{_room_listing_summary(initial_room)}"
                    ).classes("platform-muted")
                    join_feedback = ui.label(
                        "参加方法を選んでください。"
                    ).classes("platform-muted").props(
                        "role='status' aria-live='polite'"
                    )
                    join_busy = False

                    async def accept_invite(
                        *,
                        as_spectator: bool,
                    ) -> None:
                        nonlocal join_busy
                        if join_busy:
                            return
                        join_busy = True
                        player_join_button.disable()
                        spectator_join_button.disable()
                        try:
                            current_principal = await principal_for(
                                request
                            )
                            if current_principal is None:
                                ui.navigate.to(
                                    "/login?"
                                    + urlencode(
                                        {
                                            "next": f"/join/{room_code}"
                                        }
                                    )
                                )
                                return
                            if not consume_invite_attempt(
                                request, current_principal.account.id
                            ):
                                join_feedback.set_text(
                                    "確認回数が多すぎます。"
                                    "少し待ってからお試しください。"
                                )
                                return
                            join_method = (
                                lobby.join_as_spectator
                                if as_spectator
                                else lobby.join_as_player
                            )
                            joined = await asyncio.to_thread(
                                join_method,
                                current_principal.account.id,
                                initial_room.room_code,
                            )
                        except LobbyError:
                            join_feedback.set_text(
                                "部屋の状態が変わりました。"
                                "ロビーから最新の状態を確認してください。"
                            )
                        except Exception:
                            LOGGER.exception(
                                "unexpected invite acceptance failure"
                            )
                            join_feedback.set_text(
                                "部屋へ参加できませんでした。"
                            )
                        else:
                            ui.navigate.to(
                                f"/room/{joined.room_code}"
                            )
                            return
                        finally:
                            join_busy = False
                            if (
                                len(initial_room.players)
                                < initial_room.max_players
                                and initial_room.status
                                is StoredRoomStatus.WAITING
                            ):
                                player_join_button.enable()
                            if initial_room.allow_spectators:
                                spectator_join_button.enable()

                    player_join_button = ui.button(
                        "対戦参加",
                        icon="sports_esports",
                        on_click=lambda: accept_invite(
                            as_spectator=False
                        ),
                    ).props("unelevated no-caps").classes("w-full")
                    spectator_join_button = ui.button(
                        "観戦参加",
                        icon="visibility",
                        on_click=lambda: accept_invite(
                            as_spectator=True
                        ),
                    ).props("outline no-caps").classes("w-full")
                    if (
                        initial_room.status is StoredRoomStatus.ACTIVE
                        or len(initial_room.players)
                        >= initial_room.max_players
                    ):
                        player_join_button.disable()
                        if (
                            initial_room.status
                            is StoredRoomStatus.ACTIVE
                        ):
                            player_join_button.set_text("試合中（観戦のみ）")
                    if not initial_room.allow_spectators:
                        spectator_join_button.disable()
                        spectator_join_button.set_text(
                            "この部屋は観戦できません"
                        )

    @ui.page("/room/{room_code}")
    async def waiting_room_page(room_code: str, request: Request):
        principal = await principal_for(request)
        if principal is None:
            next_path = f"/room/{room_code}"
            return RedirectResponse(
                f"/login?next={next_path}", status_code=303
            )
        if lobby is None or rooms is None:
            return RedirectResponse("/lobby", status_code=303)

        user_id = principal.account.id
        try:
            initial_room = await asyncio.to_thread(
                lobby.get_room, room_code
            )
        except (LobbyError, ValueError):
            return RedirectResponse("/lobby", status_code=303)
        if initial_room.member_for(user_id) is None:
            return RedirectResponse("/lobby", status_code=303)
        if initial_room.status is StoredRoomStatus.ACTIVE:
            try:
                game_id = await asyncio.to_thread(
                    lobby.active_game_id,
                    user_id,
                    initial_room.room_code,
                )
            except LobbyError:
                return RedirectResponse("/lobby", status_code=303)
            await rooms.recover_after_restart(game_id)
            return RedirectResponse(
                f"/play/{game_id}", status_code=303
            )

        _page_shell()
        current_room = initial_room
        invite_url = _room_invite_url(
            request, initial_room.room_code
        )
        refreshing = False
        settings_saving = False
        settings_edit_revision = initial_room.revision

        def render_room(room) -> None:
            nonlocal current_room
            current_room = room
            room_name_label.set_text(room.name)
            room_code_label.set_text(
                f"参加コード: {room.room_code}"
            )
            visibility = (
                "公開" if room.is_public else "非公開"
            )
            bot_fill = (
                "不足分はNormal Bot"
                if room.fill_empty_seats_with_bots
                else "Bot補充なし"
            )
            spectator_access = (
                "新規観戦可"
                if room.allow_spectators
                else "新規観戦不可"
            )
            settings_label.set_text(
                f"{visibility}・制限時間: "
                f"{_room_timer_text(room.turn_seconds)}・"
                f"最大{room.max_players}人・{bot_fill}・{spectator_access}"
                f"・ライフ{room.lives_per_player}"
            )
            members_box.clear()
            with members_box:
                ui.label("対戦参加者").classes("aside-title")
                for index, member in enumerate(room.players, start=1):
                    name = (
                        "あなた"
                        if member.user_id == user_id
                        else f"参加者 {index}"
                    )
                    ready = "準備OK" if member.ready else "準備中"
                    with ui.row().classes(
                        "w-full items-center justify-between"
                    ):
                        ui.label(name)
                        ui.label(ready).classes("platform-muted")
                ui.label(
                    f"観戦者: {len(room.spectators)}人"
                ).classes("platform-muted")

            member = room.member_for(user_id)
            is_player = (
                member is not None
                and member.role is RoomRole.PLAYER
            )
            ready_button.set_visibility(is_player)
            if is_player:
                ready_button.enable()
                ready_button.set_text(
                    "準備を取り消す"
                    if member.ready
                    else "準備OKにする"
                )
            is_owner = room.owner_user_id == user_id
            settings_button.set_visibility(is_owner)
            if is_owner:
                if settings_saving:
                    settings_button.disable()
                else:
                    settings_button.enable()
            else:
                settings_button.disable()
                settings_dialog.close()
            start_button.set_visibility(is_owner)
            if is_owner and room.all_players_ready:
                start_button.enable()
                if room.fill_empty_seats_with_bots:
                    missing = room.max_players - len(room.players)
                    message_label.set_text(
                        "参加者の準備が完了しました。"
                        f"開始すると空き{missing}席を"
                        "Normal Botで補います。"
                    )
                else:
                    message_label.set_text(
                        "全員の準備が完了しました。"
                    )
            elif is_owner:
                start_button.disable()
                if room.fill_empty_seats_with_bots:
                    message_label.set_text(
                        "参加中の全プレイヤーが準備OKなら、"
                        "1人でも開始できます。"
                    )
                else:
                    message_label.set_text(
                        "2人以上が準備OKになると開始できます。"
                    )
            elif is_player:
                message_label.set_text(
                    "部屋の作成者が開始するまでお待ちください。"
                )
            else:
                message_label.set_text(
                    "観戦者として参加しています。"
                )

        async def waiting_session_is_valid() -> bool:
            current_principal = await principal_for(request)
            if (
                current_principal is not None
                and current_principal.account.id == user_id
            ):
                return True
            poll_timer.deactivate()
            ui.navigate.to(
                f"/login?next=/room/{current_room.room_code}"
            )
            return False

        async def refresh_room() -> None:
            nonlocal refreshing
            if refreshing or not await waiting_session_is_valid():
                return
            refreshing = True
            try:
                room = await asyncio.to_thread(
                    lobby.get_room, room_code
                )
                if room.status is StoredRoomStatus.ACTIVE:
                    game_id = await asyncio.to_thread(
                        lobby.active_game_id,
                        user_id,
                        room.room_code,
                    )
                    poll_timer.deactivate()
                    ui.navigate.to(f"/play/{game_id}")
                    return
                if room.member_for(user_id) is None:
                    poll_timer.deactivate()
                    ui.navigate.to("/lobby")
                    return
                render_room(room)
            except (LobbyError, ValueError):
                poll_timer.deactivate()
                message_label.set_text(
                    "部屋が終了したか、30分間動きがなかったため削除されました。"
                    "ロビーへ戻ってください。"
                )
            except Exception:
                LOGGER.exception("failed to refresh waiting room")
            finally:
                refreshing = False

        async def toggle_ready() -> None:
            if not await waiting_session_is_valid():
                return
            displayed_room = current_room
            member = displayed_room.member_for(user_id)
            if member is None or member.role is not RoomRole.PLAYER:
                return
            ready_button.disable()
            try:
                room = await asyncio.to_thread(
                    lobby.set_ready,
                    user_id,
                    displayed_room.room_code,
                    ready=not member.ready,
                    expected_gameplay_settings=(
                        displayed_room.max_players,
                        displayed_room.turn_seconds,
                        displayed_room.fill_empty_seats_with_bots,
                        displayed_room.lives_per_player,
                    ),
                )
            except LobbyRevisionConflict as error:
                latest = error.current_room
                if latest is not None:
                    render_room(latest)
                else:
                    await refresh_room()
                message_label.set_text(
                    "対戦設定が変更されました。最新の内容を確認して、"
                    "もう一度準備OKを押してください。"
                )
            except LobbyError:
                message_label.set_text(
                    "準備状態を変更できませんでした。"
                )
            else:
                render_room(room)

        async def start_match() -> None:
            if not await waiting_session_is_valid():
                return
            start_button.disable()
            try:
                result = await asyncio.to_thread(
                    lobby.start,
                    user_id,
                    current_room.room_code,
                )
                await rooms.recover_after_restart(result.game_id)
            except (LobbyError, RoomError):
                message_label.set_text(
                    "全員の準備を確認してください。"
                )
                await refresh_room()
            else:
                poll_timer.deactivate()
                ui.navigate.to(f"/play/{result.game_id}")

        async def leave_room() -> None:
            if not await waiting_session_is_valid():
                return
            try:
                await asyncio.to_thread(
                    lobby.leave,
                    user_id,
                    current_room.room_code,
                )
            except LobbyError:
                message_label.set_text(
                    "現在は退出できません。"
                )
            else:
                poll_timer.deactivate()
                ui.navigate.to("/lobby")

        def populate_settings_editor(room: LobbyRoomSnapshot) -> None:
            nonlocal settings_edit_revision
            settings_edit_revision = room.revision
            settings_players_select.set_value(room.max_players)
            settings_lives_select.set_value(room.lives_per_player)
            settings_timer_select.set_value(
                _room_timer_value(room.turn_seconds)
            )
            settings_spectator_switch.set_value(room.allow_spectators)
            settings_public_switch.set_value(room.is_public)
            settings_bot_fill_switch.set_value(
                room.fill_empty_seats_with_bots
            )

        def open_settings_editor() -> None:
            if (
                settings_saving
                or current_room.owner_user_id != user_id
                or current_room.status is not StoredRoomStatus.WAITING
            ):
                return
            settings_error.set_text("")
            populate_settings_editor(current_room)
            settings_dialog.open()

        async def save_room_settings() -> None:
            nonlocal settings_saving
            if settings_saving:
                return
            settings_saving = True
            settings_save_button.disable()
            settings_cancel_button.disable()
            settings_button.disable()
            try:
                if not await waiting_session_is_valid():
                    return
                max_players = settings_players_select.value
                if type(max_players) is not int or not 2 <= max_players <= 8:
                    raise ValueError("invalid player count")
                lives_per_player = _validate_lives_per_player(
                    settings_lives_select.value
                )
                turn_seconds = _parse_room_timer_value(
                    settings_timer_select.value
                )
                allow_spectators = settings_spectator_switch.value
                is_public = settings_public_switch.value
                fill_empty_seats_with_bots = (
                    settings_bot_fill_switch.value
                )
                if any(
                    type(value) is not bool
                    for value in (
                        allow_spectators,
                        is_public,
                        fill_empty_seats_with_bots,
                    )
                ):
                    raise ValueError("invalid room switch")
                gameplay_changed = (
                    current_room.max_players != max_players
                    or current_room.lives_per_player != lives_per_player
                    or current_room.turn_seconds != turn_seconds
                    or current_room.fill_empty_seats_with_bots
                    is not fill_empty_seats_with_bots
                )
                room = await asyncio.to_thread(
                    lobby.update_settings,
                    user_id,
                    current_room.room_code,
                    expected_revision=settings_edit_revision,
                    max_players=max_players,
                    lives_per_player=lives_per_player,
                    allow_spectators=allow_spectators,
                    turn_seconds=turn_seconds,
                    is_public=is_public,
                    fill_empty_seats_with_bots=(
                        fill_empty_seats_with_bots
                    ),
                )
            except LobbyRevisionConflict as error:
                latest = error.current_room
                if latest is not None:
                    render_room(latest)
                    populate_settings_editor(latest)
                else:
                    await refresh_room()
                settings_error.set_text(
                    "参加者または設定が更新されました。"
                    "最新の内容を確認して、もう一度保存してください。"
                )
            except LobbyCapacityError:
                settings_error.set_text(
                    "現在の対戦参加者より少ない人数には変更できません。"
                )
            except LobbyAuthorizationError:
                settings_error.set_text(
                    "部屋の設定を変更できるのは現在の部屋主だけです。"
                )
            except LobbyStateError:
                settings_error.set_text(
                    "部屋の設定は待機中だけ変更できます。"
                )
            except (LobbyError, TypeError, ValueError):
                settings_error.set_text(
                    "設定内容を確認して、もう一度お試しください。"
                )
            except Exception:
                LOGGER.exception("failed to update waiting room settings")
                settings_error.set_text(
                    "設定を保存できませんでした。少し待ってお試しください。"
                )
            else:
                render_room(room)
                settings_dialog.close()
                message_label.set_text(
                    "設定を保存しました。対戦条件が変わったため、"
                    "全員の準備を解除しました。"
                    if gameplay_changed
                    else "設定を保存しました。"
                )
            finally:
                settings_saving = False
                settings_save_button.enable()
                settings_cancel_button.enable()
                if current_room.owner_user_id == user_id:
                    settings_button.enable()
        def copy_invite_url() -> None:

            ui.clipboard.write(invite_url)
            invite_feedback.set_text(
                "招待URLをコピーしました。"
            )

        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("platform-wrap"):
                with ui.row().classes(
                    "w-full items-center justify-between gap-3"
                ):
                    with ui.column():
                        room_name_label = ui.label(
                            initial_room.name
                        ).classes("auth-title")
                        room_code_label = ui.label("").classes(
                            "aside-title"
                        )
                        settings_label = ui.label("").classes(
                            "platform-muted"
                        )
                    ui.link("ロビーへ", "/lobby").classes(
                        "platform-link"
                    )
                with ui.column().classes(
                    "dashboard-card invite-panel"
                ):
                    ui.label("部屋を招待する").classes("aside-title")
                    invite_input = ui.input(
                        label="招待URL",
                        value=invite_url,
                    ).props("outlined readonly").classes("w-full")
                    ui.button(
                        "URLをコピー",
                        icon="content_copy",
                        on_click=copy_invite_url,
                    ).props("outline no-caps").classes(
                        "w-full invite-copy-button"
                    )
                    invite_feedback = ui.label(
                        "公開・非公開に関係なく、このURLから参加できます。"
                    ).classes("platform-muted").props(
                        "role='status' aria-live='polite'"
                    )
                with ui.element("section").classes("dashboard-grid"):
                    with ui.column().classes("dashboard-card"):
                        members_box = ui.column().classes("w-full gap-2")
                    with ui.column().classes("dashboard-card"):
                        message_label = ui.label("").classes(
                            "platform-muted"
                        ).props("role='status' aria-live='polite'")
                        settings_button = ui.button(
                            "部屋設定を変更",
                            icon="tune",
                            on_click=open_settings_editor,
                        ).props("outline no-caps").classes("w-full")
                        settings_button.set_visibility(False)
                        ready_button = ui.button(
                            "準備OKにする",
                            on_click=toggle_ready,
                        ).props("outline no-caps").classes("w-full")
                        start_button = ui.button(
                            "対戦を始める",
                            icon="play_arrow",
                            on_click=start_match,
                        ).props("unelevated no-caps").classes("w-full")
                        ui.button(
                            "部屋から退出",
                            on_click=leave_room,
                        ).props("flat no-caps").classes("w-full")

        with ui.dialog() as settings_dialog, ui.card().classes(
            "confirm-dialog room-settings-dialog"
        ):
            ui.label("部屋設定を変更").classes("auth-title")
            ui.label(
                "部屋主だけが、対戦を始める前に変更できます。"
            ).classes("platform-muted")
            settings_players_select = ui.select(
                options={
                    number: f"{number}人"
                    for number in range(2, 9)
                },
                value=initial_room.max_players,
                label="最大人数",
            ).props("outlined options-dense").classes("w-full")
            settings_lives_select = ui.select(
                options=_life_count_options(),
                value=initial_room.lives_per_player,
                label="ライフ数",
            ).props("outlined options-dense").classes("w-full")
            settings_timer_select = ui.select(
                options={
                    "unlimited": "無制限",
                    "3": "3秒",
                    "10": "10秒",
                    "30": "30秒",
                    "60": "1分",
                    "180": "3分",
                },
                value=_room_timer_value(initial_room.turn_seconds),
                label="1手の制限時間",
            ).props("outlined options-dense").classes("w-full")
            settings_spectator_switch = ui.switch(
                "観戦を許可する",
                value=initial_room.allow_spectators,
            )
            settings_public_switch = ui.switch(
                "ロビーの公開部屋一覧に表示する",
                value=initial_room.is_public,
            )
            settings_bot_fill_switch = ui.switch(
                "不足人数をNormal Botで補う",
                value=initial_room.fill_empty_seats_with_bots,
            )
            ui.label(
                "最大人数・制限時間・ライフ数・Bot補充を変えると、"
                "全員の準備状態が解除されます。"
            ).classes("platform-muted room-setting-help")
            ui.label(
                "観戦をOFFにしても、すでに入室している観戦者は"
                "退出するまで残り、新しい観戦参加だけを止めます。"
            ).classes("platform-muted room-setting-help")
            settings_error = ui.label("").classes(
                "auth-error"
            ).props("role='alert' aria-live='assertive'")
            with ui.row().classes(
                "w-full items-center justify-end gap-2"
            ):
                settings_cancel_button = ui.button(
                    "キャンセル",
                    on_click=settings_dialog.close,
                ).props("flat no-caps")
                settings_save_button = ui.button(
                    "設定を保存",
                    icon="save",
                    on_click=save_room_settings,
                ).props("unelevated no-caps")
        render_room(initial_room)
        poll_timer = ui.timer(1.5, refresh_room)

    @ui.page("/play/{game_id}")
    async def solo_play_page(game_id: str, request: Request):
        principal = await principal_for(request)
        if principal is None:
            next_path = f"/play/{game_id}"
            return RedirectResponse(
                f"/login?next={next_path}", status_code=303
            )
        if solo is None or rooms is None or room_words is None:
            return RedirectResponse("/lobby", status_code=303)

        _page_shell()
        user_id = principal.account.id
        client = ui.context.client
        try:
            preference_storage = app.storage.user
            sound_muted = (
                preference_storage.get("game_sound_muted", False) is True
            )
            reduced_motion = (
                preference_storage.get("game_reduced_motion", False) is True
            )
        except RuntimeError:
            preference_storage = None
            sound_muted = False
            reduced_motion = False
        current_snapshot: RoomSnapshot | None = None
        seat_display_names = {user_id: principal.account.display_name}
        display_name_user_ids: tuple[str, ...] = ()
        pending_submission: tuple[str, int, str] | None = None
        transient_feedback: _VersionedFeedback | None = None
        rendered_history: tuple[object, ...] | None = None
        animation_tasks: set[asyncio.Task[None]] = set()
        reaction_bubbles: OrderedDict[str, object] = OrderedDict()
        reaction_buttons: list[object] = []
        reaction_sending = False
        attaching = False
        submitting = False
        surrendering = False
        post_match_transitioning = False
        post_match_task: asyncio.Task[None] | None = None
        post_match_auto_return_cancelled = False
        polling = False
        room_closed = False
        session_invalidated = False

        async def refresh_seat_display_names(
            snapshot: RoomSnapshot,
        ) -> None:
            nonlocal seat_display_names, display_name_user_ids
            player_user_ids = tuple(
                dict.fromkeys(
                    seat.owner_user_id
                    for seat in snapshot.players
                    if seat.owner_user_id is not None
                )
            )
            if player_user_ids == display_name_user_ids:
                return
            try:
                resolved = await asyncio.to_thread(
                    auth.display_names_for_user_ids,
                    player_user_ids,
                )
            except Exception:
                LOGGER.exception("failed to resolve room display names")
                resolved = {}
            resolved[user_id] = principal.account.display_name
            seat_display_names = resolved
            display_name_user_ids = player_user_ids
        def persist_game_preferences() -> None:

            if preference_storage is None:
                return
            try:
                preference_storage["game_sound_muted"] = sound_muted
                preference_storage["game_reduced_motion"] = reduced_motion
            except Exception:
                LOGGER.exception("failed to persist game UI preferences")

        def trigger_sound(cue: str) -> None:
            if sound_muted or client.is_deleted:
                return
            # The generated oscillator cue has no external or copyrighted
            # asset. Its JavaScript catches autoplay/security failures.
            client.run_javascript(_sound_cue_script(cue))

        def animate_element(
            element: object,
            css_class: str,
            *,
            seconds: float = 0.9,
        ) -> None:
            if reduced_motion or client.is_deleted:
                return
            element.classes(add=css_class)

            async def remove_effect() -> None:
                await asyncio.sleep(seconds)
                if client.is_deleted:
                    return
                with client:
                    element.classes(remove=css_class)

            task = asyncio.create_task(remove_effect())
            animation_tasks.add(task)
            task.add_done_callback(animation_tasks.discard)

        def apply_snapshot_effect(
            effect: _SnapshotEffect | None,
        ) -> None:
            if effect is None:
                return
            if effect.kind == "accepted":
                animate_element(history_box, "game-effect--accepted")
            elif effect.kind in {"life_loss", "elimination"}:
                animate_element(feedback_label, "game-effect--elimination")
            elif effect.kind == "finish":
                animate_element(result_panel, "game-effect--finish")
            trigger_sound(effect.sound)

        def toggle_sound(_event: object | None = None) -> None:
            nonlocal sound_muted
            sound_muted = not sound_muted
            sound_button.set_icon(
                "volume_off" if sound_muted else "volume_up"
            )
            sound_button.set_text(
                "効果音 OFF" if sound_muted else "効果音 ON"
            )
            sound_button.props(
                "aria-label='効果音をオンにする'"
                if sound_muted
                else "aria-label='効果音をオフにする'"
            )
            persist_game_preferences()
            if not sound_muted:
                trigger_sound("accepted")

        def toggle_reduced_motion(event: object) -> None:
            nonlocal reduced_motion
            reduced_motion = getattr(event, "value", False) is True
            if reduced_motion:
                game_main.classes(add="motion-reduced")
            else:
                game_main.classes(remove="motion-reduced")
            persist_game_preferences()

        def sync_reaction_buttons() -> None:
            allowed = (
                not session_invalidated
                and not room_closed
                and not reaction_sending
                and current_snapshot is not None
                and current_snapshot.role_for_user(user_id) is not None
            )
            for button in reaction_buttons:
                if allowed:
                    button.enable()
                else:
                    button.disable()

        def show_room_reaction(reaction: RoomReaction) -> None:
            snapshot = current_snapshot
            if snapshot is None or client.is_deleted:
                return
            sender = _reaction_sender_label(
                snapshot,
                reaction,
                user_id,
            )
            token = uuid4().hex
            with reaction_feed_box:
                with ui.row().classes(
                    "reaction-bubble reaction-bubble--enter "
                    "items-center gap-2"
                ).props(
                    f"aria-label='{sender}が"
                    f"{reaction.emoji}でリアクション'"
                ) as bubble:
                    ui.label(reaction.emoji).classes(
                        "reaction-bubble-emoji"
                    ).props("aria-hidden='true'")
                    ui.label(sender).classes("reaction-bubble-sender")
            reaction_bubbles[token] = bubble
            while len(reaction_bubbles) > 4:
                _old_token, old_bubble = reaction_bubbles.popitem(
                    last=False
                )
                old_bubble.delete()

            async def expire_reaction() -> None:
                await asyncio.sleep(3.2)
                stored = reaction_bubbles.pop(token, None)
                if stored is None or client.is_deleted:
                    return
                with client:
                    stored.delete()

            task = asyncio.create_task(expire_reaction())
            animation_tasks.add(task)
            task.add_done_callback(animation_tasks.discard)

        async def release_reaction_buttons(delay: float) -> None:
            nonlocal reaction_sending
            if delay > 0:
                await asyncio.sleep(delay)
            reaction_sending = False
            if client.is_deleted or session_invalidated:
                return
            with client:
                sync_reaction_buttons()

        async def send_room_reaction(
            emoji: str,
            _event: object | None = None,
        ) -> None:
            nonlocal reaction_sending
            snapshot = current_snapshot
            if (
                reaction_sending
                or emoji not in SUPPORTED_REACTIONS
                or snapshot is None
                or snapshot.role_for_user(user_id) is None
                or room_closed
                or session_invalidated
            ):
                return
            reaction_sending = True
            sync_reaction_buttons()
            cooldown = 0.0
            try:
                if not await play_session_is_valid():
                    return
                await rooms.send_reaction(game_id, user_id, emoji)
            except ReactionRateLimitError as error:
                cooldown = max(0.1, error.retry_after_seconds)
                reaction_feedback_label.set_text(
                    f"あと{max(1, math.ceil(cooldown))}秒ほど"
                    "待ってから送ってください。"
                )
            except RoomError:
                LOGGER.exception("room reaction failed")
                reaction_feedback_label.set_text(
                    "リアクションを送れませんでした。"
                    "対局への接続を確認してください。"
                )
            except Exception:
                LOGGER.exception("unexpected room reaction failure")
                reaction_feedback_label.set_text(
                    "リアクションを送れませんでした。"
                    "少し待ってからお試しください。"
                )
            else:
                cooldown = 1.0
                reaction_feedback_label.set_text(
                    "リアクションを送りました。"
                )
            finally:
                task = asyncio.create_task(
                    release_reaction_buttons(cooldown)
                )
                animation_tasks.add(task)
                task.add_done_callback(animation_tasks.discard)

        def can_submit(snapshot: RoomSnapshot) -> bool:
            seat = snapshot.seat_for_user(user_id)
            return (
                not session_invalidated
                and not room_closed
                and snapshot.status is RoomStatus.ACTIVE
                and seat is not None
                and snapshot.current_turn == seat.index
                and seat.controller is SeatController.HUMAN
            )

        def show_transient_feedback(message: str) -> None:
            nonlocal transient_feedback
            state_version = (
                current_snapshot.state_version
                if current_snapshot is not None
                else None
            )
            transient_feedback = _VersionedFeedback(
                state_version=state_version,
                message=message,
            )
            if (
                current_snapshot is None
                or current_snapshot.status is not RoomStatus.FINISHED
            ):
                feedback_label.set_text(message)

        def render(snapshot: RoomSnapshot) -> None:
            nonlocal current_snapshot, rendered_history, transient_feedback
            if session_invalidated or room_closed:
                return
            if (
                current_snapshot is not None
                and snapshot.state_version < current_snapshot.state_version
            ):
                return
            transient_message = _feedback_for_version(
                transient_feedback,
                snapshot.state_version,
            )
            effect = _snapshot_effect(
                current_snapshot,
                snapshot,
                user_id,
            )
            if transient_message is None:
                transient_feedback = None
            current_snapshot = snapshot
            timer = (
                "無制限"
                if snapshot.turn_seconds is None
                else f"{snapshot.turn_seconds}秒"
            )
            oni_presentation = _oni_challenge_presentation(
                oni_rules, snapshot
            )
            if snapshot.rule_set is RoomRuleSet.ONI:
                game_title.set_text("鬼しりとり")
                settings_label.set_text("Hard Bot 1体・30秒・ライフ3（固定）")
            elif snapshot.mode.value == "solo_bot":
                game_title.set_text("1人でBot戦")
                difficulty = _solo_difficulty_options().get(
                    snapshot.bot_difficulty,
                    snapshot.bot_difficulty,
                )
                settings_label.set_text(
                    f"Bot {len(snapshot.players) - 1}体・"
                    f"{difficulty}・{timer}・ライフ{snapshot.lives_per_player}"
                )
            else:
                game_title.set_text("オンライン対戦")
                settings_label.set_text(
                    f"{len(snapshot.players)}人対戦・{timer}・ライフ{snapshot.lives_per_player}"
                )
            status_names = {
                RoomStatus.ACTIVE: "対局中",
                RoomStatus.PAUSED: "中断中",
                RoomStatus.FINISHED: "終了",
            }
            status_label.set_text(status_names[snapshot.status])
            if (
                snapshot.status is RoomStatus.ACTIVE
                and snapshot.eliminated_seats
            ):
                status_label.set_text(
                    f"対局中・残り{len(snapshot.active_seat_indexes)}人"
                )
            expected_label.set_text(
                canonical_kana(snapshot.expected_kana)
                if snapshot.expected_kana is not None
                else "自由"
            )
            latest_word_label.set_text(
                f"前の人の単語：{_latest_word_text(snapshot.history)}"
            )
            if oni_presentation is None:
                oni_command_panel.set_visibility(False)
            else:
                oni_command_panel.set_visibility(True)
                commands_text = (
                    " / ".join(oni_presentation.commands)
                    if oni_presentation.commands
                    else "準備中（入力はサーバーで判定されます）"
                )
                oni_commands_label.set_text(
                    f"今回の鬼命令：{commands_text}"
                )
                sealed_text = (
                    "・".join(oni_presentation.sealed_endings)
                    if oni_presentation.sealed_endings
                    else "なし"
                )
                oni_seals_label.set_text(f"末尾封印：{sealed_text}")
                candidate_text = (
                    "確認中"
                    if oni_presentation.candidate_count is None
                    else f"{oni_presentation.candidate_count}語"
                )
                oni_candidate_label.set_text(
                    f"既知の正解候補：{candidate_text}"
                )
                oni_relaxed_label.set_text(
                    "候補確保のため古い封印を"
                    f"{oni_presentation.relaxed_seal_count}個解除しました。"
                )
                oni_relaxed_label.set_visibility(
                    oni_presentation.relaxed_seal_count > 0
                )

            turn_owner = _turn_seat_label(
                snapshot, user_id, seat_display_names
            )
            turn_label.set_text(f"{turn_owner}の番です")
            if _has_user_human_turn(snapshot, user_id):
                turn_card.classes(add="game-turn-card--mine")
            else:
                turn_card.classes(remove="game-turn-card--mine")
            deadline = _deadline_presentation(snapshot.deadline_at)
            deadline_label.set_text(deadline.text)
            deadline_label.classes(
                add=f"deadline--{deadline.level}",
                remove=(
                    "deadline--normal deadline--warning deadline--danger"
                ),
            )
            player_status_box.clear()
            with player_status_box:
                for seat in snapshot.players:
                    seat_status = ui.label(
                        _seat_life_text(
                            snapshot,
                            seat.index,
                            user_id,
                            seat_display_names,
                        )
                    ).classes("w-full")
                    if (
                        snapshot.status is RoomStatus.ACTIVE
                        and snapshot.current_turn == seat.index
                    ):
                        seat_status.classes(add="aside-title")
                    else:
                        seat_status.classes(add="platform-muted")
            if snapshot.life_loss_events:
                latest_loss = snapshot.life_loss_events[-1]
                life_event_label.set_text(
                    "直近のライフ変動："
                    + _life_loss_event_text(
                        snapshot, latest_loss, user_id, seat_display_names
                    )
                )
                life_event_label.set_visibility(True)
            else:
                life_event_label.set_visibility(False)

            history_signature = tuple(snapshot.history)
            history_should_scroll = _history_was_appended(
                rendered_history,
                history_signature,
            )
            if history_signature != rendered_history:
                history_box.clear()
                with history_box:
                    if not snapshot.history:
                        ui.label(
                            "先攻は好きな辞書単語から始められます。"
                        ).classes("platform-muted")
                    for index, record in enumerate(
                        snapshot.history, start=1
                    ):
                        if record.by_bot:
                            actor = f"Bot {record.seat_index + 1}"
                        elif record.actor_user_id == user_id:
                            actor = "あなた"
                        else:
                            actor = f"プレイヤー{record.seat_index + 1}"
                        with ui.row().classes(
                            "game-history-row w-full items-center "
                            "justify-between gap-3 "
                            + (
                                "game-history-row--new"
                                if (
                                    effect is not None
                                    and effect.kind == "accepted"
                                    and index == len(snapshot.history)
                                )
                                else ""
                            )
                        ):
                            ui.label(
                                f"{index}. {record.surface}"
                            ).classes("aside-title")
                            ui.label(
                                f"{actor}・よみ: {record.reading}"
                            ).classes("platform-muted")
                rendered_history = history_signature
                if history_should_scroll and not client.is_deleted:
                    client.run_javascript(
                        _history_scroll_script(
                            reduced_motion=reduced_motion,
                        )
                    )

            allowed = (
                can_submit(snapshot)
                and not submitting
                and not deadline.expired
            )
            if allowed:
                word_input.enable()
                submit_button.enable()
            else:
                word_input.disable()
                submit_button.disable()

            surrender_allowed = (
                not session_invalidated
                and not room_closed
                and _can_surrender(snapshot, user_id)
            )
            surrender_button.set_visibility(surrender_allowed)
            if surrender_allowed and not surrendering:
                surrender_button.enable()
            else:
                surrender_button.disable()
            finished = snapshot.status is RoomStatus.FINISHED
            is_solo = snapshot.mode is RoomMode.SOLO_BOT
            result_panel.set_visibility(finished)
            post_match_panel.set_visibility(finished)
            solo_rematch_button.set_visibility(finished and is_solo)
            waiting_room_button.set_visibility(finished and not is_solo)
            if finished:
                result = _match_result_presentation(snapshot, user_id, seat_display_names)
                result_panel.classes(
                    add=f"match-result--{result.tone}",
                    remove=(
                        "match-result--victory match-result--defeat "
                        "match-result--neutral"
                    ),
                )
                result_title.set_text(result.title)
                result_outcome.set_text(result.outcome)
                result_words.set_text(
                    f"{result.accepted_word_count}語"
                )
                result_reason.set_text(result.end_reason)
                result_round.set_text(result.round_summary)
                result_last_word.set_text(result.last_word)
                result_life_loss_box.clear()
                with result_life_loss_box:
                    if result.life_loss_history:
                        for entry in result.life_loss_history:
                            ui.label(f"・{entry}").classes(
                                "platform-muted w-full"
                            )
                    else:
                        ui.label("ライフ損失なし").classes("platform-muted")
                result_share_payload.set_text(
                    _result_share_text(result)
                )
            if finished and is_solo:
                post_match_label.set_text(
                    "Bot数・難易度・制限時間・ライフ数を変えずに、"
                    "新しい対局へ挑戦できます。"
                )
            elif finished:
                if post_match_auto_return_cancelled:
                    post_match_label.set_text(
                        "共有後、「待機部屋へ戻る」を押してください。"
                    )
                else:
                    post_match_label.set_text(
                        "12秒後に待機画面へ戻ります。"
                        "結果を共有すると自動移動を停止します。"
                    )
            if post_match_transitioning:
                solo_rematch_button.disable()
                waiting_room_button.disable()
            else:
                solo_rematch_button.enable()
                waiting_room_button.enable()


            if snapshot.status is RoomStatus.FINISHED:
                reasons = {
                    "ends_with_n": "「ん」で終わったため終了しました。",
                    "duplicate": "同じ読みを使ったため終了しました。",
                    "timeout": "時間切れで終了しました。",
                    "no_legal_move": "出せる単語がなく終了しました。",
                    "surrender": "降参により終了しました。",
                }
                feedback_label.set_text(
                    reasons.get(
                        snapshot.end_reason,
                        "対局が終了しました。",
                    )
                )
                winner_indexes = snapshot.active_seat_indexes
                if len(winner_indexes) == 1:
                    winner_index = winner_indexes[0]
                    own_seat = snapshot.seat_for_user(user_id)
                    winner_text = (
                        "あなたの勝ちです！"
                        if (
                            own_seat is not None
                            and own_seat.index == winner_index
                        )
                        else f"プレイヤー{winner_index + 1}の勝ちです。"
                    )
                    if (
                        snapshot.end_reason == "surrender"
                        and snapshot.losing_seat is not None
                    ):
                        surrender_text = (
                            "あなたは降参しました。"
                            if (
                                own_seat is not None
                                and own_seat.index
                                == snapshot.losing_seat
                            )
                            else (
                                f"プレイヤー"
                                f"{snapshot.losing_seat + 1}が"
                                "降参しました。"
                            )
                        )
                        feedback_label.set_text(
                            f"{surrender_text}{winner_text}"
                        )
                    else:
                        feedback_label.set_text(winner_text)
            elif transient_message is not None:
                feedback_label.set_text(transient_message)
            elif snapshot.role_for_user(user_id) is Role.SPECTATOR:
                own_seat = snapshot.seat_for_user(user_id)
                own_loss = (
                    next(
                        (
                            event
                            for event in reversed(snapshot.life_loss_events)
                            if event.seat_index == own_seat.index
                        ),
                        None,
                    )
                    if own_seat is not None
                    else None
                )
                if own_loss is not None and own_loss.eliminated:
                    feedback_label.set_text(
                        _life_loss_event_text(
                            snapshot, own_loss, user_id, seat_display_names
                        )
                        + "。観戦中です。"
                    )
                else:
                    feedback_label.set_text(
                        "脱落しました。観戦中です。"
                        if own_seat is not None
                        else "観戦者として観戦中です。"
                    )
            elif deadline.expired:
                feedback_label.set_text(
                    "時間切れを確定しています。"
                )
            elif allowed:
                feedback_label.set_text(
                    "辞書にある単語を入力してください。"
                )
            elif snapshot.life_loss_events:
                latest_loss = snapshot.life_loss_events[-1]
                feedback_label.set_text(
                    _life_loss_event_text(
                        snapshot, latest_loss, user_id, seat_display_names
                    )
                    + f"。対戦継続中は残り{len(snapshot.active_seat_indexes)}人です。"
                )
            else:
                feedback_label.set_text(
                    (
                        "Botの手を待っています。"
                        if snapshot.mode.value == "solo_bot"
                        else "相手の手を待っています。"
                    )
                )
            apply_snapshot_effect(effect)
            sync_reaction_buttons()

        def pause_auto_return_for_share(
            _event: object | None = None,
        ) -> None:
            nonlocal post_match_auto_return_cancelled, post_match_task
            snapshot = current_snapshot
            if (
                snapshot is None
                or snapshot.status is not RoomStatus.FINISHED
                or snapshot.mode is not RoomMode.PVP
            ):
                return
            post_match_auto_return_cancelled = True
            scheduled = post_match_task
            post_match_task = None
            if scheduled is not None and not scheduled.done():
                scheduled.cancel()
            post_match_label.set_text(
                "共有後、「待機部屋へ戻る」を押してください。"
            )

        async def delayed_return_to_waiting_room() -> None:
            await asyncio.sleep(12.0)
            if (
                post_match_auto_return_cancelled
                or client.is_deleted
                or session_invalidated
            ):
                return
            await return_to_waiting_room()

        def schedule_return_to_waiting_room() -> None:
            nonlocal post_match_task
            if post_match_auto_return_cancelled:
                return
            if post_match_task is not None and not post_match_task.done():
                return
            post_match_task = asyncio.create_task(
                delayed_return_to_waiting_room()
            )

        async def delayed_room_closed_redirect() -> None:
            await asyncio.sleep(2.0)
            if client.is_deleted or session_invalidated:
                return
            with client:
                ui.navigate.to("/lobby")

        def handle_closed_room(reason: str | None) -> None:
            nonlocal pending_submission, post_match_task, room_closed
            if room_closed or client.is_deleted:
                return
            room_closed = True
            pending_submission = None
            scheduled = post_match_task
            post_match_task = None
            if scheduled is not None and not scheduled.done():
                scheduled.cancel()
            poll_timer.deactivate()
            reading_dialog.close()
            surrender_dialog.close()
            word_input.disable()
            submit_button.disable()
            surrender_button.disable()
            surrender_button.set_visibility(False)
            turn_card.classes(remove="game-turn-card--mine")
            status_label.set_text("部屋終了")
            turn_label.set_text("この部屋での対局は終了しました")
            feedback_label.set_text(_room_closed_message(reason))
            reaction_feedback_label.set_text(
                "部屋が終了したため、リアクションは送れません。"
            )
            post_match_panel.set_visibility(False)
            for button in reaction_buttons:
                button.disable()
            task = asyncio.create_task(delayed_room_closed_redirect())
            animation_tasks.add(task)
            task.add_done_callback(animation_tasks.discard)

        async def on_room_event(event: RoomEvent) -> None:
            if session_invalidated or room_closed or client.is_deleted:
                return
            try:
                if event.kind is RoomEventKind.CLOSED:
                    with client:
                        handle_closed_room(event.reason)
                    return
                if event.kind is RoomEventKind.REACTION:
                    if event.reaction is not None:
                        with client:
                            show_room_reaction(event.reaction)
                    return
                if (
                    event.kind is not RoomEventKind.SNAPSHOT
                    or event.snapshot is None
                ):
                    return
                await refresh_seat_display_names(event.snapshot)
                with client:
                    render(event.snapshot)
                    if (
                        event.snapshot.status is RoomStatus.FINISHED
                        and event.snapshot.mode is RoomMode.PVP
                    ):
                        schedule_return_to_waiting_room()
            except Exception:
                LOGGER.exception("failed to render room event")

        async def invalidate_play_session() -> None:
            nonlocal pending_submission, session_invalidated
            if session_invalidated:
                return
            session_invalidated = True
            pending_submission = None
            poll_timer.deactivate()
            reading_dialog.close()
            surrender_dialog.close()
            word_input.disable()
            submit_button.disable()
            surrender_button.disable()
            surrender_button.set_visibility(False)
            sync_reaction_buttons()
            feedback_label.set_text(
                "セッションの有効期限が切れました。"
                "ログインし直してください。"
            )
            login_link.set_visibility(True)
            try:
                await rooms.disconnect_client(game_id, client.id)
            except Exception:
                LOGGER.exception(
                    "failed to disconnect invalid play session"
                )

        async def play_session_is_valid() -> bool:
            if session_invalidated:
                return False
            current_principal = await principal_for(request)
            if _session_principal_matches_user(
                current_principal, user_id
            ):
                return True
            await invalidate_play_session()
            return False

        async def attach() -> None:
            nonlocal attaching
            if attaching:
                return
            attaching = True
            try:
                if not await play_session_is_valid():
                    return
                snapshot = await rooms.connect_client(
                    game_id,
                    user_id,
                    client.id,
                    on_room_event,
                )
            except (SoloGameAuthorizationError, RoomError):
                LOGGER.exception("failed to open solo game")
                show_transient_feedback(
                    "この対局を開けません。"
                )
                word_input.disable()
                submit_button.disable()
            except Exception:
                LOGGER.exception("failed to connect solo game")
                show_transient_feedback(
                    "対局へ接続できませんでした。"
                )
            else:
                await refresh_seat_display_names(snapshot)
                render(snapshot)
            finally:
                attaching = False

        async def refresh_snapshot() -> None:
            """Keep clients in sync even across multiple server workers."""

            nonlocal polling
            if (
                polling
                or client.is_deleted
                or session_invalidated
                or room_closed
            ):
                return
            polling = True
            try:
                if not await play_session_is_valid():
                    return
                snapshot = await rooms.load_snapshot(game_id)
                if snapshot.role_for_user(user_id) is None:
                    feedback_label.set_text(
                        "この対局を開けません。"
                    )
                    word_input.disable()
                    submit_button.disable()
                    return
                await refresh_seat_display_names(snapshot)
                render(snapshot)
                if (
                    snapshot.status is RoomStatus.FINISHED
                    and snapshot.mode is RoomMode.PVP
                ):
                    schedule_return_to_waiting_room()
            except RoomError:
                handle_closed_room(None)
            except Exception:
                LOGGER.exception(
                    "failed to refresh game snapshot"
                )
            finally:
                polling = False

        async def detach() -> None:
            scheduled = post_match_task
            if scheduled is not None and not scheduled.done():
                scheduled.cancel()
            try:
                await rooms.disconnect_client(game_id, client.id)
            except Exception:
                LOGGER.exception("failed to disconnect solo client")

        async def perform_submission(
            surface: str,
            *,
            chosen_reading: str | None,
            expected_version: int,
            operation_id: str,
        ) -> None:
            nonlocal pending_submission, submitting
            if submitting:
                return
            submitting = True
            submit_button.disable()
            try:
                if not await play_session_is_valid():
                    return
                result = await room_words.submit_user_word(
                    game_id,
                    user_id,
                    surface,
                    chosen_reading=chosen_reading,
                    expected_version=expected_version,
                    operation_id=operation_id,
                )
            except RoomVersionConflict as error:
                pending_submission = None
                reading_dialog.close()
                if error.current_snapshot is not None:
                    render(error.current_snapshot)
                show_transient_feedback(
                    "状態が更新されました。もう一度お試しください。"
                )
            except RoomError:
                LOGGER.exception("solo word submission failed")
                pending_submission = None
                reading_dialog.close()
                show_transient_feedback(
                    "今はその単語を送信できません。"
                )
            except Exception:
                LOGGER.exception("unexpected solo submission failure")
                show_transient_feedback(
                    "単語を確認できませんでした。"
                )
            else:
                if (
                    result.status
                    is WordSubmissionStatus.READING_REQUIRED
                ):
                    pending_submission = (
                        surface,
                        expected_version,
                        operation_id,
                    )
                    reading_choices.clear()
                    with reading_choices:
                        for reading in result.reading_choices:
                            ui.button(
                                reading,
                                on_click=lambda _event=None,
                                value=reading: choose_reading(value),
                            ).props("outline no-caps").classes("w-full")
                    show_transient_feedback(result.message)
                    reading_dialog.open()
                elif (
                    result.status
                    is WordSubmissionStatus.COMMITTED
                ):
                    pending_submission = None
                    reading_dialog.close()
                    word_input.set_value("")
                    if (
                        result.outcome is not None
                        and result.outcome.snapshot is not None
                    ):
                        render(result.outcome.snapshot)
                    show_transient_feedback(result.message)
                else:
                    pending_submission = None
                    reading_dialog.close()
                    show_transient_feedback(result.message)
                    animate_element(
                        feedback_label,
                        "game-effect--error",
                    )
                    trigger_sound("error")
            finally:
                submitting = False
                if current_snapshot is not None:
                    render(current_snapshot)

        async def submit_word(
            _event: object | None = None,
        ) -> None:
            snapshot = current_snapshot
            if (
                snapshot is None
                or not can_submit(snapshot)
                or _deadline_presentation(snapshot.deadline_at).expired
            ):
                return
            await perform_submission(
                str(word_input.value or ""),
                chosen_reading=None,
                expected_version=snapshot.state_version,
                operation_id=uuid4().hex,
            )

        async def choose_reading(reading: str) -> None:
            pending = pending_submission
            if pending is None:
                reading_dialog.close()
                return
            surface, version, operation_id = pending
            await perform_submission(
                surface,
                chosen_reading=reading,
                expected_version=version,
                operation_id=operation_id,
            )

        def cancel_reading() -> None:
            nonlocal pending_submission
            pending_submission = None
            reading_dialog.close()
            show_transient_feedback("読みの選択を取り消しました。")

        def open_surrender_dialog() -> None:
            snapshot = current_snapshot
            if (
                snapshot is None
                or room_closed
                or surrendering
                or not _can_surrender(snapshot, user_id)
            ):
                return
            surrender_dialog.open()

        async def confirm_surrender() -> None:
            nonlocal surrendering
            snapshot = current_snapshot
            if (
                snapshot is None
                or room_closed
                or surrendering
                or not _can_surrender(snapshot, user_id)
            ):
                surrender_dialog.close()
                return
            surrendering = True
            surrender_button.disable()
            surrender_confirm_button.disable()
            try:
                if not await play_session_is_valid():
                    return
                outcome = await rooms.surrender(
                    game_id,
                    user_id,
                    expected_version=snapshot.state_version,
                    operation_id=uuid4().hex,
                )
            except RoomVersionConflict as error:
                if error.current_snapshot is not None:
                    render(error.current_snapshot)
                show_transient_feedback(
                    "状態が更新されました。"
                    "内容を確認してからもう一度お試しください。"
                )
            except RoomError:
                LOGGER.exception("room surrender failed")
                show_transient_feedback(
                    "現在は降参できません。"
                )
            except Exception:
                LOGGER.exception("unexpected surrender failure")
                show_transient_feedback(
                    "降参を確定できませんでした。"
                )
            else:
                if outcome.snapshot is not None:
                    render(outcome.snapshot)
                show_transient_feedback(
                    "降参しました。観戦に移ります。"
                )
            finally:
                surrendering = False
                surrender_confirm_button.enable()
                surrender_dialog.close()
                if current_snapshot is not None:
                    render(current_snapshot)

        async def rematch_solo(_event: object | None = None) -> None:
            nonlocal post_match_transitioning
            snapshot = current_snapshot
            if (
                post_match_transitioning
                or snapshot is None
                or snapshot.status is not RoomStatus.FINISHED
                or snapshot.mode is not RoomMode.SOLO_BOT
            ):
                return
            post_match_transitioning = True
            solo_rematch_button.disable()
            post_match_label.set_text("同じ設定で新しい対局を準備しています。")
            try:
                if not await play_session_is_valid():
                    return
                rematch = await solo.rematch(user_id, game_id)
            except (SoloGameAuthorizationError, RoomError):
                LOGGER.exception("failed to create solo rematch")
                post_match_label.set_text(
                    "再戦を準備できませんでした。もう一度お試しください。"
                )
            except Exception:
                LOGGER.exception("unexpected solo rematch failure")
                post_match_label.set_text(
                    "再戦を準備できませんでした。もう一度お試しください。"
                )
            else:
                poll_timer.deactivate()
                ui.navigate.to(f"/play/{rematch.room_id}")
                return
            finally:
                post_match_transitioning = False
                if not session_invalidated and not client.is_deleted:
                    solo_rematch_button.enable()

        async def return_to_waiting_room(
            _event: object | None = None,
        ) -> None:
            nonlocal post_match_auto_return_cancelled
            nonlocal post_match_task, post_match_transitioning
            scheduled = post_match_task
            current_task = asyncio.current_task()
            if scheduled is not current_task:
                post_match_auto_return_cancelled = True
                post_match_task = None
                if (
                    scheduled is not None
                    and not scheduled.done()
                ):
                    scheduled.cancel()
            snapshot = current_snapshot
            if (
                post_match_transitioning
                or snapshot is None
                or snapshot.status is not RoomStatus.FINISHED
                or snapshot.mode is not RoomMode.PVP
                or lobby is None
            ):
                return
            post_match_transitioning = True
            waiting_room_button.disable()
            post_match_label.set_text(
                "対局結果を保存しました。待機画面へ戻ります。"
            )
            try:
                if client.is_deleted or not await play_session_is_valid():
                    return
                waiting_room = await asyncio.to_thread(
                    _post_match_lobby_destination,
                    lobby,
                    user_id,
                    game_id,
                )
            except LobbyError:
                LOGGER.exception("failed to return finished match to lobby")
                post_match_label.set_text(
                    "待機画面へ自動で戻れませんでした。"
                    "ボタンからもう一度お試しください。"
                )
            except Exception:
                LOGGER.exception("unexpected post-match lobby failure")
                post_match_label.set_text(
                    "待機画面へ自動で戻れませんでした。"
                    "ボタンからもう一度お試しください。"
                )
            else:
                poll_timer.deactivate()
                ui.navigate.to(f"/room/{waiting_room.room_code}")
                return
            finally:
                post_match_transitioning = False
                if not session_invalidated and not client.is_deleted:
                    waiting_room_button.enable()

        with ui.element("main").classes(
            "platform-shell"
            + (" motion-reduced" if reduced_motion else "")
        ) as game_main:
            with ui.column().classes("platform-wrap"):
                with ui.row().classes(
                    "game-page-header w-full items-center "
                    "justify-between gap-3"
                ):
                    with ui.column():
                        game_title = ui.label("対局").classes("auth-title")
                        settings_label = ui.label("").classes(
                            "platform-muted"
                        )
                    with ui.column().classes(
                        "game-preferences items-end gap-1"
                    ):
                        ui.link("ロビーへ", "/lobby").classes(
                            "platform-link"
                        )
                        ui.link(
                            "遊び方", _tutorial_url(f"/play/{game_id}")
                        ).classes("platform-link")
                        with ui.row().classes(
                            "items-center justify-end gap-2"
                        ):
                            sound_button = ui.button(
                                (
                                    "効果音 OFF"
                                    if sound_muted
                                    else "効果音 ON"
                                ),
                                icon=(
                                    "volume_off"
                                    if sound_muted
                                    else "volume_up"
                                ),
                                on_click=toggle_sound,
                            ).props(
                                "flat dense no-caps "
                                + (
                                    "aria-label='効果音をオンにする'"
                                    if sound_muted
                                    else "aria-label='効果音をオフにする'"
                                )
                            ).classes("sound-toggle")
                            sound_button.on(
                                "click",
                                js_handler=(
                                    "()=>{try{const A=window.AudioContext||"
                                    "window.webkitAudioContext;if(!A)return;"
                                    "const c=window.__siritoriAudioContext||"
                                    "(window.__siritoriAudioContext=new A());"
                                    "if(c.state==='suspended'){"
                                    "void c.resume().catch(()=>{});}}catch(_){}}"
                                ),
                            )
                            ui.switch(
                                "演出を減らす",
                                value=reduced_motion,
                                on_change=toggle_reduced_motion,
                            ).props(
                                "dense aria-label='アニメーションを減らす'"
                            ).classes("motion-toggle")
                reaction_feed_box = ui.column().classes(
                    "reaction-feed gap-2"
                ).props(
                    "role='log' aria-live='polite' aria-relevant='additions'"
                )
                with ui.element("section").classes("dashboard-grid"):
                    with ui.column().classes(
                        "dashboard-card game-turn-card"
                    ) as turn_card:
                        status_label = ui.label("接続中").classes(
                            "aside-title"
                        )
                        turn_label = ui.label("").classes("aside-title")
                        latest_word_label = ui.label(
                            "前の人の単語：まだありません"
                        ).classes(
                            "game-latest-word w-full"
                        ).props(
                            "role='status' aria-live='polite' "
                            "aria-atomic='true'"
                        )
                        with ui.column().classes(
                            "game-latest-word w-full gap-1"
                        ).props(
                            "role='status' aria-live='polite' "
                            "aria-label='現在の鬼命令'"
                        ) as oni_command_panel:
                            ui.label("鬼モードの命令").classes("aside-title")
                            oni_commands_label = ui.label("").classes("w-full")
                            oni_seals_label = ui.label("").classes("w-full")
                            oni_candidate_label = ui.label("").classes("w-full")
                            oni_relaxed_label = ui.label("").classes(
                                "platform-muted w-full"
                            )
                            oni_relaxed_label.set_visibility(False)
                        oni_command_panel.set_visibility(False)
                        with ui.row().classes(
                            "w-full items-center gap-3"
                        ):
                            ui.label("次の文字")
                            expected_label = ui.label("自由").classes(
                                "aside-title"
                            )
                        deadline_label = ui.label("").classes(
                            "deadline-label deadline--normal"
                        ).props(
                            "role='status' aria-live='polite' "
                            "aria-atomic='true'"
                        )
                        ui.label("プレイヤーとライフ").classes(
                            "aside-title"
                        )
                        player_status_box = ui.column().classes(
                            "w-full gap-1"
                        ).props(
                            "role='status' aria-live='polite' "
                            "aria-label='プレイヤーごとの残りライフ'"
                        )
                        life_event_label = ui.label("").classes(
                            "game-latest-word w-full"
                        ).props(
                            "role='alert' aria-live='assertive' aria-atomic='true'"
                        )
                        life_event_label.set_visibility(False)
                        word_input = ui.input(
                            label="次のことば",
                            placeholder="漢字・ひらがな・カタカナ",
                        ).props(
                            "outlined clearable maxlength=30 autocomplete=off"
                        ).classes("w-full")
                        submit_button = ui.button(
                            "つなぐ",
                            icon="arrow_forward",
                            on_click=submit_word,
                        ).props("unelevated no-caps").classes("w-full")
                        word_input.on("keydown.enter", submit_word)
                        surrender_button = ui.button(
                            "降参する",
                            icon="flag",
                            on_click=open_surrender_dialog,
                        ).props(
                            "outline no-caps color=negative"
                        ).classes("w-full surrender-button")
                        surrender_button.set_visibility(False)
                        feedback_label = ui.label(
                            "対局へ接続しています。"
                        ).classes("platform-muted").props(
                            "role='status' aria-live='polite'"
                        )
                        with ui.element("section").classes(
                            "match-result-card w-full"
                        ).props(
                            "role='status' aria-live='polite' "
                            "aria-atomic='true'"
                        ) as result_panel:
                            ui.label("対局リザルト").classes(
                                "match-result-eyebrow"
                            )
                            result_title = ui.label("").classes(
                                "match-result-title"
                            )
                            result_outcome = ui.label("").classes(
                                "match-result-outcome"
                            )
                            with ui.element("div").classes(
                                "match-result-grid"
                            ):
                                with ui.element("div"):
                                    ui.label(
                                        "成立したことば"
                                    ).classes("match-result-metric-label")
                                    result_words = ui.label("").classes(
                                        "match-result-metric-value"
                                    )
                                with ui.element("div"):
                                    ui.label("終了理由").classes(
                                        "match-result-metric-label"
                                    )
                                    result_reason = ui.label("").classes(
                                        "match-result-metric-value"
                                    )
                                with ui.element("div"):
                                    ui.label("対戦概要").classes(
                                        "match-result-metric-label"
                                    )
                                    result_round = ui.label("").classes(
                                        "match-result-metric-value"
                                    )
                                with ui.element("div"):
                                    ui.label("最後のことば").classes(
                                        "match-result-metric-label"
                                    )
                                    result_last_word = ui.label("").classes(
                                        "match-result-metric-value"
                                    )
                            ui.label("ライフ損失履歴").classes(
                                "match-result-metric-label"
                            )
                            result_life_loss_box = ui.column().classes(
                                "w-full gap-1"
                            )
                        result_panel.set_visibility(False)
                        result_share_payload = ui.label("").classes(
                            "result-share-payload"
                        ).props(
                            "id='siritori-result-share-payload' "
                            "aria-hidden='true'"
                        )
                        with ui.element("section").classes(
                            "reaction-panel w-full"
                        ):
                            ui.label("リアクション").classes(
                                "reaction-panel-title"
                            )
                            with ui.row().classes(
                                "reaction-buttons w-full items-center"
                            ):
                                for emoji in SUPPORTED_REACTIONS:
                                    button = ui.button(
                                        emoji,
                                        on_click=(
                                            lambda _event=None, value=emoji:
                                            send_room_reaction(value)
                                        ),
                                    ).props(
                                        "flat round dense "
                                        f"aria-label='{emoji}を送る'"
                                    ).classes("reaction-button")
                                    reaction_buttons.append(button)
                            reaction_feedback_label = ui.label(
                                "対戦中も観戦中も送れます。"
                            ).classes(
                                "platform-muted reaction-feedback"
                            ).props(
                                "role='status' aria-live='polite'"
                            )
                        with ui.column().classes(
                            "post-match-actions w-full gap-2"
                        ) as post_match_panel:
                            post_match_label = ui.label("").classes(
                                "platform-muted"
                            ).props(
                                "role='status' aria-live='polite'"
                            )
                            result_share_button = ui.button(
                                "結果を共有",
                                icon="share",
                                on_click=pause_auto_return_for_share,
                            ).props(
                                "outline no-caps "
                                "aria-label='対局結果を共有する'"
                            ).classes("w-full result-share-button")
                            result_share_button.on(
                                "click",
                                js_handler=_result_share_script(),
                            )
                            ui.label("").classes(
                                "platform-muted result-share-status"
                            ).props(
                                "id='siritori-result-share-status' "
                                "role='status' aria-live='polite' "
                                "aria-atomic='true'"
                            )
                            solo_rematch_button = ui.button(
                                "同じ設定でもう一度挑戦",
                                icon="replay",
                                on_click=rematch_solo,
                            ).props("unelevated no-caps").classes("w-full")
                            waiting_room_button = ui.button(
                                "待機部屋へ戻る",
                                icon="groups",
                                on_click=return_to_waiting_room,
                            ).props("unelevated no-caps").classes("w-full")
                        post_match_panel.set_visibility(False)
                        ui.link(
                            "辞書にない単語を申請",
                            "/word-suggestions",
                        ).classes("platform-link")
                        login_link = ui.link(
                            "ログインし直す",
                            f"/login?next=/play/{game_id}",
                        ).classes("platform-link")
                        login_link.set_visibility(False)
                    with ui.column().classes("dashboard-card"):
                        ui.label("ことばの履歴").classes("aside-title")
                        history_box = ui.column().classes(
                            "game-history w-full gap-2"
                        ).props(
                            "id='siritori-game-history' "
                            "role='region' aria-label='ことばの履歴' "
                            "tabindex='0'"
                        )

                with ui.dialog() as reading_dialog, ui.card():
                    ui.label("読みを選んでください").classes(
                        "aside-title"
                    )
                    reading_choices = ui.column().classes("w-full gap-2")
                    ui.button(
                        "取り消す",
                        on_click=cancel_reading,
                    ).props("flat no-caps")

                with ui.dialog() as surrender_dialog, ui.card().classes(
                    "confirm-dialog"
                ):
                    ui.label("本当に降参しますか？").classes(
                        "aside-title"
                    )
                    ui.label(
                        "降参すると対戦には戻れません。"
                        "複数人対戦では観戦に回ります。"
                    ).classes("platform-muted")
                    surrender_confirm_button = ui.button(
                        "降参を確定",
                        icon="flag",
                        on_click=confirm_surrender,
                    ).props("unelevated no-caps color=negative").classes(
                        "w-full"
                    )
                    ui.button(
                        "対戦を続ける",
                        on_click=surrender_dialog.close,
                    ).props("outline no-caps").classes("w-full")

        word_input.disable()
        submit_button.disable()
        for reaction_button in reaction_buttons:
            reaction_button.disable()
        client.on_connect(attach)
        client.on_disconnect(detach)
        poll_timer = ui.timer(1.0, refresh_snapshot)

    @ui.page("/saved-games")
    async def saved_games_page(request: Request):
        principal = await principal_for(request)
        if principal is None:
            return RedirectResponse(
                "/login?next=/saved-games", status_code=303
            )
        if solo is None:
            saves = await asyncio.to_thread(
                games.list_solo_saves, principal.account.id
            )
        else:
            saves = await solo.list_paused(principal.account.id)
        _page_shell()
        with ui.element("main").classes("platform-shell"):
            with ui.column().classes("platform-wrap"):
                ui.link("← ロビーへ", "/lobby").classes("platform-link")
                ui.label("保存したBot戦").classes("auth-title")
                if not saves:
                    ui.label("保存中のBot戦はありません。").classes(
                        "platform-muted"
                    )
                for save in saves:
                    with ui.column().classes("dashboard-card"):
                        if solo is None:
                            ui.label(save.slot_name).classes("aside-title")
                            ui.label(
                                f"状態バージョン: {save.saved_state_version}"
                            ).classes("platform-muted")
                        else:
                            save_title = (
                                "鬼しりとり（Hard Bot）"
                                if save.rule_set is RoomRuleSet.ONI
                                else save.bot_difficulty
                            )
                            ui.label(save_title).classes("aside-title")
                            timer = (
                                "無制限"
                                if save.turn_seconds is None
                                else f"{save.turn_seconds}秒"
                            )
                            ui.label(
                                f"Bot {save.bot_count}体・{timer}・"
                                f"ライフ{save.lives_per_player}・{save.move_count}手"
                            ).classes("platform-muted")
                            ui.label(
                                f"対局ID: {save.game_id}"
                            ).classes("platform-muted")
                            ui.link(
                                "この対局を再開", f"/play/{save.game_id}"
                            ).classes("platform-link")
                        ui.label(
                            f"保存日時: {save.updated_at.astimezone(timezone.utc).isoformat()}"
                        ).classes("platform-muted")

    @app.post("/auth/logout")
    async def logout_action(request: Request):
        form = await _read_form(request)
        token = request.cookies.get(settings.session_cookie_name, "")
        try:
            principal = await asyncio.to_thread(
                auth.authenticate_session, token
            )
        except InvalidSessionError:
            principal = None
        if (
            principal is None
            or not _same_origin(request)
            or not csrf.verify(
                form.get("csrf_token", ""), principal.session_id
            )
        ):
            raise HTTPException(status_code=403, detail="invalid logout")
        await asyncio.to_thread(auth.logout, token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(
            settings.session_cookie_name,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return response


__all__ = [
    "AuthWebServices",
    "CsrfProtector",
    "LoginAttemptLimiter",
    "PasswordWorkLimiter",
    "register_auth_pages",
    "session_principal_from_request",
]
