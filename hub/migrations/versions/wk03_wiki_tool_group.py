"""the tool group wiki for the stored programs Projekte and Verwaltung

Revision ID: wk03_wiki_tool_group
Revises: wk02_wiki_pages
Create Date: 2026-11-12

The built-in programs are rewritten at a start when their revision in ``ui/seeds.py`` is newer;
a stored program ``project`` or ``admin`` that an admin changed (no longer built in) is not. Both
get the group ``wiki`` in their ceiling here, so their assistants read the released wiki pages.
A ceiling of ``None`` (every group) is left as it is.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "wk03_wiki_tool_group"
down_revision: str | None = "wk02_wiki_pages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE ui_templates
        SET agent = jsonb_set(agent, '{tool_scope,groups}',
                              (agent #> '{tool_scope,groups}') || '["wiki"]'::jsonb)
        WHERE key IN ('project', 'admin')
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
        WHERE key IN ('project', 'admin')
          AND jsonb_typeof(agent #> '{tool_scope,groups}') = 'array'
        """
    )
