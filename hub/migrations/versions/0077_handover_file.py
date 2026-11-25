"""handovers: the Akte (case file) of a hand-over

Revision ID: 0077
Revises: 0076
Create Date: 2026-10-28

Every hand-over gets a file from the moment it is prepared: the request, the steps, and the report
with its sources (``file`` as data, ``file_markdown`` as the text the person reads and the knowledge
base holds). ``knowledge_path`` names the file of a finished one in the person's knowledge source
"Akten". Hand-overs from before get theirs the first time somebody asks for it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0077"
down_revision: str | None = "0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("handovers", sa.Column("file", postgresql.JSONB(), nullable=True))
    op.add_column("handovers", sa.Column("file_markdown", sa.Text(), nullable=True))
    op.add_column("handovers", sa.Column("knowledge_path", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("handovers", "knowledge_path")
    op.drop_column("handovers", "file_markdown")
    op.drop_column("handovers", "file")
