"""quiet hours and the hour of the overdue digest of a person (PPH-04, 04h)

Revision ID: 0083
Revises: 0082
Create Date: 2026-10-29

Three optional columns on ``users``, all read on the wall clock of the person's time zone (D4-4):

* ``project_quiet_start`` / ``project_quiet_end``: minutes after midnight. While the local time lies
  inside the span (it may cross midnight) no reminder and no digest is delivered; they wait and go
  out when the span ends. Both NULL = no quiet hours; setting only one is refused.
* ``project_digest_hour``: the local hour (0-23) from which the daily overdue digest may go to the
  inbox. NULL = no digest.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0083"
down_revision: str | None = "0082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("project_quiet_start", sa.SmallInteger()))
    op.add_column("users", sa.Column("project_quiet_end", sa.SmallInteger()))
    op.add_column("users", sa.Column("project_digest_hour", sa.SmallInteger()))
    op.create_check_constraint(
        "ck_users_project_quiet",
        "users",
        "(project_quiet_start IS NULL) = (project_quiet_end IS NULL)"
        " AND (project_quiet_start IS NULL OR project_quiet_start BETWEEN 0 AND 1439)"
        " AND (project_quiet_end IS NULL OR project_quiet_end BETWEEN 0 AND 1439)",
    )
    op.create_check_constraint(
        "ck_users_project_digest_hour",
        "users",
        "project_digest_hour IS NULL OR project_digest_hour BETWEEN 0 AND 23",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_project_digest_hour", "users", type_="check")
    op.drop_constraint("ck_users_project_quiet", "users", type_="check")
    op.drop_column("users", "project_digest_hour")
    op.drop_column("users", "project_quiet_end")
    op.drop_column("users", "project_quiet_start")
