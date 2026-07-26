"""Add review-only user dictionary suggestions.

Revision ID: 0006_word_suggestions
Revises: 0005_room_current_game
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0006_word_suggestions"
down_revision: str | None = "0005_room_current_game"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "word_suggestions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("surface", sa.String(length=30), nullable=False),
        sa.Column("reading", sa.String(length=60), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
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
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "length(surface) >= 1 AND length(surface) <= 30",
            name="ck_word_suggestions_surface_length",
        ),
        sa.CheckConstraint(
            "length(reading) >= 1 AND length(reading) <= 60",
            name="ck_word_suggestions_reading_length",
        ),
        sa.CheckConstraint(
            "note IS NULL OR length(note) <= 200",
            name="ck_word_suggestions_note_length",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_word_suggestions_valid_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND reviewed_at IS NULL) OR "
            "(status IN ('approved', 'rejected') AND reviewed_at IS NOT NULL)",
            name="ck_word_suggestions_valid_review_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_word_suggestions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_word_suggestions"),
        sa.UniqueConstraint(
            "user_id",
            "surface",
            "reading",
            name="uq_word_suggestions_user_surface_reading",
        ),
    )
    op.create_index(
        "ix_word_suggestions_user_created",
        "word_suggestions",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_word_suggestions_review_queue",
        "word_suggestions",
        ["status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("word_suggestions")
