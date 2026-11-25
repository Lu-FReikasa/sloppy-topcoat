"""the record of model requests (SCH-03 step 3)

Revision ID: sch03_job_records
Revises: sch02_speeds
Create Date: 2026-10-31

One row per request the gateway served or refused: work label, caller, chosen node and model,
how long it waited and ran, the outcome. Append-only, never content. The rows give the waiting time
per node before and after the queue work and are what the admin view of OBS-01 will show.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "sch03_job_records"
down_revision: str | None = "sch02_speeds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE")
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("task_id", postgresql.UUID(as_uuid=True)),
        sa.Column("flow_run_id", sa.String(64)),
        sa.Column("application", sa.String(40)),
        sa.Column("work", sa.String(40), nullable=False, server_default=""),
        sa.Column("caller", sa.String(200), nullable=False, server_default=""),
        sa.Column("requested", sa.String(255), nullable=False, server_default=""),
        sa.Column("chosen_model", sa.String(255), nullable=False, server_default=""),
        sa.Column("chosen_node", sa.String(63), nullable=False, server_default=""),
        sa.Column("place", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(24), nullable=False, server_default="picked"),
        sa.Column("reason", sa.String(80), nullable=False, server_default=""),
        sa.Column("outcome", sa.String(12), nullable=False, server_default="done"),
        sa.Column("error_code", sa.String(40)),
        sa.Column("waited_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_loaded", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens", sa.Integer()),
    )
    op.create_index("ix_job_records_created_at", "job_records", ["created_at"])
    op.create_index("ix_job_records_conversation_id", "job_records", ["conversation_id"])
    op.create_index("ix_job_records_task_id", "job_records", ["task_id"])
    op.create_index("ix_job_records_user_id", "job_records", ["user_id"])


def downgrade() -> None:
    op.drop_table("job_records")
