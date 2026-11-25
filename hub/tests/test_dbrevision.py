"""A build that meets a database ahead of it says so in one sentence (instead of Alembic's
traceback and a crash loop with no explanation)."""

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text

from tests.conftest import TEST_DB_URL, alembic_config
from topcoat_hub import serve
from topcoat_hub.config import get_settings
from topcoat_hub.dbrevision import UnknownRevisionError, check_known, database_revisions


@pytest.fixture
def at_revision(migrated_db: str) -> Iterator[object]:
    """Set the database's recorded revision for a test and put it back afterwards."""
    engine = create_engine(migrated_db)
    with engine.connect() as conn:
        original = conn.scalar(text("SELECT version_num FROM alembic_version"))

    def set_to(revision: str) -> None:
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :r"), {"r": revision})

    yield set_to
    set_to(original)
    engine.dispose()


def test_a_database_at_this_builds_head_or_older_is_fine(migrated_db: str, at_revision) -> None:
    cfg = alembic_config()
    check_known(cfg, migrated_db)
    at_revision("0015")  # older than this build: the migrations bring it forward
    check_known(cfg, migrated_db)
    assert database_revisions(migrated_db) == {"0015"}


def test_a_database_ahead_of_the_build_is_named(migrated_db: str, at_revision) -> None:
    at_revision("0099")
    with pytest.raises(UnknownRevisionError) as exc:
        check_known(alembic_config(), migrated_db)
    message = str(exc.value)
    assert "database is at revision 0099 which this build does not know" in message
    assert "newer build deployed before?" in message and exc.value.newest in message
    assert "Traceback" not in message


def test_the_hub_refuses_to_start_with_that_message(
    migrated_db: str,
    at_revision,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    at_revision("0099")
    monkeypatch.setenv("TOPCOAT_DATABASE_URL", migrated_db)
    get_settings.cache_clear()
    monkeypatch.setattr(serve, "alembic_config", alembic_config)
    try:
        with pytest.raises(SystemExit) as exit_:
            serve.main([])
        assert exit_.value.code == 1
        err = capsys.readouterr().err  # serve configures logging itself (to stderr)
        assert (
            "ERROR topcoat_hub.serve: database is at revision 0099 which this build does not know"
            in err
        )
        assert "Traceback" not in err
        # --no-migrate does not skip the check
        with pytest.raises(SystemExit):
            serve.main(["--no-migrate"])
    finally:
        get_settings.cache_clear()


def test_a_database_that_was_never_migrated_is_fine() -> None:
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS empty_check"))
    engine.dispose()
    url = (
        TEST_DB_URL.replace("topcoat_test", "topcoat_test") + "?options=-csearch_path%3Dempty_check"
    )
    try:
        assert database_revisions(url) == set()
    finally:
        engine = create_engine(TEST_DB_URL)
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA empty_check CASCADE"))
        engine.dispose()
