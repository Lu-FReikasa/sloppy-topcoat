"""D20: conversations, settings and API keys belong to one user. User B can neither read nor
change nor infer anything of user A's, through the API or the data layer."""

import inspect
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import online_node
from tests.test_chat import ask, chat_requests, new_conv, sent_messages, sse_events, use_model
from tests.users import make_user
from topcoat_hub.chat_store import ChatStore

SECRET_A = "Geheimnis-von-A: Steuernummer und Krankengeschichte"


@pytest.fixture
def two_users(client: TestClient, clean_db: str, connect) -> dict[str, Any]:
    """A (the dev admin) has a conversation with an answer, a system prompt and a key; B is a
    normal user with an API key of their own."""
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    a_conv = new_conv(client, title="Meine Steuererklärung")
    status, events, _ = ask(client, a_conv, SECRET_A)
    assert status == 200 and events[-1][0] == "done"
    r = client.put(
        "/api/chat/settings",
        headers=AUTH,
        json={"system_prompt": "PROMPT-VON-A", "temperature": 0.1},
    )
    assert r.status_code == 200
    client.post("/api/keys", headers=AUTH, json={"name": "a-key"})
    b_id, b = make_user(clean_db, "user-b")
    return {"node": node, "a_conv": a_conv, "b_id": b_id, "b": b}


def test_b_cannot_list_open_change_or_delete_a_conversations(
    client: TestClient, two_users: dict[str, Any]
) -> None:
    a_conv, b = two_users["a_conv"], two_users["b"]
    assert client.get("/api/conversations", headers=b).json() == []
    assert client.get(f"/api/conversations/{a_conv}", headers=b).status_code == 404
    assert (
        client.patch(
            f"/api/conversations/{a_conv}", headers=b, json={"title": "gehackt"}
        ).status_code
        == 404
    )
    assert client.delete(f"/api/conversations/{a_conv}", headers=b).status_code == 404
    r = client.post(f"/api/conversations/{a_conv}/messages", headers=b, json={"content": "hi"})
    assert r.status_code == 404
    assert client.post(f"/api/conversations/{a_conv}/regenerate", headers=b).status_code == 404
    assert client.post(f"/api/conversations/{a_conv}/continue", headers=b).status_code == 404

    # Nothing of it happened to A's conversation.
    a = client.get(f"/api/conversations/{a_conv}", headers=AUTH).json()
    assert a["title"] == "Meine Steuererklärung"
    assert [m["role"] for m in a["messages"]] == ["user", "assistant"]
    assert a["messages"][0]["content"] == SECRET_A


def test_b_cannot_find_a_conversations_by_searching(
    client: TestClient, two_users: dict[str, Any]
) -> None:
    b = two_users["b"]
    for q in ("Steuer", "steuererklärung", "Meine", "%", "_", "e"):
        assert client.get("/api/conversations", headers=b, params={"q": q}).json() == [], q
    assert [
        c["title"]
        for c in client.get("/api/conversations", headers=AUTH, params={"q": "Steuer"}).json()
    ] == ["Meine Steuererklärung"]


def test_each_user_sees_only_their_own_and_chats_only_over_their_own_history(
    client: TestClient, two_users: dict[str, Any]
) -> None:
    node, b = two_users["node"], two_users["b"]
    b_conv = new_conv(client, headers=b, title="Urlaub")
    status, events, _ = ask(client, b_conv, "Wohin im Sommer?", headers=b)
    assert status == 200 and events[-1][0] == "done"
    # The prompt for B contains B's message only: nothing of A's history or settings.
    sent = "\n".join(m["content"] for m in sent_messages(node))
    assert "Wohin im Sommer?" in sent
    assert SECRET_A not in sent and "PROMPT-VON-A" not in sent
    body = chat_requests(node)[-1]
    assert "temperature" not in body  # A's temperature is not B's

    assert [c["title"] for c in client.get("/api/conversations", headers=b).json()] == ["Urlaub"]
    assert [c["title"] for c in client.get("/api/conversations", headers=AUTH).json()] == [
        "Meine Steuererklärung"
    ]
    assert client.get(f"/api/conversations/{b_conv}", headers=AUTH).status_code == 404

    # Regenerate and delete act on the caller's own data only.
    r = client.post(f"/api/conversations/{b_conv}/regenerate", headers=b)
    assert sse_events(r.text)[-1][0] == "done"
    assert client.delete(f"/api/conversations/{b_conv}", headers=b).status_code == 204
    assert len(client.get("/api/conversations", headers=AUTH).json()) == 1


def test_settings_are_personal(client: TestClient, two_users: dict[str, Any]) -> None:
    b = two_users["b"]
    seen = client.get("/api/chat/settings", headers=b).json()
    assert seen["user"] == {
        "system_prompt": None,
        "temperature": None,
        "address": "du",
        "memory_enabled": True,
        "quality": "balanced",
        "internet": True,
        "persona_id": None,
    }
    assert "PROMPT-VON-A" not in str(seen)
    client.put("/api/chat/settings", headers=b, json={"system_prompt": "PROMPT-VON-B"})
    assert (
        client.get("/api/chat/settings", headers=AUTH).json()["user"]["system_prompt"]
        == "PROMPT-VON-A"
    )
    assert (
        client.get("/api/chat/settings", headers=b).json()["user"]["system_prompt"]
        == "PROMPT-VON-B"
    )


def test_api_keys_are_personal_and_never_open_the_admin_api(
    client: TestClient, clean_db: str, two_users: dict[str, Any]
) -> None:
    b = two_users["b"]
    assert (
        client.get("/api/keys", headers=b).status_code == 403
    )  # keys are managed with the admin token
    for path in ("/api/nodes", "/api/pairing-codes"):
        method = client.post if path == "/api/pairing-codes" else client.get
        assert method(path, headers=b).status_code == 403, path
    assert (
        client.put("/api/scheduling/max-loaded", headers=b, json={"max_loaded": {}}).status_code
        == 403
    )
    # Not even the key of a user with the admin role: keys act for a user, they are no admin token.
    _, admin_key = make_user(clean_db, "admin-2", role="admin")
    assert client.get("/api/nodes", headers=admin_key).status_code == 403
    assert client.get("/api/conversations", headers=admin_key).json() == []
    # A's key list is A's alone.
    assert [k["name"] for k in client.get("/api/keys", headers=AUTH).json()] == ["a-key"]
    # A user's identity comes from the key, and a revoked or unknown key is worth nothing.
    assert client.get("/api/me", headers=b).json()["name"] == "user-b"
    assert (
        client.get("/api/conversations", headers={"Authorization": "Bearer tc_unknown"}).status_code
        == 401
    )


# --- the data layer -----------------------------------------------------------------------------

STORE_METHODS_COVERED = {
    "list_conversations",
    "create_conversation",
    "continue_conversation",
    "get_conversation",
    "update_conversation",
    "delete_conversation",
    "messages",
    "add_message",
    "get_message",
    "update_message",
    "delete_message",
    "get_settings",
    "set_settings",
    "last_tool_scope",
    "remember_tool_scope",
    "set_feedback",
    "save_snapshot",
    "get_snapshot",
    "running_conversations",
    "set_state",
}


def test_the_store_scopes_every_statement_by_user(
    client: TestClient, two_users: dict[str, Any]
) -> None:
    """A guard: a new ChatStore method must be added to the test below (and thus be scoped)."""
    public = {
        name
        for name, fn in inspect.getmembers(ChatStore, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }
    assert public == STORE_METHODS_COVERED

    a_conv = uuid.UUID(two_users["a_conv"])
    b_id: uuid.UUID = two_users["b_id"]
    sessionmaker = client.app.state.sessionmaker
    a_user = uuid.UUID("00000000-0000-4000-8000-000000000001")

    async def a_message_id() -> int:
        async with sessionmaker() as db:
            return (await ChatStore(db, a_user).messages(a_conv))[0].id

    a_msg = client.portal.call(a_message_id)

    async def a_remembers() -> None:
        async with sessionmaker() as db:
            await ChatStore(db, a_user).remember_tool_scope({"nodes": None, "groups": ["docker"]})
            await db.commit()

    client.portal.call(a_remembers)

    async def a_is_answering() -> None:
        async with sessionmaker() as db:
            store = ChatStore(db, a_user)
            answer = (await store.messages(a_conv))[1]
            await store.update_message(a_conv, answer.id, status="running")
            await db.commit()

    client.portal.call(a_is_answering)

    async def as_b() -> dict[str, Any]:
        found: dict[str, Any] = {}
        async with sessionmaker() as db:
            store = ChatStore(db, b_id)
            found["list"] = list(await store.list_conversations())
            found["search"] = list(await store.list_conversations("Steuer"))
            found["get"] = await store.get_conversation(a_conv)
            found["update"] = await store.update_conversation(a_conv, title="gehackt", model="x")
            found["messages"] = await store.messages(a_conv)
            found["delete_message"] = await store.delete_message(a_conv, 1)
            found["update_message"] = await store.update_message(a_conv, 2, content="gehackt")
            found["settings"] = await store.get_settings()
            found["scope"] = await store.last_tool_scope()
            found["get_message"] = await store.get_message(a_msg)
            found["feedback"] = await store.set_feedback(a_conv, a_msg, -1)
            found["snapshot"] = await store.get_snapshot("0" * 64)
            found["running"] = await store.running_conversations()
            found["set_state"] = await store.set_state(a_conv, pinned=True, done=True)
            try:
                await store.add_message(a_conv, "user", "injected")
                found["add_message"] = "accepted"
            except LookupError:
                found["add_message"] = "refused"
            found["delete"] = await store.delete_conversation(a_conv)
            await db.commit()
        return found

    found = client.portal.call(as_b)
    assert found["list"] == [] and found["search"] == []
    assert found["get"] is None and found["update"] is None
    assert found["messages"] == [] and found["delete_message"] is False
    assert found["update_message"] is None
    assert found["settings"] is None and found["get_message"] is None
    assert found["scope"] is None  # A's last tool choice is not B's
    assert found["feedback"] is None and found["snapshot"] is None
    assert found["running"] == set()  # A's running answer is not B's
    assert found["set_state"] is None  # B cannot pin or close A's conversation
    assert found["add_message"] == "refused" and found["delete"] is False

    async def as_a() -> tuple[Any, list[Any]]:
        async with sessionmaker() as db:
            store = ChatStore(db, a_user)
            assert await store.get_message(a_msg) is not None  # it exists: B's None was the scoping
            assert await store.last_tool_scope() == {
                "nodes": None,
                "groups": ["docker"],
                "internet": None,
            }
            assert await store.running_conversations() == {a_conv}
            return await store.get_conversation(a_conv), await store.messages(a_conv)

    conv, messages = client.portal.call(as_a)
    assert conv is not None and conv.title == "Meine Steuererklärung" and conv.model is None
    assert messages[0].content == SECRET_A and len(messages) == 2


def test_deleting_a_user_removes_their_data_and_only_theirs(
    client: TestClient, clean_db: str, two_users: dict[str, Any]
) -> None:
    from sqlalchemy import create_engine, text

    b = two_users["b"]
    new_conv(client, headers=b, title="B")
    engine = create_engine(clean_db)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(two_users["b_id"])})
        left = conn.scalar(text("SELECT count(*) FROM conversations"))
        orphans = conn.scalar(
            text(
                "SELECT count(*) FROM messages m LEFT JOIN conversations c "
                "ON c.id = m.conversation_id WHERE c.id IS NULL"
            )
        )
    engine.dispose()
    assert left == 1 and orphans == 0
    assert (
        client.get("/api/conversations", headers=AUTH).json()[0]["title"] == "Meine Steuererklärung"
    )
