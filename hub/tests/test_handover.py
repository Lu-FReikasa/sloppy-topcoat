"""Hand-over between applications (docs/PLATFORM.md, 3.8): the dossier is prepared and shown, the
click sends exactly that, the other application works in a conversation of its own and the answer
comes back as a summary in the inbox. Nothing of the other side is written into the sender."""

import json
import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import FakeNode, eventually
from tests.test_agent import finish, use
from tests.test_tools import tools_node
from tests.users import make_user
from topcoat_hub import handover
from topcoat_hub.models import DEV_ADMIN_ID

DOSSIER = {
    "question": "Why does the web container restart?",
    "context": [
        {"role": "user", "text": "my web container keeps restarting"},
        {"role": "assistant", "text": "I cannot see your servers from here."},
    ],
    "asked": "Look at the logs of the web container and say why it restarts.",
    "expected": "A short summary without secrets.",
    "withheld": ["the address of the machine, not needed"],
}


def stub_model(client: TestClient, summary: str = "The container ran out of memory.") -> list[str]:
    """The hub's own model calls (the dossier and the summary) answered by a script."""
    seen: list[str] = []

    async def ask(system: str, user_text: str, max_tokens: int = 700) -> str | None:
        seen.append(user_text)
        if system == handover.DOSSIER_SYSTEM:
            return json.dumps(DOSSIER)
        return summary

    client.app.state.handovers._ask = ask
    return seen


def conversation(client: TestClient, application: str = "chat") -> str:
    r = client.post("/api/conversations", headers=AUTH, json={"application": application})
    conv_id = r.json()["id"]
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        for role, content in (
            ("user", "my web container keeps restarting"),
            ("assistant", "I cannot see your servers from here."),
        ):
            conn.execute(
                text(
                    "INSERT INTO messages (conversation_id, user_id, role, content) "
                    "VALUES (:c, :u, :r, :t)"
                ),
                {"c": conv_id, "u": str(DEV_ADMIN_ID), "r": role, "t": content},
            )
    engine.dispose()
    return conv_id


def prepare(client: TestClient, conv_id: str, to: str = "admin", who=AUTH, **body: Any) -> Any:
    return client.post(
        "/api/handovers",
        headers=who,
        json={"to_application": to, "conversation_id": conv_id, **body},
    )


def send(client: TestClient, view: dict[str, Any], who=AUTH, hash_: str | None = None) -> Any:
    return client.post(
        f"/api/handovers/{view['id']}/send",
        headers=who,
        json={"hash": hash_ or view["hash"]},
    )


def messages_of(conv_id: str) -> list[tuple[str, str]]:
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT role, content FROM messages WHERE conversation_id = :c ORDER BY id"),
            {"c": conv_id},
        ).all()
    engine.dispose()
    return [(r[0], r[1]) for r in rows]


def inbox(client: TestClient) -> list[dict[str, Any]]:
    return client.get("/api/inbox", headers=AUTH).json()["items"]


def tools(client: TestClient, connect: Any) -> FakeNode:
    return tools_node(client, connect, "vm-docker")[1]


def test_the_dossier_is_prepared_and_nothing_is_sent_yet(client: TestClient) -> None:
    stub_model(client)
    conv = conversation(client)
    r = prepare(client, conv)
    assert r.status_code == 201, r.text
    view = r.json()
    assert view["status"] == "proposed" and view["task_id"] is None
    assert view["from_application"] == "chat" and view["to_application"] == "admin"
    assert view["dossier"]["question"] == DOSSIER["question"]
    assert [q["role"] for q in view["dossier"]["context"]] == ["user", "assistant"]
    assert view["dossier"]["withheld"] == DOSSIER["withheld"]
    assert len(view["hash"]) == 64
    assert client.get("/api/handovers", headers=AUTH).json()["handovers"][0]["id"] == view["id"]


def test_a_model_that_cannot_help_still_gives_a_dossier_from_the_request(
    client: TestClient,
) -> None:
    async def silent(*_a: Any, **_k: Any) -> None:
        return None

    client.app.state.handovers._ask = silent
    view = prepare(client, conversation(client)).json()
    assert view["dossier"]["question"] == "my web container keeps restarting"
    assert view["dossier"]["context"] == []


def test_the_application_must_exist_and_differ_from_the_sender(client: TestClient) -> None:
    stub_model(client)
    conv = conversation(client, "admin")
    assert prepare(client, conv, "admin").status_code == 409
    assert prepare(client, conv, "no-such-place").status_code == 404
    assert prepare(client, str(uuid.uuid4()), "dev").status_code == 404
    assert (
        client.post("/api/handovers", headers=AUTH, json={"to_application": "dev"}).status_code
        == 422
    )


def test_only_the_dossier_the_person_saw_is_sent(client: TestClient, connect: Any) -> None:
    tools(client, connect)
    stub_model(client)
    view = prepare(client, conversation(client)).json()
    wrong = send(client, view, hash_="0" * 64)
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "dossier_changed"
    # a dossier changed behind the person's back does not go either
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE handovers SET dossier = jsonb_set(dossier, '{asked}', '\"delete it\"')")
        )
    engine.dispose()
    tampered = send(client, view)
    assert tampered.status_code == 409 and tampered.json()["detail"]["code"] == "dossier_changed"
    assert client.get(f"/api/handovers/{view['id']}", headers=AUTH).json()["status"] == "proposed"


def test_the_other_application_works_in_a_conversation_of_its_own_and_answers_in_the_inbox(
    client: TestClient, connect: Any
) -> None:
    tools(client, connect)
    stub_model(client)
    use(client, finish("It ran out of memory; the limit is 256 MB."))
    sender = conversation(client)
    before = messages_of(sender)
    view = prepare(client, sender).json()
    r = send(client, view)
    assert r.status_code == 200, r.text
    sent = r.json()
    assert sent["status"] == "sent" and sent["task_id"] and sent["target_conversation_id"]
    assert sent["target_conversation_id"] != sender

    done = eventually(
        lambda: _done(client, view["id"]),
        timeout=15,
    )
    assert done["result_summary"] == "The container ran out of memory."
    # the other side got the dossier, in a conversation of the other application
    target = client.get(f"/api/conversations/{sent['target_conversation_id']}", headers=AUTH)
    assert target.json()["application"] == "admin"
    first = messages_of(sent["target_conversation_id"])[0]
    assert first[0] == "user" and DOSSIER["question"] in first[1]
    assert DOSSIER["context"][0]["text"] in first[1]
    # the summary is in the inbox, marked with the application it comes from
    items = [i for i in inbox(client) if i["kind"] == "handover_result"]
    assert len(items) == 1 and items[0]["application"] == "admin"
    assert items[0]["summary"] == "The container ran out of memory."
    # and nothing of it is in the sending conversation
    assert messages_of(sender) == before
    # sending twice is refused
    assert send(client, view).status_code == 409


def _done(client: TestClient, handover_id: str) -> dict[str, Any]:
    got = client.get(f"/api/handovers/{handover_id}", headers=AUTH).json()
    assert got["status"] == "done", got["status"]
    return got


def test_a_summary_with_private_data_is_replaced_by_a_pointer(
    client: TestClient, connect: Any
) -> None:
    tools(client, connect)
    stub_model(client, summary="Found it on 10.0.0.5: out of memory.")
    use(client, finish("The machine 10.0.0.5 ran out of memory."))
    view = prepare(client, conversation(client)).json()
    send(client, view)
    done = eventually(lambda: _done(client, view["id"]), timeout=15)
    assert "10.0.0.5" not in done["result_summary"]
    assert "stay in admin" in done["result_summary"]
    item = next(i for i in inbox(client) if i["kind"] == "handover_result")
    assert "10.0.0.5" not in item["summary"]


def test_an_incognito_tab_hands_over_the_question_and_no_context(
    client: TestClient,
) -> None:
    seen = stub_model(client)
    tab = client.post("/api/incognito", headers=AUTH, json={}).json()["id"]
    store = client.app.state.incognito.store
    store.get(DEV_ADMIN_ID, tab).messages = [
        {"role": "user", "content": "what does exit code 137 mean?"},
        {"role": "assistant", "content": "SECRET-earlier-answer"},
    ]
    r = client.post(
        "/api/handovers", headers=AUTH, json={"to_application": "dev", "incognito_tab": tab}
    )
    assert r.status_code == 201, r.text
    view = r.json()
    assert view["from_application"] == "incognito" and view["conversation_id"] is None
    assert view["dossier"]["context"] == []  # no context leaves the tab
    assert all("SECRET-earlier-answer" not in s for s in seen)
    assert (
        client.post(
            "/api/handovers", headers=AUTH, json={"to_application": "dev", "incognito_tab": "nope"}
        ).status_code
        == 404
    )


def test_a_hand_over_is_the_callers_own(client: TestClient, clean_db: str) -> None:
    stub_model(client)
    view = prepare(client, conversation(client)).json()
    _, other = make_user(clean_db, "stranger")
    assert client.get(f"/api/handovers/{view['id']}", headers=other).status_code == 404
    assert client.get("/api/handovers", headers=other).json()["handovers"] == []
    # other users sign in with keys until OIDC, and a key cannot send at all (auth.person_user)
    assert send(client, view, who=other).status_code == 403
    assert client.post(f"/api/handovers/{view['id']}/cancel", headers=other).status_code == 404


def test_a_proposed_hand_over_can_be_withdrawn(client: TestClient) -> None:
    stub_model(client)
    view = prepare(client, conversation(client)).json()
    r = client.post(f"/api/handovers/{view['id']}/cancel", headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert send(client, view).status_code == 409
