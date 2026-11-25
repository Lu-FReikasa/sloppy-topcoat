"""the application key `planning` becomes `project` (PPH-04, piece 04.0)

Revision ID: 0078
Revises: 0077
Create Date: 2026-10-29

The Projects application is called `project` everywhere: its tables (`projects`, `project_nodes`,
`project_changes`, with their constraints and indexes), the `application` value in every row that
carries one, the capability names of the prepared changes, the built-in workspace template and the
module ids that name it (also in the people's saved desktops), the grants that name the workspace,
and the root of the memory tag tree. Conversations, notes, tasks and hand-overs of the old key are
found under the new one. The downgrade reverses all of it.
"""

from collections.abc import Sequence

from alembic import op

# every statement is built from the constants of this file, never from input
# ruff: noqa: S608

revision: str = "0078"
down_revision: str | None = "0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# old table name, new table name
TABLES = (
    ("planning_projects", "projects"),
    ("planning_nodes", "project_nodes"),
    ("planning_changes", "project_changes"),
)
APPLICATION_COLUMNS = "('application', 'from_application', 'to_application')"
# what the old workspace and its pages were called, by what they are called now
MODULES = (("page.chat.planning", "page.chat.project"), ("page.planning", "page.project"))
# planning.project.<x> became project.<x> (the project is the application now); the others only
# changed their prefix
PROJECT_CAPABILITIES = ("list", "get", "update")


# Migration 0071 renamed `planning_tasks` to `planning_nodes` but left the names of the table's
# constraints and indexes (`planning_tasks_pkey`, `ix_planning_tasks_user_id` …) as they were
LEGACY_NAMES = {"planning_nodes": ("planning_tasks",)}


def _rename_table(old: str, new: str) -> None:
    """Rename a table and every constraint and index whose name carries the old table name (or a
    name the table had before)."""
    names = (old, *LEGACY_NAMES.get(old, ()))
    renamed = "{name}"
    for name in names:
        renamed = f"replace({renamed}, '{name}', '{new}')"
    constraints = " OR ".join(f"conname LIKE '%{name}%'" for name in names)
    indexes = " OR ".join(f"indexname LIKE '%{name}%'" for name in names)
    op.execute(
        f"""
        DO $$
        DECLARE r record;
        BEGIN
          FOR r IN SELECT conname FROM pg_constraint
                   WHERE conrelid = '{old}'::regclass AND ({constraints})
          LOOP
            EXECUTE format('ALTER TABLE {old} RENAME CONSTRAINT %I TO %I',
                           r.conname, {renamed.format(name="r.conname")});
          END LOOP;
          FOR r IN SELECT indexname FROM pg_indexes
                   WHERE schemaname = current_schema() AND tablename = '{old}'
                     AND ({indexes})
                     AND indexname NOT IN (SELECT conname FROM pg_constraint
                                           WHERE conrelid = '{old}'::regclass)
          LOOP
            EXECUTE format('ALTER INDEX %I RENAME TO %I',
                           r.indexname, {renamed.format(name="r.indexname")});
          END LOOP;
        END $$;
        """
    )
    op.rename_table(old, new)


def _application_values(old: str, new: str) -> None:
    """The `application` value of every row that carries one, and the server defaults."""
    op.execute(
        f"""
        DO $$
        DECLARE r record;
        BEGIN
          FOR r IN SELECT table_name, column_name, column_default
                   FROM information_schema.columns
                   WHERE table_schema = current_schema()
                     AND column_name IN {APPLICATION_COLUMNS}
                     AND table_name IN (SELECT table_name FROM information_schema.tables
                                        WHERE table_schema = current_schema()
                                          AND table_type = 'BASE TABLE')
          LOOP
            EXECUTE format('UPDATE %I SET %I = %L WHERE %I = %L',
                           r.table_name, r.column_name, '{new}', r.column_name, '{old}');
            IF r.column_default LIKE '%''{old}''%' THEN
              EXECUTE format('ALTER TABLE %I ALTER COLUMN %I SET DEFAULT %L',
                             r.table_name, r.column_name, '{new}');
            END IF;
          END LOOP;
        END $$;
        """
    )


def _json_tokens(table: str, column: str, pairs: Sequence[tuple[str, str]]) -> str:
    """SQL that replaces whole JSON string tokens in a jsonb column, only in rows that have one."""
    expr = f"{column}::text"
    for old, new in pairs:
        expr = f"replace({expr}, '\"{old}\"', '\"{new}\"')"
    first = pairs[0][0]
    return (
        f"UPDATE {table} SET {column} = ({expr})::jsonb WHERE {column}::text LIKE '%\"{first}\"%'"
    )


def _workspace(old: str, new: str, modules: Sequence[tuple[str, str]]) -> None:
    """The built-in template, the grants that name the workspace, the people's saved desktops."""
    op.execute(f"UPDATE ui_templates SET key = '{new}' WHERE key = '{old}'")
    op.execute(
        f"UPDATE ui_templates SET title_key = 'workspace.{new}' WHERE title_key = 'workspace.{old}'"
    )
    op.execute(f"UPDATE ui_grants SET key = '{new}' WHERE kind = 'workspace' AND key = '{old}'")
    pairs = [*modules, (old, new)]
    for table, column in (
        ("ui_templates", "modules"),
        ("ui_templates", "open"),
        ("ui_templates", "agent"),
        ("users", "ui_prefs"),
    ):
        for pair in pairs:
            op.execute(_json_tokens(table, column, [pair]))


def _tag_root(old: str, new: str) -> None:
    """The root of the memory tag tree. Tag names are unique per person: a tag the person made
    with the new name gets out of the way (its id makes it unique), the root takes the name."""
    op.execute(
        f"""
        UPDATE memory_tags AS other SET name = 'x-' || left(other.id::text, 8)
        WHERE other.name = '{new}'
          AND EXISTS (SELECT 1 FROM memory_tags AS root
                      WHERE root.user_id = other.user_id AND root.name = '{old}'
                        AND root.parent_id IS NULL)
        """
    )
    op.execute(f"UPDATE memory_tags SET name = '{new}' WHERE name = '{old}' AND parent_id IS NULL")
    op.execute(f"UPDATE memory_tags SET responsible = '{new}' WHERE responsible = '{old}'")


def upgrade() -> None:
    for old, new in TABLES:
        _rename_table(old, new)
    _application_values("planning", "project")
    # planning.project.list -> project.list, planning.node.create -> project.node.create
    op.execute(
        "UPDATE project_changes SET capability = substr(capability, 10) "
        "WHERE capability LIKE 'planning.project.%'"
    )
    op.execute(
        "UPDATE project_changes SET capability = 'project.' || substr(capability, 10) "
        "WHERE capability LIKE 'planning.%'"
    )
    _workspace("planning", "project", MODULES)
    _tag_root("planning", "project")


def downgrade() -> None:
    _tag_root("project", "planning")
    _workspace("project", "planning", [(new, old) for old, new in MODULES])
    renamed = ", ".join(f"'project.{c}'" for c in PROJECT_CAPABILITIES)
    op.execute(
        f"UPDATE project_changes SET capability = CASE WHEN capability IN ({renamed}) "
        "THEN 'planning.project.' || substr(capability, 9) "
        "ELSE 'planning.' || substr(capability, 9) END WHERE capability LIKE 'project.%'"
    )
    _application_values("project", "planning")
    for old, new in reversed(TABLES):
        _rename_table(new, old)
