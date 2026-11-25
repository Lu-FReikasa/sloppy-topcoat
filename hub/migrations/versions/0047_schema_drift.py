"""schema drift: timestamps of three tables are NOT NULL, as the models say

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-09

``alembic check`` found the models and the migrations disagreeing: the migrations of artifacts,
model_transfers and node_speeds created their timestamps nullable (with a server default), the
models say NOT NULL. Rows that somehow hold NULL are filled with now() first. The two composite
indexes the migrations created are now declared in the models, nothing changes in the database.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    ("artifacts", "created_at"),
    ("artifacts", "updated_at"),
    ("model_transfers", "created_at"),
    ("model_transfers", "updated_at"),
    ("node_speeds", "updated_at"),
)


def upgrade() -> None:
    for table, column in COLUMNS:
        op.execute(f"UPDATE {table} SET {column} = now() WHERE {column} IS NULL")  # noqa: S608
        op.alter_column(table, column, existing_type=sa.DateTime(timezone=True), nullable=False)


def downgrade() -> None:
    for table, column in COLUMNS:
        op.alter_column(table, column, existing_type=sa.DateTime(timezone=True), nullable=True)
