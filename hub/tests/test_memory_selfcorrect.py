"""The memory keeps itself consistent (D19 extended): explicit corrections replace, "that was a
test" withdraws, everything else that contradicts becomes one clarification question, the
answer is applied like a statement, and the assistant never claims to be sure about a note
that is in dispute."""

import json
import re
import uuid
from collections.abc import Sequence
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import FakeNode
from tests.test_chat import ask, new_conv, sent_messages
from tests.test_memory import (
    CONSOLIDATION_PREFIX,
    _user_part,
    chat_once,
    memory_items,
    note,
    run_consolidation,
    setup_node,
)
from topcoat_hub.memory import CLARIFY_SYSTEM, EXTRACTION_SYSTEM
from topcoat_hub.memory_clarify import Evidence


def says(replies: dict[str, dict[str, Any]], clarify: Any = None) -> Any:
    """A fake memory model: the JSON reply for the first keyword in the user's message;
    ``clarify`` (a function of the prompt) answers the "which note is true" question, by
    default with no winner. The prompts it was asked are in ``answer.asked``."""
    asked: list[str] = []

    def answer(req: dict[str, Any]) -> str | None:
        if req.get("stream"):
            return None
        system, user = req["messages"][0]["content"], req["messages"][-1]["content"]
        if system == EXTRACTION_SYSTEM:
            message = _user_part(user)
            for keyword, reply in replies.items():
                if keyword in message:
                    return json.dumps(reply)
            return json.dumps({"memories": []})
        if system.startswith(CONSOLIDATION_PREFIX):
            return json.dumps({})
        if system == CLARIFY_SYSTEM:
            asked.append(user)
            return json.dumps(clarify(user) if clarify else {"winner": None})
        return None

    answer.asked = asked  # type: ignore[attr-defined]
    return answer


LIVES = {
    "Hamburg": {"memories": [note("Lebt in Hamburg")]},
    "Berlin": {"memories": [note("Lebt in Berlin")]},
    "Kiel": {"memories": [note("Lebt in Kiel")]},
    "musterstadt": {"memories": [note("Lebt in Musterstadt")]},
}


def states(client: TestClient) -> dict[str, str]:
    """Every note with its status, the deleted ones included."""
    return {
        i["text"]: i["status"]
        for status in ("active", "disputed", "superseded", "deleted")
        for i in memory_items(client, status=status)
    }


def questions(client: TestClient) -> list[dict[str, Any]]:
    r = client.get("/api/memory/questions", headers=AUTH)
    assert r.status_code == 200
    return r.json()


def disputed_pair(client: TestClient, connect: Any) -> FakeNode:
    """The hardware situation: two plain statements about the residence, both in dispute."""
    node = setup_node(client, connect)
    node.answer_fn = says(LIVES)
    chat_once(client, "Ich wohne in Hamburg, nur zur Info.")
    chat_once(client, "Ich wohne in Berlin, nur zur Info.")
    assert states(client) == {"Lebt in Hamburg": "disputed", "Lebt in Berlin": "disputed"}
    assert len(questions(client)) == 1
    return node


# --- the case of the hardware test -------------------------------------------------------------


def test_a_new_residence_with_the_old_ones_called_a_test_ends_the_dispute(
    client: TestClient, connect
) -> None:
    node = disputed_pair(client, connect)
    conv = new_conv(client)
    events = chat_once(
        client,
        "ich wohne tatsächlich in musterstadt, die beiden anderen Städte waren zum testen",
        conv,
    )
    mem = events[-1][1]
    [fix] = mem["corrected"]
    assert fix["text"] == "Lebt in Musterstadt" and fix["withdrawn"] is True
    assert {r["text"] for r in fix["replaced"]} == {"Lebt in Hamburg", "Lebt in Berlin"}

    # Neither city is active or disputed any more, the question is closed, nothing is asked.
    assert states(client) == {
        "Lebt in Musterstadt": "active",
        "Lebt in Hamburg": "deleted",
        "Lebt in Berlin": "deleted",
    }
    assert questions(client) == []

    # Traceable: version with reason and source.
    for gone in memory_items(client, status="deleted"):
        detail = client.get(f"/api/memory/{gone['id']}", headers=AUTH).json()
        last = detail["versions"][0]
        assert last["actor"] == "chat" and "wrong or a test" in last["reason"]
        assert last["source"]["conversation_id"] == conv and last["source"]["evidence"]

    # The next chat recalls only the true note.
    events = chat_once(client, "Wo wohne ich eigentlich?")
    assert [n["text"] for n in events[0][1]["memory"]] == ["Lebt in Musterstadt"]
    system = "\n".join(m["content"] for m in sent_messages(node))
    assert "Musterstadt" in system and "Hamburg" not in system and "Berlin" not in system

    # ... and it is reversible: undo brings both cities back (in dispute again is not needed:
    # the versions restore their earlier state).
    assert client.post(f"/api/memory/{fix['id']}/undo", headers=AUTH).status_code == 200
    assert "Lebt in Musterstadt" not in states(client) or (
        states(client)["Lebt in Musterstadt"] == "deleted"
    )
    assert {"Lebt in Hamburg", "Lebt in Berlin"} <= {
        i["text"] for i in memory_items(client, status="active")
    } | {i["text"] for i in memory_items(client, status="disputed")}


def test_a_correction_word_replaces_the_older_note_without_a_question(
    client: TestClient, connect
) -> None:
    node = setup_node(client, connect)
    node.answer_fn = says(LIVES)
    chat_once(client, "Ich wohne in Hamburg, nur zur Info.")
    events = chat_once(client, "Ich wohne inzwischen in Kiel.")
    mem = events[-1][1]
    assert mem["questions"] == 0 and [c["text"] for c in mem["corrected"]] == ["Lebt in Kiel"]
    assert states(client) == {"Lebt in Kiel": "active", "Lebt in Hamburg": "superseded"}
    assert questions(client) == []


def test_the_model_can_retract_a_note_the_user_calls_wrong(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    node.answer_fn = says(LIVES)
    chat_once(client, "Ich wohne in Hamburg, nur zur Info.")
    # the model reads the numbered "Existing notes" and names note 1 in "retract"
    node.answer_fn = says({"falsch": {"memories": [], "retract": [1]}})
    events = chat_once(client, "Das mit Hamburg war falsch, ich habe dich veräppelt.")
    assert [r["text"] for r in events[-1][1]["retracted"]] == ["Lebt in Hamburg"]
    assert states(client) == {"Lebt in Hamburg": "deleted"}
    # without a cue in the user's message a retraction by the model alone is not applied
    node.answer_fn = says(LIVES)
    chat_once(client, "Ich wohne in Berlin, nur zur Info.")
    node.answer_fn = says({"Berlin": {"memories": [], "retract": [1]}})
    events = chat_once(client, "Kennst du Berlin?")
    assert events[-1][1].get("retracted", []) == []
    assert states(client)["Lebt in Berlin"] == "active"


# --- clarification questions --------------------------------------------------------------------


def test_the_question_names_the_notes_with_their_dates(client: TestClient, connect) -> None:
    disputed_pair(client, connect)
    [q] = questions(client)
    assert q["status"] == "open" and q["topic"] == "residence"
    assert [c["text"] for c in q["candidates"]] == ["Lebt in Hamburg", "Lebt in Berlin"]
    assert all(c["at"] for c in q["candidates"])
    overview = client.get("/api/memory", headers=AUTH).json()
    assert [x["id"] for x in overview["questions"]] == [q["id"]]


def test_answering_with_a_note_settles_the_dispute(client: TestClient, connect) -> None:
    disputed_pair(client, connect)
    [q] = questions(client)
    berlin = next(c for c in q["candidates"] if c["text"] == "Lebt in Berlin")
    r = client.post(
        f"/api/memory/questions/{q['id']}/answer", headers=AUTH, json={"note_id": berlin["id"]}
    )
    assert r.status_code == 200, r.text
    assert states(client) == {"Lebt in Berlin": "active", "Lebt in Hamburg": "superseded"}
    assert questions(client) == []
    detail = client.get(f"/api/memory/{berlin['id']}", headers=AUTH).json()
    assert detail["versions"][0]["actor"] == "user"
    assert detail["versions"][0]["source"]["question_id"] == q["id"]
    assert detail["confirmed_count"] == 1
    # an answered question can not be answered twice
    again = client.post(
        f"/api/memory/questions/{q['id']}/answer", headers=AUTH, json={"none": True}
    )
    assert again.status_code == 404


def test_answering_none_withdraws_all_candidates(client: TestClient, connect) -> None:
    disputed_pair(client, connect)
    [q] = questions(client)
    r = client.post(f"/api/memory/questions/{q['id']}/answer", headers=AUTH, json={"none": True})
    assert r.status_code == 200, r.text
    assert set(states(client).values()) == {"deleted"}
    assert questions(client) == []


def test_answering_with_a_text_stores_it_as_a_statement_of_the_user(
    client: TestClient, connect
) -> None:
    disputed_pair(client, connect)
    [q] = questions(client)
    url = f"/api/memory/questions/{q['id']}/answer"
    assert client.post(url, headers=AUTH, json={}).status_code == 422  # nothing chosen
    secret = client.post(url, headers=AUTH, json={"text": "Passwort: abc12345"})
    assert secret.status_code == 422 and secret.json()["detail"]["code"] == "secret_blocked"
    assert client.get("/api/memory", headers=AUTH).json()["blocked"] == 1
    unknown = client.post(
        url, headers=AUTH, json={"note_id": "00000000-0000-4000-8000-0000000000aa"}
    )
    assert unknown.status_code == 422
    r = client.post(url, headers=AUTH, json={"none": True, "text": "Lebt in Musterstadt"})
    assert r.status_code == 200, r.text
    assert states(client) == {
        "Lebt in Musterstadt": "active",
        "Lebt in Hamburg": "superseded",
        "Lebt in Berlin": "superseded",
    }
    assert questions(client) == []


def test_a_dismissed_question_is_not_asked_again_until_something_changes(
    client: TestClient, connect
) -> None:
    node = disputed_pair(client, connect)
    [q] = questions(client)
    assert client.post(f"/api/memory/questions/{q['id']}/dismiss", headers=AUTH).status_code == 200
    assert questions(client) == []
    assert set(states(client).values()) == {"disputed"}  # the doubt stays, the asking stops
    run = run_consolidation(client)
    assert run["stats"]["questions"] == 0 and questions(client) == []
    # a third city is new: the group is wider, so it is asked again
    node.answer_fn = says(LIVES)
    chat_once(client, "Ich wohne in Kiel, nur zur Info.")
    [again] = questions(client)
    assert len(again["candidates"]) == 3


def test_repeating_a_disputed_note_settles_it(client: TestClient, connect) -> None:
    node = disputed_pair(client, connect)
    node.answer_fn = says(LIVES)
    events = chat_once(client, "Ja, ich wohne in Hamburg.")
    assert [c["text"] for c in events[-1][1]["corrected"]] == ["Lebt in Hamburg"]
    assert states(client) == {"Lebt in Hamburg": "active", "Lebt in Berlin": "superseded"}
    assert questions(client) == []


# --- tidy-up ------------------------------------------------------------------------------------


def test_the_tidy_up_settles_what_became_clear_and_keeps_asking_about_the_rest(
    client: TestClient, connect
) -> None:
    node = disputed_pair(client, connect)
    run = run_consolidation(client)
    # nothing new: the doubt stays and the question is still there; the model read the user's
    # own messages once and found nothing that corrects (no winner)
    assert run["stats"]["resolved"] == 0 and run["stats"]["questions"] == 1
    assert run["stats"]["tokens"] > 0
    assert set(states(client).values()) == {"disputed"}
    versions_before = {
        i["id"]: client.get(f"/api/memory/{i['id']}", headers=AUTH).json()["version"]
        for i in memory_items(client)
    }
    again = run_consolidation(client)
    assert again["stats"]["tokens"] == 0  # the same doubt is not put to the model again
    versions_after = {
        i["id"]: client.get(f"/api/memory/{i['id']}", headers=AUTH).json()["version"]
        for i in memory_items(client)
    }
    assert versions_before == versions_after  # a repeated look is not a new version

    # New evidence: the user said one of them again (a confirmation newer than the rest).
    berlin = next(i for i in memory_items(client) if i["text"] == "Lebt in Berlin")
    client.portal.call(lambda: _confirm(client, berlin["id"]))
    run = run_consolidation(client)
    assert run["stats"]["resolved"] == 1 and run["stats"]["questions"] == 0
    assert states(client) == {"Lebt in Berlin": "active", "Lebt in Hamburg": "superseded"}
    assert node is not None


async def _confirm(client: TestClient, item_id: str) -> None:
    import uuid

    from topcoat_hub.memory_store import MemoryStore

    async with client.app.state.sessionmaker() as db:
        await MemoryStore(db, uuid.UUID("00000000-0000-4000-8000-000000000001")).confirm(
            uuid.UUID(item_id)
        )
        await db.commit()


def test_a_disputed_note_is_active_again_when_its_rival_is_gone(
    client: TestClient, connect
) -> None:
    disputed_pair(client, connect)
    berlin = next(i for i in memory_items(client) if i["text"] == "Lebt in Berlin")
    assert client.delete(f"/api/memory/{berlin['id']}", headers=AUTH).status_code == 200
    run_consolidation(client)
    assert states(client) == {"Lebt in Hamburg": "active", "Lebt in Berlin": "deleted"}
    assert questions(client) == []


class DecisiveCheck:
    """A later evidence source (knowledge base, web, tool): it knows which note is true."""

    name = "check"

    def __init__(self, text: str) -> None:
        self.text = text

    async def gather(self, notes: Sequence[Any]) -> list[Evidence]:
        return [
            Evidence(n.id, "check", n.created_at, "verified elsewhere", decisive_for=n.id)
            for n in notes
            if n.text == self.text
        ]


def test_a_decisive_evidence_source_settles_a_conflict_without_the_user(
    client: TestClient, connect
) -> None:
    disputed_pair(client, connect)
    service = client.app.state.memory
    service.evidence_sources = [DecisiveCheck("Lebt in Hamburg")]
    run = run_consolidation(client)
    assert run["stats"]["resolved"] == 1
    assert states(client) == {"Lebt in Hamburg": "active", "Lebt in Berlin": "superseded"}
    berlin = next(i for i in memory_items(client, status="superseded"))
    last = client.get(f"/api/memory/{berlin['id']}", headers=AUTH).json()["versions"][0]
    assert "checked (check)" in last["reason"] and "verified elsewhere" in last["reason"]
    assert last["actor"] == "consolidation"


# --- honest certainty ----------------------------------------------------------------------------


def test_the_prompt_carries_status_and_confirmations_and_follow_ups_keep_the_notes(
    client: TestClient, connect
) -> None:
    node = disputed_pair(client, connect)
    conv = new_conv(client)
    chat_once(client, "Wo wohne ich, in Hamburg oder in Berlin?", conv)
    first = sent_messages(node)
    assert first[-1]["content"].count(" — disputed: contradicts another note") == 2
    assert "never claim to be sure about those" in first[0]["content"]

    # "How sure are you?" has nothing in it that finds a note: the last answer's are re-supplied.
    status, events, _ = ask(client, conv, "Wie sicher bist du dir?")
    assert status == 200
    system = sent_messages(node)[-1]["content"]
    assert system.count(" — disputed:") >= 2 and "Hamburg" in system and "Berlin" in system
    assert {n["text"] for n in events[0][1]["memory"]} == {"Lebt in Hamburg", "Lebt in Berlin"}


def test_confirmations_are_shown_to_the_model(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    node.answer_fn = says(LIVES)
    chat_once(client, "Ich wohne in Hamburg, nur zur Info.")
    chat_once(client, "Ja, in Hamburg wohne ich seit Jahren.")
    chat_once(client, "Wo wohne ich denn, in Hamburg?")
    assert (
        "- Lebt in Hamburg — the user said it, confirmed once" in sent_messages(node)[-1]["content"]
    )


# --- a dispute never outlives its opponents ---------------------------------------------------


def trio(client: TestClient, connect: Any) -> FakeNode:
    """Three plain statements about the residence: all disputed, one question with 3 notes."""
    node = setup_node(client, connect)
    node.answer_fn = says({**LIVES, "Musterstadt": {"memories": [note("Lebt in Musterstadt")]}})
    for city in ("Hamburg", "Berlin", "Musterstadt"):
        chat_once(client, f"Ich wohne in {city}, nur zur Info.")
    assert set(states(client).values()) == {"disputed"}
    [q] = questions(client)
    assert len(q["candidates"]) == 3
    return node


def note_history(client: TestClient, text: str) -> list[dict[str, Any]]:
    item = next(i for i in memory_items(client) if i["text"] == text)
    return client.get(f"/api/memory/{item['id']}", headers=AUTH).json()["versions"]


def test_retracting_all_opponents_in_a_chat_leaves_the_last_note_active(
    client: TestClient, connect
) -> None:
    """Hardware test: after retracting Hamburg and Berlin, "Lebt in Musterstadt" stayed
    disputed and the model called the information contradictory to itself."""
    node = trio(client, connect)

    def retract_hamburg_and_berlin(req: dict[str, Any]) -> str | None:
        if req.get("stream") or req["messages"][0]["content"] != EXTRACTION_SYSTEM:
            return None
        user = req["messages"][-1]["content"]
        numbers = [
            int(m.group(1))
            for m in re.finditer(r"^(\d+)\. (.*)$", user, re.MULTILINE)
            if "Hamburg" in m.group(2) or "Berlin" in m.group(2)
        ]
        return json.dumps({"memories": [], "retract": numbers})

    node.answer_fn = retract_hamburg_and_berlin
    events = chat_once(client, "Hamburg und Berlin waren nur ein Test, das ist falsch.")
    assert {r["text"] for r in events[-1][1]["retracted"]} == {"Lebt in Hamburg", "Lebt in Berlin"}
    assert states(client) == {
        "Lebt in Musterstadt": "active",
        "Lebt in Hamburg": "deleted",
        "Lebt in Berlin": "deleted",
    }
    last = note_history(client, "Lebt in Musterstadt")[0]
    assert last["status"] == "active" and last["reason"].startswith("contradiction resolved")
    assert questions(client) == []  # nothing left to ask
    # and the prompt no longer calls it disputed
    chat_once(client, "Wo wohne ich?")
    system = "\n".join(m["content"] for m in sent_messages(node))
    assert "Musterstadt — the user said it" in system and " — disputed:" not in system


def test_deleting_opponents_in_the_gui_heals_step_by_step(client: TestClient, connect) -> None:
    trio(client, connect)
    notes = {i["text"]: i for i in memory_items(client)}
    assert (
        client.delete(f"/api/memory/{notes['Lebt in Hamburg']['id']}", headers=AUTH).status_code
        == 200
    )
    assert states(client)["Lebt in Berlin"] == states(client)["Lebt in Musterstadt"] == "disputed"
    assert (
        client.delete(f"/api/memory/{notes['Lebt in Berlin']['id']}", headers=AUTH).status_code
        == 200
    )
    assert states(client)["Lebt in Musterstadt"] == "active"
    assert note_history(client, "Lebt in Musterstadt")[0]["reason"].startswith(
        "contradiction resolved"
    )
    assert questions(client) == []


def test_answering_a_question_leaves_no_orphaned_dispute(client: TestClient, connect) -> None:
    trio(client, connect)
    [q] = questions(client)
    berlin = next(c for c in q["candidates"] if c["text"] == "Lebt in Berlin")
    r = client.post(
        f"/api/memory/questions/{q['id']}/answer", headers=AUTH, json={"note_id": berlin["id"]}
    )
    assert r.status_code == 200
    assert states(client) == {
        "Lebt in Berlin": "active",
        "Lebt in Hamburg": "superseded",
        "Lebt in Musterstadt": "superseded",
    }
    assert questions(client) == []


def test_undoing_a_correction_leaves_no_orphaned_dispute(client: TestClient, connect) -> None:
    node = setup_node(client, connect)
    node.answer_fn = says(LIVES)
    chat_once(client, "Ich wohne in Hamburg, nur zur Info.")
    chat_once(client, "Ich wohne in Berlin, nur zur Info.")  # disputed pair
    events = chat_once(client, "Ich wohne inzwischen in Kiel.")  # corrects both
    [fix] = events[-1][1]["corrected"]
    assert client.post(f"/api/memory/{fix['id']}/undo", headers=AUTH).status_code == 200
    # both are back; whatever state they are in, none is disputed without an opponent
    live = {i["text"]: i for i in memory_items(client)}
    for text in ("Lebt in Hamburg", "Lebt in Berlin"):
        assert live[text]["status"] in ("active", "disputed")
    if live["Lebt in Hamburg"]["status"] == "disputed":
        assert live["Lebt in Berlin"]["status"] == "disputed"
    else:
        assert live["Lebt in Berlin"]["status"] == "active"


def test_the_tidy_up_heals_a_disputed_note_whose_opponents_are_gone(
    client: TestClient, connect
) -> None:
    """The safety net: data as the older code left it (disputed, its rivals deleted)."""
    trio(client, connect)
    notes = {i["text"]: i for i in memory_items(client)}

    async def orphan() -> None:
        from topcoat_hub.memory_store import MemoryStore

        async with client.app.state.sessionmaker() as db:
            store = MemoryStore(db, uuid.UUID("00000000-0000-4000-8000-000000000001"))
            for text in ("Lebt in Hamburg", "Lebt in Berlin"):
                await store.change(
                    uuid.UUID(notes[text]["id"]), reason="x", actor="user", status="deleted"
                )
            await db.commit()

    client.portal.call(orphan)
    assert states(client)["Lebt in Musterstadt"] == "disputed"
    assert len(questions(client)) == 1  # left open by the old code
    run_consolidation(client)
    assert states(client)["Lebt in Musterstadt"] == "active"
    assert note_history(client, "Lebt in Musterstadt")[0]["reason"].startswith(
        "contradiction resolved"
    )
    assert questions(client) == []


# --- the source messages of the notes are the evidence -----------------------------------------


async def _say_instead(client: TestClient, starts_with: str, new_text: str) -> None:
    """Replace the stored user message a note came from (data as an older version left it)."""
    from sqlalchemy import update

    from topcoat_hub.models import Message

    async with client.app.state.sessionmaker() as db:
        await db.execute(
            update(Message)
            .where(Message.role == "user", Message.content.like(f"{starts_with}%"))
            .values(content=new_text)
        )
        await db.commit()


def test_a_correction_in_the_source_message_settles_the_doubt_and_is_quoted(
    client: TestClient, connect
) -> None:
    """Hardware test: the note came from "ich wohne tatsächlich in …, die beiden anderen
    Städte waren zum testen" and the tidy-up still only asked (resolved 0, questions 1)."""
    node = trio(client, connect)
    said = "ich wohne tatsächlich in musterstadt, die beiden anderen Städte waren zum testen"
    client.portal.call(lambda: _say_instead(client, "Ich wohne in Musterstadt", said))
    node.answer_fn = says(LIVES)
    run = run_consolidation(client)
    assert run["stats"]["resolved"] == 1 and run["stats"]["questions"] == 0
    assert node.answer_fn.asked == []  # type: ignore[attr-defined]  # clear words: no model asked
    assert states(client) == {
        "Lebt in Musterstadt": "active",
        "Lebt in Hamburg": "superseded",
        "Lebt in Berlin": "superseded",
    }
    reason = note_history(client, "Lebt in Hamburg")[0]["reason"]
    assert "corrected it" in reason and f"“{said}”" in reason  # quoted in the reason
    assert questions(client) == []


def test_the_model_reads_the_source_messages_before_a_question_is_asked(
    client: TestClient, connect
) -> None:
    node = trio(client, connect)
    said = "Ich wohne in Musterstadt. Die beiden anderen Städte habe ich nur erfunden."
    client.portal.call(lambda: _say_instead(client, "Ich wohne in Musterstadt", said))

    def clarify(prompt: str) -> dict[str, Any]:
        assert "the user's message: “" + said in prompt  # the evidence is in front of the model
        assert "Lebt in Hamburg" in prompt and "Lebt in Berlin" in prompt
        return {"winner": 3, "quote": "habe ich nur erfunden", "reason": "the user says so"}

    node.answer_fn = says(LIVES, clarify)
    run = run_consolidation(client)
    assert run["stats"]["resolved"] == 1 and run["stats"]["questions"] == 0
    assert run["stats"]["tokens"] > 0
    assert len(node.answer_fn.asked) == 1  # type: ignore[attr-defined]
    assert states(client) == {
        "Lebt in Musterstadt": "active",
        "Lebt in Hamburg": "superseded",
        "Lebt in Berlin": "superseded",
    }
    reason = note_history(client, "Lebt in Hamburg")[0]["reason"]
    assert "the model read the user's message" in reason and "“habe ich nur erfunden”" in reason


def test_a_model_verdict_needs_the_users_own_words_and_the_last_word(
    client: TestClient, connect
) -> None:
    node = trio(client, connect)
    said = "Ich wohne in Musterstadt. Die beiden anderen Städte habe ich nur erfunden."
    client.portal.call(lambda: _say_instead(client, "Ich wohne in Musterstadt", said))
    bad = [
        {"winner": 3, "quote": "das habe ich nie gesagt", "reason": "invented quote"},
        {"winner": 3, "quote": "", "reason": "no quote"},
        {"winner": 1, "quote": "habe ich nur erfunden", "reason": "an older note cannot win"},
        {"winner": 7, "quote": "habe ich nur erfunden", "reason": "no such note"},
        {"winner": None, "quote": None, "reason": "unsure"},
    ]
    for answer in bad:
        node.answer_fn = says(LIVES, lambda _p, a=answer: a)
        run = run_consolidation(client)
        assert run["stats"]["resolved"] == 0, answer
        assert set(states(client).values()) == {"disputed"}, answer
        assert len(questions(client)) == 1, answer
        # a changed doubt is asked again: say one of the notes again to change the signature
        note_id = next(i["id"] for i in memory_items(client) if i["text"] == "Lebt in Hamburg")
        client.portal.call(lambda i=note_id: _confirm(client, i))
        client.portal.call(lambda i=note_id: _unconfirm(client, i))


async def _unconfirm(client: TestClient, item_id: str) -> None:
    """Undo the time of a confirmation, keep the counter (a different signature per call)."""
    import uuid

    from topcoat_hub.memory_store import MemoryStore

    async with client.app.state.sessionmaker() as db:
        item = await MemoryStore(db, uuid.UUID("00000000-0000-4000-8000-000000000001")).get(
            uuid.UUID(item_id)
        )
        assert item is not None
        item.last_confirmed_at = None
        await db.commit()


def test_internal_markings_never_reach_the_answer_shown_or_stored(
    client: TestClient, connect
) -> None:
    """Hardware test: the 7B answered "… [doc] (confirmed 5x) hast aber einen Hund …". The
    fake node echoes the user's message as its answer, so the message carries the markings."""
    node = setup_node(client, connect)
    node.answer_fn = says({})
    leaky = "Gut zu wissen, [doc] (confirmed 5x) du hast aber einen Hund [live] namens Bello."
    conv = new_conv(client)
    status, events, _ = ask(client, conv, leaky)
    assert status == 200
    expected = (
        f"Antwort von {node.node_id[:8]}: Gut zu wissen, du hast aber einen Hund namens Bello."
    )
    shown = "".join(e[1]["text"] for e in events if e[0] == "delta")
    done = next(e[1]["message"] for e in events if e[0] == "done")
    assert shown == expected and done["content"] == expected
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"][-1]
    assert stored["content"] == expected


def test_the_prompt_prefix_is_the_same_for_every_question_and_the_cache_is_asked_for(
    client: TestClient, connect
) -> None:
    """D27: what changes per question (the notes) is in the last user turn only; the system
    prompt and the older turns are identical, and the request asks for prompt caching."""
    from tests.test_chat import chat_requests

    node = setup_node(client, connect)
    node.answer_fn = says(LIVES)
    chat_once(client, "Ich wohne in Hamburg, nur zur Info.")
    chat_once(client, "Ich habe einen Hund, der heißt Rex, nur zur Info.")
    conv = new_conv(client)
    chat_once(client, "Hallo, wie geht es dir heute so?", conv)
    chat_once(client, "Wo wohne ich denn, in Hamburg?", conv)  # notes: Hamburg
    chat_once(client, "Erkläre mir bitte die Kaffeezubereitung.", conv)  # no notes
    requests = chat_requests(node)
    last = requests[-1]["messages"]
    before = requests[-2]["messages"]
    assert last[0] == before[0]  # the system prompt: byte-identical with and without notes
    assert "Hamburg" in before[-1]["content"]  # the notes of the question: its last user turn
    assert all(r["cache_prompt"] is True for r in requests)
    # the second request's messages (before its own turn) are the start of the first one's
    # history: the older turns did not change either
    first = requests[-3]["messages"]
    assert before[1]["content"] == first[1]["content"] == "Hallo, wie geht es dir heute so?"
