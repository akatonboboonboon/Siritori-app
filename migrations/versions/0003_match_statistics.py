"""Add immutable match results and leaderboard privacy.

Revision ID: 0003_match_statistics
Revises: 0002_room_discovery
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0003_match_statistics"
down_revision: str | None = "0002_room_discovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "leaderboard_visible",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )

    op.create_table(
        "match_participations",
        sa.Column("game_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("seat_index", sa.Integer(), nullable=False),
        sa.Column("result", sa.String(length=8), nullable=False),
        sa.Column("placement", sa.Integer(), nullable=True),
        sa.Column("player_count", sa.Integer(), nullable=False),
        sa.Column(
            "word_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("end_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mode IN ('multiplayer', 'solo')",
            name="ck_match_participations_valid_mode",
        ),
        sa.CheckConstraint(
            "result IN ('win', 'loss', 'draw')",
            name="ck_match_participations_valid_result",
        ),
        sa.CheckConstraint(
            "seat_index >= 0 AND seat_index < 8",
            name="ck_match_participations_seat_index_range",
        ),
        sa.CheckConstraint(
            "placement IS NULL OR (placement >= 1 AND placement <= 8)",
            name="ck_match_participations_placement_range",
        ),
        sa.CheckConstraint(
            "player_count >= 2 AND player_count <= 8",
            name="ck_match_participations_player_count_range",
        ),
        sa.CheckConstraint(
            "word_count >= 0",
            name="ck_match_participations_word_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name="fk_match_participations_game_id_games",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_match_participations_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "game_id",
            "user_id",
            name="pk_match_participations",
        ),
    )
    op.create_index(
        "ix_match_participations_user_finished",
        "match_participations",
        ["user_id", "finished_at"],
        unique=False,
    )
    op.create_index(
        "ix_match_participations_pvp_result",
        "match_participations",
        ["mode", "result", "user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("match_participations")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("leaderboard_visible")
