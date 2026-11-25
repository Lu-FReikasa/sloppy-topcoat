"""iterations of a task: numbered results, what each one was built on, the requirements

Revision ID: it01_iterations
Revises: ri01_report_images
Create Date: 2026-11-17

Live 2026-11-16 (task 1.8.2, Wireframe theme v1-v4): every new version took 2-4 chat turns, the
version number lived only in the title, the writer never saw the version before, and the feedback
lived in the task text, which the chat rewrote each time (v4 lost "dark" and came out light again).

On a result (a report below a task): ``iteration`` (1, 2, ...), ``based_on_id`` (the result it
continued) and ``feedback`` (what the person said about that one). On a task: ``requirements``
(what every further iteration must keep, a list the person edits) and ``next_iteration`` (what the
running iteration was started with, read when its result is filed). Existing results are numbered
by their order below their task.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "it01_iterations"
down_revision: str | None = "ri01_report_images"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("project_nodes", sa.Column("iteration", sa.Integer(), nullable=True))
    op.add_column(
        "project_nodes",
        sa.Column(
            "based_on_id",
            UUID(as_uuid=True),
            sa.ForeignKey("project_nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("project_nodes", sa.Column("feedback", sa.Text(), nullable=True))
    op.add_column("project_nodes", sa.Column("requirements", JSONB(), nullable=True))
    op.add_column("project_nodes", sa.Column("next_iteration", JSONB(), nullable=True))
    op.execute(
        """
        UPDATE project_nodes AS r SET iteration = o.n
        FROM (
            SELECT c.id, row_number() OVER (PARTITION BY c.parent_id ORDER BY c.created_at) AS n
            FROM project_nodes c JOIN project_nodes p ON p.id = c.parent_id
            WHERE c.kind = 'report' AND p.kind = 'task' AND c.title LIKE 'Ergebnis:%'
        ) AS o
        WHERE r.id = o.id
        """
    )


def downgrade() -> None:
    for name in ("next_iteration", "requirements", "feedback", "based_on_id", "iteration"):
        op.drop_column("project_nodes", name)
