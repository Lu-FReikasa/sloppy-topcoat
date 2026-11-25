"""upload sources for the knowledge base (E2-03 rest): ``knowledge_sources.kind``

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-02

Additive: every existing source is read through a node (``node``), so nothing changes for them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_sources",
        sa.Column("kind", sa.String(8), server_default="node", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("knowledge_sources", "kind")
