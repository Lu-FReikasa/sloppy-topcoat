"""model ratings: the result of a bench suite for one model on one node

Revision ID: d106_model_ratings
Revises: d105_job_stats
Create Date: 2026-11-05

One row per finished suite of a bench run (PRG-00 R6): the share of cases that passed the
deterministic checks, the speed, and which cases failed. The suite version is part of the row,
so ratings of different versions are never compared.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d106_model_ratings"
down_revision: str | None = "d105_job_stats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_ratings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("node", sa.String(63), nullable=False),
        sa.Column("suite", sa.String(40), nullable=False),
        sa.Column("function_kind", sa.String(40), nullable=False),
        sa.Column("suite_version", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("passed", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("tps", sa.Float()),
        sa.Column("failed", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.create_index("ix_model_ratings_run_id", "model_ratings", ["run_id"])
    op.create_index("ix_model_ratings_lookup", "model_ratings", ["model", "node", "suite"])


def downgrade() -> None:
    op.drop_index("ix_model_ratings_lookup", table_name="model_ratings")
    op.drop_index("ix_model_ratings_run_id", table_name="model_ratings")
    op.drop_table("model_ratings")
