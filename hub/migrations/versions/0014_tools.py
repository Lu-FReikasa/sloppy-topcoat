"""tool nodes: capability grants and the tool-call audit log (E2-04, D15, D20)

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "capability_grants",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capability", sa.String(100), nullable=False),
        sa.Column("granted_by", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("user_id", "node_id", "capability"),
    )
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_id", postgresql.UUID(as_uuid=True)),
        sa.Column("node_name", sa.String(63), nullable=False),
        sa.Column("capability", sa.String(100), nullable=False),
        sa.Column("input", postgresql.JSONB(), nullable=False),
        sa.Column("via", sa.String(10), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True)),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("code", sa.String(40)),
        sa.Column("error", sa.Text()),
        sa.Column("output_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_excerpt", sa.Text()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tool_calls_user_id", "tool_calls", ["user_id"])
    op.create_index("ix_tool_calls_node_id", "tool_calls", ["node_id"])
    op.create_index("ix_tool_calls_created_at", "tool_calls", ["created_at"])


def downgrade() -> None:
    op.drop_table("tool_calls")
    op.drop_table("capability_grants")
