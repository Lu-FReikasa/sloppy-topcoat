"""fixes of issues: the edits, the workshop's branch, commit, diff and tests, the roll-out

Revision ID: it02_issue_fixes
Revises: it01_iterations
Create Date: 2026-11-17

An issue (a task filed from the running system) is checked by ``issue_check`` and fixed by
``issue_fix``. The fix is a row here: the edits the run wrote, then what happened on the person's
click (applied on a branch of the workshop, committed, tested) and on the second click (rolled out
to the hub through the host's spool). ARCHITECTURE D103, D104.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "it02_issue_fixes"
down_revision: str | None = "it01_iterations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issue_fixes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("application", sa.String(10), nullable=False, server_default="project"),
        sa.Column(
            "issue_id",
            UUID(as_uuid=True),
            sa.ForeignKey("project_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("edits", JSONB(), nullable=False),
        sa.Column("files", JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(12), nullable=False, server_default="draft"),
        sa.Column("branch", sa.String(120), nullable=True),
        sa.Column("base_commit", sa.String(40), nullable=True),
        sa.Column("commit", sa.String(40), nullable=True),
        sa.Column("diff", sa.Text(), nullable=False, server_default=""),
        sa.Column("tests", JSONB(), nullable=True),
        sa.Column("rollout", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("decided_by", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "state IN ('draft', 'applying', 'testing', 'passed', 'failed', 'queued', 'live', "
            "'rolled_back', 'refused', 'discarded')",
            name="ck_issue_fixes_state",
        ),
    )
    op.create_index("ix_issue_fixes_user_id", "issue_fixes", ["user_id"])
    op.create_index("ix_issue_fixes_issue_id", "issue_fixes", ["issue_id"])


def downgrade() -> None:
    op.drop_index("ix_issue_fixes_issue_id", table_name="issue_fixes")
    op.drop_index("ix_issue_fixes_user_id", table_name="issue_fixes")
    op.drop_table("issue_fixes")
