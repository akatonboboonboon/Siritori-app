"""Add authoritative resumable score attack runs.

Revision ID: 0004_score_attack_runs
Revises: 0003_match_statistics
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0004_score_attack_runs"
down_revision: str | None = "0003_match_statistics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "score_attack_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "state_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "rules_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "duration_seconds",
            sa.Integer(),
            server_default=sa.text("180"),
            nullable=False,
        ),
        sa.Column(
            "score",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "accepted_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("finish_reason", sa.String(length=16), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "deadline_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'finished')",
            name="ck_score_attack_runs_valid_status",
        ),
        sa.CheckConstraint(
            "state_version >= 0",
            name="ck_score_attack_runs_state_version_nonnegative",
        ),
        sa.CheckConstraint(
            "rules_version >= 1",
            name="ck_score_attack_runs_rules_version_positive",
        ),
        sa.CheckConstraint(
            "duration_seconds = 180",
            name="ck_score_attack_runs_fixed_duration",
        ),
        sa.CheckConstraint(
            "score >= 0",
            name="ck_score_attack_runs_score_nonnegative",
        ),
        sa.CheckConstraint(
            "accepted_count >= 0",
            name="ck_score_attack_runs_accepted_count_nonnegative",
        ),
        sa.CheckConstraint(
            "finish_reason IS NULL OR "
            "finish_reason IN ('timeout', 'ends_with_n', 'duplicate')",
            name="ck_score_attack_runs_valid_finish_reason",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND deadline_at IS NOT NULL "
            "AND finished_at IS NULL AND finish_reason IS NULL) OR "
            "(status = 'finished' AND deadline_at IS NULL "
            "AND finished_at IS NOT NULL AND finish_reason IS NOT NULL)",
            name="ck_score_attack_runs_valid_lifecycle",
        ),
        sa.CheckConstraint(
            "deadline_at IS NULL OR deadline_at > started_at",
            name="ck_score_attack_runs_deadline_after_start",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_score_attack_runs_finish_after_start",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_score_attack_runs_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_score_attack_runs"),
    )
    op.create_index(
        "uq_score_attack_runs_active_user",
        "score_attack_runs",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_score_attack_runs_user_finished",
        "score_attack_runs",
        ["user_id", "finished_at"],
        unique=False,
    )
    op.create_index(
        "ix_score_attack_runs_ranking",
        "score_attack_runs",
        [
            "rules_version",
            "status",
            "score",
            "accepted_count",
            "finished_at",
        ],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("score_attack_runs")
