"""nodes: state 'paused' (E2-05 B, D24)

A node freezes its models for the person using the machine and reports the state ``paused``;
the hub stores it like every other state.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATES = ("available", "busy_gaming", "degraded", "offline", "yielded")


def _state_check(states: Sequence[str]) -> None:
    op.drop_constraint("nodes_state_check", "nodes", type_="check")
    values = ", ".join(f"'{s}'" for s in states)
    op.create_check_constraint("nodes_state_check", "nodes", f"state IN ({values})")


def upgrade() -> None:
    _state_check((*STATES, "paused"))


def downgrade() -> None:
    op.execute("UPDATE nodes SET state = 'available' WHERE state = 'paused'")
    _state_check(STATES)
