"""Memory care (E3-05): the care run with fake nodes and a fake clock — expiry, archive and
restore, no live checks and no lowered trust (D124), isolation between users, the question rule in
the chat and the counting of recalls and uses."""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import FakeNode, eventually, online_node
from tests.test_agent import use
from tests.test_chat import ask, new_conv, sent_messages, use_model
from tests.test_tools import set_config, tools_node
from tests.users import make_user
from topcoat_hub import chat, memory_care
from topcoat_hub.memory import EXTRACTION_SYSTEM
from topcoat_hub.models import DEV_ADMIN_ID, MemoryItem, MemoryVersion

PS = {"capability": "docker.ps", "input": {}}


class Clock:
    """The fake clock of the care: starts at the real now (the database stamps real times)."""

    def __init__(self) -> None:
        self.at = datetime.now(UTC)

    def now(self) -> datetime:
        return self.at

    def advance(self, days: float) -> None:
        self.at += timedelta(days=days)

    def ago(self, days: float) -> datetime:
        return self.at - timedelta(days=days)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(memory_care, "utcnow", c.now)
    return c


def seed(clock: Clock, text_: str, user: uuid.UUID = DEV_ADMIN_ID, **kw: Any) -> str:
    """A note exactly as a test needs it (dates relative to the fake clock)."""
    age = kw.pop("age", 0)
    fields: dict[str, Any] = {
        "status": "active",
        "confidence": 0.9,
        "evidence": "doc",
        "source_type": "chat",
        "source_ref": {},
        "kind": "operational",
        "created_at": clock.ago(age),
        "updated_at": clock.ago(age),
    }
    fields.update(kw)
    engine = create_engine(TEST_DB_URL)
    with Session(engine) as s:
        item = MemoryItem(user_id=user, text=text_, version=1, **fields)
        s.add(item)
        s.flush()
        s.add(
            MemoryVersion(
                item_id=item.id,
                user_id=user,
                version=1,
                text=text_,
                status=fields["status"],
                evidence=fields["evidence"],
                confidence=0.9,
                reason="seed",
                actor="user",
                source={},
            )
        )
        s.commit()
        item_id = str(item.id)
    engine.dispose()
    return item_id


def row(item_id: str) -> dict[str, Any]:
    engine = create_engine(TEST_DB_URL)
    with Session(engine) as s:
        item = s.scalar(select(MemoryItem).where(MemoryItem.id == uuid.UUID(item_id)))
        assert item is not None
        out = {c.name: getattr(item, c.name) for c in MemoryItem.__table__.columns}
    engine.dispose()
    return out


def sql(query: str, **params: Any) -> list[Any]:
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        rows = conn.execute(text(query), params).all()
    engine.dispose()
    return rows


@pytest.fixture
def world(client: TestClient, connect: Callable[..., FakeNode]) -> tuple[FakeNode, FakeNode]:
    _, tnode = tools_node(client, connect, "vm-docker")
    mnode = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    return tnode, mnode


def run_care(client: TestClient, user: uuid.UUID = DEV_ADMIN_ID, trigger: str = "manual") -> Any:
    care = client.app.state.memory_care
    return client.portal.call(care.run, user, trigger)


def last_run(client: TestClient, who: dict[str, str] = AUTH) -> dict[str, Any]:
    runs = client.get("/api/memory", headers=who).json()["care"]["runs"]
    assert runs, "no care run was recorded"
    return runs[0]


def change(run: dict[str, Any], action: str) -> dict[str, Any]:
    found = [c for c in run["stats"]["changes"] if c["action"] == action]
    assert found, (action, run["stats"]["changes"])
    return found[0]


# --- no live checks, no lowered trust (D124) ---------------------------------------------------


def test_a_care_run_checks_no_note_and_lowers_no_trust(
    client: TestClient, world, clock: Clock
) -> None:
    checkable = seed(clock, "Docker läuft auf vm-docker", age=40, verify_with=PS)
    blind = seed(clock, "Der lil switch ist absichtlich aus", age=40)
    model = use(client)
    run_care(client)
    assert model.calls == [] and _tasks() == []
    for item in (checkable, blind):
        after = row(item)
        assert after["trust"] == 1.0 and after["verified_count"] == 0 and after["version"] == 1
        assert after["care_checked_at"] is None
    run = last_run(client)
    assert run["status"] == "ok" and run["stats"]["changes"] == []


def test_a_mirrored_profile_with_a_check_hint_is_not_checked_and_gets_no_file_edit_proposal(
    client: TestClient, world, clock: Clock, connect: Callable[..., FakeNode]
) -> None:
    # the profile's host has a connected tool node with the read-only shell: still no check
    node_id, _ = tools_node(client, connect, "tools-alpha", with_shell=True)
    set_config(client, node_id, lambda c: c["capabilities"].append("shell.read"))
    item = seed(
        clock,
        "alpha runs the containers",
        age=40,
        source_type="kb",
        source_ref={"path": "steckbriefe/alpha.md", "host": "alpha", "check_hint": "docker ps"},
    )
    model = use(client)
    run_care(client)
    assert model.calls == [] and _tasks() == []
    assert row(item)["trust"] == 1.0 and row(item)["version"] == 1
    assert client.get("/api/core/proposals", headers=AUTH).json() == []


# --- expiry and archive -----------------------------------------------------------------------


def test_a_volatile_note_is_struck_after_its_expiry_with_the_reason_and_can_be_restored(
    client: TestClient, world, clock: Clock
) -> None:
    item = seed(clock, "Dienst X ist down", age=0, expires_at=clock.now() + timedelta(hours=24))
    run_care(client)
    assert row(item)["status"] == "active"  # not yet
    clock.advance(1.01)
    run_care(client)
    after = row(item)
    assert after["status"] == "deleted"
    entry = change(last_run(client), "expired")
    assert entry["undo"] == "restore" and "volatile state" in entry["reason"]
    versions = client.get(f"/api/memory/{item}", headers=AUTH).json()["versions"]
    assert versions[0]["actor"] == "care" and "expired" in versions[0]["reason"]
    assert client.post(f"/api/memory/{item}/restore", headers=AUTH).status_code == 200
    assert row(item)["status"] == "active"


def test_striking_a_note_leaves_other_open_questions_alone(
    client: TestClient, world, clock: Clock
) -> None:
    keeper = seed(clock, "Mag Tee", kind="person")
    other = seed(clock, "Mag Kaffee", kind="person")
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO memory_questions (id, user_id, item_ids, topic, origin, status, "
                "context) VALUES (gen_random_uuid(), :u, CAST(:ids AS jsonb), '', 'chat', "
                "'open', '{}')"
            ),
            {"u": str(DEV_ADMIN_ID), "ids": json.dumps([keeper, other])},
        )
    engine.dispose()
    seed(clock, "Dienst X ist down", expires_at=clock.now() - timedelta(hours=1))
    run_care(client)
    assert len(client.get("/api/memory/questions", headers=AUTH).json()) == 1


def test_a_useless_note_sinks_in_the_ranking_is_archived_after_the_period_and_comes_back(
    client: TestClient, world, clock: Clock
) -> None:
    useful = seed(clock, "Backup läuft nachts um drei", age=3, kind="operational")
    useless = seed(
        clock, "Backup Ziel ist das alte NAS", age=200, kind="operational", recalled_count=25
    )
    memory = client.app.state.memory
    hits = client.portal.call(memory.recall, DEV_ADMIN_ID, "Backup", 2000)
    assert [str(h.id) for h in hits][:2] == [useful, useless]  # it sinks
    # not yet: it was used (never) but the period is not over, and age alone does not count
    assert memory_care.archive_due(_as_note(useless), clock.now()) is True  # 200 d idle
    run_care(client)
    assert row(useless)["status"] == "archived" and row(useful)["status"] == "active"
    entry = change(last_run(client), "archived")
    assert entry["undo"] == "restore" and "not used for 200 days" in entry["reason"]
    listed = client.get("/api/memory", headers=AUTH).json()["items"]
    assert [i["id"] for i in listed] == [useful]  # gone from the normal list ...
    archived = client.get("/api/memory?status=archived", headers=AUTH).json()["items"]
    assert [i["id"] for i in archived] == [useless]  # ... visible in the archive
    found = client.get("/api/memory?q=NAS", headers=AUTH).json()["items"]
    assert [i["id"] for i in found] == [useless]  # and searchable
    hits = client.portal.call(memory.recall, DEV_ADMIN_ID, "Backup", 2000)
    assert [str(h.id) for h in hits] == [useful]  # no longer recalled
    assert client.post(f"/api/memory/{useless}/restore", headers=AUTH).status_code == 200
    assert row(useless)["status"] == "active"


def test_an_old_but_often_used_note_stays(client: TestClient, world, clock: Clock) -> None:
    item = seed(
        clock,
        "Docker läuft auf vm-docker",
        age=400,
        recalled_count=45,
        used_count=40,
        last_used_at=clock.ago(3),
    )
    run_care(client)
    assert last_run(client)["stats"]["archived"] == 0 and row(item)["status"] == "active"


def test_age_alone_never_archives_even_a_very_old_note(
    client: TestClient, world, clock: Clock
) -> None:
    item = seed(clock, "Hat einen Hund", age=4000, kind="person")
    run_care(client)
    assert row(item)["status"] == "active"


# --- isolation (D20) --------------------------------------------------------------------------


def test_a_care_run_only_touches_the_notes_of_its_own_user(
    client: TestClient, world, clock: Clock, clean_db: str
) -> None:
    b_id, b_auth = make_user(clean_db, "bea")
    a_gone = seed(clock, "Dienst X ist down", expires_at=clock.ago(1))
    b_gone = seed(clock, "Dienst Y ist down", user=b_id, expires_at=clock.ago(1))
    run_care(client)  # for the dev admin only
    assert row(a_gone)["status"] == "deleted"
    assert row(b_gone)["status"] == "active"
    assert client.get("/api/memory", headers=b_auth).json()["care"]["runs"] == []
    # B's own run strikes B's note and leaves A's alone
    run_care(client, b_id)
    assert row(b_gone)["status"] == "deleted"
    assert [
        r["stats"]["expired"]
        for r in client.get("/api/memory", headers=b_auth).json()["care"]["runs"]
    ] == [1]
    assert len(client.get("/api/memory", headers=AUTH).json()["care"]["runs"]) == 1


def test_feedback_counts_for_the_owner_only(
    client: TestClient, clock: Clock, clean_db: str
) -> None:
    _, b_auth = make_user(clean_db, "bea")
    item = seed(clock, "Mag Jazz", kind="person")
    r = client.post(f"/api/memory/{item}/feedback", headers=AUTH, json={"helped": False})
    assert r.status_code == 200 and r.json()["feedback_negative"] == 1
    assert (
        client.post(
            f"/api/memory/{item}/feedback", headers=b_auth, json={"helped": True}
        ).status_code
        == 404
    )
    assert row(item)["feedback_positive"] == 0
    one = client.get(f"/api/memory/{item}", headers=AUTH).json()["usefulness"]
    client.post(f"/api/memory/{item}/feedback", headers=AUTH, json={"helped": False})
    assert client.get(f"/api/memory/{item}", headers=AUTH).json()["usefulness"] < one
    # feedback deletes nothing
    assert row(item)["status"] == "active"
    r = client.post(f"/api/memory/{item}/feedback", headers=AUTH, json={"helped": True})
    assert r.json()["feedback_positive"] == 1


def test_a_user_can_archive_and_a_second_user_cannot_see_it(
    client: TestClient, clock: Clock, clean_db: str
) -> None:
    _, b_auth = make_user(clean_db, "bea")
    item = seed(clock, "Mag Jazz", kind="person")
    assert client.post(f"/api/memory/{item}/archive", headers=b_auth).status_code == 404
    assert client.post(f"/api/memory/{item}/archive", headers=AUTH).json()["status"] == "archived"
    assert client.get("/api/memory?status=archived", headers=b_auth).json()["items"] == []


def test_the_care_can_be_started_by_hand_and_says_why_it_cannot(
    client: TestClient, clock: Clock
) -> None:
    r = client.post("/api/memory/care", headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_node_available"


# --- when the care runs -----------------------------------------------------------------------


def test_the_scheduler_runs_once_per_night_and_only_when_there_is_something_to_do(
    client: TestClient, world, clock: Clock
) -> None:
    care = client.app.state.memory_care
    client.app.state.settings.memory_care_hour = clock.now().astimezone().hour
    seed(clock, "Dienst X ist down", expires_at=clock.ago(1))
    assert client.portal.call(care.tick) == 1  # the first idle moment after the care hour
    assert last_run(client)["trigger"] == "nightly"
    assert client.portal.call(care.tick) == 0  # the same night: not again
    clock.advance(1)
    assert client.portal.call(care.tick) == 0  # a new night, but nothing to do: no run, no row
    assert len(client.get("/api/memory", headers=AUTH).json()["care"]["runs"]) == 1
    seed(clock, "Dienst Y ist down", expires_at=clock.ago(1))  # something to do again
    assert client.portal.call(care.tick) == 1
    assert len(client.get("/api/memory", headers=AUTH).json()["care"]["runs"]) == 2


def test_a_hub_that_is_not_idle_does_not_start_a_care_run(
    client: TestClient, world, clock: Clock
) -> None:
    care = client.app.state.memory_care
    seed(clock, "Dienst X ist down", expires_at=clock.ago(1))
    client.app.state.memory.settings.memory_idle_seconds = 120  # the optional quiet time is on
    client.app.state.memory.activity.begin()  # somebody is chatting
    assert client.portal.call(care.tick) == 0


def test_without_a_quiet_time_a_care_run_starts_and_the_node_queue_puts_chat_first(
    client: TestClient, world, clock: Clock
) -> None:
    care = client.app.state.memory_care
    seed(clock, "Dienst X ist down", expires_at=clock.ago(1))
    assert client.app.state.memory.settings.memory_idle_seconds == 0  # the default
    client.app.state.memory.activity.begin()  # somebody is chatting: the queue sorts it out
    assert client.portal.call(care.tick) == 1


# --- the chat: stages, one question per conversation, recalls and uses ------------------------


def notes_in(node: FakeNode) -> str:
    """The notes block of the newest request: it sits in the final user message."""
    return sent_messages(node)[-1]["content"]


def test_the_model_sees_stages_and_asks_about_an_outdated_person_note_once_per_conversation(
    client: TestClient, world, clock: Clock
) -> None:
    _, mnode = world
    seed(clock, "Wohnt in Hamburg", age=3000, kind="person")
    seed(clock, "Der Container web braucht viel Speicher", age=90, kind="operational")
    conv = new_conv(client)
    status, _, _ = ask(client, conv, "Wo wohne ich eigentlich, Hamburg oder Berlin?")
    assert status == 200
    first = notes_in(mnode)
    assert "Wohnt in Hamburg" in first and "ask once, in passing" in first
    assert not any(ch.isdigit() for ch in first.split(chat.NOTES_HEADER, 1)[1][:300])
    ask(client, conv, "Und das Wetter in Hamburg im Winter?")
    second = notes_in(mnode)
    assert "Wohnt in Hamburg" in second and "ask once, in passing" not in second
    # only the first answer carries the permission to ask; the refs say so
    refs = [
        m.get("memory_refs") or []
        for m in client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
        if m["role"] == "assistant"
    ]
    assert [bool(r and r[0].get("ask")) for r in refs] == [True, False]
    # a new conversation may ask again
    ask(client, new_conv(client), "Wo wohne ich eigentlich, Hamburg oder Berlin?")
    assert "ask once, in passing" in notes_in(mnode)


def test_an_outdated_operational_note_is_flagged_not_asked_about(
    client: TestClient, world, clock: Clock
) -> None:
    _, mnode = world
    seed(clock, "Der Container web braucht viel Speicher", age=90, kind="operational")
    ask(client, new_conv(client), "Wie viel Speicher braucht der Container web?")
    prompt = notes_in(mnode)
    assert "possibly outdated, say that it should be checked" in prompt
    assert "ask once" not in prompt


def _extraction(used: list[int]) -> Callable[[dict[str, Any]], str | None]:
    def answer(req: dict[str, Any]) -> str | None:
        if req.get("stream") or req["messages"][0]["content"] != EXTRACTION_SYSTEM:
            return None
        return json.dumps({"memories": [], "used": used})

    return answer


def test_recalls_and_uses_are_counted_and_the_model_alone_does_not_decide(
    client: TestClient, world, clock: Clock
) -> None:
    _, mnode = world
    item = seed(clock, "Wohnt in Hamburg", kind="person")
    mnode.answer_fn = _extraction([1])
    ask(client, new_conv(client), "Wo wohne ich? Hamburg?")  # the (echo) answer shares the terms
    eventually(lambda: _assert(row(item)["recalled_count"] == 1 and row(item)["used_count"] == 1))
    assert row(item)["last_used_at"] is not None
    before = row(item)["used_count"]
    # a model that names no note: the overlap alone is not enough below the high bar
    mnode.answer_fn = _extraction([])
    ask(client, new_conv(client), "Und was ist mit Hamburg im Winter?")
    eventually(lambda: _assert(row(item)["recalled_count"] == 2))
    assert row(item)["used_count"] == before


# --- helpers ----------------------------------------------------------------------------------


def _assert(ok: bool) -> None:
    assert ok


def _as_note(item_id: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(**row(item_id))


def _tasks() -> list[Any]:
    from topcoat_hub.models import AgentTask

    engine = create_engine(TEST_DB_URL)
    with Session(engine) as s:
        rows = list(s.scalars(select(AgentTask)))
        s.expunge_all()
    engine.dispose()
    return rows


# --- migration 0022 -----------------------------------------------------------------


def test_migration_0022_keeps_existing_notes_and_goes_back_down_without_losing_them(
    clean_db: str,
) -> None:
    """Up on a database with notes: they become ``person`` notes that were never recalled and
    keep their dates. Down: archived notes become deleted (restorable), the columns go."""
    from alembic import command

    from tests.conftest import alembic_config

    cfg = alembic_config()
    command.downgrade(cfg, "0021")
    admin = str(DEV_ADMIN_ID)
    old = str(uuid.uuid4())
    engine = create_engine(TEST_DB_URL)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO memory_items (id, user_id, text, status, confidence, evidence, "
                    "source_type, source_ref, version, confirmed_count, created_at) VALUES "
                    "(:i, :u, 'Wohnt in Hamburg', 'active', 0.9, 'doc', 'chat', '{}', 1, 2, "
                    "'2025-01-01T00:00:00Z')"
                ),
                {"i": old, "u": admin},
            )
        command.upgrade(cfg, "0022")
        with engine.begin() as conn:
            item = conn.execute(
                text(
                    "SELECT kind, verified_count, trust, recalled_count, used_count, expires_at, "
                    "verify_with, confirmed_count, created_at FROM memory_items WHERE id = :i"
                ),
                {"i": old},
            ).one()
            assert item.kind == "person" and item.verified_count == 0 and item.trust == 1.0
            assert (item.recalled_count, item.used_count) == (0, 0)
            assert item.expires_at is None and item.verify_with is None
            assert item.confirmed_count == 2 and item.created_at.year == 2025
            # the new status and kind are accepted, others are not
            conn.execute(
                text("UPDATE memory_items SET status = 'archived' WHERE id = :i"), {"i": old}
            )
            conn.execute(
                text(
                    "INSERT INTO memory_versions (item_id, user_id, version, text, status, "
                    "evidence, confidence, reason, actor, source) VALUES (:i, :u, 2, 'x', "
                    "'archived', 'doc', 0.9, 'r', 'care', '{}')"
                ),
                {"i": old, "u": admin},
            )
        with pytest.raises(Exception, match="memory_items_kind_check"), engine.begin() as conn:
            conn.execute(
                text("UPDATE memory_items SET kind = 'nonsense' WHERE id = :i"), {"i": old}
            )
        command.downgrade(cfg, "0021")
        with engine.connect() as conn:
            status = conn.execute(
                text("SELECT status FROM memory_items WHERE id = :i"), {"i": old}
            ).scalar()
            versions = [
                r.status
                for r in conn.execute(text("SELECT status FROM memory_versions ORDER BY version"))
            ]
            cols = {
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'memory_items'"
                    )
                )
            }
        assert status == "deleted" and versions[-1] == "deleted"  # struck, not lost
        assert "kind" not in cols and "trust" not in cols and "recalled_count" not in cols
    finally:
        engine.dispose()
        command.upgrade(cfg, "head")


def test_a_run_that_died_with_the_hub_is_interrupted_at_startup(client: TestClient) -> None:
    from sqlalchemy import create_engine, text

    from tests.conftest import AUTH  # noqa: F401
    from topcoat_hub.memory_store import sweep_orphaned_runs

    engine = create_engine(client.app.state.settings.database_url)
    with engine.begin() as conn:
        uid = conn.execute(text("SELECT id FROM users LIMIT 1")).scalar_one()
        conn.execute(
            text(
                "INSERT INTO memory_runs (id, user_id, trigger, kind, status, stats) "
                "VALUES (gen_random_uuid(), :u, 'nightly', 'care', 'running', '{}')"
            ),
            {"u": uid},
        )
    swept = client.portal.call(lambda: sweep_orphaned_runs(client.app.state.sessionmaker))
    with engine.begin() as conn:
        status = (
            conn.execute(text("SELECT status FROM memory_runs WHERE user_id = :u"), {"u": uid})
            .scalars()
            .all()
        )
    engine.dispose()
    assert swept >= 1 and "running" not in status and "interrupted" in status
