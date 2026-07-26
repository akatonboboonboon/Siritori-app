"""Track the current game for multi-round lobby rooms.

Revision ID: 0005_room_current_game
Revises: 0004_score_attack_runs
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0005_room_current_game"
down_revision: str | None = "0004_score_attack_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("games") as batch_op:
        batch_op.add_column(
            sa.Column(
                "rematch_of_game_id",
                sa.String(length=36),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_games_rematch_of_game_id_games",
            "games",
            ["rematch_of_game_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_games_rematch_of_game_id",
            ["rematch_of_game_id"],
        )

    with op.batch_alter_table("rooms") as batch_op:
        batch_op.add_column(
            sa.Column(
                "current_game_id",
                sa.String(length=36),
                nullable=True,
            )
        )

    rooms = sa.table(
        "rooms",
        sa.column("id", sa.String(length=36)),
        sa.column("current_game_id", sa.String(length=36)),
        sa.column("status", sa.String(length=16)),
    )
    games = sa.table(
        "games",
        sa.column("id", sa.String(length=36)),
        sa.column("room_id", sa.String(length=36)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    connection = op.get_bind()
    room_ids = tuple(
        connection.execute(
            sa.select(rooms.c.id)
            .where(rooms.c.status == "active")
        ).scalars()
    )
    for room_id in room_ids:
        latest_game_id = connection.execute(
            sa.select(games.c.id)
            .where(games.c.room_id == room_id)
            .order_by(games.c.created_at.desc(), games.c.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest_game_id is not None:
            connection.execute(
                rooms.update()
                .where(rooms.c.id == room_id)
                .values(current_game_id=latest_game_id)
            )

    with op.batch_alter_table("rooms") as batch_op:
        batch_op.create_foreign_key(
            "fk_rooms_current_game_id_games",
            "games",
            ["current_game_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint(
            "uq_rooms_current_game_id",
            ["current_game_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("rooms") as batch_op:
        batch_op.drop_constraint(
            "uq_rooms_current_game_id",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_rooms_current_game_id_games",
            type_="foreignkey",
        )
        batch_op.drop_column("current_game_id")

    with op.batch_alter_table("games") as batch_op:
        batch_op.drop_constraint(
            "uq_games_rematch_of_game_id",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_games_rematch_of_game_id_games",
            type_="foreignkey",
        )
        batch_op.drop_column("rematch_of_game_id")
