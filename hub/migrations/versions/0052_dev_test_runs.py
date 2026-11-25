"""dev nodes: the capabilities of background test runs

Revision ID: 0052
Revises: 0051
Create Date: 2026-10-15

Data only. A test run of the dev workshop can go on in the node after the call that started it
(repo.test_start, repo.test_status, repo.test_cancel). The three names are added to the
capabilities of every dev node, so nobody has to switch them on by hand. A node that does not
offer them (an older program) simply does not list them.
"""

from collections.abc import Sequence

from alembic import op

from topcoat_hub.configmigrate import add_default_capabilities

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMES = ["repo.test_start", "repo.test_status", "repo.test_cancel"]


def upgrade() -> None:
    add_default_capabilities(op.get_bind(), NAMES, "0052_dev_test_runs", only_role="dev")


def downgrade() -> None:
    # the names stay in the lists: a capability a node does not offer is ignored
    pass
