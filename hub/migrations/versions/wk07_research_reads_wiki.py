"""Research reads the wiki too (the maintainer, 2026-11-18)

Revision ID: wk07_research_reads_wiki
Revises: wk06_dev_reads_wiki
Create Date: 2026-11-18

A research about Topcoat itself had no access to its documentation (the planner even described
Research as "the web and the wiki"). The built-in program gets the group in ui/seeds.py (revision
11); a stored one an admin changed gets it here, as wk06 did for Entwicklung. A ceiling of None
(every group) is left as it is. The wiki tools only read.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "wk07_research_reads_wiki"
down_revision: str | None = "wk06_dev_reads_wiki"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE ui_templates
        SET agent = jsonb_set(agent, '{tool_scope,groups}',
                              (agent #> '{tool_scope,groups}') || '["wiki"]'::jsonb)
        WHERE key = 'research'
          AND jsonb_typeof(agent #> '{tool_scope,groups}') = 'array'
          AND NOT (agent #> '{tool_scope,groups}') ? 'wiki'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE ui_templates
        SET agent = jsonb_set(agent, '{tool_scope,groups}',
                              (agent #> '{tool_scope,groups}') - 'wiki')
        WHERE key = 'research'
          AND jsonb_typeof(agent #> '{tool_scope,groups}') = 'array'
        """
    )
