"""no escalation to an external model: the tables and the setting go

Revision ID: 0074
Revises: 0073
Create Date: 2026-10-28

The escalation of E3-02 (a question to an external model through the redaction layer, with a
budget) was never used on the production hub: both tables were empty. The code that called the
external model is removed with them; nothing in the hub sends data to an external model any more.
The downgrade makes the empty tables again (no data comes back).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0074"
down_revision: str | None = "0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("escalation_maps")
    op.drop_index("ix_escalations_user_created", table_name="escalations")
    op.drop_index("ix_escalations_user_id", table_name="escalations")
    op.drop_table("escalations")
    op.drop_column("user_settings", "escalate_without_asking")


def downgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("escalate_without_asking", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_table(
        "escalations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_kind", sa.String(10), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("scope_kind", sa.String(14), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("redaction", postgresql.JSONB(), nullable=False),
        sa.Column("answer", sa.Text()),
        sa.Column("answer_redacted", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("tokens_in", sa.Integer()),
        sa.Column("tokens_out", sa.Integer()),
        sa.Column("cost_usd", sa.Float()),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("auto", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_escalations_user_id", "escalations", ["user_id"])
    op.create_index("ix_escalations_user_created", "escalations", ["user_id", "created_at"])
    op.create_table(
        "escalation_maps",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_kind", sa.String(14), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("entries", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "scope_kind", "scope_id"),
    )
