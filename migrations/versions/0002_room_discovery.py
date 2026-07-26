"""Add room discovery, active-name identity, and optional Bot seat filling.

Revision ID: 0002_room_discovery
Revises: 0001_initial_schema
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
import unicodedata

from alembic import op
import sqlalchemy as sa


revision: str = "0002_room_discovery"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _normalize_room_name(value: str) -> str:
    """Mirror ``shiritori.lobby.normalize_room_name`` without app imports."""

    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split())


def _room_name_key(value: str) -> str:
    canonical = _normalize_room_name(value).casefold()
    return sha256(canonical.encode("utf-8")).hexdigest()


def _deduplicated_name(
    base_name: str,
    used_keys: set[str],
) -> tuple[str, str]:
    candidate = base_name[:64]
    candidate_key = _room_name_key(candidate)
    if candidate_key not in used_keys:
        return candidate, candidate_key

    number = 2
    while True:
        suffix = f" ({number})"
        prefix = base_name[: 64 - len(suffix)].rstrip()
        candidate = f"{prefix}{suffix}"
        candidate_key = _room_name_key(candidate)
        if candidate_key not in used_keys:
            return candidate, candidate_key
        number += 1


def upgrade() -> None:
    op.add_column(
        "rooms",
        sa.Column("name_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "rooms",
        sa.Column(
            "is_public",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "rooms",
        sa.Column(
            "fill_empty_seats_with_bots",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )

    rooms = sa.table(
        "rooms",
        sa.column("id", sa.String(length=36)),
        sa.column("name", sa.String(length=64)),
        sa.column("name_key", sa.String(length=64)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("deleted_at", sa.DateTime(timezone=True)),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(
            rooms.c.id,
            rooms.c.name,
            rooms.c.deleted_at,
        ).order_by(rooms.c.created_at.asc(), rooms.c.id.asc())
    ).mappings()

    used_active_keys: set[str] = set()
    for row in rows:
        normalized_name = _normalize_room_name(row["name"])[:64]
        if not normalized_name:
            # Legacy validation rejected blank names. This fallback keeps a
            # hand-edited database migratable without introducing NULL keys.
            normalized_name = "Room"
        if row["deleted_at"] is None:
            stored_name, key = _deduplicated_name(
                normalized_name,
                used_active_keys,
            )
            used_active_keys.add(key)
        else:
            stored_name = normalized_name
            key = _room_name_key(stored_name)
        connection.execute(
            rooms.update()
            .where(rooms.c.id == row["id"])
            .values(name=stored_name, name_key=key)
        )

    with op.batch_alter_table("rooms") as batch_op:
        batch_op.alter_column(
            "name_key",
            existing_type=sa.String(length=64),
            nullable=False,
        )

    op.create_index(
        "uq_rooms_active_name_key",
        "rooms",
        ["name_key"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_rooms_active_name_key", table_name="rooms")
    with op.batch_alter_table("rooms") as batch_op:
        batch_op.drop_column("fill_empty_seats_with_bots")
        batch_op.drop_column("is_public")
        batch_op.drop_column("name_key")
