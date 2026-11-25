"""job records: the tokens, the cache and the times the model reported

Revision ID: d105_job_stats
Revises: d102_reports
Create Date: 2026-11-05

Five nullable columns on ``job_records``: ``prompt_tokens``, ``cached_tokens``, ``prompt_ms``,
``predicted_ms`` and ``finish_reason`` (``tokens`` already exists and now holds the generated
tokens). With them the speed of a node is a query (tokens per second of the answer, the speed of
reading the prompt, how much the cache saved) and a run that ended on its length shows as such.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d105_job_stats"
down_revision: str | None = "d102_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job_records", sa.Column("prompt_tokens", sa.Integer()))
    op.add_column("job_records", sa.Column("cached_tokens", sa.Integer()))
    op.add_column("job_records", sa.Column("prompt_ms", sa.Integer()))
    op.add_column("job_records", sa.Column("predicted_ms", sa.Integer()))
    op.add_column("job_records", sa.Column("finish_reason", sa.String(16)))


def downgrade() -> None:
    for name in ("finish_reason", "predicted_ms", "prompt_ms", "cached_tokens", "prompt_tokens"):
        op.drop_column("job_records", name)
