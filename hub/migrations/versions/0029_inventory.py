"""inventory of what runs on the nodes (E3-10, D45): ``inventory_nodes``, ``inventory_items``

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-02

Additive: two new tables. Existing tool nodes get ``inventory.snapshot`` among their switched-on
capabilities (it is read-only and a node that does not offer it just does not list it), so the
inventory works without editing every node's config by hand.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from topcoat_hub.configmigrate import add_default_capabilities

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inventory_nodes",
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("attempted_at", sa.DateTime(timezone=True)),
        sa.Column("collected_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(12), nullable=False, server_default="unavailable"),
        sa.Column("error", sa.Text()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("sections", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("host", postgresql.JSONB()),
    )
    op.create_table(
        "inventory_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("state_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gone_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("node_id", "kind", "name", name="uq_inventory_item"),
    )
    op.create_index("ix_inventory_items_node_kind", "inventory_items", ["node_id", "kind"])
    add_default_capabilities(op.get_bind(), ["inventory.snapshot"], "0029_inventory")


def downgrade() -> None:
    op.drop_index("ix_inventory_items_node_kind", table_name="inventory_items")
    op.drop_table("inventory_items")
    op.drop_table("inventory_nodes")
