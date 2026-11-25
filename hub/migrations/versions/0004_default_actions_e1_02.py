"""nodes: add the E1-02 default actions to existing node configs (D14)

Nodes paired with an E1-01 hub only allow models.list/load/unload. This adds
models.download, models.delete and node.benchmark and records a config_changed event.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-28
"""

from collections.abc import Sequence

from alembic import op

from topcoat_hub.configmigrate import add_default_actions

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIONS = ["models.download", "models.delete", "node.benchmark"]


def upgrade() -> None:
    add_default_actions(op.get_bind(), ACTIONS, source="migration 0004")


def downgrade() -> None:
    # Permissions are not taken away again: an admin may rely on them by now.
    pass
