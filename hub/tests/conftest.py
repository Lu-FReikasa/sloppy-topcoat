import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from tests.fakenode import FakeNode

from topcoat_hub import wiki_tools
from topcoat_hub.config import Settings
from topcoat_hub.main import create_app
from topcoat_hub.models import DEV_ADMIN_ID

HUB_DIR = Path(__file__).resolve().parents[1]
BASE_TEST_DB_URL = os.environ.get(
    "TOPCOAT_TEST_DATABASE_URL",
    "postgresql+psycopg://topcoat:topcoat@localhost:5432/topcoat_test",
)


def _worker_db_url(base: str) -> str:
    """Give every pytest-xdist worker its own database (topcoat_test_gw0, ...)."""
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker:
        return base
    url = make_url(base)
    name = f"{url.database}_{worker}"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name})
        if exists.first() is None:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    # the migrations create the vector extension (the test role is a superuser)
    return url.set(database=name).render_as_string(hide_password=False)


TEST_DB_URL = _worker_db_url(BASE_TEST_DB_URL)
DEV_TOKEN = "test-dev-token"
AUTH = {"Authorization": f"Bearer {DEV_TOKEN}"}


def alembic_config() -> Config:
    cfg = Config(str(HUB_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(HUB_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    return cfg


@pytest.fixture(scope="session")
def migrated_db() -> str:
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return TEST_DB_URL


@pytest.fixture
def clean_db(migrated_db: str) -> Iterator[str]:
    engine = create_engine(migrated_db)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE node_events, node_inventory, pairing_codes, nodes CASCADE"))
        # Personal data of all users and every user but the seeded dev admin (D20 tests).
        conn.execute(
            text(
                "TRUNCATE memory_versions, memory_reports, memory_items, memory_tags, "
                "memory_blocks, memory_runs, "
                "memory_questions, messages, conversations, user_settings, hub_settings, "
                "agent_proposals, agent_evidence, agent_steps, agent_tasks, tool_calls, "
                "capability_grants, "
                "knowledge_chunks, knowledge_files, "
                "knowledge_sources, memory_proposals, prompt_snapshots, personas, api_keys, "
                "dev_repos, setting_changes, "
                "ui_templates, ui_themes, inbox_items, handovers, "
                "project_events, project_node_versions, project_views, project_labels, "
                "project_change_sets, project_changes, project_templates, "
                "project_nodes, "
                "model_ratings, "
                "projects CASCADE"
            )
        )
        # profiles are referenced by users: delete them (grants go with them, users.profile_id
        # becomes NULL) instead of truncating, which would cascade into the users
        conn.execute(text("DELETE FROM ui_profiles"))
        conn.execute(text("DELETE FROM users WHERE id <> :admin"), {"admin": str(DEV_ADMIN_ID)})
        # the dev admin stays: its own choices (the desktop, the language) start fresh
        conn.execute(text("UPDATE users SET ui_prefs = '{}', language = NULL, profile_id = NULL"))
        conn.execute(
            text(
                "UPDATE users SET project_quiet_start = NULL, project_quiet_end = NULL,"
                " project_digest_hour = NULL"
            )
        )
    engine.dispose()
    yield migrated_db


@pytest.fixture
def settings(clean_db: str, tmp_path: Path) -> Settings:
    return Settings(
        database_url=clean_db,
        dev_admin_token=DEV_TOKEN,
        action_timeout_seconds=2.0,
        hello_timeout_seconds=2.0,
        sse_keepalive_seconds=0.2,
        web_dist=tmp_path / "no-web-dist",
        tls_dir=tmp_path / "tls",
        tls_hostnames="hub.example.lan",
        memory_consolidation_interval_seconds=0,  # tests call the job themselves
        # the question flow keeps its tests; the hub's own decision has its own
        # (test_memory_strict_rules.py)
        memory_auto_settle=False,
        knowledge_reindex_seconds=0,
        self_tool_enabled=False,  # on only in the tests of the Topcoat tool
        inventory_interval_seconds=0,  # tests read the inventory themselves
        embedding_watch_seconds=0,  # no background look at the gateway while a test connects nodes
        retention_tick_seconds=0,  # the tests call the pruning themselves
        project_reminder_tick_seconds=0,  # the tests of 04h call the tick with a fake clock
        memory_note_wait_seconds=5.0,
        review_after_answer=False,  # on only in the tests of the second look right after an answer
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


def assert_clean_shutdown(db_url: str) -> None:
    """After the hub is down none of its connections may be left "idle in transaction".

    That is what a background task dropped in the middle of a query leaves behind (its
    connection is never returned): the next test's ``TRUNCATE`` then waits for it forever and
    the whole suite hangs. Checking right here turns that hang into a failure that names the
    test that leaked and the statement it was in."""
    engine = create_engine(db_url)
    rows: list = []
    try:
        deadline = time.monotonic() + 3.0
        while True:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT pid, left(query, 90) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                        "AND state LIKE 'idle in transaction%'"
                    )
                ).all()
            if not rows or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        if rows:  # do not let the leak block every test after this one: end it, then fail
            with engine.connect() as conn:
                for pid, _ in rows:
                    conn.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
                conn.commit()
    finally:
        engine.dispose()
    assert not rows, f"the hub left connections in a transaction after shutdown: {rows}"


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
    # the lifespan has ended: background tasks are cancelled and awaited, the engine is disposed
    assert_clean_shutdown(app.state.settings.database_url)


@pytest.fixture
def connect(client: TestClient) -> Iterator[Callable[[str, str], "FakeNode"]]:
    """Open fake node sessions that are always closed before the client shuts down."""
    from tests.fakenode import FakeNode

    opened: list[FakeNode] = []

    def _connect(node_id: str, key: "Ed25519PrivateKey", version: int | None = None) -> FakeNode:
        node = (
            FakeNode(client, node_id, key)
            if version is None
            else FakeNode(client, node_id, key, version)
        )
        opened.append(node)
        return node

    yield _connect
    for node in opened:
        node.close()


@pytest.fixture
def open_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool ceiling of a program (app_scope) lifted: for a test whose research run looks at a
    machine of the test world. A real Research reads the web and the notes only."""

    async def everything(db: object, user: object, application: str | None) -> dict[str, object]:
        return {"nodes": None, "groups": None, "internet": None}

    monkeypatch.setattr("topcoat_hub.agent_service.app_scope.ceiling", everything)


@pytest.fixture(autouse=True)
def _chat_without_wiki(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Chat reads Topcoat's wiki since 2026-11-12, which gives every chat one tool, also in a test
    hub without any tool node. Most chat tests are about a Chat without tools (a forced check is
    refused, the prompt has no tool paragraph); they keep that world. A test about the wiki in
    Chat marks itself with ``@pytest.mark.chat_wiki``."""
    if request.node.get_closest_marker("chat_wiki"):
        return
    monkeypatch.setattr(wiki_tools, "APPLICATIONS", ("project", "admin"))
