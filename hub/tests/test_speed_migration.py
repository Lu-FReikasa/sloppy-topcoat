"""SCH-02: the wrong generation speeds are emptied once, everything else stays."""

import importlib.util
import pathlib
from unittest import mock

from sqlalchemy import create_engine, text

from tests.conftest import TEST_DB_URL

MIGRATION = (
    pathlib.Path(__file__).parent.parent / "migrations" / "versions" / "sch02_reset_speeds.py"
)


def test_the_migration_empties_the_generation_speeds_and_leaves_the_load_speeds() -> None:
    spec = importlib.util.spec_from_file_location("sch02_reset_speeds", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "ns01_releases"

    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TEMP TABLE node_speeds (tg_tps float, tg_samples int, load float)")
        )
        conn.execute(text("INSERT INTO node_speeds VALUES (730.0, 165, 12.0)"))
        with mock.patch.object(module.op, "execute", lambda sql: conn.execute(text(sql))):
            module.upgrade()
        row = conn.execute(text("SELECT tg_tps, tg_samples, load FROM node_speeds")).one()
    engine.dispose()
    assert tuple(row) == (None, 0, 12.0)
