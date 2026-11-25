"""Entwicklung reads the wiki too (the maintainer, 2026-11-17)

Revision ID: wk06_dev_reads_wiki
Revises: it02_issue_fixes
Create Date: 2026-11-17

A task of Projekte run in Entwicklung surveys the code; the wiki describes it page by page (live:
a run without it guessed paths and found none of the image code there is). The built-in program
gets the group in ui/seeds.py (revision 16); a stored one an admin changed gets it here, as wk03
and wk05 did. A ceiling of None (every group) is left as it is. The wiki tools only read.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "wk06_dev_reads_wiki"
down_revision: str | None = "it02_issue_fixes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE ui_templates
        SET agent = jsonb_set(agent, '{tool_scope,groups}',
                              (agent #> '{tool_scope,groups}') || '["wiki"]'::jsonb)
        WHERE key = 'dev'
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
        WHERE key = 'dev'
          AND jsonb_typeof(agent #> '{tool_scope,groups}') = 'array'
        """
    )
