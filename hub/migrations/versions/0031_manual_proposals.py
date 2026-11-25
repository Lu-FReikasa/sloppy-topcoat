"""manual proposals and stack logs: ``agent_proposals.task_id`` may be NULL, ``origin``

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-03

Additive: one new column with a default and a column that may now be empty. A proposal the person
makes themselves in the inventory (restart a container or a stack) belongs to no agent task.
Existing
tool nodes get ``compose.logs`` among their switched-on capabilities (read-only; a node that does
not offer it just does not list it).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from topcoat_hub.configmigrate import add_default_capabilities

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_proposals",
        sa.Column("origin", sa.String(10), nullable=False, server_default="agent"),
    )
    op.alter_column("agent_proposals", "task_id", existing_type=sa.UUID(), nullable=True)
    add_default_capabilities(op.get_bind(), ["compose.logs"], "0031_manual_proposals")


def downgrade() -> None:
    # a manual proposal has no task to belong to: it goes (they are short-lived by design)
    op.execute("DELETE FROM agent_proposals WHERE task_id IS NULL")
    op.alter_column("agent_proposals", "task_id", existing_type=sa.UUID(), nullable=False)
    op.drop_column("agent_proposals", "origin")
