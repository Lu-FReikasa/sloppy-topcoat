"""agent_proposals.preview: the text a proposal shows besides its four sentences (a diff)

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-09

Additive: one nullable text column. It is part of the proposal's content hash when it is set, so
the diff a person confirmed is the diff that is pushed. Rows written before stay NULL and keep
their hash.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_proposals", sa.Column("preview", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_proposals", "preview")
