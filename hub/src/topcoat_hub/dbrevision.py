"""Is the database one this build can work with? (checked before the migrations run)

A newer build may have migrated the database further than this one knows (an image was rolled
back, or a test build deployed over production). Alembic then dies with a traceback about a
revision it cannot locate; here the operator is told what happened in one sentence.
"""

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, pool


class UnknownRevisionError(RuntimeError):
    """The database is at a revision this build does not have."""

    def __init__(self, unknown: list[str], newest: str) -> None:
        self.unknown, self.newest = unknown, newest
        super().__init__(
            f"database is at revision {', '.join(unknown)} which this build does not know "
            f"(its newest is {newest}) - newer build deployed before? Deploy that build again "
            "(or one with the same migrations), or restore a database backup; this build "
            "does not touch the database."
        )


def database_revisions(url: str) -> set[str]:
    """The revisions the database is at (empty for a database that was never migrated)."""
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            return set(MigrationContext.configure(connection).get_current_heads())
    finally:
        engine.dispose()


def check_known(cfg: Config, url: str) -> None:
    """Raise :class:`UnknownRevisionError` if the database is ahead of this build's migrations."""
    script = ScriptDirectory.from_config(cfg)
    known = {r.revision for r in script.walk_revisions()}
    unknown = sorted(rev for rev in database_revisions(url) if rev not in known)
    if unknown:
        raise UnknownRevisionError(unknown, ", ".join(script.get_heads()))
