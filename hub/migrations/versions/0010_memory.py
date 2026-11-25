"""memory (E2-02, D19/D20): items with embeddings, versions, blocked candidates, runs

Every table carries user_id. ``memory_items.embedding`` is a ``vector`` without a fixed
dimension (different embedders have different sizes; a user's memory is small, so queries
scan exactly, filtered by user and embedder).

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uid() -> sa.Column:
    return sa.Column(
        "user_id",
        UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column(
        "user_settings",
        sa.Column("memory_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("messages", sa.Column("memory_refs", JSONB()))
    op.create_table(
        "memory_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        _uid(),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedder", sa.String(100)),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.7"),
        sa.Column("evidence", sa.String(16), nullable=False, server_default="doc"),
        sa.Column("source_type", sa.String(16), nullable=False, server_default="chat"),
        sa.Column("source_ref", JSONB(), nullable=False, server_default="{}"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("disputed_with", UUID(as_uuid=True)),
        sa.Column("superseded_by", UUID(as_uuid=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'disputed', 'deleted')",
            name="memory_items_status_check",
        ),
        sa.CheckConstraint(
            "evidence IN ('live', 'doc', 'assumption')", name="memory_items_evidence_check"
        ),
    )
    op.execute("ALTER TABLE memory_items ADD COLUMN embedding vector")
    op.create_index("ix_memory_items_user_id", "memory_items", ["user_id"])
    op.create_table(
        "memory_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("memory_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _uid(),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("evidence", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_memory_versions_item", "memory_versions", ["item_id", "version"], unique=True
    )
    op.create_index("ix_memory_versions_user_id", "memory_versions", ["user_id"])
    op.create_table(
        "memory_blocks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _uid(),
        sa.Column("reason", sa.String(50), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_ref", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_memory_blocks_user_id", "memory_blocks", ["user_id"])
    op.create_table(
        "memory_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        _uid(),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("stats", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_memory_runs_user_id", "memory_runs", ["user_id"])


def downgrade() -> None:
    op.drop_table("memory_runs")
    op.drop_table("memory_blocks")
    op.drop_table("memory_versions")
    op.drop_table("memory_items")
    op.drop_column("messages", "memory_refs")
    op.drop_column("user_settings", "memory_enabled")
