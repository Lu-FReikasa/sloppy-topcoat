"""planning: requests to other applications are recorded as reports with a state

Revision ID: 0072
Revises: 0071
Create Date: 2026-10-27

A request of the planning agent to another application (for example a report from Research, or a
branch in Development) is a report of the project. It records the target, the action and its
state: ``awaiting`` (the grant is ``confirm``: the person must confirm it), or ``sent`` (the grant
is ``free``). Nothing is sent to the other application by this migration or by the store; the
hand-over is a later brief (PPH-02).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("planning_nodes", sa.Column("request_target", sa.String(20), nullable=True))
    op.add_column("planning_nodes", sa.Column("request_action", sa.String(30), nullable=True))
    op.add_column("planning_nodes", sa.Column("request_state", sa.String(12), nullable=True))


def downgrade() -> None:
    op.drop_column("planning_nodes", "request_state")
    op.drop_column("planning_nodes", "request_action")
    op.drop_column("planning_nodes", "request_target")
