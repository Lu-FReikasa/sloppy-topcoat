"""conversations.flow_id: the flow that makes the answers of a conversation (E7-05)

Revision ID: 0056
Revises: 0055
Create Date: 2026-10-21

Additive: one nullable column. ``NULL`` keeps the answer path every conversation had; a flow makes
the answer from a graph and shows its chain in the chat. A deleted flow releases the conversation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "flow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flows.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("conversations", "flow_id")
