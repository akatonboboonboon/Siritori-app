"""Add the configurable per-player life count to lobby rooms.

Revision ID: 0008_life_system
Revises: 0007_final_features
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008_life_system"
down_revision: str | None = "0007_final_features"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The server default both backfills existing rooms and makes a rolling
    # deployment safe while an older process may still insert a legacy room.
    with op.batch_alter_table("rooms") as batch_op:
        batch_op.add_column(
            sa.Column(
                "lives_per_player",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_rooms_lives_per_player_range",
            "lives_per_player >= 1 AND lives_per_player <= 5",
        )


def downgrade() -> None:
    with op.batch_alter_table("rooms") as batch_op:
        batch_op.drop_constraint(
            "ck_rooms_lives_per_player_range",
            type_="check",
        )
        batch_op.drop_column("lives_per_player")
