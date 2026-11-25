"""initial schema: users, nodes, node_inventory, node_events, pairing_codes

Revision ID: 0001
Revises:
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEV_ADMIN_ID = "00000000-0000-4000-8000-000000000001"


def upgrade() -> None:
    # pgvector is needed from stage 2 (knowledge/memory); enabling it now proves the
    # database image is right.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("role IN ('admin', 'user')", name="users_role_check"),
    )
    op.create_table(
        "nodes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(63), nullable=False, unique=True),
        sa.Column("os", sa.String(20), nullable=False),
        sa.Column("arch", sa.String(20), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("credential_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="offline"),
        sa.Column("agent_version", sa.String(50)),
        sa.Column("last_seen", sa.DateTime(timezone=True)),
        sa.Column("config", JSONB(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "state IN ('available', 'busy_gaming', 'degraded', 'offline')",
            name="nodes_state_check",
        ),
    )
    op.create_table(
        "node_inventory",
        sa.Column(
            "node_id",
            UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("inventory", JSONB()),
        sa.Column("status", JSONB()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "node_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "node_id",
            UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("message_id", UUID(as_uuid=True), unique=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("data", JSONB(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_node_events_node_id", "node_events", ["node_id"])
    # node_events is append-only: forbid UPDATE at the database level.
    op.execute(
        """
        CREATE FUNCTION node_events_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'node_events is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER node_events_no_update BEFORE UPDATE ON node_events "
        "FOR EACH ROW EXECUTE FUNCTION node_events_append_only()"
    )
    op.create_table(
        "pairing_codes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("node_id", UUID(as_uuid=True), sa.ForeignKey("nodes.id", ondelete="SET NULL")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_pairing_codes_user_id", "pairing_codes", ["user_id"])

    # Seed the single dev admin. Authentication for it is the dev token (see auth.py).
    op.execute(
        # constant, not user input
        f"INSERT INTO users (id, name, role) VALUES ('{DEV_ADMIN_ID}', 'dev-admin', 'admin')"  # noqa: S608
    )


def downgrade() -> None:
    op.drop_table("pairing_codes")
    op.execute("DROP TRIGGER IF EXISTS node_events_no_update ON node_events")
    op.drop_table("node_events")
    op.execute("DROP FUNCTION IF EXISTS node_events_append_only()")
    op.drop_table("node_inventory")
    op.drop_table("nodes")
    op.drop_table("users")
