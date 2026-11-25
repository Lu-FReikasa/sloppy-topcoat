# ruff: noqa: F811  (pytest fixtures imported from another test module)
"""What happens around the core without a person asking (E3-06): the mirror of the infrastructure
profiles, what a new chat reads of the latest chats (D100) and the care run's review of the
core — everything a model or a file may suggest, and nothing it may write."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH
from tests.fakenode import online_node
from tests.test_chat import new_conv, use_model
from tests.test_core import store_call, system_of
from tests.test_knowledge import Docs, add_source, indexed, ready, world  # noqa: F401
from tests.test_memory import chat_once
from topcoat_hub import core_review, memory_care
from topcoat_hub.digest_store import DigestStore
from topcoat_hub.gateway import GatewayError
from topcoat_hub.memory_care_service import plan_for
from topcoat_hub.memory_store import MemoryStore, ProtectedWrite

A_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")

HOST = """# node-amd

active · 2026-09-20 · amd-box, gpu-host · server

- Runs the llama-server for the small models.
prüfen: systemctl status llama
"""
SERVICE = """# plex

active · 2026-08-01 · media · docker

The media server runs as a container on node-nvidia.
"""


def mirrored(client: TestClient) -> list[dict[str, Any]]:
    items = client.get("/api/memory", headers=AUTH, params={"status": "active"}).json()["items"]
    return [i for i in items if i["source_type"] == "kb"]


def all_kb_rows(db_url: str) -> list[Any]:
    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, text, status, version, verified_count, last_verified_at, source_ref, "
                "verify_with FROM memory_items WHERE source_type = 'kb' ORDER BY text"
            )
        ).all()
    engine.dispose()
    return rows


def reindex(client: TestClient, source_id: str, docs: Docs) -> None:
    docs.mtime += 10  # a changed mtime makes the hub look again
    client.post(f"/api/knowledge/sources/{source_id}/reindex", headers=AUTH)
    ready(client, source_id)


# --- the mirror -------------------------------------------------------------------------------


def test_a_steckbrief_becomes_a_read_only_operational_note_with_the_header_of_its_file(
    client: TestClient, world: dict[str, Any], clean_db: str
) -> None:
    docs: Docs = world["docs"]
    docs.files["steckbriefe/node-amd.md"] = HOST
    docs.files["steckbriefe/plex.md"] = SERVICE
    docs.files["servers/other.md"] = HOST  # not in the folder: indexed, not mirrored
    indexed(client, world)
    rows = all_kb_rows(clean_db)
    assert len(rows) == 2
    host = next(r for r in rows if "node-amd" in r.text)
    assert host.status == "active" and host.verified_count == 0  # a file's date is no live check
    assert host.last_verified_at == datetime(2026, 9, 20, tzinfo=UTC)
    assert (
        host.source_ref["path"] == "steckbriefe/node-amd.md"
        and len(host.source_ref["sha256"]) == 64
    )
    assert host.source_ref["verified"].startswith("2026-09-20")
    assert host.source_ref["check_hint"] == "systemctl status llama"  # kept as a hint ...
    assert host.verify_with is None  # ... never turned into a way to check
    assert "amd-box" in host.text and "llama-server" in host.text
    items = mirrored(client)
    assert {i["kind"] for i in items} == {"operational"}


def test_the_mirror_is_idempotent_follows_the_file_and_archives_what_vanished(
    client: TestClient, world: dict[str, Any], clean_db: str
) -> None:
    docs: Docs = world["docs"]
    docs.files["steckbriefe/node-amd.md"] = HOST
    source = indexed(client, world)
    [first] = all_kb_rows(clean_db)
    reindex(client, source["id"], docs)  # nothing changed: nothing happens
    [again] = all_kb_rows(clean_db)
    assert (again.id, again.version) == (first.id, first.version)
    # the file changes: the same note, a new version, no duplicate
    docs.files["steckbriefe/node-amd.md"] = HOST.replace("active", "degraded").replace(
        "2026-09-20", "2026-09-28"
    )
    reindex(client, source["id"], docs)
    [changed] = all_kb_rows(clean_db)
    assert changed.id == first.id and changed.version == first.version + 1
    assert (
        "degraded" in changed.text and changed.last_verified_at.date().isoformat() == "2026-09-28"
    )
    assert changed.source_ref["sha256"] != first.source_ref["sha256"]
    # the file is gone: archived (searchable, restorable), not deleted
    del docs.files["steckbriefe/node-amd.md"]
    reindex(client, source["id"], docs)
    [gone] = all_kb_rows(clean_db)
    assert gone.status == "archived" and gone.id == first.id
    # the file is back, unchanged since: the same note wakes up again
    docs.files["steckbriefe/node-amd.md"] = HOST.replace("active", "degraded").replace(
        "2026-09-20", "2026-09-28"
    )
    reindex(client, source["id"], docs)
    [back] = all_kb_rows(clean_db)
    assert back.status == "active" and back.id == first.id and len(all_kb_rows(clean_db)) == 1


def test_a_mirrored_note_is_read_only_for_the_assistant_and_its_check_is_set_by_the_care_only(
    client: TestClient, world: dict[str, Any], clean_db: str
) -> None:
    docs: Docs = world["docs"]
    docs.files["steckbriefe/node-amd.md"] = HOST
    indexed(client, world)
    [row] = all_kb_rows(clean_db)
    item_id = row.id

    async def attempt(store: MemoryStore) -> None:
        for actor in ("chat", "care", "consolidation"):
            with pytest.raises(ProtectedWrite):
                await store.change(item_id, reason="x", actor=actor, text_="node-amd is gone")
            with pytest.raises(ProtectedWrite):
                await store.change(item_id, reason="x", actor=actor, status="archived")
        for actor in ("chat", "user", "consolidation"):  # only the care run sets how to check it
            with pytest.raises(ProtectedWrite):
                await store.change(
                    item_id, reason="x", actor=actor,
                    verify_with={"capability": "system.service_status", "input": {}},
                )  # fmt: skip
        # the care run may, after a confirmed check — and that changes no content
        changed = await store.change(
            item_id, reason="care: confirmed", actor="care",
            verify_with={"capability": "system.service_status", "input": {"unit": "llama"}},
        )  # fmt: skip
        assert changed is not None and changed.text == row.text
        assert [i.id for i in await store.unchecked()] == []  # the consolidation never touches it
        assert item_id not in [i.id for i in await store.active()]  # (nor does it see it)
        assert item_id in [i.id for i in await store.active(mirror=True)]

    store_call(client, A_ID, attempt)


def test_a_secret_in_a_steckbrief_is_not_mirrored(
    client: TestClient, world: dict[str, Any], clean_db: str
) -> None:
    docs: Docs = world["docs"]
    docs.files["steckbriefe/nas.md"] = (
        "# nas\n\nactive · 2026-09-01 · storage · server\n\nAdmin Passwort ist Sommer2024xyz.\n"
    )
    docs.files["steckbriefe/plex.md"] = SERVICE
    indexed(client, world)
    texts = [r.text for r in all_kb_rows(clean_db)]
    assert len(texts) == 1 and "plex" in texts[0]
    assert all("Sommer2024" not in t for t in texts)


# --- the care run -----------------------------------------------------------------------------


def test_the_care_never_archives_rewrites_or_distrusts_a_mirrored_note() -> None:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    from types import SimpleNamespace

    def kb(**kw: Any) -> SimpleNamespace:
        base: dict[str, Any] = {
            "id": uuid.uuid4(), "kind": "operational", "source_type": "kb", "status": "active",
            "text": "node-amd (server)", "evidence": "doc", "trust": 0.2, "confirmed_count": 0,
            "verified_count": 0, "recalled_count": 0, "used_count": 0, "feedback_positive": 0,
            "feedback_negative": 0, "created_at": now - timedelta(days=400),
            "last_confirmed_at": None, "last_verified_at": None, "last_used_at": None,
            "expires_at": None, "verify_with": None, "care_checked_at": None,
        }  # fmt: skip
        return SimpleNamespace(**{**base, **kw})

    cfg = memory_care.DEFAULT
    plain = kb()
    plan = plan_for([plain], [], now, cfg)
    assert plan.archive == [] and plan.unverifiable == [] and plan.checks == []
    # the same note as a user's fact would be archived or distrusted: the file decides, not the care
    user = kb(source_type="chat", recalled_count=20)
    assert plan_for([user], [], now, cfg).archive == [user]
    # a note with a way to check (set by the care itself earlier) is checked like any other
    checkable = kb(verify_with={"capability": "system.info", "input": {}}, trust=1.0)
    assert plan_for([checkable], [], now, cfg).checks == [checkable]


def test_a_live_check_that_contradicts_a_steckbrief_becomes_a_proposal_to_edit_the_file(
    client: TestClient, world: dict[str, Any], clean_db: str
) -> None:
    docs: Docs = world["docs"]
    docs.files["steckbriefe/node-amd.md"] = HOST
    indexed(client, world)
    [row] = all_kb_rows(clean_db)
    care = client.app.state.memory_care

    async def differs() -> str:
        async with care.sessionmaker() as db:
            item = await MemoryStore(db, A_ID).get(row.id)
        stats: dict[str, Any] = {"changed": 0, "asked": 0, "proposed": 0, "changes": []}
        verdict = {"verdict": "changed", "text": "node-amd: llama-server is stopped",
                   "evidence": ["E1"], "reason": "the unit is inactive"}  # fmt: skip
        outcome = await care._differs(A_ID, item, verdict, ["E1"], {"care": {}}, stats)
        assert stats["proposed"] == 1 and stats["changed"] == 0
        assert stats["changes"][0]["action"] == "proposed"
        return outcome

    assert client.portal.call(differs) == "asked"
    [after] = all_kb_rows(clean_db)
    assert after.text == row.text and after.version == row.version  # the note did not change
    [proposal] = client.get("/api/core/proposals", headers=AUTH).json()
    assert proposal["action"] == "kb_edit" and proposal["origin"] == "care"
    assert "llama-server is stopped" in proposal["evidence"]["markdown"]  # Markdown to copy
    assert proposal["evidence"]["path"] == "steckbriefe/node-amd.md"
    # accepting a file edit writes no file: it just closes the proposal (the user copies the text)
    r = client.post(f"/api/core/proposals/{proposal['id']}/accept", headers=AUTH)
    assert r.status_code == 200 and r.json()["item"] is None
    assert all_kb_rows(clean_db)[0].text == row.text


class FakeGateway:
    """The model of the review: answers with a fixed JSON, remembers what it was asked."""

    def __init__(self, answer: dict[str, Any] | None = None, fail: bool = False) -> None:
        self.answer, self.fail, self.calls = answer or {"conflicts": []}, fail, 0

    async def call(
        self, model: str, path: str, payload: dict[str, Any], label: str = ""
    ) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise GatewayError(503, "busy", "busy")
        return {"choices": [{"message": {"content": json.dumps(self.answer)}}]}


def add_rule(client: TestClient, text_: str, kind: str = "rule") -> dict[str, Any]:
    r = client.post("/api/core/items", headers=AUTH, json={"kind": kind, "text": text_})
    assert r.status_code == 201, r.text
    return r.json()


def test_the_review_of_the_core_proposes_it_never_changes_and_looks_again_only_when_it_changed(
    client: TestClient,
) -> None:
    a = add_rule(client, "Sprich mich immer mit du an.", "style")
    b = add_rule(client, "Sprich mich immer mit Sie an.", "style")
    care = client.app.state.memory_care
    gateway = FakeGateway({"conflicts": [{"a": 1, "b": 2, "drop": "b", "reason": "du or Sie"}]})
    care.review = core_review.CoreReview(gateway, care.cfg)  # type: ignore[arg-type]
    now = memory_care.utcnow()

    async def review(last: str | None) -> tuple[list[dict[str, Any]], str]:
        async with care.sessionmaker() as db:
            out = await care.review.review(MemoryStore(db, A_ID), "small", last, now)
            await db.commit()
            return out

    lines, fp = client.portal.call(review, None)
    assert gateway.calls == 1 and len(lines) == 1 and lines[0]["action"] == "proposed"
    proposals = client.get("/api/core/proposals", headers=AUTH).json()
    # the legacy language / address notes of the settings are in the core too: a contradiction
    # is a proposal to remove one of the two, aimed at the one the model named
    [p] = [x for x in proposals if x["action"] == "remove"]
    assert p["origin"] == "care" and p["evidence"]["review"] is True
    assert p["target_id"] in (a["id"], b["id"]) and "contradicts" in p["reason"]
    for item in (a, b):  # nothing was changed behind the user's back
        assert client.get(f"/api/memory/{item['id']}", headers=AUTH).json()["version"] == 1
    # the same core: no new model call, no second proposal
    client.portal.call(review, fp)
    assert gateway.calls == 1 and len(client.get("/api/core/proposals", headers=AUTH).json()) == 1
    # a changed core is looked at again
    client.patch(
        f"/api/core/items/{a['id']}", headers=AUTH, json={"text": "Sprich mich mit du an."}
    )
    client.portal.call(review, fp)
    assert gateway.calls == 2


def test_a_core_fact_that_is_given_often_and_never_used_is_proposed_for_unpinning(
    client: TestClient,
) -> None:
    item = store_call(
        client,
        A_ID,
        lambda s: s.add(
            "Mag Espresso", embedding=None, embedder=None, evidence="doc", confidence=0.9,
            source_type="chat", source_ref={}, reason="said so", actor="chat", kind="preference",
        ),
    )  # fmt: skip
    r = client.patch(
        f"/api/core/items/{item.id}", headers=AUTH, json={"core": True}
    )  # the user pins it
    assert r.status_code == 200 and r.json()["core"] is True
    engine = create_engine(client.app.state.settings.database_url)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE memory_items SET recalled_count = 12, used_count = 0 WHERE id = :i"),
            {"i": str(item.id)},
        )
    engine.dispose()
    care = client.app.state.memory_care
    care.review = core_review.CoreReview(FakeGateway(fail=True), care.cfg)  # type: ignore[arg-type]
    now = memory_care.utcnow()

    async def review() -> list[dict[str, Any]]:
        async with care.sessionmaker() as db:
            lines, _ = await care.review.review(MemoryStore(db, A_ID), None, None, now)
            await db.commit()
            return lines

    assert len(client.portal.call(review)) == 1  # no model needed for the counters
    [p] = [
        x for x in client.get("/api/core/proposals", headers=AUTH).json() if x["action"] == "unpin"
    ]
    assert p["target_id"] == str(item.id) and "never used" in p["reason"]
    assert client.get(f"/api/memory/{item.id}", headers=AUTH).json()["version"] == 2  # only the pin
    r = client.post(f"/api/core/proposals/{p['id']}/accept", headers=AUTH)  # one click
    assert r.status_code == 200 and r.json()["item"]["core"] is False


def test_a_care_run_with_only_a_changed_core_has_work_and_records_the_review(
    client: TestClient, connect: Any
) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    add_rule(client, "Antworte immer ausführlich.")
    add_rule(client, "Antworte immer ganz knapp.")
    care = client.app.state.memory_care
    gateway = FakeGateway()
    care.review = core_review.CoreReview(gateway, care.cfg)  # type: ignore[arg-type]
    run_id = client.portal.call(care.run, A_ID, "manual", True)
    run = client.get("/api/memory", headers=AUTH).json()["care"]["runs"][0]
    assert run_id is not None and gateway.calls == 1
    assert run["status"] == "ok" and run["stats"]["core_review"]
    # nothing changed since: a nightly run has nothing to do
    assert client.portal.call(care.run, A_ID, "nightly", True) is None and gateway.calls == 1


# --- what a new chat reads of the latest chats (D100) -------------------------------------------


class QuietModel:
    """Answers a chat; the extraction after it finds nothing to note."""

    def __call__(self, req: dict[str, Any]) -> str | None:
        if req.get("stream"):
            return None
        return json.dumps({"memories": [], "used": [], "instructions": []})


@pytest.fixture
def recent(client: TestClient, connect: Any) -> dict[str, Any]:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.answer_fn = QuietModel()
    return {"node": node}


def put_digest(
    client: TestClient,
    conv: str,
    statements: list[str],
    *,
    verified: bool = True,
    when: datetime | None = None,
) -> None:
    """The running summary of a chat, as the digester leaves it."""
    refs = [
        {"text": t, "verified": verified, "note": None if verified else "quote_not_found"}
        for t in statements
    ]

    async def go() -> None:
        async with client.app.state.sessionmaker() as db:
            await DigestStore(db, A_ID).put(
                uuid.UUID(conv), 99, " ".join(statements), ("small", "node-a"),
                when or memory_care.utcnow(), refs,
            )  # fmt: skip
            await db.commit()

    client.portal.call(go)


BACKUP = ["Backup-Job: läuft nachts um 3 Uhr", "Aufbewahrung: noch offen, 7 oder 14 Tage"]


def test_the_key_terms_of_the_latest_chat_stand_in_the_core_of_the_next_one(
    client: TestClient, recent: dict[str, Any]
) -> None:
    node = recent["node"]
    first = new_conv(client)
    chat_once(client, "Wie sichere ich die Datenbank?", first)
    put_digest(client, first, BACKUP)
    chat_once(client, "Noch eine ganz andere Frage", None)
    system = system_of(node)
    assert "## Discussed last" in system
    assert "Backup-Job: läuft nachts um 3 Uhr; Aufbewahrung: noch offen, 7 oder 14 Tage" in system
    # it is a line of key terms, not the summary as running text, and it names no note: nothing
    # on the notes page, nothing for the recall to find
    assert client.get("/api/memory", headers=AUTH).json()["items"] == []


def test_a_chat_does_not_read_itself_and_unchecked_statements_are_not_read_at_all(
    client: TestClient, recent: dict[str, Any]
) -> None:
    node = recent["node"]
    own = new_conv(client)
    put_digest(client, own, BACKUP)
    chat_once(client, "Wie sichere ich die Datenbank?", own)
    assert "Backup-Job" not in system_of(node)  # the block of a chat never names the chat itself
    other = new_conv(client)
    put_digest(client, other, ["Erfunden: nicht belegt"], verified=False)
    chat_once(client, "Noch eine ganz andere Frage", None)
    assert "Erfunden" not in system_of(node)


def test_only_the_chats_of_the_same_program_and_of_the_last_days_are_read(
    client: TestClient, recent: dict[str, Any]
) -> None:
    node = recent["node"]
    dev = client.post("/api/conversations", headers=AUTH, json={"application": "dev"}).json()["id"]
    put_digest(client, dev, ["Repo: Branch topcoat/x angelegt"])
    old = new_conv(client)
    put_digest(
        client, old, ["Alt: vor Wochen besprochen"], when=memory_care.utcnow() - timedelta(days=30)
    )
    chat_once(client, "Noch eine ganz andere Frage", None)
    system = system_of(node)
    assert "Repo: Branch" not in system  # a program's memory is its own (D74)
    assert "Alt: vor Wochen" not in system  # older than core_recent_days


def test_without_memory_a_chat_reads_nothing_of_the_others(
    client: TestClient, recent: dict[str, Any]
) -> None:
    node = recent["node"]
    first = new_conv(client)
    put_digest(client, first, BACKUP)
    client.put("/api/chat/settings", headers=AUTH, json={"memory_enabled": False})
    chat_once(client, "Noch eine ganz andere Frage", None)
    assert "Backup-Job" not in system_of(node)
