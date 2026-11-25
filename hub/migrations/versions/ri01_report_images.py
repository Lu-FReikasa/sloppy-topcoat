"""pictures of a report, kept by the hub

Revision ID: ri01_report_images
Revises: cs01_conversation_state
Create Date: 2026-11-13

``report_images``: a picture a research run placed in its report (``research.report.image``),
fetched once by a node (``web.image``) and shown only from the hub, only to its person.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "ri01_report_images"
down_revision: str | None = "cs01_conversation_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_images",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("content_type", sa.String(20), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("image_url", sa.Text(), nullable=False),
        sa.Column("page_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("caption", sa.String(200), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_report_images_task_id", "report_images", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_report_images_task_id", table_name="report_images")
    op.drop_table("report_images")
