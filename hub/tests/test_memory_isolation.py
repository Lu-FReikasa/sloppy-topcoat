"""D20 for the memory: user B's chat can not recall, and B's memory page, API calls and
consolidation runs can not see, change or influence, anything of user A's notes."""

import inspect
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH
from tests.fakenode import FakeNode, eventually, online_node
from tests.test_chat import ask, new_conv, sent_messages, use_model
from tests.test_memory import (
    CONSOLIDATION_PREFIX,
    _user_part,
    memory_items,
    note,
    run_consolidation,
    with_contradicts,
)
from tests.users import make_user
from topcoat_hub.embedding import HashingEmbedder
from topcoat_hub.memory import EXTRACTION_SYSTEM
from topcoat_hub.memory_store import MemoryStore

A_NOTES = ["Wohnt in Hamburg", "Hund heißt Bello", "Steuerberater ist Frau Sommer"]


class Recorder:
    """The fake memory model: extracts A's notes from A's messages, B's own from B's, and
    records every consolidation prompt so a test can see what the model was shown."""

    def __init__(self) -> None:
        self.consolidation_prompts: list[str] = []

    def __call__(self, req: dict[str, Any]) -> str | None:
        if req.get("stream"):
            return None
        system, user = req["messages"][0]["content"], req["messages"][-1]["content"]
        if system == EXTRACTION_SYSTEM:
            prompt, user = user, _user_part(user)
            if "A-MARKER" in user:
                return json.dumps({"memories": [note(t) for t in A_NOTES]})
            if "B-MARKER" in user:
                notes = [note("Isst gern Pizza"), note("Wohnt in Kiel")]
                return json.dumps({"memories": with_contradicts(notes, prompt)})
            return json.dumps({"memories": []})
        if system.startswith(CONSOLIDATION_PREFIX):
            self.consolidation_prompts.append(user)
            return json.dumps({})
        return None


@pytest.fixture
def memory_users(client: TestClient, clean_db: str, connect) -> dict[str, Any]:
    """A (the dev admin) has three notes; B is a normal user with a key and no notes."""
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    recorder = Recorder()
    node.answer_fn = recorder
    conv_a = new_conv(client)
    status, events, _ = ask(client, conv_a, "A-MARKER Ich wohne in Hamburg, mein Hund heißt Bello.")
    assert status == 200 and events[-1][0] == "memory"
    a_items = {i["text"]: i for i in memory_items(client)}
    assert set(a_items) == set(A_NOTES)
    b_id, b = make_user(clean_db, "user-b")
    return {"node": node, "rec": recorder, "conv_a": conv_a, "a": a_items, "b_id": b_id, "b": b}


def test_b_asking_about_a_facts_recalls_nothing(
    client: TestClient, memory_users: dict[str, Any]
) -> None:
    node, b = memory_users["node"], memory_users["b"]
    question = (
        "Wo wohne ich, in Hamburg? Und wie heißt mein Hund, Bello? Wer ist mein Steuerberater?"
    )
    # Control: the same question from A does recall A's notes (the test is not vacuous).
    status, events, _ = ask(client, new_conv(client), question)
    assert status == 200 and {n["text"] for n in events[0][1]["memory"]} >= {
        "Wohnt in Hamburg", "Hund heißt Bello",
    }  # fmt: skip
    assert "Bello" in sent_messages(node)[-1]["content"]

    b_conv = new_conv(client, headers=b)
    status, events, _ = ask(client, b_conv, question, headers=b)
    assert status == 200
    assert events[0][1]["memory"] == []
    prompt = "\n".join(m["content"] for m in sent_messages(node))
    for fact in ("Hamburg" + " ist", "Bello" + " ", "Sommer", "Your notes about the user"):
        assert fact not in prompt.replace(question, "")
    done = next(d for e, d in events if e == "done")["message"]
    assert done["memory_refs"] == []
    assert memory_items(client, headers=b) == []
    body = client.get("/api/memory", headers=b).json()
    assert body["counts"] == {
        "active": 0,
        "superseded": 0,
        "disputed": 0,
        "deleted": 0,
        "archived": 0,
    }
    assert body["runs"] == [] and body["blocked"] == 0


def test_b_cannot_read_change_or_delete_a_notes(
    client: TestClient, memory_users: dict[str, Any]
) -> None:
    b = memory_users["b"]
    item = memory_users["a"]["Hund heißt Bello"]
    url = f"/api/memory/{item['id']}"
    body = {"text": "gehackt"}
    assert client.get(url, headers=b).status_code == 404
    assert client.patch(url, headers=b, json=body).status_code == 404
    assert client.post(f"{url}/revert", headers=b, json={"version": 1}).status_code == 404
    assert client.delete(url, headers=b).status_code == 404
    assert client.post(f"{url}/restore", headers=b).status_code == 404
    assert client.post(f"{url}/resolve", headers=b, json={"keep": "this"}).status_code == 404
    conv_notes = client.get(f"/api/conversations/{memory_users['conv_a']}/memory", headers=b)
    assert conv_notes.status_code == 200 and conv_notes.json() == []
    for q in ("Bello", "hamburg", "Sommer"):
        assert memory_items(client, headers=b, q=q) == []
    # Bulk operations of B touch nothing of A's.
    assert client.post("/api/memory/forget-all", headers=b).json() == {"forgotten": 0}
    assert client.post("/api/memory/purge-deleted", headers=b).json() == {"purged": 0}
    # A's notes are exactly as they were.
    after = {i["text"]: i for i in memory_items(client)}
    assert after == memory_users["a"]
    detail = client.get(url, headers=AUTH).json()
    assert detail["version"] == 1 and len(detail["versions"]) == 1


def test_b_has_notes_of_their_own_and_a_secret_block_counts_for_b_only(
    client: TestClient, memory_users: dict[str, Any]
) -> None:
    b = memory_users["b"]
    status, events, _ = ask(
        client, new_conv(client, headers=b), "B-MARKER Ich esse gern Pizza.", headers=b
    )
    assert status == 200 and events[-1][0] == "memory"
    b_texts = {i["text"] for i in memory_items(client, headers=b)}
    assert b_texts == {"Isst gern Pizza", "Wohnt in Kiel"}
    assert {i["text"] for i in memory_items(client)} == set(A_NOTES)  # A sees only A's
    item = next(i for i in memory_items(client, headers=b) if i["text"] == "Wohnt in Kiel")
    r = client.patch(f"/api/memory/{item['id']}", headers=b, json={"text": "Passwort: abc12345"})
    assert r.status_code == 422
    assert client.get("/api/memory", headers=b).json()["blocked"] == 1
    assert client.get("/api/memory", headers=AUTH).json()["blocked"] == 0


def test_a_consolidation_prompt_never_contains_notes_of_two_users(
    client: TestClient, memory_users: dict[str, Any]
) -> None:
    b, rec = memory_users["b"], memory_users["rec"]
    ask(client, new_conv(client, headers=b), "B-MARKER Ich esse gern Pizza.", headers=b)
    service = client.app.state.memory
    service.settings.memory_idle_seconds = 0

    # B starts a run: the model is shown B's notes only.
    r = client.post("/api/memory/consolidate", headers=b)
    assert r.status_code == 202

    def b_done() -> None:
        assert not client.get("/api/memory", headers=b).json()["running"]
        assert client.get("/api/memory", headers=b).json()["runs"]

    eventually(b_done)
    assert rec.consolidation_prompts, "the model was not asked"
    shown = "\n".join(rec.consolidation_prompts)
    assert "Pizza" in shown and "Kiel" in shown
    for fact in A_NOTES:
        assert fact not in shown
    # A's notes were not looked at by B's run (still unchecked) ...
    rec.consolidation_prompts.clear()
    # ... the scheduler handles A's and any later work one user at a time.
    ask(client, new_conv(client, headers=b), "B-MARKER Nochmal Pizza, wie gesagt.", headers=b)
    assert client.portal.call(service.run_scheduled) >= 1
    for prompt in rec.consolidation_prompts:
        mentions_a = any(t in prompt for t in A_NOTES)
        mentions_b = any(t in prompt for t in ("Pizza", "Kiel"))
        assert mentions_a != mentions_b, prompt
    run = run_consolidation(client)  # A's own run is unaffected by B's
    assert run["status"] == "ok"


STORE_METHODS_COVERED = {
    "get", "items", "counts", "add", "change", "mark_checked", "versions", "version", "top",
    "find_same", "confirm", "nearest", "keyword", "similar_pairs", "needing_embedding", "unchecked",
    "active", "forget_all", "purge_deleted", "add_block", "blocked_count", "start_run",
    "finish_run", "runs", "running_run", "live", "get_many", "superseded_by", "add_question",
    "questions", "question", "close_question", "close_questions_about", "mark_question_checked",
    "annotate_question",
    "record_recalled", "record_used", "feedback",
    "expired", "instructions", "profile", "pinned_facts", "household", "system_rules",
    "legacy_item", "proposals", "get_proposal",
    "propose", "accept_proposal", "reject_proposal", "learn_instruction",
    "counts_by_application", "set_placement", "tagged", "tag_names",
}  # fmt: skip


def test_the_memory_store_scopes_every_statement_by_user(
    client: TestClient, memory_users: dict[str, Any]
) -> None:
    """A guard: a new MemoryStore method must be added to the checks below (and thus be
    scoped by user)."""
    public = {
        name
        for name, fn in inspect.getmembers(MemoryStore, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }
    assert public == STORE_METHODS_COVERED

    b_id: uuid.UUID = memory_users["b_id"]
    a_item = memory_users["a"]["Hund heißt Bello"]
    item_id = uuid.UUID(a_item["id"])
    emb = HashingEmbedder()  # the app's embedder is the same one until a model is served
    assert client.app.state.memory.embedder.id == emb.id
    vec = emb.embed_one("Hund Bello Hamburg Steuerberater")
    sessionmaker = client.app.state.sessionmaker
    question_id = uuid.UUID(_a_question(client)["id"])
    a_user = uuid.UUID("00000000-0000-4000-8000-000000000001")

    async def a_proposal() -> uuid.UUID:
        async with sessionmaker() as db:
            proposal, why = await MemoryStore(db, a_user).propose(
                action="add",
                kind="rule",
                text_="Antworte immer kurz.",
                reason="the user asked for it",
                evidence={"quote": "Antworte bitte kurz"},
                origin="chat",
                source_type="chat",
            )
            assert proposal is not None, why
            await db.commit()
            return proposal.id

    proposal_id = client.portal.call(a_proposal)

    async def as_b() -> dict[str, Any]:
        found: dict[str, Any] = {}
        async with sessionmaker() as db:
            store = MemoryStore(db, b_id)
            found["get"] = await store.get(item_id)
            found["items"] = await store.items()
            found["search"] = await store.items(q="Bello")
            found["counts"] = await store.counts()
            found["by_application"] = await store.counts_by_application()
            found["placement"] = await store.set_placement(item_id, application="dev", tags=["x"])
            found["change"] = await store.change(
                item_id, reason="x", actor="user", status="deleted"
            )
            found["versions"] = await store.versions(item_id)
            found["version"] = await store.version(item_id, 1)
            found["same"] = await store.find_same("Hund heißt Bello", emb.id)
            found["confirm"] = await store.confirm(item_id)
            found["nearest"] = await store.nearest(vec, emb.id)
            found["keyword"] = await store.keyword("Bello Hamburg")
            # D74: A's tags find nothing for B, and B's tree names no tag on A's note
            from sqlalchemy import select as _select

            from topcoat_hub.models import MemoryItem as _Item
            from topcoat_hub.models import MemoryItemTag as _Tag

            every_tag = set(await db.scalars(_select(_Tag.tag_id)))
            assert every_tag  # A's notes carry tags
            found["tagged"] = await store.tagged(every_tag)
            foreign = await db.get(_Item, item_id)
            assert foreign is not None
            found["tag_names"] = await store.tag_names([foreign])
            found["pairs"] = await store.similar_pairs(emb.id, 0.0)
            found["needing"] = await store.needing_embedding("other-embedder")
            found["unchecked"] = await store.unchecked()
            found["active"] = await store.active()
            found["top"] = await store.top()
            await store.mark_checked([item_id])
            found["forget"] = await store.forget_all()
            found["purge"] = await store.purge_deleted()
            found["blocked"] = await store.blocked_count()
            found["runs"] = await store.runs()
            found["live"] = await store.live()
            found["many"] = await store.get_many([item_id])
            found["superseded_by"] = await store.superseded_by(item_id)
            found["questions"] = await store.questions("open")
            found["question"] = await store.question(question_id)
            found["closed"] = await store.close_question(question_id, "dismissed", "x")
            await store.close_questions_about([item_id], "x")
            await store.mark_question_checked([item_id], "sig")
            await store.annotate_question(question_id, question="x")  # another user's: nothing
            # care (E3-05): every statement is scoped by user, too
            now = datetime.now(UTC)
            await store.record_recalled([item_id])
            await store.record_used([item_id], now)
            found["feedback"] = await store.feedback(item_id, False, now)
            found["expired"] = await store.expired(now + timedelta(days=9999))
            # the core (E3-06): B sees neither A's rules and facts nor A's proposals
            found["instructions"] = await store.instructions()
            found["profile"] = await store.profile()
            found["pinned"] = await store.pinned_facts()
            found["legacy"] = await store.legacy_item("system_prompt")
            found["proposals"] = await store.proposals(None)
            found["get_proposal"] = await store.get_proposal(proposal_id)
            found["accept"] = await store.accept_proposal(proposal_id)
            found["reject"] = await store.reject_proposal(proposal_id)
            found["household"] = await store.household()  # the household is nobody's note
            found["system"] = await store.system_rules()
            mine, why = await store.propose(
                action="add",
                kind="rule",
                text_="B will es anders.",
                reason="x",
                evidence={},
                origin="chat",
                source_type="chat",
            )
            assert mine is not None, why
            # learning looks only at B's own rules: A's wording is no "exists" for B
            learned, why = await store.learn_instruction(
                kind="rule", text_="Antworte immer kurz.", example=None, evidence={"quote": "kurz"}
            )
            assert learned is not None, why
            await db.commit()
        return found

    # A's own chat above counted recalls and uses: what must not change is A's state from here on
    before = {i["text"]: i for i in memory_items(client)}
    found = client.portal.call(as_b)
    assert found["get"] is None and found["change"] is None and found["version"] is None
    assert found["placement"] is None and found["by_application"] == {}
    assert found["tag_names"] == {item_id: []}
    for key in ("items", "search", "versions", "nearest", "keyword", "pairs", "needing", "tagged",
                "unchecked", "active", "top", "runs", "live", "many", "superseded_by",
                "questions"):  # fmt: skip
        assert found[key] == [], key
    assert found["feedback"] is None and found["expired"] == []
    assert found["same"] is None and found["confirm"] is None
    assert found["question"] is None and found["closed"] is None
    for key in ("instructions", "profile", "pinned", "proposals", "household"):
        assert found[key] == [], key
    assert found["legacy"] is None
    assert found["get_proposal"] is None and found["accept"] is None and found["reject"] is None
    assert found["counts"] == {
        "active": 0,
        "superseded": 0,
        "disputed": 0,
        "deleted": 0,
        "archived": 0,
    }
    assert found["forget"] == 0 and found["purge"] == 0 and found["blocked"] == 0

    assert [q["id"] for q in client.get("/api/memory/questions", headers=AUTH).json()] == [
        str(question_id)
    ]  # A's question is still open
    after = {i["text"]: i for i in memory_items(client)}
    assert after["Hund heißt Bello"] == before["Hund heißt Bello"]
    assert after["Steuerberater ist Frau Sommer"] == before["Steuerberater ist Frau Sommer"]
    assert after["Hund heißt Bello"]["text"] == memory_users["a"]["Hund heißt Bello"]["text"]
    detail = client.get(f"/api/memory/{a_item['id']}", headers=AUTH).json()
    assert detail["version"] == 1
    # ... A's proposal is still waiting, untouched by B's attempts (the checks are not vacuous)

    async def a_proposals() -> list[str]:
        async with sessionmaker() as db:
            return [p.text for p in await MemoryStore(db, a_user).proposals()]

    assert client.portal.call(a_proposals) == ["Antworte immer kurz."]

    async def as_a() -> tuple[int, int]:
        async with sessionmaker() as db:
            store = MemoryStore(db, uuid.UUID("00000000-0000-4000-8000-000000000001"))
            return len(await store.nearest(vec, emb.id)), len(await store.keyword("Bello Hamburg"))

    near, kw = client.portal.call(as_a)
    assert near >= 2 and kw >= 2


def _a_question(client: TestClient) -> dict[str, Any]:
    """A says something that contradicts a note of theirs: one open clarification question."""
    open_ = client.get("/api/memory/questions", headers=AUTH).json()
    if open_:
        return open_[0]
    status, events, _ = ask(client, new_conv(client), "B-MARKER Ich wohne in Kiel, nur zur Info.")
    assert status == 200 and events[-1][1]["questions"] == 1
    [question] = client.get("/api/memory/questions", headers=AUTH).json()
    return question


def test_b_never_sees_answers_or_changes_a_question_of_a(
    client: TestClient, memory_users: dict[str, Any]
) -> None:
    b = memory_users["b"]
    question = _a_question(client)
    assert {c["text"] for c in question["candidates"]} == {"Wohnt in Hamburg", "Wohnt in Kiel"}
    qurl = f"/api/memory/questions/{question['id']}"
    assert client.get("/api/memory/questions", headers=b).json() == []
    assert client.get("/api/memory", headers=b).json()["questions"] == []
    assert client.post(f"{qurl}/answer", headers=b, json={"none": True}).status_code == 404
    assert client.post(f"{qurl}/dismiss", headers=b).status_code == 404
    hamburg = next(c for c in question["candidates"] if c["text"] == "Wohnt in Hamburg")
    assert client.post(f"/api/memory/{hamburg['id']}/undo", headers=b).status_code == 404
    # B's chat neither shows nor asks about A's question, whatever B says.
    status, events, _ = ask(
        client, new_conv(client, headers=b), "B-MARKER Wo wohne ich, Hamburg?", headers=b
    )
    assert status == 200 and "Hamburg" not in json.dumps(events[0][1]["memory"])
    assert "Hamburg" not in sent_messages(memory_users["node"])[0]["content"]
    # A's question is untouched and A can still answer it.
    assert [q["id"] for q in client.get("/api/memory/questions", headers=AUTH).json()] == [
        question["id"]
    ]
    r = client.post(f"{qurl}/answer", headers=AUTH, json={"note_id": hamburg["id"]})
    assert r.status_code == 200, r.text
    assert client.get("/api/memory/questions", headers=AUTH).json() == []


def test_deleting_a_user_removes_their_notes_and_only_theirs(
    client: TestClient, clean_db: str, memory_users: dict[str, Any]
) -> None:
    b = memory_users["b"]
    ask(client, new_conv(client, headers=b), "B-MARKER Ich esse gern Pizza.", headers=b)
    engine = create_engine(clean_db)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(memory_users["b_id"])})
        left = conn.execute(
            text("SELECT count(*) FROM memory_items WHERE kind IN ('person', 'preference')")
        ).scalar()
        versions = conn.execute(
            text(
                "SELECT count(*) FROM memory_versions v JOIN memory_items i ON i.id = v.item_id "
                "WHERE i.kind IN ('person', 'preference')"
            )
        ).scalar()
        b_rows = conn.execute(
            text("SELECT count(*) FROM memory_items WHERE user_id = :id"),
            {"id": str(memory_users["b_id"])},
        ).scalar()
    engine.dispose()
    assert left == 3 and versions == 3 and b_rows == 0
    assert {i["text"] for i in memory_items(client)} == set(A_NOTES)


def test_the_memory_of_a_user_is_not_in_the_prompt_of_the_consolidation_of_none(
    client: TestClient, memory_users: dict[str, Any]
) -> None:
    """Memory switched off: neither the scheduler nor a manual run touches the user's notes."""
    rec: Recorder = memory_users["rec"]
    client.put("/api/chat/settings", headers=AUTH, json={"memory_enabled": False})
    service = client.app.state.memory
    service.settings.memory_idle_seconds = 0
    assert client.portal.call(service.run_scheduled) == 0
    assert client.post("/api/memory/consolidate", headers=AUTH).status_code == 409
    assert rec.consolidation_prompts == []
    node: FakeNode = memory_users["node"]
    assert node is not None
