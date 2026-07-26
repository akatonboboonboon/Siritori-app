"""Add reviewed words, daily challenges, and tutorial progress.

Revision ID: 0007_final_features
Revises: 0006_word_suggestions
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0007_final_features"
down_revision: str | None = "0006_word_suggestions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "word_suggestion_reviews",
        sa.Column("suggestion_id", sa.String(length=36), nullable=False),
        sa.Column("reviewer_user_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("review_note", sa.String(length=200)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_word_suggestion_reviews_valid_decision",
        ),
        sa.CheckConstraint(
            "review_note IS NULL OR length(review_note) <= 200",
            name="ck_word_suggestion_reviews_review_note_length",
        ),
        sa.ForeignKeyConstraint(
            ["suggestion_id"],
            ["word_suggestions.id"],
            name=(
                "fk_word_suggestion_reviews_suggestion_id_word_suggestions"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"],
            ["users.id"],
            name="fk_word_suggestion_reviews_reviewer_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "suggestion_id", name="pk_word_suggestion_reviews"
        ),
    )
    op.create_index(
        "ix_word_suggestion_reviews_reviewer_time",
        "word_suggestion_reviews",
        ["reviewer_user_id", "reviewed_at"],
    )

    op.create_table(
        "approved_words",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("surface", sa.String(length=30), nullable=False),
        sa.Column("reading", sa.String(length=60), nullable=False),
        sa.Column("approved_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("source_suggestion_id", sa.String(length=36)),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(surface) >= 1 AND length(surface) <= 30",
            name="ck_approved_words_surface_length",
        ),
        sa.CheckConstraint(
            "length(reading) >= 1 AND length(reading) <= 60",
            name="ck_approved_words_reading_length",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["users.id"],
            name="fk_approved_words_approved_by_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_suggestion_id"],
            ["word_suggestions.id"],
            name="fk_approved_words_source_suggestion_id_word_suggestions",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approved_words"),
        sa.UniqueConstraint(
            "source_suggestion_id",
            name="uq_approved_words_source_suggestion_id",
        ),
        sa.UniqueConstraint(
            "surface",
            "reading",
            name="uq_approved_words_surface_reading",
        ),
    )
    op.create_index(
        "ix_approved_words_surface",
        "approved_words",
        ["surface"],
    )

    op.create_table(
        "daily_challenge_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("challenge_date", sa.Date(), nullable=False),
        sa.Column("condition_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("rules_version", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("finish_reason", sa.String(length=16)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(condition_key) = 64",
            name="ck_daily_challenge_runs_condition_key_sha256",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'finished')",
            name="ck_daily_challenge_runs_valid_status",
        ),
        sa.CheckConstraint(
            "state_version >= 0",
            name="ck_daily_challenge_runs_state_version_nonnegative",
        ),
        sa.CheckConstraint(
            "rules_version >= 1",
            name="ck_daily_challenge_runs_rules_version_positive",
        ),
        sa.CheckConstraint(
            "duration_seconds = 180",
            name="ck_daily_challenge_runs_fixed_duration",
        ),
        sa.CheckConstraint(
            "score >= 0",
            name="ck_daily_challenge_runs_score_nonnegative",
        ),
        sa.CheckConstraint(
            "accepted_count >= 0",
            name="ck_daily_challenge_runs_accepted_count_nonnegative",
        ),
        sa.CheckConstraint(
            "finish_reason IS NULL OR "
            "finish_reason IN ('timeout', 'ends_with_n', 'duplicate')",
            name="ck_daily_challenge_runs_valid_finish_reason",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND deadline_at IS NOT NULL "
            "AND finished_at IS NULL AND finish_reason IS NULL) OR "
            "(status = 'finished' AND deadline_at IS NULL "
            "AND finished_at IS NOT NULL AND finish_reason IS NOT NULL)",
            name="ck_daily_challenge_runs_valid_lifecycle",
        ),
        sa.CheckConstraint(
            "deadline_at IS NULL OR deadline_at > started_at",
            name="ck_daily_challenge_runs_deadline_after_start",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_daily_challenge_runs_finish_after_start",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_daily_challenge_runs_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_daily_challenge_runs"),
        sa.UniqueConstraint(
            "user_id",
            "challenge_date",
            name="uq_daily_challenge_runs_user_date",
        ),
    )
    op.create_index(
        "uq_daily_challenge_runs_active_user",
        "daily_challenge_runs",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_daily_challenge_runs_ranking",
        "daily_challenge_runs",
        [
            "challenge_date",
            "condition_key",
            "rules_version",
            "status",
            "score",
            "accepted_count",
            "finished_at",
        ],
    )

    op.create_table(
        "user_onboarding",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("tutorial_version", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "tutorial_version >= 1",
            name="ck_user_onboarding_tutorial_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_onboarding_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_onboarding"),
    )


def downgrade() -> None:
    op.drop_table("user_onboarding")
    op.drop_table("daily_challenge_runs")
    op.drop_table("approved_words")
    op.drop_table("word_suggestion_reviews")
