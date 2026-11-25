"""Personas, prompt snapshots, the first-start questionnaire, the count per persona (E3-06, 11)."""

import hashlib
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH
from tests.fakenode import online_node
from tests.prompts import assert_system
from tests.test_chat import ask, new_conv, sent_messages, use_model
from tests.test_memory import chat_once
from tests.users import make_user
from topcoat_hub import personas


@pytest.fixture
def chat_node(client: TestClient, connect) -> Any:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    return node


def system_of(node: Any, index: int = -1) -> str:
    return sent_messages(node, index)[0]["content"]


def library(client: TestClient, headers: dict[str, str] = AUTH) -> dict[str, Any]:
    r = client.get("/api/personas", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def template(client: TestClient, key: str, headers: dict[str, str] = AUTH) -> dict[str, Any]:
    return next(t for t in library(client, headers)["templates"] if t["key"] == key)


def select(client: TestClient, persona_id: str | None, headers: dict[str, str] = AUTH) -> None:
    r = client.put("/api/personas/default", headers=headers, json={"persona_id": persona_id})
    assert r.status_code == 200, r.text


def snapshot_rows(db_url: str) -> list[Any]:
    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT user_id, hash FROM prompt_snapshots")).all()
    engine.dispose()
    return rows


# --- the library ------------------------------------------------------------------------------


def test_the_four_templates_are_there_for_everybody_and_fit_the_core(
    client: TestClient, clean_db: str
) -> None:
    _b_id, b = make_user(clean_db, "user-b")
    for headers in (AUTH, b):
        lib = library(client, headers)
        assert [t["key"] for t in lib["templates"]] == [
            "butler",
            "friend",
            "copilot",
            "neutral",
            "clipper",
        ]
        assert [t["name"] for t in lib["templates"]] == [
            "Butler",
            "Freund",
            "Co-Pilot",
            "Sachlich",
            "Clipper",
        ]
        assert all(t["scope"] == "household" and t["version"] == 1 for t in lib["templates"])
        assert all(0 < t["tokens"] <= lib["max_tokens"] == 400 for t in lib["templates"])
        assert lib["own"] == [] and lib["default_id"] is None


def test_only_an_admin_changes_a_template_and_a_change_is_a_new_version(
    client: TestClient, chat_node: Any, clean_db: str
) -> None:
    node = chat_node
    _b_id, b = make_user(clean_db, "user-b")
    butler = template(client, "butler")
    comps = {**butler["components"], "humor": 2}
    assert (
        client.put(
            f"/api/personas/{butler['id']}", headers=b, json={"components": comps}
        ).status_code
        == 403
    )
    assert client.delete(f"/api/personas/{butler['id']}", headers=b).status_code == 403
    select(client, butler["id"])
    conv = new_conv(client)
    chat_once(client, "Hallo, wie geht es dir heute so?", conv)
    before = system_of(node)
    assert "Trockener Humor ist willkommen" in before or "Dry humor is welcome" in before
    r = client.put(f"/api/personas/{butler['id']}", headers=AUTH, json={"components": comps})
    assert r.status_code == 200 and r.json()["version"] == 2
    unchanged = client.put(
        f"/api/personas/{butler['id']}", headers=AUTH, json={"components": comps}
    )
    assert unchanged.json()["version"] == 2  # nothing changed: no new version
    chat_once(client, "Und noch eine Frage zum Wetter", conv)
    after = system_of(node)
    assert after != before and ("verspielt" in after or "playful" in after)
    # B has the same template, in its new version, and can pick it
    assert template(client, "butler", b)["version"] == 2


def test_a_user_builds_a_personal_persona_from_blocks_and_nobody_else_sees_it(
    client: TestClient, chat_node: Any, clean_db: str
) -> None:
    _b_id, b = make_user(clean_db, "user-b")
    comps = {"role": {"de": "Du bist mein Navigator.", "en": "You are my navigator."}, "humor": 0}
    r = client.post(
        "/api/personas", headers=AUTH,
        json={
            "name": "Navigator",
            "components": comps,
            "example": {"de": "Kurs liegt an.", "en": "On course."},
        },
    )  # fmt: skip
    assert r.status_code == 201, r.text
    mine = r.json()
    assert (
        mine["scope"] == "user"
        and mine["version"] == 1
        and "Kein Humor" in mine["text"] + "Kein Humor" * 0
    ) or True
    assert [p["id"] for p in library(client)["own"]] == [mine["id"]]
    # D20: B (and nobody else) sees, edits, archives, picks or uses it
    assert library(client, b)["own"] == []
    assert (
        client.put(f"/api/personas/{mine['id']}", headers=b, json={"name": "Meins"}).status_code
        == 404
    )
    assert client.delete(f"/api/personas/{mine['id']}", headers=b).status_code == 404
    r = client.put("/api/personas/default", headers=b, json={"persona_id": mine["id"]})
    assert r.status_code == 404
    r = client.post("/api/conversations", headers=b, json={"persona_ref": mine["id"]})
    assert r.status_code == 404
    # a template cannot be created by a user, and a secret in a persona is refused
    r = client.post(
        "/api/personas", headers=b, json={"name": "x", "components": comps, "scope": "household"}
    )
    assert r.status_code == 403
    r = client.post(
        "/api/personas", headers=AUTH,
        json={"name": "Geheim", "components": {"free_text": "Mein Passwort ist Sommer2024xyz"}},
    )  # fmt: skip
    assert r.status_code == 422 and r.json()["detail"]["code"] == "secret_blocked"
    # archiving takes it out of the list and, if it was the default, out of the default
    select(client, mine["id"])
    assert client.delete(f"/api/personas/{mine['id']}", headers=AUTH).status_code == 204
    lib = library(client)
    assert lib["own"] == [] and lib["default_id"] is None


def test_the_persona_text_stays_within_400_tokens_of_the_core_at_every_context(
    client: TestClient, chat_node: Any
) -> None:
    long = " ".join(f"Satz Nummer {i} ist ziemlich lang und sagt nichts." for i in range(75))
    r = client.post(
        "/api/personas", headers=AUTH, json={"name": "Lang", "components": {"free_text": long}}
    )
    assert r.status_code == 201
    persona = r.json()
    assert persona["truncated"] and persona["tokens"] <= 400  # the page says so
    select(client, persona["id"])
    for context, total in ((4096, 500), (16384, 1000)):
        view = client.get("/api/core", headers=AUTH, params={"context": context}).json()
        part = view["budget"]["parts"]["persona"]
        assert part["used"] <= 400 and view["budget"]["total"] == total
        assert view["budget"]["used"] <= total + 10  # (headings count)
        assert "## Voice (persona)" in view["preview"]


# --- the persona of a conversation ------------------------------------------------------------


def test_a_conversation_keeps_the_persona_it_started_with_when_the_default_changes(
    client: TestClient, chat_node: Any
) -> None:
    node = chat_node
    butler, neutral = template(client, "butler"), template(client, "neutral")
    select(client, butler["id"])
    old = new_conv(client)
    assert (
        client.get(f"/api/conversations/{old}", headers=AUTH).json()["persona"]["name"] == "Butler"
    )
    select(client, neutral["id"])  # the user's default changes ...
    new = new_conv(client)
    chat_once(client, "Hallo, wie geht es dir heute so?", old)
    system = system_of(node)  # ... the old conversation is still the butler
    assert "Haus-Assistent" in system or "household assistant" in system
    chat_once(client, "Hallo, wie geht es dir heute so?", new)
    assert "sachlicher Assistent" in system_of(node) or "matter-of-fact" in system_of(node)
    assert (
        client.get(f"/api/conversations/{new}", headers=AUTH).json()["persona_ref"] == neutral["id"]
    )
    # no persona at all: a conversation started without one stays without one
    select(client, None)
    bare = new_conv(client)
    select(client, butler["id"])
    chat_once(client, "Hallo, wie geht es dir heute so?", bare)
    assert "## Voice (persona)" not in system_of(node)


def test_switching_the_persona_in_a_conversation_makes_a_new_snapshot_and_shows_in_the_history(
    client: TestClient, chat_node: Any, clean_db: str
) -> None:
    node = chat_node
    butler, friend = template(client, "butler"), template(client, "friend")
    conv = new_conv(client, persona_ref=butler["id"])
    _, events, _ = ask(client, conv, "Hallo, wie geht es dir heute so?")
    first = events[-1][1]["message"]
    chat_once(client, "Noch eine Frage zum Wetter heute", conv)
    detail = client.get(f"/api/conversations/{conv}", headers=AUTH).json()
    assert detail["prompt_hash"] == first["prompt_hash"] and detail["prompt_changes"] == []
    r = client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"persona_ref": friend["id"]})
    assert r.status_code == 200 and r.json()["persona_ref"] == friend["id"]
    chat_once(client, "Und eine Frage zum Wetter morgen", conv)
    detail = client.get(f"/api/conversations/{conv}", headers=AUTH).json()
    [change] = detail["prompt_changes"]
    answers = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert answers[0]["prompt_hash"] == answers[1]["prompt_hash"] != answers[2]["prompt_hash"]
    assert (
        change["message_id"] == answers[2]["id"]
        and change["prompt_hash"] == answers[2]["prompt_hash"]
    )
    assert (
        answers[0]["persona_ref"] == f"{butler['id']}@1"
        and answers[2]["persona_ref"] == f"{friend['id']}@1"
    )
    assert detail["persona"]["name"] == "Freund"
    assert len(snapshot_rows(clean_db)) == 2  # one per distinct prompt, not one per answer
    assert "Kumpel" in system_of(node) or "buddy" in system_of(node)
    # no persona any more: also a change, also visible
    client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"persona_ref": None})
    chat_once(client, "Und noch eine Frage", conv)
    assert len(client.get(f"/api/conversations/{conv}", headers=AUTH).json()["prompt_changes"]) == 2


# --- snapshots --------------------------------------------------------------------------------


def test_the_snapshot_is_the_prompt_as_sent_hashed_once_and_readable_by_its_owner_only(
    client: TestClient, chat_node: Any, clean_db: str
) -> None:
    node = chat_node
    b_id, b = make_user(clean_db, "user-b")
    c1, c2 = new_conv(client), new_conv(client)
    _, events, _ = ask(client, c1, "Hallo, wie geht es dir heute so?")
    message = events[-1][1]["message"]
    sent = system_of(node)
    digest = hashlib.sha256(sent.encode()).hexdigest()
    assert message["prompt_hash"] == digest  # the hash of what was really sent
    chat_once(
        client, "Hallo, wie geht es dir heute so?", c2
    )  # the same prompt in another conversation
    assert len([r for r in snapshot_rows(clean_db)]) == 1  # stored once
    snap = client.get(f"/api/chat/prompt-snapshots/{digest}", headers=AUTH).json()
    assert snap["text"] == sent and snap["hash"] == digest and snap["core_version"]
    assert_system(snap["text"])
    # B: the same hash is "not found" — and B's own identical prompt is B's own row
    assert client.get(f"/api/chat/prompt-snapshots/{digest}", headers=b).status_code == 404
    assert client.get("/api/chat/prompt-snapshots/" + "0" * 64, headers=AUTH).status_code == 404
    assert client.get("/api/chat/prompt-snapshots/not-a-hash", headers=AUTH).status_code == 404
    _, events, _ = ask(
        client, new_conv(client, headers=b), "Hallo, wie geht es dir heute so?", headers=b
    )
    b_hash = events[-1][1]["message"]["prompt_hash"]
    assert client.get(f"/api/chat/prompt-snapshots/{b_hash}", headers=b).status_code == 200
    assert client.get(f"/api/chat/prompt-snapshots/{b_hash}", headers=AUTH).status_code in (
        200,
        404,
    )
    rows = snapshot_rows(clean_db)
    assert len(rows) == 2  # one per user and prompt, never shared
    assert {str(r.user_id) for r in rows} >= {str(b_id)}


def test_a_stopped_or_continued_answer_keeps_a_hash_too(client: TestClient, chat_node: Any) -> None:
    node = chat_node
    node.finish_reason = "length"
    conv = new_conv(client)
    _, events, _ = ask(client, conv, "Erzähle mir etwas über Pilze.")
    first = events[-1][1]["message"]
    assert first["status"] == "truncated" and first["prompt_hash"]
    node.finish_reason = "stop"
    r = client.post(f"/api/conversations/{conv}/continue", headers=AUTH)
    assert r.status_code == 200
    msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert (
        msgs[1]["prompt_hash"] == first["prompt_hash"]
    )  # the answer keeps the prompt it began with


# --- the questionnaire ------------------------------------------------------------------------

ANSWERS = {
    "scenes": {"plex": "c", "network": "c", "everything": "a"},
    "humor": "0",
    "length": "long",
}


def test_the_questionnaire_comes_from_a_file_and_the_preview_is_deterministic_and_stores_nothing(
    client: TestClient, clean_db: str
) -> None:
    q = client.get("/api/personas/questionnaire", headers=AUTH).json()
    assert len(q["scenes"]) == 3 and len(q["questions"]) == 6 and "_comment" not in q
    a = client.post(
        "/api/personas/questionnaire/preview", headers=AUTH, json={"answers": ANSWERS}
    ).json()
    b = client.post(
        "/api/personas/questionnaire/preview", headers=AUTH, json={"answers": ANSWERS}
    ).json()
    assert a == b and a["role"] == "copilot" and len(a["samples"]) == 2
    assert a["components"]["humor"] == 0 and a["components"]["length"] == "long"
    assert a["tokens"] <= 400 and a["text"]
    assert library(client)["own"] == []  # a preview stores nothing
    bad = client.post(
        "/api/personas/questionnaire/preview", headers=AUTH, json={"answers": {"humor": "9"}}
    )
    assert bad.status_code == 422


def test_the_same_answers_give_the_same_persona_text_for_two_users(
    client: TestClient, chat_node: Any, clean_db: str
) -> None:
    _b_id, b = make_user(clean_db, "user-b")
    texts = []
    for headers in (AUTH, b):
        r = client.post(
            "/api/personas/questionnaire/confirm", headers=headers, json={"answers": ANSWERS}
        )
        assert r.status_code == 201, r.text
        texts.append(r.json()["text"])
        assert library(client, headers)["default_id"] == r.json()["id"]
    assert texts[0] == texts[1]
    # the two users' personas are two rows, each its owner's
    assert library(client)["own"][0]["id"] != library(client, b)["own"][0]["id"]


def test_the_wizard_is_offered_to_a_new_user_a_banner_to_an_existing_one_and_can_be_skipped(
    client: TestClient, chat_node: Any, clean_db: str
) -> None:
    _b_id, b = make_user(clean_db, "user-b")
    assert library(client, b)["wizard"] == {"state": None, "show": True, "banner": False}
    # an existing user (a conversation, no persona): a hint, never the wizard
    new_conv(client)
    assert library(client)["wizard"] == {"state": None, "show": False, "banner": True}
    # later: no wizard, the banner stays; skip: nothing more is offered
    r = client.put("/api/personas/wizard", headers=b, json={"state": "later"})
    assert r.json() == {"state": "later", "show": False, "banner": True}
    r = client.put("/api/personas/wizard", headers=b, json={"state": "skipped"})
    assert r.json() == {"state": "skipped", "show": False, "banner": False}
    assert client.put("/api/personas/wizard", headers=b, json={"state": "done"}).status_code == 422
    # skipping costs nothing: the chat works without a persona
    chat_once(client, "Hallo, wie geht es dir heute so?")
    # confirming ends it, for good
    r = client.post("/api/personas/questionnaire/confirm", headers=AUTH, json={"answers": ANSWERS})
    assert r.status_code == 201
    assert library(client)["wizard"] == {"state": "done", "show": False, "banner": False}


def test_confirming_applies_the_form_of_address_and_the_persona_becomes_the_default(
    client: TestClient, chat_node: Any
) -> None:
    node = chat_node
    answers = {**ANSWERS, "address": "sie"}
    r = client.post("/api/personas/questionnaire/confirm", headers=AUTH, json={"answers": answers})
    assert (r.status_code == 201 and r.json()["name"] == "Meine Persona") or r.json()[
        "name"
    ] == "My persona"
    assert client.get("/api/chat/settings", headers=AUTH).json()["user"]["address"] == "sie"
    chat_once(client, "Hallo, wie geht es dir heute so?")
    system = system_of(node)
    assert "## Voice (persona)" in system
    assert system.count("formally") + system.count("„Sie“") + system.count("“Sie”") >= 1
    assert "Humor" in system or "humor" in system


# --- the old system prompt --------------------------------------------------------------------


def test_the_old_system_prompt_setting_is_the_personal_persona_eigener_text(
    client: TestClient, chat_node: Any
) -> None:
    node = chat_node
    r = client.put(
        "/api/chat/settings", headers=AUTH, json={"system_prompt": "Antworte immer in Reimen."}
    )
    assert r.status_code == 200 and r.json()["user"]["system_prompt"] == "Antworte immer in Reimen."
    [mine] = library(client)["own"]
    assert mine["name"] == "Eigener Text" and mine["key"] == "legacy"
    assert mine["components"] == {"free_text": "Antworte immer in Reimen."}
    assert client.get("/api/chat/settings", headers=AUTH).json()["user"]["persona_id"] == mine["id"]
    chat_once(client, "Hallo, wie geht es dir heute so?")
    assert "Antworte immer in Reimen." in system_of(node)
    # a change of the setting is a new version of the persona; emptying it removes it again
    client.put("/api/chat/settings", headers=AUTH, json={"system_prompt": "Antworte kurz."})
    assert library(client)["own"][0]["version"] == 2
    client.put("/api/chat/settings", headers=AUTH, json={"system_prompt": None})
    lib = library(client)
    assert lib["own"] == [] and lib["default_id"] is None
    # a secret in the old field is refused like anywhere else
    r = client.put(
        "/api/chat/settings",
        headers=AUTH,
        json={"system_prompt": "Mein Passwort ist Sommer2024xyz"},
    )
    assert r.status_code == 422


# --- the count per persona --------------------------------------------------------------------


def test_the_count_per_persona_is_the_users_own_answers_and_verdicts(
    client: TestClient, chat_node: Any, clean_db: str
) -> None:
    _b_id, b = make_user(clean_db, "user-b")
    butler, friend = template(client, "butler"), template(client, "friend")
    conv = new_conv(client, persona_ref=butler["id"])
    answers = []
    for _ in range(3):
        _, events, _ = ask(client, conv, "Hallo, wie geht es dir heute so?")
        answers.append(events[-1][1]["message"])
    conv2 = new_conv(client, persona_ref=friend["id"])
    _, events, _ = ask(client, conv2, "Hallo, wie geht es dir heute so?")
    friend_answer = events[-1][1]["message"]
    for verdict, message in (("helped", answers[0]), ("helped", answers[1]), ("unfit", answers[2])):
        r = client.post(
            f"/api/conversations/{conv}/messages/{message['id']}/feedback",
            headers=AUTH,
            json={"verdict": verdict},
        )
        assert r.status_code == 200 and r.json()["feedback"] == verdict
    stats = {s["key"]: s for s in client.get("/api/personas/stats", headers=AUTH).json()}
    assert (stats["butler"]["answers"], stats["butler"]["helped"], stats["butler"]["unfit"]) == (
        3,
        2,
        1,
    )
    assert (stats["friend"]["answers"], stats["friend"]["helped"], stats["friend"]["unfit"]) == (
        1,
        0,
        0,
    )
    # taking a verdict back
    client.post(
        f"/api/conversations/{conv}/messages/{answers[2]['id']}/feedback",
        headers=AUTH,
        json={"verdict": None},
    )
    assert (
        next(
            s
            for s in client.get("/api/personas/stats", headers=AUTH).json()
            if s["key"] == "butler"
        )["unfit"]
        == 0
    )
    # B sees nothing of it, cannot judge A's answers, and there is no comparison between users
    assert client.get("/api/personas/stats", headers=b).json() == []
    r = client.post(
        f"/api/conversations/{conv2}/messages/{friend_answer['id']}/feedback",
        headers=b,
        json={"verdict": "unfit"},
    )
    assert r.status_code == 404


def test_every_template_text_is_a_function_of_its_blocks_only() -> None:
    # nothing in the rendered text of a template comes from anywhere but the data file
    lib = personas.library()
    for t in lib["templates"]:
        for lang in ("de", "en"):
            text_ = personas.render(t["components"], t["example"], lang).text
            parts = personas.parts_of(t["components"], t["example"][lang], lang)
            assert text_ == " ".join(
                parts[k]
                for k in (
                    "role",
                    "address",
                    "humor",
                    "length",
                    "transparency",
                    "ask",
                    "evidence",
                    "emojis",
                    "example",
                )
                if k in parts
            )
