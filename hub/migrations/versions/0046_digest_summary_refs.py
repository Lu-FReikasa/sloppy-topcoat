"""chat_digests.summary_refs: the statements behind a summary, each with message and quote

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-07

Additive: one nullable JSON column. Rows written before stay NULL (their summary has no
citations). (Renumbered from 0040 when it joined the chain after 0045.)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_digests", sa.Column("summary_refs", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_digests", "summary_refs")
