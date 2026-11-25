"""The cross-check of the program memories (ARCHITECTURE D74): a discrepancy between two memories
goes to the program responsible for its topic — not to the owner of a note —, that program
decides in its own frame (keep, correct, retire, or ask the person), and it alone chooses what the
other program is told."""

import json
import re
import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.test_memory import setup_node
from tests.test_memory_applications import seed, tag_id
from topcoat_hub.memory import COMPARE_SYSTEM
from topcoat_hub.models import DEV_ADMIN_ID


def embed_alike(client: TestClient, *ids: str) -> None:
    """Make notes look alike to the search (the same vector of the hub's own embedder)."""
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        for item in ids:
            conn.execute(
                text("UPDATE memory_items SET embedding = :v, embedder = :e WHERE id = :i"),
                {"v": "[1,0,0]", "e": client.app.state.memory.embedder.id, "i": item},
            )
    engine.dispose()


def run_check(client: TestClient) -> dict[str, Any]:
    stats: dict[str, Any] = {}

    async def go() -> None:
        await client.app.state.memory_care.crosscheck.run(DEV_ADMIN_ID, stats)

    client.portal.call(go)
    return stats


def fake_model(
    relation: str = "contradict",
    decide: Any = None,
    calls: list[str] | None = None,
) -> Any:
    """The comparison says ``relation``; the responsible program answers with ``decide(notes)``,
    where ``notes`` maps the memory that keeps a note ("chat", "admin", "core") to its letter."""

    def answer(req: dict[str, Any]) -> str | None:
        if req.get("stream"):
            return None
        system, user = req["messages"][0]["content"], req["messages"][-1]["content"]
        if system == COMPARE_SYSTEM:
            if calls is not None:
                calls.append("compare")
            return json.dumps(
                {"relation": relation, "keep": 1, "question": "Wann läuft das Backup?"}
            )
        if system.startswith("You are the assistant of the program"):
            if calls is not None:
                calls.append(system)
            letters = {
                m.group(2): m.group(1) for m in re.finditer(r"Note (A|B) \(kept by (\w+)\)", user)
            }
            return json.dumps(decide(letters) if decide else {})
        return None

    return answer


def world(client: TestClient) -> tuple[str, str]:
    backup = tag_id(client, "infrastructure")
    chat = seed("Das Backup läuft um drei Uhr nachts", application="chat", tags=[backup])
    admin = seed("Das Backup läuft um zwei Uhr nachts", application="admin", tags=[backup])
    embed_alike(client, chat, admin)
    return chat, admin


def reports(client: TestClient, recipient: str) -> list[dict[str, Any]]:
    r = client.get(f"/api/memory/reports?recipient={recipient}", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()["reports"]


def test_the_responsible_program_decides_and_tells_the_other_only_what_it_chooses(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    chat, admin = world(client)
    calls: list[str] = []

    def decide(letters: dict[str, str]) -> dict[str, Any]:
        return {
            letters["chat"]: {"action": "correct", "text": "Das Backup läuft um zwei Uhr nachts"},
            letters["admin"]: {"action": "keep", "text": ""},
            "notify": [{"note": letters["chat"], "message": "Die Uhrzeit des Backups war falsch."}],
            "reason": "checked on the NAS",
        }

    node.answer_fn = fake_model(decide=decide, calls=calls)
    stats = run_check(client)
    assert stats["discrepancies"] == 1 and stats["decided"] == 1
    # Administration decided, in its own frame (infrastructure is its topic)
    decided = [c for c in calls if c.startswith("You are")]
    assert len(decided) == 1 and "Administration" in decided[0]
    assert "operator on duty" in decided[0]
    # Chat's note was corrected, with the reason and who did it
    note = client.get(f"/api/memory/{chat}", headers=AUTH).json()
    assert note["text"] == "Das Backup läuft um zwei Uhr nachts"
    assert note["versions"][0]["actor"] == "crosscheck"
    assert note["versions"][0]["reason"] == "cross-check by admin: checked on the NAS"
    assert client.get(f"/api/memory/{admin}", headers=AUTH).json()["version"] == 1
    # Administration sees the discrepancy with both notes ...
    (mine,) = reports(client, "admin")
    assert mine["kind"] == "discrepancy" and mine["status"] == "decided"
    assert {mine["a"]["application"], mine["b"]["application"]} == {"chat", "admin"}
    # ... Chat sees only the notice Administration wrote, about its own note
    (told,) = reports(client, "chat")
    assert told["kind"] == "notice" and told["sender"] == "admin"
    assert told["message"] == "Die Uhrzeit des Backups war falsch."
    assert told["a"]["id"] == chat and told["b"] is None
    assert client.post(f"/api/memory/reports/{told['id']}/seen", headers=AUTH).status_code == 200
    assert reports(client, "chat")[0]["status"] == "seen"


def test_agreeing_notes_are_looked_at_once(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    world(client)
    calls: list[str] = []
    node.answer_fn = fake_model(relation="same", calls=calls)
    assert run_check(client)["agreed"] == 1
    assert run_check(client).get("crosschecked", 0) == 0  # the same pair in the same state
    assert calls == ["compare"]
    assert reports(client, "admin") == [] and reports(client, "chat") == []


def test_a_topic_of_the_person_is_a_question_to_the_person(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    person = tag_id(client, "person")
    core = seed("Wohnt in Hamburg", kind="person", application=None, tags=[person])
    chat = seed("Wohnt in Bremen", kind="person", application="chat", tags=[person])
    embed_alike(client, core, chat)
    calls: list[str] = []
    node.answer_fn = fake_model(calls=calls)
    stats = run_check(client)
    assert stats["asked_person"] == 1 and stats.get("decided", 0) == 0
    assert not [c for c in calls if c.startswith("You are")]  # no program decides for the person
    (q,) = client.get("/api/memory/questions", headers=AUTH).json()
    assert q["program"] == "person" and q["origin"] == "crosscheck"
    assert q["question"] == "Wann läuft das Backup?"


def test_a_program_that_cannot_decide_asks_the_person_where_it_decides(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    world(client)
    node.answer_fn = fake_model(decide=lambda _: {"ask": True, "question": "Zwei oder drei Uhr?"})
    run_check(client)
    (q,) = client.get("/api/memory/questions", headers=AUTH).json()
    assert q["program"] == "admin"
    assert reports(client, "admin")[0]["status"] == "asked"


def test_a_secret_never_becomes_a_correction(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    chat, _ = world(client)

    def decide(letters: dict[str, str]) -> dict[str, Any]:
        return {
            letters["chat"]: {"action": "correct", "text": "Das Passwort des NAS ist hunter2"},
            letters["admin"]: {"action": "keep"},
        }

    node.answer_fn = fake_model(decide=decide)
    run_check(client)
    assert client.get(f"/api/memory/{chat}", headers=AUTH).json()["version"] == 1


def test_without_a_model_nothing_is_recorded_and_the_pair_waits(client: TestClient) -> None:
    world(client)
    stats = run_check(client)
    assert stats.get("crosschecked", 0) == 0
    assert reports(client, "admin") == []


def test_the_reports_are_personal(client: TestClient, clean_db: str, connect) -> None:
    from tests.users import make_user

    node = setup_node(client, connect)
    world(client)
    node.answer_fn = fake_model(
        decide=lambda letters: {
            letters["chat"]: {"action": "retire"},
            letters["admin"]: {"action": "keep"},
            "notify": [{"note": letters["chat"], "message": "Erledigt."}],
        }
    )
    run_check(client)
    _, other = make_user(clean_db, "user-b")
    assert client.get("/api/memory/reports?recipient=chat", headers=other).json()["reports"] == []
    notice = reports(client, "chat")[0]["id"]
    assert client.post(f"/api/memory/reports/{notice}/seen", headers=other).status_code == 404
    assert client.post(f"/api/memory/reports/{uuid.uuid4()}/seen", headers=AUTH).status_code == 404
