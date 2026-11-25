"""Chat reads the wiki too (the maintainer, 2026-11-12)

Revision ID: wk05_chat_reads_wiki
Revises: cv01_conversation_context
Create Date: 2026-11-12

Clipper at a desktop window talks in the application of the window, and a window without an
assistant of its own (the Wiki) talks in Chat; it should read the page it is attached to. The
built-in Chat gets the group in ui/seeds.py (revision 12); a stored Chat an admin changed (no longer
built in) gets it here, like wk03 did for Projekte and Verwaltung. A ceiling of None (every group)
is left as it is. Only released pages are read, and only by the wiki tools (read only).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "wk05_chat_reads_wiki"
down_revision: str | None = "cv01_conversation_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE ui_templates
        SET agent = jsonb_set(agent, '{tool_scope,groups}',
                              (agent #> '{tool_scope,groups}') || '["wiki"]'::jsonb)
        WHERE key = 'chat'
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
        WHERE key = 'chat'
          AND jsonb_typeof(agent #> '{tool_scope,groups}') = 'array'
        """
    )
