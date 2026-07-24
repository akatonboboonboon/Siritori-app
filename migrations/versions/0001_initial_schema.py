"""Create authentication, rooms, games, moves, and solo saves.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-07-24
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("username_key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=40), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(username) >= 3", name="ck_users_username_min_length"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username_key", name="uq_users_username_key"),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(token_hash) = 64", name="ck_sessions_token_hash_sha256"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
    )
    op.create_index(
        "ix_sessions_user_expiry",
        "sessions",
        ["user_id", "expires_at"],
        unique=False,
    )

    op.create_table(
        "rooms",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("room_code", sa.String(length=12), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'waiting'"),
            nullable=False,
        ),
        sa.Column(
            "max_players",
            sa.Integer(),
            server_default=sa.text("2"),
            nullable=False,
        ),
        sa.Column(
            "allow_spectators",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "theme_key",
            sa.String(length=32),
            server_default=sa.text("'all'"),
            nullable=False,
        ),
        sa.Column("turn_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "revision",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('waiting', 'active', 'closed')",
            name="ck_rooms_valid_status",
        ),
        sa.CheckConstraint(
            "max_players >= 2 AND max_players <= 8",
            name="ck_rooms_max_players_range",
        ),
        sa.CheckConstraint(
            "turn_seconds IS NULL OR "
            "(turn_seconds >= 3 AND turn_seconds <= 180)",
            name="ck_rooms_turn_seconds_range",
        ),
        sa.CheckConstraint(
            "revision >= 0",
            name="ck_rooms_revision_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_rooms_owner_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_rooms"),
        sa.UniqueConstraint("room_code", name="uq_rooms_room_code"),
    )
    op.create_index(
        "ix_rooms_status_updated",
        "rooms",
        ["status", "updated_at"],
        unique=False,
    )

    op.create_table(
        "room_memberships",
        sa.Column("room_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column(
            "role",
            sa.String(length=16),
            server_default=sa.text("'spectator'"),
            nullable=False,
        ),
        sa.Column("seat_index", sa.Integer(), nullable=True),
        sa.Column(
            "presence",
            sa.String(length=16),
            server_default=sa.text("'offline'"),
            nullable=False,
        ),
        sa.Column(
            "connected_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "is_bot_substituting",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "ready",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "presence_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "role IN ('player', 'spectator')",
            name="ck_room_memberships_valid_role",
        ),
        sa.CheckConstraint(
            "presence IN ('connected', 'grace', 'offline')",
            name="ck_room_memberships_valid_presence",
        ),
        sa.CheckConstraint(
            "connected_count >= 0",
            name="ck_room_memberships_connected_count_nonnegative",
        ),
        sa.CheckConstraint(
            "seat_index IS NULL OR (seat_index >= 0 AND seat_index < 8)",
            name="ck_room_memberships_seat_index_range",
        ),
        sa.CheckConstraint(
            "role = 'player' OR seat_index IS NULL",
            name="ck_room_memberships_spectator_has_no_seat",
        ),
        sa.CheckConstraint(
            "role = 'player' OR ready = false",
            name="ck_room_memberships_spectator_not_ready",
        ),
        sa.ForeignKeyConstraint(
            ["room_id"],
            ["rooms.id"],
            name="fk_room_memberships_room_id_rooms",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_room_memberships_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "room_id", "user_id", name="pk_room_memberships"
        ),
        sa.UniqueConstraint(
            "room_id", "seat_index", name="uq_room_memberships_seat"
        ),
    )
    op.create_index(
        "ix_room_memberships_presence",
        "room_memberships",
        ["room_id", "presence"],
        unique=False,
    )

    op.create_table(
        "games",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("room_id", sa.String(length=36), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'waiting'"),
            nullable=False,
        ),
        sa.Column(
            "theme_key",
            sa.String(length=32),
            server_default=sa.text("'all'"),
            nullable=False,
        ),
        sa.Column("turn_time_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "bot_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("bot_difficulty", sa.String(length=16), nullable=True),
        sa.Column(
            "settings",
            sa.JSON(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.JSON(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "starting_seat_index",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "current_turn_index",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("current_word_surface", sa.String(length=128), nullable=True),
        sa.Column("current_word_reading", sa.String(length=128), nullable=True),
        sa.Column("expected_kana", sa.String(length=4), nullable=True),
        sa.Column(
            "state_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_remaining_seconds", sa.Integer(), nullable=True),
        sa.Column("winner_user_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "mode IN ('multiplayer', 'solo')", name="ck_games_valid_mode"
        ),
        sa.CheckConstraint(
            "status IN ('waiting', 'active', 'paused', 'finished', 'abandoned')",
            name="ck_games_valid_status",
        ),
        sa.CheckConstraint(
            "turn_time_seconds IS NULL OR "
            "(turn_time_seconds >= 3 AND turn_time_seconds <= 180)",
            name="ck_games_turn_time_range",
        ),
        sa.CheckConstraint(
            "bot_count >= 0 AND bot_count <= 7",
            name="ck_games_bot_count_range",
        ),
        sa.CheckConstraint(
            "bot_difficulty IS NULL OR "
            "bot_difficulty IN ('easy', 'normal', 'hard')",
            name="ck_games_valid_bot_difficulty",
        ),
        sa.CheckConstraint(
            "state_version >= 0", name="ck_games_state_version_nonnegative"
        ),
        sa.CheckConstraint(
            "paused_remaining_seconds IS NULL OR paused_remaining_seconds >= 0",
            name="ck_games_paused_remaining_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_games_created_by_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["room_id"],
            ["rooms.id"],
            name="fk_games_room_id_rooms",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["winner_user_id"],
            ["users.id"],
            name="fk_games_winner_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_games"),
    )
    op.create_index(
        "ix_games_owner_mode_status",
        "games",
        ["created_by_user_id", "mode", "status"],
        unique=False,
    )
    op.create_index(
        "ix_games_room_status",
        "games",
        ["room_id", "status"],
        unique=False,
    )

    op.create_table(
        "moves",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("game_id", sa.String(length=36), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_seat_index", sa.Integer(), nullable=True),
        sa.Column("turn_number", sa.Integer(), nullable=False),
        sa.Column("surface", sa.String(length=128), nullable=False),
        sa.Column("reading", sa.String(length=128), nullable=False),
        sa.Column("canonical_key", sa.String(length=128), nullable=False),
        sa.Column(
            "result_code",
            sa.String(length=32),
            server_default=sa.text("'accepted'"),
            nullable=False,
        ),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor_kind IN ('user', 'bot', 'system')",
            name="ck_moves_valid_actor_kind",
        ),
        sa.CheckConstraint(
            "turn_number >= 1", name="ck_moves_turn_number_positive"
        ),
        sa.CheckConstraint(
            "state_version >= 1", name="ck_moves_state_version_positive"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_moves_actor_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name="fk_moves_game_id_games",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_moves"),
        sa.UniqueConstraint(
            "game_id", "operation_id", name="uq_moves_operation"
        ),
        sa.UniqueConstraint(
            "game_id", "turn_number", name="uq_moves_turn_number"
        ),
    )
    op.create_index(
        "ix_moves_game_canonical",
        "moves",
        ["game_id", "canonical_key"],
        unique=False,
    )
    op.create_index(
        "ix_moves_game_created",
        "moves",
        ["game_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "solo_game_saves",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("game_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column(
            "slot_name",
            sa.String(length=32),
            server_default=sa.text("'autosave'"),
            nullable=False,
        ),
        sa.Column(
            "snapshot",
            sa.JSON(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("remaining_seconds", sa.Integer(), nullable=True),
        sa.Column("saved_state_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "remaining_seconds IS NULL OR remaining_seconds >= 0",
            name="ck_solo_game_saves_remaining_seconds_nonnegative",
        ),
        sa.CheckConstraint(
            "saved_state_version >= 0",
            name="ck_solo_game_saves_saved_state_version_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name="fk_solo_game_saves_game_id_games",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_solo_game_saves_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_solo_game_saves"),
        sa.UniqueConstraint("game_id", name="uq_solo_game_saves_game_id"),
        sa.UniqueConstraint(
            "user_id", "slot_name", name="uq_solo_saves_user_slot"
        ),
    )
    op.create_index(
        "ix_solo_saves_user_updated",
        "solo_game_saves",
        ["user_id", "updated_at"],
        unique=False,
    )

    op.create_table(
        "room_command_receipts",
        sa.Column("room_id", sa.String(length=36), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("command_kind", sa.String(length=16), nullable=False),
        sa.Column("command_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column(
            "result_snapshot",
            sa.JSON(none_as_null=True),
            nullable=True,
        ),
        sa.Column(
            "deleted", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "command_kind IN ('compare_and_swap', 'delete')",
            name="ck_room_command_receipts_valid_command_kind",
        ),
        sa.CheckConstraint(
            "length(command_fingerprint) = 64",
            name="ck_room_command_receipts_command_fingerprint_sha256",
        ),
        sa.CheckConstraint(
            "expected_version >= 0",
            name="ck_room_command_receipts_expected_version_nonnegative",
        ),
        sa.CheckConstraint(
            "(deleted = false AND result_snapshot IS NOT NULL) OR "
            "(deleted = true AND result_snapshot IS NULL)",
            name="ck_room_command_receipts_receipt_result_shape",
        ),
        sa.PrimaryKeyConstraint(
            "room_id",
            "operation_id",
            name="pk_room_command_receipts",
        ),
    )
    op.create_index(
        "ix_room_command_receipts_created",
        "room_command_receipts",
        ["created_at"],
        unique=False,
    )

def downgrade() -> None:
    op.drop_table("room_command_receipts")
    op.drop_table("solo_game_saves")
    op.drop_table("moves")
    op.drop_table("games")
    op.drop_table("room_memberships")
    op.drop_table("rooms")
    op.drop_table("sessions")
    op.drop_table("users")
