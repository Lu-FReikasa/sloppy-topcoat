"""node aliases: the names a node had before it was renamed

Revision ID: na01_node_aliases
Revises: pb01_project_batches
Create Date: 2026-11-10

A node can be renamed (node_rename.py). The living settings are rewritten to the new name; the
history keeps the old one, and this table lets an old name still reach the node.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "na01_node_aliases"
down_revision: str | None = "pb01_project_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "node_aliases",
        sa.Column("name", sa.String(63), primary_key=True),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "renamed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_node_aliases_node_id", "node_aliases", ["node_id"])


def downgrade() -> None:
    op.drop_index("ix_node_aliases_node_id", table_name="node_aliases")
    op.drop_table("node_aliases")
