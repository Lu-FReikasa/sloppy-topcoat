"""Migration 0084: the table of project templates comes and goes with the migration, and its
name is unique per person. The session's database is brought back to head at the end."""

from alembic import command
from sqlalchemy import create_engine, inspect

from tests.conftest import TEST_DB_URL, alembic_config


def test_0084_creates_and_drops_the_templates_table(migrated_db: str) -> None:
    cfg = alembic_config()
    engine = create_engine(TEST_DB_URL)
    try:
        command.downgrade(cfg, "0083")
        assert "project_templates" not in inspect(engine).get_table_names()
        command.upgrade(cfg, "0084")
        inspector = inspect(engine)
        assert "project_templates" in inspector.get_table_names()
        assert {c["name"] for c in inspector.get_columns("project_templates")} == {
            "id",
            "user_id",
            "application",
            "name",
            "description",
            "content",
            "created_at",
        }
        uniques = inspector.get_unique_constraints("project_templates")
        assert [u["column_names"] for u in uniques] == [["user_id", "application", "name"]]
        command.downgrade(cfg, "0083")
        assert "project_templates" not in inspect(engine).get_table_names()
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()
