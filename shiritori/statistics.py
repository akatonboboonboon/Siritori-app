"""Read models and privacy controls for persisted match statistics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, case, func, select

from .database import Database
from .models import (
    GameMode,
    MatchParticipation,
    MatchResult,
    ScoreAttackRun,
    User,
    utc_now,
)
from .score_attack import SCORE_RULES_VERSION, ScoreAttackStatus


class StatisticsUserNotFound(LookupError):
    """Raised when statistics are requested for an unknown account."""


@dataclass(frozen=True, slots=True)
class UserStatsSummary:
    user_id: str
    games_played: int
    wins: int
    losses: int
    draws: int
    pvp_wins: int
    solo_wins: int
    accepted_words: int
    win_rate: float
    leaderboard_visible: bool


@dataclass(frozen=True, slots=True)
class RecentMatch:
    game_id: str
    finished_at: datetime
    mode: str
    result: str
    placement: int | None
    player_count: int
    move_count: int
    end_reason: str | None


@dataclass(frozen=True, slots=True)
class PvpWinLeaderboardEntry:
    rank: int
    display_name: str
    wins: int
    games_played: int
    win_rate: float


@dataclass(frozen=True, slots=True)
class ScoreAttackPersonalBest:
    run_id: str
    score: int
    accepted_count: int
    finished_at: datetime
    rules_version: int


@dataclass(frozen=True, slots=True)
class ScoreAttackLeaderboardEntry:
    rank: int
    display_name: str
    score: int
    accepted_count: int
    finished_at: datetime


def _identifier(value: str, name: str = "user_id") -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.isspace()
        or len(value) > 36
    ):
        raise ValueError(f"{name} must contain 1-36 characters")
    return value


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    return value


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class StatisticsRepository:
    """Query immutable results without trusting browser-supplied aggregates."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def get_user_summary(self, user_id: str) -> UserStatsSummary:
        identifier = _identifier(user_id)
        with self.database.read_session() as session:
            user = session.get(User, identifier)
            if user is None:
                raise StatisticsUserNotFound(identifier)

            wins_expression = func.sum(
                case(
                    (
                        MatchParticipation.result == MatchResult.WIN.value,
                        1,
                    ),
                    else_=0,
                )
            )
            losses_expression = func.sum(
                case(
                    (
                        MatchParticipation.result == MatchResult.LOSS.value,
                        1,
                    ),
                    else_=0,
                )
            )
            draws_expression = func.sum(
                case(
                    (
                        MatchParticipation.result == MatchResult.DRAW.value,
                        1,
                    ),
                    else_=0,
                )
            )
            pvp_wins_expression = func.sum(
                case(
                    (
                        and_(
                            MatchParticipation.mode
                            == GameMode.MULTIPLAYER.value,
                            MatchParticipation.result
                            == MatchResult.WIN.value,
                        ),
                        1,
                    ),
                    else_=0,
                )
            )
            solo_wins_expression = func.sum(
                case(
                    (
                        and_(
                            MatchParticipation.mode == GameMode.SOLO.value,
                            MatchParticipation.result
                            == MatchResult.WIN.value,
                        ),
                        1,
                    ),
                    else_=0,
                )
            )
            row = session.execute(
                select(
                    func.count(MatchParticipation.game_id),
                    wins_expression,
                    losses_expression,
                    draws_expression,
                    pvp_wins_expression,
                    solo_wins_expression,
                    func.sum(MatchParticipation.word_count),
                ).where(MatchParticipation.user_id == identifier)
            ).one()
            games_played = int(row[0] or 0)
            wins = int(row[1] or 0)
            losses = int(row[2] or 0)
            draws = int(row[3] or 0)
            return UserStatsSummary(
                user_id=identifier,
                games_played=games_played,
                wins=wins,
                losses=losses,
                draws=draws,
                pvp_wins=int(row[4] or 0),
                solo_wins=int(row[5] or 0),
                accepted_words=int(row[6] or 0),
                win_rate=(wins / games_played if games_played else 0.0),
                leaderboard_visible=user.leaderboard_visible,
            )

    def list_recent_matches(
        self,
        user_id: str,
        *,
        limit: int = 20,
    ) -> tuple[RecentMatch, ...]:
        identifier = _identifier(user_id)
        maximum = _limit(limit)
        with self.database.read_session() as session:
            if session.get(User, identifier) is None:
                raise StatisticsUserNotFound(identifier)
            participations = tuple(
                session.scalars(
                    select(MatchParticipation)
                    .where(MatchParticipation.user_id == identifier)
                    .order_by(
                        MatchParticipation.finished_at.desc(),
                        MatchParticipation.game_id.desc(),
                    )
                    .limit(maximum)
                )
            )
            return tuple(
                RecentMatch(
                    game_id=participation.game_id,
                    finished_at=_aware_utc(participation.finished_at),
                    mode=participation.mode,
                    result=participation.result,
                    placement=participation.placement,
                    player_count=participation.player_count,
                    move_count=participation.word_count,
                    end_reason=participation.end_reason,
                )
                for participation in participations
            )

    def set_leaderboard_visibility(
        self,
        user_id: str,
        visible: bool,
    ) -> bool:
        identifier = _identifier(user_id)
        if type(visible) is not bool:
            raise ValueError("visible must be boolean")
        with self.database.transaction() as session:
            user = session.scalar(
                select(User).where(User.id == identifier).with_for_update()
            )
            if user is None:
                raise StatisticsUserNotFound(identifier)
            user.leaderboard_visible = visible
            user.updated_at = utc_now()
            session.flush()
            return user.leaderboard_visible

    def list_pvp_win_leaderboard(
        self,
        *,
        limit: int = 50,
    ) -> tuple[PvpWinLeaderboardEntry, ...]:
        maximum = _limit(limit)
        wins_expression = func.sum(
            case(
                (
                    MatchParticipation.result == MatchResult.WIN.value,
                    1,
                ),
                else_=0,
            )
        )
        games_expression = func.count(MatchParticipation.game_id)
        with self.database.read_session() as session:
            rows = session.execute(
                select(
                    User.username,
                    User.display_name,
                    wins_expression.label("wins"),
                    games_expression.label("games_played"),
                )
                .join(
                    MatchParticipation,
                    MatchParticipation.user_id == User.id,
                )
                .where(
                    User.leaderboard_visible.is_(True),
                    User.disabled_at.is_(None),
                    MatchParticipation.mode == GameMode.MULTIPLAYER.value,
                )
                .group_by(
                    User.id,
                    User.username,
                    User.username_key,
                    User.display_name,
                )
                .having(wins_expression > 0)
                .order_by(
                    wins_expression.desc(),
                    games_expression.asc(),
                    User.username_key.asc(),
                    User.id.asc(),
                )
                .limit(maximum)
            ).all()

            entries: list[PvpWinLeaderboardEntry] = []
            previous_wins: int | None = None
            current_rank = 0
            for index, row in enumerate(rows, start=1):
                wins = int(row.wins)
                games_played = int(row.games_played)
                if previous_wins != wins:
                    current_rank = index
                    previous_wins = wins
                entries.append(
                    PvpWinLeaderboardEntry(
                        rank=current_rank,
                        display_name=row.display_name or row.username,
                        wins=wins,
                        games_played=games_played,
                        win_rate=wins / games_played,
                    )
                )
            return tuple(entries)

    def get_score_attack_personal_best(
        self,
        user_id: str,
    ) -> ScoreAttackPersonalBest | None:
        """Return the authenticated user's best run for current rules."""

        identifier = _identifier(user_id)
        with self.database.read_session() as session:
            if session.get(User, identifier) is None:
                raise StatisticsUserNotFound(identifier)
            run = session.scalar(
                select(ScoreAttackRun)
                .where(
                    ScoreAttackRun.user_id == identifier,
                    ScoreAttackRun.status
                    == ScoreAttackStatus.FINISHED.value,
                    ScoreAttackRun.rules_version == SCORE_RULES_VERSION,
                )
                .order_by(
                    ScoreAttackRun.score.desc(),
                    ScoreAttackRun.accepted_count.desc(),
                    ScoreAttackRun.finished_at.asc(),
                    ScoreAttackRun.id.asc(),
                )
                .limit(1)
            )
            if run is None:
                return None
            return ScoreAttackPersonalBest(
                run_id=run.id,
                score=run.score,
                accepted_count=run.accepted_count,
                finished_at=_aware_utc(run.finished_at),
                rules_version=run.rules_version,
            )

    def list_score_attack_leaderboard(
        self,
        *,
        limit: int = 50,
    ) -> tuple[ScoreAttackLeaderboardEntry, ...]:
        """Rank each opted-in user's current-rules personal best."""

        maximum = _limit(limit)
        personal_runs = (
            select(
                ScoreAttackRun.id.label("run_id"),
                ScoreAttackRun.user_id.label("user_id"),
                ScoreAttackRun.score.label("score"),
                ScoreAttackRun.accepted_count.label("accepted_count"),
                ScoreAttackRun.finished_at.label("finished_at"),
                func.row_number()
                .over(
                    partition_by=ScoreAttackRun.user_id,
                    order_by=(
                        ScoreAttackRun.score.desc(),
                        ScoreAttackRun.accepted_count.desc(),
                        ScoreAttackRun.finished_at.asc(),
                        ScoreAttackRun.id.asc(),
                    ),
                )
                .label("personal_order"),
            )
            .where(
                ScoreAttackRun.status
                == ScoreAttackStatus.FINISHED.value,
                ScoreAttackRun.rules_version == SCORE_RULES_VERSION,
            )
            .subquery()
        )
        with self.database.read_session() as session:
            rows = session.execute(
                select(
                    User.username,
                    User.display_name,
                    personal_runs.c.score,
                    personal_runs.c.accepted_count,
                    personal_runs.c.finished_at,
                )
                .join(
                    personal_runs,
                    personal_runs.c.user_id == User.id,
                )
                .where(
                    personal_runs.c.personal_order == 1,
                    User.leaderboard_visible.is_(True),
                    User.disabled_at.is_(None),
                )
                .order_by(
                    personal_runs.c.score.desc(),
                    personal_runs.c.accepted_count.desc(),
                    personal_runs.c.finished_at.asc(),
                    User.username_key.asc(),
                    personal_runs.c.run_id.asc(),
                )
                .limit(maximum)
            ).all()
            return tuple(
                ScoreAttackLeaderboardEntry(
                    rank=index,
                    display_name=row.display_name or row.username,
                    score=int(row.score),
                    accepted_count=int(row.accepted_count),
                    finished_at=_aware_utc(row.finished_at),
                )
                for index, row in enumerate(rows, start=1)
            )


__all__ = [
    "PvpWinLeaderboardEntry",
    "RecentMatch",
    "ScoreAttackLeaderboardEntry",
    "ScoreAttackPersonalBest",
    "StatisticsRepository",
    "StatisticsUserNotFound",
    "UserStatsSummary",
]
