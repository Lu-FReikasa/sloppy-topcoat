"""Memory (E2-02, D19): extraction after a turn, recall into the prompt, merge, versions with
reasons, undo, secret filter, idle consolidation with a fake model."""

import asyncio
import json
import re
import uuid
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import FakeNode, eventually, online_node
from tests.prompts import assert_system
from tests.test_chat import ask, new_conv, sent_messages, use_model
from topcoat_hub.embedding import GatewayEmbedder
from topcoat_hub.memory import COMPARE_SYSTEM, EXTRACTION_SYSTEM

CONSOLIDATION_PREFIX = "You review a user's memory notes"


def _user_part(extraction_prompt: str) -> str:
    """The user's message of an extraction prompt: not the "Existing notes" before it and not
    the assistant reply after it (a fake node echoes the whole prompt into its answer)."""
    return extraction_prompt.split("User message:", 1)[-1].split("\n\nAssistant reply:", 1)[0]


def note(text: str, **kw: Any) -> dict[str, Any]:
    return {"text": text, "confidence": 0.9, "inferred": False, "secret": False, **kw}


_RESIDENCE = re.compile(r"^(?:\d+\. )?(?:Lebt|Wohnt|lebt|wohnt) in (\w+)", re.MULTILINE)


def with_contradicts(notes: list[dict[str, Any]], prompt: str) -> list[dict[str, Any]]:
    """What the extraction prompt asks of the model (D124: no slots in the hub): a new note about
    where the user lives names the listed existing notes that say another place
    (``contradicts``). A note that names its own numbers keeps them."""
    listed = prompt.split("User message:", 1)[0]
    existing = {
        int(m.group(1)): m.group(2)
        for m in re.finditer(r"^(\d+)\. (?:Lebt|Wohnt|lebt|wohnt) in (\w+)", listed, re.MULTILINE)
    }
    out = []
    for n in notes:
        place = _RESIDENCE.match(n.get("text", ""))
        if place and "contradicts" not in n:
            others = [k for k, v in existing.items() if v != place.group(1)]
            n = {**n, "contradicts": others} if others else n
        out.append(n)
    return out


def model(
    extraction: dict[str, list[dict[str, Any]]] | None = None,
    consolidation: Callable[[dict[str, int]], dict[str, Any]] | None = None,
) -> Callable[[dict[str, Any]], str | None]:
    """A fake memory model: extraction answers by a keyword of the user's message,
    consolidation answers with a plan computed from the numbered notes it was given."""

    def answer(req: dict[str, Any]) -> str | None:
        if req.get("stream"):
            return None
        system, user = req["messages"][0]["content"], req["messages"][-1]["content"]
        if system == EXTRACTION_SYSTEM:
            message = _user_part(user)  # not the "Existing notes" before it, not the reply
            for keyword, notes in (extraction or {}).items():
                if keyword in message:
                    return json.dumps({"memories": with_contradicts(notes, user)})
            return json.dumps({"memories": []})
        if system.startswith(CONSOLIDATION_PREFIX) and consolidation:
            numbers = {
                m.group(2): int(m.group(1))
                for m in re.finditer(r"^(\d+)\. \([^)]*\) (.*)$", user, re.MULTILINE)
            }
            return json.dumps(consolidation(numbers))
        return None

    return answer


def memory_items(client: TestClient, headers: dict[str, str] = AUTH, **params: str) -> list[Any]:
    r = client.get("/api/memory", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def chat_once(client: TestClient, text: str, conv: str | None = None) -> list[Any]:
    status, events, _ = ask(client, conv or new_conv(client), text)
    assert status == 200
    return events


def run_consolidation(client: TestClient) -> dict[str, Any]:
    """Start a consolidation run and return it once it has finished. The run starts in the
    background after the 202: wait for *this* run, never the one before it, and return the row
    that was seen finished (a second look could find the next run still running, #118)."""
    before = {r["id"] for r in client.get("/api/memory", headers=AUTH).json()["runs"]}
    r = client.post("/api/memory/consolidate", headers=AUTH)
    assert r.status_code == 202, r.text
    seen: list[dict[str, Any]] = []

    def finished() -> None:
        runs = client.get("/api/memory", headers=AUTH).json()["runs"]
        assert runs and runs[0]["id"] not in before and runs[0]["status"] != "running"
        seen.append(runs[0])

    eventually(finished)
    return seen[-1]


def setup_node(client: TestClient, connect: Any, **kw: Any) -> FakeNode:
    node = online_node(client, connect, "node-a", loaded=["small"], **kw)
    use_model(client)
    return node


# --- writing during chat -----------------------------------------------------------------------


def test_a_note_is_remembered_after_the_answer_with_source_and_first_version(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    node.answer_fn = model({"Hamburg": [note("Wohnt in Hamburg"), note("Ist Tischler")]})
    conv = new_conv(client)
    events = chat_once(client, "Ich wohne in Hamburg und arbeite als Tischler.", conv)
    assert [e for e, _ in events][-2:] == ["done", "memory"]
    done, mem = events[-2][1]["message"], events[-1][1]
    assert mem["message_id"] == done["id"] and mem["blocked"] == 0
    assert [n["text"] for n in mem["remembered"]] == ["Wohnt in Hamburg", "Ist Tischler"]

    items = memory_items(client)
    assert {i["text"] for i in items} == {"Wohnt in Hamburg", "Ist Tischler"}
    first = items[0] | {}
    assert first["status"] == "active" and first["evidence"] == "doc"  # the user said it
    assert first["source_type"] == "chat" and first["version"] == 1
    assert first["source_ref"] == {"conversation_id": conv, "message_id": done["id"]}
    detail = client.get(f"/api/memory/{first['id']}", headers=AUTH).json()
    assert [(v["version"], v["actor"], v["reason"]) for v in detail["versions"]] == [
        (1, "chat", "remembered from conversation")
    ]
    # the notes of one conversation are listed for the "remembered" lines after a reload
    r = client.get(f"/api/conversations/{conv}/memory", headers=AUTH)
    assert {i["text"] for i in r.json()} == {"Wohnt in Hamburg", "Ist Tischler"}


def test_inferred_notes_are_assumptions_and_short_messages_cost_no_model_call(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    node.answer_fn = model({"Werkstatt": [note("Hat vermutlich eine Werkstatt", inferred=True)]})
    chat_once(client, "hi")  # too short to be worth a model call
    assert memory_items(client) == []
    chat_once(client, "Ich muss morgen in die Werkstatt fahren.")
    [item] = memory_items(client)
    assert item["evidence"] == "assumption"


def test_saying_it_again_confirms_with_a_counter_not_a_new_version(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    node.answer_fn = model({"Hamburg": [note("Wohnt in Hamburg")]})
    chat_once(client, "Ich wohne in Hamburg, wie gesagt.")
    [before] = memory_items(client)
    assert before["confirmed_count"] == 0 and before["last_confirmed_at"] is None
    events = chat_once(client, "Ja, in Hamburg wohne ich seit Jahren.")
    mem = events[-1][1]
    assert mem["remembered"] == [] and [n["text"] for n in mem["confirmed"]] == ["Wohnt in Hamburg"]
    [item] = memory_items(client)
    assert item["version"] == 1  # nothing about the note changed
    assert item["confirmed_count"] == 1 and item["last_confirmed_at"]
    assert item["updated_at"] == before["updated_at"]  # the consolidation is not woken up
    versions = client.get(f"/api/memory/{item['id']}", headers=AUTH).json()["versions"]
    assert [v["reason"] for v in versions] == ["remembered from conversation"]
    chat_once(client, "Wie gesagt: Hamburg ist mein Wohnort.")
    assert memory_items(client)[0]["confirmed_count"] == 2


def test_secrets_are_never_stored_and_the_block_is_counted_without_the_value(
    client: TestClient, connect, caplog: Any
) -> None:
    node = setup_node(client, connect)
    node.answer_fn = model(
        {
            "WLAN": [
                note("Das WLAN-Passwort ist Sommer2024!"),  # deterministic filter
                note("Bankkonto beim Onkel", secret=True),  # the model says: secret
                note("Hat ein WLAN im Keller"),
            ]
        }
    )
    with caplog.at_level("WARNING"):
        events = chat_once(client, "Unser WLAN heißt Keller, das Passwort ist Sommer2024!")
    mem = events[-1][1]
    assert mem["blocked"] == 2 and [n["text"] for n in mem["remembered"]] == [
        "Hat ein WLAN im Keller"
    ]
    assert {i["text"] for i in memory_items(client)} == {"Hat ein WLAN im Keller"}
    assert "Sommer2024" not in caplog.text and "Bankkonto" not in caplog.text
    assert "refused a note (password)" in caplog.text and "(model_flag)" in caplog.text
    body = client.get("/api/memory", headers=AUTH).json()
    assert body["blocked"] == 2
    assert "Sommer2024" not in json.dumps(body)


def test_memory_can_be_switched_off(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    node.answer_fn = model({"Hamburg": [note("Wohnt in Hamburg")]})
    r = client.put("/api/chat/settings", headers=AUTH, json={"memory_enabled": False})
    assert r.json()["user"]["memory_enabled"] is False
    events = chat_once(client, "Ich wohne in Hamburg, nur zur Info.")
    assert events[-1][0] == "done" and memory_items(client) == []
    system = sent_messages(node)[0]["content"]
    assert_system(system, memory=False)
    assert "Hamburg" not in system  # memory off: no profile in the core either
    assert "remember other conversations" in system  # honest: no memory, no claims
    assert client.post("/api/memory/consolidate", headers=AUTH).status_code == 409


# --- recall ------------------------------------------------------------------------------------


def test_recalled_notes_reach_the_prompt_with_their_tags_and_are_shown_with_the_answer(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    node.answer_fn = model(
        {
            "Hamburg": [note("Wohnt in Hamburg")],
            "Espresso": [note("Trinkt Espresso ohne Milch", inferred=True)],
        }
    )
    chat_once(client, "Ich wohne in Hamburg, nur zur Info.")
    chat_once(client, "Ich trinke gerne Espresso ohne Milch.")
    events = chat_once(client, "Wie ist das Wetter in Hamburg im Winter?")
    meta = events[0][1]
    assert [n["text"] for n in meta["memory"]] == ["Wohnt in Hamburg"]  # not the espresso note
    messages = sent_messages(node)
    # the notes are in the last user turn (D27), the system prompt only says how to read them
    assert "Notes about the user for this message" in messages[-1]["content"]
    assert "- Wohnt in Hamburg — the user said it" in messages[-1]["content"]
    # the profile stands in the core, first, without any search (E3-06): both facts
    assert_system(messages[0]["content"], contains=("- Wohnt in Hamburg", "Espresso ohne Milch"))
    assert "Espresso" not in "\n".join(m["content"] for m in messages[1:])
    done = next(d for e, d in events if e == "done")["message"]
    assert done["memory_refs"][0]["text"] == "Wohnt in Hamburg"
    assert done["memory_refs"][0]["evidence"] == "doc"
    stored = client.get(f"/api/conversations/{meta['conversation_id']}", headers=AUTH).json()
    assert stored["messages"][-1]["memory_refs"] == done["memory_refs"]


def test_recall_finds_notes_by_keyword_even_without_a_vector(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    node.answer_fn = model({"Tischler": [note("Arbeitet als Tischler")]})
    chat_once(client, "Ich arbeite als Tischler in einer kleinen Werkstatt.")
    service = client.app.state.memory

    async def strip_vectors() -> None:
        from sqlalchemy import text

        async with service.sessionmaker() as db:
            await db.execute(text("UPDATE memory_items SET embedding = NULL, embedder = NULL"))
            await db.commit()

    client.portal.call(strip_vectors)
    events = chat_once(client, "Was macht ein Tischler den ganzen Tag?")
    assert [n["text"] for n in events[0][1]["memory"]] == ["Arbeitet als Tischler"]


def test_notes_about_other_things_are_not_recalled_for_a_question_about_steam_or_wifi(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    node.answer_fn = model(
        {"Tischler": [note("Arbeitet als Tischler")], "Rex": [note("Der Hund heißt Rex")]}
    )
    chat_once(client, "Ich arbeite als Tischler in einer kleinen Werkstatt.")
    chat_once(client, "Mein Hund heißt Rex, nur damit du es weißt.")
    node.answer_fn = model()
    # "mein" / "ich" about a thing is no question about the user: nothing is recalled, nothing is
    # shown as used, nothing goes into the prompt
    for question in (
        "Warum startet mein Steam nicht mehr?",
        "Wie richte ich mein WLAN auf einen anderen Kanal ein?",
        "Welche Geräte sind in meinem WLAN?",
    ):
        events = chat_once(client, question)
        assert events[0][1]["memory"] == [], question
        assert "Notes about the user" not in sent_messages(node)[-1]["content"], question
        assert "Tischler" not in sent_messages(node)[-1]["content"]
    # a question about the person still gets their notes, with or without a shared word
    events = chat_once(client, "Wie heißt mein Hund?")
    assert "Der Hund heißt Rex" in [n["text"] for n in events[0][1]["memory"]]
    events = chat_once(client, "Was weißt du über mich?")
    assert {n["text"] for n in events[0][1]["memory"]} == {
        "Arbeitet als Tischler",
        "Der Hund heißt Rex",
    }


def test_a_disputed_note_is_marked_in_the_prompt_and_unrelated_chat_recalls_nothing(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    node.answer_fn = model(
        {"Berlin": [note("Wohnt in Berlin")], "Hamburg": [note("Wohnt in Hamburg")]}
    )
    chat_once(client, "Ich wohne in Berlin, nur zur Info.")
    chat_once(client, "Ich wohne in Hamburg, nur zur Info.")  # a plain contradiction: not decided
    assert {i["status"] for i in memory_items(client)} == {"disputed"}
    node.answer_fn = model()
    events = chat_once(client, "Wie sicher bist du dir, wo ich wohne?")
    assert {n["status"] for n in events[0][1]["memory"]} == {"disputed"}
    messages = sent_messages(node)
    assert messages[-1]["content"].count(" — disputed: contradicts another note") == 2
    assert "never claim to be sure about those" in messages[0]["content"]  # what it means
    events = chat_once(client, "Erkläre mir bitte Quantenverschränkung kurz.")
    assert events[0][1]["memory"] == []


# --- edit, undo, revert, delete ----------------------------------------------------------------


def _remember(
    client: TestClient, node: FakeNode, text: str, trigger: str, message: str | None = None
) -> dict[str, Any]:
    """Remember ``text``. A ``message`` about the person (``Ich wohne in …``) recalls their notes,
    so the model sees the ones it may contradict; the default message recalls nothing."""
    node.answer_fn = model({trigger: [note(text)]})
    chat_once(client, message or f"{trigger} steht hier, das ist eine Information für dich.")
    return next(i for i in memory_items(client) if i["text"] == text)


def with_compare(
    base: Callable[[dict[str, Any]], str | None], relation: dict[str, Any] | None
) -> Callable[[dict[str, Any]], str | None]:
    """The fake memory model plus an answer to "do these two notes contradict?"."""

    def answer(req: dict[str, Any]) -> str | None:
        if not req.get("stream") and req["messages"][0]["content"] == COMPARE_SYSTEM:
            return json.dumps(relation) if relation is not None else None
        return base(req)

    return answer


def live_texts(client: TestClient) -> list[str]:
    return [i["text"] for i in memory_items(client) if i["status"] in ("active", "disputed")]


def _flag_pair(client: TestClient, node: FakeNode, relation: dict[str, Any] | None) -> list[Any]:
    """Two notes the consolidation flags as a conflict, then the run; the notes by id order."""
    a = "Die Nachprüfung zeigt das Modell und die Node kurz an"
    b = "Nachprüfungen sollen den Modellnamen samt Node knapp nennen"
    _remember(client, node, a, "Modell")
    _remember(client, node, b, "Modellname")
    node.answer_fn = with_compare(
        model(
            consolidation=lambda n: {"conflict": [{"a": n[a], "b": n[b], "reason": "other words"}]}
        ),
        relation,
    )
    run_consolidation(client)
    return [a, b]


def test_edit_revert_delete_restore_keep_the_history(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    item = _remember(client, node, "Wohnt in Hamburg", "Hamburg")
    url = f"/api/memory/{item['id']}"

    r = client.patch(url, headers=AUTH, json={"text": "Wohnt in Hamburg-Altona"})
    assert r.status_code == 200 and r.json()["version"] == 2
    assert r.json()["text"] == "Wohnt in Hamburg-Altona"
    r = client.patch(url, headers=AUTH, json={"evidence": "live"})
    assert r.json()["version"] == 3 and r.json()["evidence"] == "live"

    r = client.post(f"{url}/revert", headers=AUTH, json={"version": 1})
    assert r.status_code == 200
    reverted = r.json()
    assert reverted["text"] == "Wohnt in Hamburg" and reverted["evidence"] == "doc"
    assert reverted["version"] == 4  # the history stays, the revert is a new version
    assert client.post(f"{url}/revert", headers=AUTH, json={"version": 99}).status_code == 404

    r = client.delete(url, headers=AUTH)  # soft: hidden, history stays
    assert r.json()["status"] == "deleted"
    assert memory_items(client) == [] and len(memory_items(client, status="deleted")) == 1
    assert client.get(url, headers=AUTH).json()["version"] == 5
    assert client.delete(url, headers=AUTH).json()["version"] == 5  # idempotent
    assert client.post(f"{url}/restore", headers=AUTH).json()["status"] == "active"
    assert client.post(f"{url}/restore", headers=AUTH).status_code == 409

    versions = client.get(url, headers=AUTH).json()["versions"]
    assert [(v["version"], v["actor"]) for v in versions] == [
        (6, "user"), (5, "user"), (4, "user"), (3, "user"), (2, "user"), (1, "chat"),
    ]  # fmt: skip
    assert [v["reason"] for v in versions][:3] == [
        "restored by the user", "deleted by the user", "reverted to version 1",
    ]  # fmt: skip


def test_a_text_that_looks_like_a_secret_cannot_be_saved_by_editing(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    item = _remember(client, node, "Wohnt in Hamburg", "Hamburg")
    r = client.patch(
        f"/api/memory/{item['id']}", headers=AUTH, json={"text": "Passwort: geheim1234"}
    )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "secret_blocked"
    assert memory_items(client)[0]["text"] == "Wohnt in Hamburg"
    assert client.get("/api/memory", headers=AUTH).json()["blocked"] == 1


def test_forget_all_is_soft_and_purge_is_explicit(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    _remember(client, node, "Wohnt in Hamburg", "Hamburg")
    _remember(client, node, "Ist Tischler", "Tischler")
    assert client.post("/api/memory/forget-all", headers=AUTH).json() == {"forgotten": 2}
    assert memory_items(client) == []
    deleted = memory_items(client, status="deleted")
    assert len(deleted) == 2  # still there, with history, restorable
    client.post(f"/api/memory/{deleted[0]['id']}/restore", headers=AUTH)
    assert len(memory_items(client)) == 1
    assert client.post("/api/memory/purge-deleted", headers=AUTH).json() == {"purged": 1}
    assert memory_items(client, status="deleted") == [] and len(memory_items(client)) == 1
    counts = client.get("/api/memory", headers=AUTH).json()["counts"]
    assert counts["active"] == 1 and counts["deleted"] == 0


def test_search_and_filters(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    _remember(client, node, "Wohnt in Hamburg", "Hamburg")
    node.answer_fn = model({"Werkstatt": [note("Hat eine Werkstatt", inferred=True)]})
    chat_once(client, "Ich habe eine kleine Werkstatt im Keller.")
    assert [i["text"] for i in memory_items(client, q="hamburg")] == ["Wohnt in Hamburg"]
    assert [i["text"] for i in memory_items(client, evidence="assumption")] == [
        "Hat eine Werkstatt"
    ]
    assert memory_items(client, q="100%") == []  # LIKE wildcards are not wildcards
    assert client.get("/api/memory", headers=AUTH, params={"status": "bogus"}).status_code == 422


# --- consolidation -----------------------------------------------------------------------------


def test_two_conflicting_statements_become_a_disputed_pair_with_one_question(
    client: TestClient, connect
) -> None:
    """A second, plain statement that the model says contradicts a note is not decided (without
    the hub's own settling) — both notes are marked at once and the user gets ONE question."""
    node = setup_node(client, connect)
    berlin = _remember(client, node, "Wohnt in Berlin", "Berlin", "Ich wohne in Berlin.")
    hamburg = _remember(client, node, "Wohnt in Hamburg", "Hamburg", "Ich wohne in Hamburg.")
    assert berlin["status"] == "active" and hamburg["status"] == "disputed"  # listed before
    notes = _states(client)
    b, h = notes["Wohnt in Berlin"], notes["Wohnt in Hamburg"]
    assert b["status"] == h["status"] == "disputed"
    assert b["disputed_with"] == h["id"] and h["disputed_with"] == b["id"]
    history = client.get(f"/api/memory/{h['id']}", headers=AUTH).json()["versions"]
    assert "the newer note; suggested to keep" in history[0]["reason"]
    assert history[1]["reason"] == "remembered from conversation"  # nothing was lost

    [question] = client.get("/api/memory/questions", headers=AUTH).json()
    assert question["topic"] == "" and question["origin"] == "chat"  # no slots (D124)
    assert [c["text"] for c in question["candidates"]] == ["Wohnt in Berlin", "Wohnt in Hamburg"]

    # A tidy-up run looks at it again, finds no evidence that settles it and keeps the one
    # question (no second question, no new versions).
    run = run_consolidation(client)
    assert run["stats"]["resolved"] == 0 and run["stats"]["questions"] == 1
    run_consolidation(client)
    assert len(client.get("/api/memory/questions", headers=AUTH).json()) == 1
    after = client.get(f"/api/memory/{h['id']}", headers=AUTH).json()
    assert after["version"] == h["version"]

    # The user decides on the memory page: the newer note wins; the question is closed.
    r = client.post(f"/api/memory/{h['id']}/resolve", headers=AUTH, json={"keep": "this"})
    assert r.json()["status"] == "active" and r.json()["disputed_with"] is None
    old = client.get(f"/api/memory/{b['id']}", headers=AUTH).json()
    assert old["status"] == "superseded" and old["superseded_by"] == h["id"]
    assert client.get("/api/memory/questions", headers=AUTH).json() == []
    assert (
        client.post(
            f"/api/memory/{h['id']}/resolve", headers=AUTH, json={"keep": "this"}
        ).status_code
        == 409
    )
    # Revert works: going back to version 1 makes the superseded note active again.
    r = client.post(f"/api/memory/{b['id']}/revert", headers=AUTH, json={"version": 1})
    assert r.json()["status"] == "active" and r.json()["superseded_by"] is None


def test_the_model_can_flag_a_conflict_the_kind_of_note_does_not_show(
    client: TestClient, connect
) -> None:
    """The brief's integration test: two statements → one `disputed` pair after the
    consolidation, revert works. (Jobs may be several, so only the model can call it.)"""
    node = setup_node(client, connect)
    tischler = _remember(client, node, "Arbeitet als Tischler", "Tischler")
    baecker = _remember(client, node, "Arbeitet als Bäcker", "Bäcker")
    assert tischler["status"] == baecker["status"] == "active"  # jobs may be several

    prompts: list[str] = []

    def plan(numbers: dict[str, int]) -> dict[str, Any]:
        prompts.append(json.dumps(numbers))
        return {
            "conflict": [
                {
                    "a": numbers["Arbeitet als Tischler"],
                    "b": numbers["Arbeitet als Bäcker"],
                    "reason": "one job",
                }
            ]
        }

    node.answer_fn = model(consolidation=plan)
    run = run_consolidation(client)
    assert run["status"] == "ok" and run["trigger"] == "manual"
    assert run["stats"]["disputed"] == 1 and run["stats"]["batches"] >= 1
    assert run["stats"]["tokens"] > 0 and run["stats"]["model"].startswith("small")
    notes = _states(client)
    t, b = notes["Arbeitet als Tischler"], notes["Arbeitet als Bäcker"]
    assert t["status"] == b["status"] == "disputed" and t["disputed_with"] == b["id"]
    history = client.get(f"/api/memory/{b['id']}", headers=AUTH).json()["versions"]
    assert history[0]["actor"] == "consolidation" and "one job" in history[0]["reason"]
    assert len(client.get("/api/memory/questions", headers=AUTH).json()) == 1

    calls = len(prompts)  # nothing new to look at: the model is not asked again
    run_consolidation(client)
    assert len(prompts) == calls

    r = client.post(f"/api/memory/{t['id']}/revert", headers=AUTH, json={"version": 1})
    assert r.json()["status"] == "active"
    other = client.get(f"/api/memory/{b['id']}", headers=AUTH).json()
    assert other["status"] == "active"
    assert client.get("/api/memory/questions", headers=AUTH).json() == []


def test_reverting_a_disputed_note_ends_the_dispute_for_its_partner(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    _remember(client, node, "Wohnt in Berlin", "Berlin", "Ich wohne in Berlin.")
    _remember(client, node, "Wohnt in Hamburg", "Hamburg", "Ich wohne in Hamburg.")
    items = {i["text"]: i for i in memory_items(client)}
    r = client.post(
        f"/api/memory/{items['Wohnt in Berlin']['id']}/revert", headers=AUTH, json={"version": 1}
    )
    assert r.json()["status"] == "active"
    other = client.get(f"/api/memory/{items['Wohnt in Hamburg']['id']}", headers=AUTH).json()
    assert other["status"] == "active" and other["disputed_with"] is None
    assert other["versions"][0]["reason"].startswith("contradiction resolved")
    assert client.get("/api/memory/questions", headers=AUTH).json() == []


def test_duplicates_are_merged_into_one_and_the_rest_is_superseded(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    a = _remember(client, node, "Wohnt in Hamburg", "Hamburg")
    node.answer_fn = model({"Elbe": [note("Lebt in Hamburg an der Elbe")]})
    chat_once(client, "Ich lebe direkt an der Elbe, in Hamburg.")
    node.answer_fn = model(
        consolidation=lambda n: {
            "merge": [
                {
                    "keep": n["Wohnt in Hamburg"],
                    "drop": [n["Lebt in Hamburg an der Elbe"]],
                    "text": "Wohnt in Hamburg an der Elbe",
                    "reason": "same fact",
                }
            ]
        }
    )
    run = run_consolidation(client)
    assert run["stats"]["merged"] == 1
    active = memory_items(client, status="active")
    assert [i["text"] for i in active] == ["Wohnt in Hamburg an der Elbe"]
    assert active[0]["id"] == a["id"] and active[0]["version"] == 2
    [gone] = memory_items(client, status="superseded")
    assert gone["superseded_by"] == a["id"]
    reasons = client.get(f"/api/memory/{gone['id']}", headers=AUTH).json()["versions"]
    assert "duplicate" in reasons[0]["reason"]


def test_outdated_notes_get_a_corrected_version_and_nonsense_plans_do_nothing(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    item = _remember(client, node, "Hat morgen einen Zahnarzttermin", "Zahnarzt")
    node.answer_fn = model(
        consolidation=lambda n: {
            "correct": [
                {"id": 99, "text": "invalid number is ignored"},
                {
                    "id": n["Hat morgen einen Zahnarzttermin"],
                    "text": "Passwort: abc12345",
                    "reason": "x",
                },
                {
                    "id": n["Hat morgen einen Zahnarzttermin"],
                    "text": "Hatte einen Zahnarzttermin",
                    "reason": "outdated: the date has passed",
                },
            ],
            "merge": [{"keep": 1, "drop": [1]}, "junk"],
            "conflict": [{"a": 1, "b": 1}],
        }
    )
    run = run_consolidation(client)
    assert run["stats"]["corrected"] == 1 and run["stats"]["blocked"] == 1
    [now] = memory_items(client)
    assert now["id"] == item["id"] and now["text"] == "Hatte einen Zahnarzttermin"
    history = client.get(f"/api/memory/{item['id']}", headers=AUTH).json()["versions"]
    assert "outdated" in history[0]["reason"]
    assert client.get("/api/memory", headers=AUTH).json()["blocked"] == 1


def test_a_run_stops_at_its_budget_and_a_missing_model_is_reported(
    client: TestClient, connect, monkeypatch: Any
) -> None:
    node = setup_node(client, connect)
    monkeypatch.setattr("topcoat_hub.memory.REST_BATCH", 1)  # one note per batch
    for text, word in (
        ("Wohnt in Hamburg", "Hamburg"),
        ("Ist Tischler", "Tischler"),
        ("Mag Espresso", "Espresso"),
    ):
        _remember(client, node, text, word)
    service = client.app.state.memory
    service.settings.memory_run_max_tokens = 1  # the first batch already uses it up
    node.answer_fn = model(consolidation=lambda n: {})
    run = run_consolidation(client)
    assert run["status"] == "stopped_budget" and run["stats"]["batches"] == 1
    service.settings.memory_run_max_tokens = 6000

    node.close()
    eventually(lambda: None)  # let the hub notice the node is gone
    r = client.post("/api/memory/consolidate", headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_node_available"


def test_the_scheduler_only_runs_when_idle(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    _remember(client, node, "Wohnt in Hamburg", "Hamburg")
    node.answer_fn = model(consolidation=lambda n: {})
    service = client.app.state.memory
    settings = service.settings

    def tick() -> int:
        return client.portal.call(service.run_scheduled)

    settings.memory_idle_seconds = 120  # the optional quiet time is on
    service.activity.begin()  # somebody is chatting
    assert tick() == 0
    service.activity.end()
    settings.memory_idle_seconds = 3600  # ... and just stopped
    assert tick() == 0
    settings.memory_idle_seconds = 0
    node.state = "busy_gaming"  # a game is running
    node.heartbeat()

    def gaming() -> None:
        assert not client.portal.call(client.app.state.gateway.any_node_available)

    eventually(gaming)
    assert tick() == 0
    node.state = "available"
    node.heartbeat()

    def available() -> None:
        assert client.portal.call(client.app.state.gateway.any_node_available)

    eventually(available)
    assert tick() == 1
    assert client.get("/api/memory", headers=AUTH).json()["runs"][0]["trigger"] == "scheduled"
    assert tick() == 0  # nothing left to check


def test_the_consolidation_helper_returns_its_own_run_even_when_it_starts_late(
    client: TestClient, connect: Any, monkeypatch: Any
) -> None:
    """#118: the run starts in the background after the 202; the helper must not hand back the
    run before it (or the next one, still running and without stats)."""
    setup_node(client, connect)
    first = run_consolidation(client)
    memory = client.app.state.memory
    consolidate = memory.consolidate

    async def late(user_id: uuid.UUID, trigger: str) -> Any:
        await asyncio.sleep(0.5)
        return await consolidate(user_id, trigger)

    monkeypatch.setattr(memory, "consolidate", late)
    second = run_consolidation(client)
    assert second["id"] != first["id"] and second["status"] != "running"
    assert "resolved" in second["stats"]


def test_switching_the_embedder_re_embeds_the_old_notes_during_consolidation(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    node.embedding_dim = 16
    _remember(client, node, "Wohnt in Hamburg", "Hamburg")
    service = client.app.state.memory
    old = service.embedder.id
    assert old.startswith("hashing")
    service.embedder = GatewayEmbedder(service.gateway, "small")  # E2-03: bge-m3 instead
    node.answer_fn = model(consolidation=lambda n: {})
    run = run_consolidation(client)
    assert run["stats"]["reembedded"] == 1 and run["stats"]["embedder"] == "gateway:small"
    [item] = memory_items(client)
    assert item["embedder"] == "gateway:small"
    body = client.get("/api/memory", headers=AUTH).json()
    assert body["embedder"] == {"id": "gateway:small", "transitional": False}
    # recall works with the new vectors
    events = chat_once(client, "Was weißt du über Hamburg?")
    assert [n["text"] for n in events[0][1]["memory"]] == ["Wohnt in Hamburg"]


def test_the_gateway_embedder_reports_a_missing_model_as_embedding_error(
    client: TestClient, connect
) -> None:
    import pytest

    from topcoat_hub.embedding import EmbeddingError

    setup_node(client, connect)
    emb = GatewayEmbedder(client.app.state.gateway, "not-a-model")

    async def go() -> None:
        await emb.embed(["x"])

    with pytest.raises(EmbeddingError):
        client.portal.call(go)
    # ... and a chat still works: recall degrades instead of failing
    client.app.state.memory.embedder = emb
    assert chat_once(client, "Eine ganz normale Frage ohne Erinnerung.")[-1][0] in (
        "done",
        "memory",
    )


def test_unknown_ids_are_404(client: TestClient) -> None:
    ghost = uuid.uuid4()
    assert client.get(f"/api/memory/{ghost}", headers=AUTH).status_code == 404
    assert client.patch(f"/api/memory/{ghost}", headers=AUTH, json={"text": "x"}).status_code == 404
    assert client.delete(f"/api/memory/{ghost}", headers=AUTH).status_code == 404
    assert (
        client.post(f"/api/memory/{ghost}/resolve", headers=AUTH, json={"keep": "this"}).status_code
        == 404
    )
    assert client.get("/api/memory").status_code == 401


# --- a merge must never lose a fact (hardware test: a newer note was rewritten to the older one) -


class SameForAll:
    """An embedder that finds everything alike: clusters notes without merging them by itself."""

    id = "same"
    dup_threshold = 1.1  # never a near-duplicate at chat time
    recall_min = 0.0
    cluster_threshold = 0.3

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


def _two_jobs(client: TestClient, node: FakeNode) -> tuple[dict[str, Any], dict[str, Any]]:
    """The older note says carpenter, the newer baker (the order of the hardware test)."""
    tischler = _remember(client, node, "Arbeitet als Tischler", "Tischler")
    baecker = _remember(client, node, "Arbeitet als Bäcker", "Bäcker")
    return tischler, baecker


def _states(client: TestClient) -> dict[str, dict[str, Any]]:
    return {i["text"]: i for i in memory_items(client)}


def test_a_merge_that_talks_about_a_contradiction_is_a_conflict_and_loses_nothing(
    client: TestClient, connect
) -> None:
    """Regression: the model 'merged' the newer note into the older one and rewrote
    "Arbeitet als Bäcker" as "Arbeitet als Tischler", reason "contradicting the previous note"."""
    node = setup_node(client, connect)
    tischler, baecker = _two_jobs(client, node)
    node.answer_fn = model(
        consolidation=lambda n: {
            "merge": [
                {
                    "keep": n["Arbeitet als Bäcker"],
                    "drop": [n["Arbeitet als Tischler"]],
                    "text": "Arbeitet als Tischler",
                    "reason": "The user stated they work as a carpenter, contradicting the note.",
                }
            ]
        }
    )
    run = run_consolidation(client)
    assert run["stats"]["merged"] == 0 and run["stats"]["disputed"] == 1
    notes = _states(client)
    assert set(notes) == {"Arbeitet als Bäcker", "Arbeitet als Tischler"}  # neither text changed
    assert {n["status"] for n in notes.values()} == {"disputed"}
    assert notes["Arbeitet als Bäcker"]["disputed_with"] == tischler["id"]
    assert memory_items(client, status="superseded") == []  # the new fact is not lost
    newer = client.get(f"/api/memory/{baecker['id']}", headers=AUTH).json()
    older = client.get(f"/api/memory/{tischler['id']}", headers=AUTH).json()
    assert "newer note; suggested to keep" in newer["versions"][0]["reason"]
    assert "suggested" not in older["versions"][0]["reason"]


def test_a_merge_whose_result_lacks_a_value_of_a_merged_note_is_a_conflict(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    _two_jobs(client, node)
    node.answer_fn = model(
        consolidation=lambda n: {
            "merge": [
                {
                    "keep": n["Arbeitet als Bäcker"],
                    "drop": [n["Arbeitet als Tischler"]],
                    "text": "Arbeitet als Tischler",  # neutral reason, but the baker would be gone
                    "reason": "same fact",
                }
            ]
        }
    )
    run_consolidation(client)
    assert {n["status"] for n in _states(client).values()} == {"disputed"}
    assert "Arbeitet als Bäcker" in _states(client)


def test_a_merge_without_a_text_cannot_swallow_a_note_with_another_value(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    _two_jobs(client, node)
    node.answer_fn = model(
        consolidation=lambda n: {
            "merge": [
                {"keep": n["Arbeitet als Bäcker"], "drop": [n["Arbeitet als Tischler"]]},
            ]
        }
    )
    run_consolidation(client)
    assert {n["status"] for n in _states(client).values()} == {"disputed"}


def test_a_correction_that_copies_another_notes_fact_is_a_conflict(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    _two_jobs(client, node)
    node.answer_fn = model(
        consolidation=lambda n: {
            "correct": [
                {
                    "id": n["Arbeitet als Bäcker"],
                    "text": "Arbeitet als Tischler",
                    "reason": "outdated",
                }
            ]
        }
    )
    run = run_consolidation(client)
    assert run["stats"]["corrected"] == 0 and run["stats"]["disputed"] == 1
    assert set(_states(client)) == {"Arbeitet als Bäcker", "Arbeitet als Tischler"}


def test_a_restated_fact_is_a_confirmation_not_a_second_note(client: TestClient, connect) -> None:
    """The same note again confirms it. (Another wording of it is no longer matched by slot
    words, D124: the consolidation merges such duplicates.)"""
    node = setup_node(client, connect)
    _remember(client, node, "Hund heißt Bello", "Bello")
    node.answer_fn = model({"Rufname": [note("Hund heißt Bello")]})
    events = chat_once(client, "Der Rufname meines Hundes ist Bello, wie gesagt.")
    assert events[-1][1]["remembered"] == [] and events[-1][1]["confirmed"]
    [only] = memory_items(client)
    assert only["text"] == "Hund heißt Bello" and only["confirmed_count"] == 1


def test_real_duplicates_still_merge_and_keep_their_wording(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    client.app.state.memory.embedder = SameForAll()
    _remember(client, node, "Trinkt gern Espresso", "Espresso")
    node.answer_fn = model({"Kaffeesorte": [note("Espresso trinkt er gern")]})
    chat_once(client, "Meine Kaffeesorte ist Espresso, wie gesagt.")
    node.answer_fn = model(
        consolidation=lambda n: {
            "merge": [
                {
                    "keep": n["Trinkt gern Espresso"],
                    "drop": [n["Espresso trinkt er gern"]],
                    "text": "Lebt und trinkt Espresso",  # awkward rewording: ignored
                    "reason": "same fact",
                }
            ]
        }
    )
    run = run_consolidation(client)
    assert run["stats"]["merged"] == 1 and run["stats"]["disputed"] == 0
    [active] = memory_items(client, status="active")
    assert active["text"] == "Trinkt gern Espresso"  # the kept note keeps its own wording
    [gone] = memory_items(client, status="superseded")
    assert gone["text"] == "Espresso trinkt er gern"


def test_a_merge_that_adds_information_may_rewrite_the_kept_note(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    _remember(client, node, "Wohnt in Hamburg", "Hamburg")
    node.answer_fn = model({"Elbe": [note("Lebt in Hamburg an der Elbe")]})
    chat_once(client, "Ich lebe direkt an der Elbe, in Hamburg.")
    node.answer_fn = model(
        consolidation=lambda n: {
            "merge": [
                {
                    "keep": n["Wohnt in Hamburg"],
                    "drop": [n["Lebt in Hamburg an der Elbe"]],
                    "text": "Wohnt in Hamburg an der Elbe",
                    "reason": "same fact, more precise",
                }
            ]
        }
    )
    run_consolidation(client)
    assert [i["text"] for i in memory_items(client, status="active")] == [
        "Wohnt in Hamburg an der Elbe"
    ]


# --- a contradiction in the conversation is disputed at once -----------------------------------


def extraction_seeing_existing(
    trigger: str, text: str, contradicts_text: str | None, inferred: bool = False
) -> Callable[[dict[str, Any]], str | None]:
    """A fake extraction model that reads the numbered "Existing notes" it is given."""

    def answer(req: dict[str, Any]) -> str | None:
        if req.get("stream") or req["messages"][0]["content"] != EXTRACTION_SYSTEM:
            return None
        user = req["messages"][-1]["content"]
        if trigger not in user:
            return json.dumps({"memories": []})
        numbers = {
            m.group(2): int(m.group(1)) for m in re.finditer(r"^(\d+)\. (.*)$", user, re.MULTILINE)
        }
        entry = note(text, inferred=inferred)
        if contradicts_text is not None:
            entry["contradicts"] = [numbers.get(contradicts_text, 99)]
        return json.dumps({"memories": [entry]})

    return answer


def test_a_plain_contradiction_is_disputed_at_once_with_one_question(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    hamburg = _remember(client, node, "Wohnt in Hamburg", "Hamburg")
    node.answer_fn = extraction_seeing_existing("Berlin", "Wohnt in Berlin", "Wohnt in Hamburg")
    events = chat_once(client, "Ich wohne in Berlin, nur damit du es weißt.")
    prompt = "\n".join(m["content"] for m in sent_messages(node))
    assert "- Wohnt in Hamburg — the user said it" in prompt  # the note was in front of the model

    mem = events[-1][1]
    [new] = mem["remembered"]
    assert new["text"] == "Wohnt in Berlin"
    assert new["contradicts"] == [{"id": hamburg["id"], "text": "Wohnt in Hamburg"}]
    assert mem["questions"] == 1 and mem["corrected"] == []
    notes = _states(client)
    assert notes["Wohnt in Berlin"]["status"] == notes["Wohnt in Hamburg"]["status"] == "disputed"
    assert notes["Wohnt in Berlin"]["disputed_with"] == hamburg["id"]
    assert notes["Wohnt in Hamburg"]["disputed_with"] == new["id"]
    old = client.get(f"/api/memory/{hamburg['id']}", headers=AUTH).json()
    assert old["versions"][0]["actor"] == "chat" and "contradicts" in old["versions"][0]["reason"]
    fresh = client.get(f"/api/memory/{new['id']}", headers=AUTH).json()
    assert "the newer note" in fresh["versions"][0]["reason"]

    # Undo of the new note ends the dispute: the old note is active again.
    assert client.delete(f"/api/memory/{new['id']}", headers=AUTH).json()["status"] == "deleted"
    assert _states(client)["Wohnt in Hamburg"]["status"] == "active"
    assert client.get("/api/memory/questions", headers=AUTH).json() == []


def test_a_statement_that_says_it_replaces_a_note_corrects_it_at_once(
    client: TestClient, connect
) -> None:
    """D124: the person's latest statement wins over their earlier one (the hub settles it, as
    live by default); no cue word is needed and none is read."""
    client.app.state.settings.memory_auto_settle = True
    node = setup_node(client, connect)
    hamburg = _remember(client, node, "Wohnt in Hamburg", "Hamburg")
    node.answer_fn = extraction_seeing_existing("Berlin", "Wohnt in Berlin", "Wohnt in Hamburg")
    events = chat_once(client, "Ich wohne in Berlin.")
    assert events[-1][1]["questions"] == 0
    assert [i["text"] for i in memory_items(client, status="active")] == ["Wohnt in Berlin"]
    [gone] = memory_items(client, status="superseded")
    assert gone["text"] == "Wohnt in Hamburg"
    old = client.get(f"/api/memory/{hamburg['id']}", headers=AUTH).json()
    assert (
        old["versions"][0]["actor"] == "chat" and "the latest word" in old["versions"][0]["reason"]
    )
    # the decision is a version: reverting it brings the old note back
    r = client.post(f"/api/memory/{hamburg['id']}/revert", headers=AUTH, json={"version": 1})
    assert r.status_code == 200 and r.json()["status"] == "active"


def test_a_claimed_contradiction_between_paraphrases_or_unknown_numbers_is_ignored(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    _remember(client, node, "Wohnt in Hamburg", "Hamburg")
    node.answer_fn = extraction_seeing_existing("Elbe", "Lebt in Hamburg", "Wohnt in Hamburg")
    chat_once(client, "Ich lebe in Hamburg, an der Elbe, wie gesagt.")  # same value: no dispute
    assert {i["status"] for i in memory_items(client)} == {"active"}
    node.answer_fn = extraction_seeing_existing(
        "Tischlerei", "Arbeitet als Tischler", "no such note"
    )
    chat_once(client, "Ich arbeite in einer Tischlerei, ganz kurz.")  # number out of range
    assert {i["status"] for i in memory_items(client)} == {"active"}
    assert "Arbeitet als Tischler" in _states(client)


def test_two_residences_are_a_conflict_even_when_the_model_names_no_note(
    client: TestClient, connect
) -> None:
    """The slot decides, not the model: "Kiel" and "Hamburg" share no words."""
    node = setup_node(client, connect)
    _remember(client, node, "Wohnt in Hamburg", "Hamburg")
    node.answer_fn = model({"Kiel": [note("Wohnt in Kiel")]})
    events = chat_once(client, "Ich wohne in Kiel, nur zur Info.")
    assert events[-1][1]["questions"] == 1
    assert {n["status"] for n in _states(client).values()} == {"disputed"}


def test_near_duplicates_never_swallow_a_different_value(client: TestClient, connect) -> None:
    """With a good embedder "Lebt in Berlin" is close to "Lebt in Hamburg": merging by
    similarity alone would lose the new fact."""
    node = setup_node(client, connect)
    service = client.app.state.memory

    class SameForAll:
        id = "same"
        dup_threshold = 0.9
        recall_min = 0.0
        cluster_threshold = 0.3

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0, 0.0] for _ in texts]

    service.embedder = SameForAll()
    _remember(client, node, "Lebt in Hamburg", "Hamburg")
    _remember(client, node, "Lebt in Berlin", "Berlin")  # similarity 1.0, other value: kept
    _remember(client, node, "Lebt in Hamburg an der Elbe", "Elbe")  # adds a value: kept, too
    node.answer_fn = model({"wohne": [note("Wohnt in Hamburg")]})  # nothing new: confirmed
    events = chat_once(client, "Ich wohne in Hamburg, nur zur Info.")
    assert events[-1][1]["remembered"] == [] and events[-1][1]["confirmed"]
    assert set(_states(client)) == {
        "Lebt in Hamburg",
        "Lebt in Berlin",
        "Lebt in Hamburg an der Elbe",
    }


# --- questions about the user get the important notes ------------------------------------------


def test_questions_about_the_user_get_their_important_notes_when_the_search_finds_none(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    _remember(client, node, "Arbeitet als Tischler", "Tischler")
    node.answer_fn = model({"Werkstatt": [note("Vermutet eine Erkältung", inferred=True)]})
    chat_once(client, "Ich glaube, ich habe eine Erkältung, Werkstatt hin oder her.")
    for question in ("Was weißt du über mich?", "kannst du mir sagen, wo ich wohne?"):
        events = chat_once(client, question)
        texts = [n["text"] for n in events[0][1]["memory"]]
        assert texts == ["Arbeitet als Tischler", "Vermutet eine Erkältung"], question
        assert "- Arbeitet als Tischler — the user said it" in sent_messages(node)[-1]["content"]
    # "doc" before "assumption", surest first; a message that is not about the user: nothing
    events = chat_once(client, "Erkläre mir bitte Quantenverschränkung kurz.")
    assert events[0][1]["memory"] == []


def test_the_fallback_notes_stay_within_the_budget_and_count(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    for i, word in enumerate(
        ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta", "Theta")
    ):
        # said in the user's own words: a statement ("doc"), not the model's reading of it
        node.answer_fn = model({word: [note(f"Kennt {word}nummer {i} sehr gut")]})
        chat_once(client, f"Ich kenne {word}nummer {i} sehr gut.")
    events = chat_once(client, "Was weißt du alles über mich?")
    assert len(events[0][1]["memory"]) == 6  # MAX_RECALLED
    assert events[0][1]["memory"][0]["evidence"] == "doc"


def test_memory_off_gives_no_fallback_notes(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    _remember(client, node, "Arbeitet als Tischler", "Tischler")
    client.put("/api/chat/settings", headers=AUTH, json={"memory_enabled": False})
    assert chat_once(client, "Was weißt du über mich?")[0][1]["memory"] == []


# --- the secret filter names its rule ----------------------------------------------------------


def test_the_log_names_the_deterministic_rule_even_when_the_model_also_flagged_the_note(
    client: TestClient, connect, caplog: Any
) -> None:
    """Hardware test M3: the log only said "model_flag" for the password, the IBAN and the token."""
    node = setup_node(client, connect)
    node.answer_fn = model(
        {
            "WLAN": [
                note("Das WLAN-Passwort ist Sommer2024!", secret=True),
                note("Die IBAN ist DE89 3704 0044 0532 0130 00", secret=True),
                note("Mein Token ist ghp_abcdefghijklmnopqrstuvwxyz0123456789", secret=True),
                note("Hat ein Konto beim Onkel", secret=True),  # only the model flags this one
            ]
        }
    )
    with caplog.at_level("WARNING"):
        events = chat_once(client, "Das WLAN-Passwort ist Sommer2024!, IBAN und Token stehen oben.")
    assert events[-1][1]["blocked"] == 4 and events[-1][1]["remembered"] == []
    text = caplog.text
    for rule in ("(password)", "(iban)", "(api_key)", "(model_flag)"):
        assert f"refused a note {rule}" in text, rule
    assert text.count("(model_flag)") == 1  # not one per note
    assert "Sommer2024" not in text and "DE89" not in text and "ghp_" not in text

    async def reasons() -> list[str]:
        from sqlalchemy import text as sql

        async with client.app.state.sessionmaker() as db:
            rows = await db.execute(sql("SELECT reason FROM memory_blocks ORDER BY id"))
            return [r.reason for r in rows]

    assert client.portal.call(reasons) == ["password", "iban", "api_key", "model_flag"]


# --- a note is confirmed only when the user's own message says it ------------------------------


def echoing_model(notes: list[str]) -> Callable[[dict[str, Any]], str | None]:
    """A fake extraction model that repeats existing notes whatever the user said (the
    hardware test: it did so on "Was weißt du über mich?")."""

    def answer(req: dict[str, Any]) -> str | None:
        if req.get("stream") or req["messages"][0]["content"] != EXTRACTION_SYSTEM:
            return None
        return json.dumps({"memories": [note(t) for t in notes]})

    return answer


def test_questions_and_notes_in_the_prompt_confirm_nothing(client: TestClient, connect) -> None:
    """Hardware test: "confirmed again" on every answer that merely used the notes, on
    "Was weißt du über mich?" and on a message about passwords; "Lebt in Hamburg" reached v7."""
    node = setup_node(client, connect)
    hamburg = _remember(client, node, "Lebt in Hamburg", "Hamburg")
    node.answer_fn = echoing_model(["Lebt in Hamburg", "Wohnt in Hamburg"])
    for text in (
        "Was weißt du über mich?",
        "Kannst du mir sagen, wo ich wohne?",
        "Mein WLAN-Passwort sollte ich bald ändern, kannst du mir Tipps geben?",
        "Wie ist das Wetter in Kiel morgen, weißt du das?",
    ):
        events = chat_once(client, text)
        assert events[-1][0] == "done", text  # no "confirmed" line, nothing stored
    [item] = memory_items(client)
    assert item["id"] == hamburg["id"] and item["text"] == "Lebt in Hamburg"
    assert item["version"] == 1 and item["confirmed_count"] == 0
    assert item["last_confirmed_at"] is None


def test_a_disputed_pair_is_never_confirmed_in_one_go(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    _remember(client, node, "Lebt in Hamburg", "Hamburg")
    _remember(client, node, "Lebt in Berlin", "Berlin")
    node.answer_fn = model(
        consolidation=lambda n: {
            "conflict": [{"a": n["Lebt in Hamburg"], "b": n["Lebt in Berlin"], "reason": "homes"}]
        }
    )
    run_consolidation(client)
    assert {i["status"] for i in memory_items(client)} == {"disputed"}
    # the model repeats both notes although the user said neither
    node.answer_fn = echoing_model(["Lebt in Hamburg", "Lebt in Berlin"])
    events = chat_once(client, "Wo wohne ich eigentlich, in Hamburg oder in Berlin?")
    assert events[-1][0] == "done"
    assert {i["confirmed_count"] for i in memory_items(client)} == {0}
    # even if the message names both (a question that lists them), only one is confirmed
    node.answer_fn = echoing_model(["Lebt in Hamburg", "Lebt in Berlin"])
    events = chat_once(client, "Ich lebe in Hamburg und ich lebe in Berlin, ehrlich gesagt.")
    counts = sorted(i["confirmed_count"] for i in memory_items(client))
    assert counts == [0, 1]
    assert len(events[-1][1]["confirmed"]) == 1


def test_two_notes_that_only_differ_in_wording_are_merged_not_disputed(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    _, b = _flag_pair(client, node, {"relation": "same", "keep": 2})
    assert live_texts(client) == [b]
    assert client.get("/api/memory/questions", headers=AUTH).json() == []


def test_a_real_contradiction_asks_the_concrete_question(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    question = "Läuft die Nachprüfung auf node-a oder auf node-b?"
    _flag_pair(client, node, {"relation": "contradict", "question": question})
    assert {i["status"] for i in memory_items(client)} == {"disputed"}
    [asked] = client.get("/api/memory/questions", headers=AUTH).json()
    assert asked["question"] == question and len(asked["candidates"]) == 2


def test_without_a_trustworthy_answer_the_old_question_stays(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    _flag_pair(client, node, None)  # the model says nothing usable
    [asked] = client.get("/api/memory/questions", headers=AUTH).json()
    assert asked["question"] is None and len(asked["candidates"]) == 2


def test_a_same_verdict_never_merges_notes_that_name_other_numbers(
    client: TestClient, connect
) -> None:
    """D124: no slot decides any more what cannot both be true; the model's verdict stands, but a
    note whose numbers the kept one lacks is never merged away (live 2026-11-21: agent limits
    10 000 / 16 384)."""
    node = setup_node(client, connect)
    _remember(client, node, "Das Agent-Limit ist 10000 Tokens", "Limit")
    _remember(client, node, "Das Agent-Limit ist 16384 Tokens", "Grenze")
    node.answer_fn = with_compare(
        model(
            consolidation=lambda n: {
                "conflict": [
                    {
                        "a": n["Das Agent-Limit ist 10000 Tokens"],
                        "b": n["Das Agent-Limit ist 16384 Tokens"],
                        "reason": "limits",
                    }
                ]
            }
        ),
        {"relation": "same", "keep": 2},  # a model that wrongly calls two limits the same
    )
    run_consolidation(client)
    assert {i["status"] for i in memory_items(client)} == {"disputed"}


def test_questions_that_are_open_already_are_looked_at_again(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    a, b = _flag_pair(client, node, None)
    assert len(client.get("/api/memory/questions", headers=AUTH).json()) == 1
    # the next run compares them: other words only, so the question closes and one note stays
    node.answer_fn = with_compare(model(), {"relation": "refines", "keep": 1})
    run = run_consolidation(client)
    assert client.get("/api/memory/questions", headers=AUTH).json() == []
    assert live_texts(client) == [a] and run["stats"]["resolved"] >= 1
    assert b not in live_texts(client)


def test_a_restated_note_that_the_user_never_said_is_dropped_not_added(
    client: TestClient, connect
) -> None:
    """A paraphrase of a note from the prompt is no new fact either."""
    node = setup_node(client, connect)
    _remember(client, node, "Wohnt in Hamburg an der Elbe", "Elbe")
    node.answer_fn = echoing_model(["Lebt in Hamburg an der Elbe"])
    chat_once(client, "Was weißt du über mich?")
    assert [i["text"] for i in memory_items(client)] == ["Wohnt in Hamburg an der Elbe"]


def test_the_memory_page_and_a_revert_do_not_count_as_confirmations(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    item = _remember(client, node, "Wohnt in Hamburg", "Hamburg")
    client.patch(f"/api/memory/{item['id']}", headers=AUTH, json={"text": "Wohnt in Altona"})
    client.post(f"/api/memory/{item['id']}/revert", headers=AUTH, json={"version": 1})
    [now] = memory_items(client)
    assert now["version"] == 3 and now["confirmed_count"] == 0
