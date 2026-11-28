"""Chat API (E2-01): conversations, streamed answers through the gateway, settings."""

import json
import uuid
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import FakeNode, eventually, online_node
from tests.policy_helpers import use_model_everywhere
from tests.prompts import assert_system
from topcoat_hub import chat


def sse_events(text: str) -> list[tuple[str, dict[str, Any]]]:
    """The named events of a stream (each has an ``id:`` line since E3-07; comments are skipped)."""
    out = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.strip().split("\n") if ": " in line)
        if "event" in fields and "data" in fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


def ask(
    client: TestClient, conv: str, content: str, headers: dict[str, str] = AUTH
) -> tuple[int, list[tuple[str, dict[str, Any]]], Any]:
    r = client.post(
        f"/api/conversations/{conv}/messages", headers=headers, json={"content": content}
    )
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        return r.status_code, sse_events(r.text), r
    return r.status_code, [], r


def new_conv(client: TestClient, headers: dict[str, str] = AUTH, **body: Any) -> str:
    """A conversation as a program with an API key makes it: without an application it has no tool
    ceiling beyond the person's grants (D20), which is what these tests exercise. A browser session
    without an application gets Chat's ceiling (test_app_scope.py); name ``application`` for it."""
    key_id = None
    if "application" not in body:
        key = client.post("/api/keys", headers=headers, json={"name": "tests"})
        if key.status_code == 201:
            key_id, as_user = key.json()["id"], headers
            headers = {"Authorization": f"Bearer {key.json()['key']}"}
    r = client.post("/api/conversations", headers=headers, json=body)
    if key_id is not None:  # the key was only for this call: key lists stay as the test expects
        client.delete(f"/api/keys/{key_id}", headers=as_user)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def use_model(client: TestClient, model: str = "small") -> None:
    use_model_everywhere(client, model)


def answer_text(events: list[tuple[str, dict[str, Any]]]) -> str:
    return "".join(d["text"] for e, d in events if e == "delta")


def chat_requests(node: FakeNode) -> list[dict[str, Any]]:
    """What the node was asked to answer for the chat itself (streamed); memory calls
    (non-streamed extraction, consolidation, embeddings) are left out."""
    import base64

    bodies = [json.loads(base64.b64decode(r["body"]["body"])) for r in node.proxy_requests]
    return [b for b in bodies if b.get("stream")]


def sent_messages(node: FakeNode, index: int = -1) -> list[dict[str, str]]:
    return chat_requests(node)[index]["messages"]


def test_conversation_crud_and_title_search(client: TestClient) -> None:
    a = new_conv(client)
    b = new_conv(client, title="Rezepte für Kürbissuppe", model="small", node="node-a")
    listed = client.get("/api/conversations", headers=AUTH).json()
    assert [c["id"] for c in listed] == [b, a]  # most recently created first
    assert listed[0]["model"] == "small" and listed[0]["node"] == "node-a"
    assert listed[0]["message_count"] == 0

    r = client.patch(f"/api/conversations/{a}", headers=AUTH, json={"title": "Steuererklärung"})
    assert r.status_code == 200 and r.json()["title"] == "Steuererklärung"
    assert client.get(f"/api/conversations/{a}", headers=AUTH).json()["messages"] == []

    def titles(q: str) -> list[str]:
        r = client.get("/api/conversations", headers=AUTH, params={"q": q})
        return [c["title"] for c in r.json()]

    assert titles("kürbis") == ["Rezepte für Kürbissuppe"]  # case-insensitive substring
    assert titles("STEUER") == ["Steuererklärung"]
    assert titles("%") == []  # wildcards are literal
    assert titles("_") == []
    assert titles("") == ["Steuererklärung", "Rezepte für Kürbissuppe"]  # updated_at order

    # The picker: pin a node, reset to automatic with null.
    r = client.patch(
        f"/api/conversations/{a}", headers=AUTH, json={"model": "big", "node": "node-b"}
    )
    assert (r.json()["model"], r.json()["node"]) == ("big", "node-b")
    r = client.patch(f"/api/conversations/{a}", headers=AUTH, json={"node": None})
    assert (r.json()["model"], r.json()["node"]) == ("big", None)
    assert (
        client.patch(
            f"/api/conversations/{a}", headers=AUTH, json={"node": "Not A Node"}
        ).status_code
        == 422
    )
    assert (
        client.patch(f"/api/conversations/{a}", headers=AUTH, json={"nope": 1}).status_code == 422
    )

    assert client.delete(f"/api/conversations/{a}", headers=AUTH).status_code == 204
    assert client.get(f"/api/conversations/{a}", headers=AUTH).status_code == 404
    assert client.delete(f"/api/conversations/{a}", headers=AUTH).status_code == 404
    assert client.get("/api/conversations").status_code == 401


def test_message_is_answered_through_the_gateway_and_stored(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    conv = new_conv(client)
    status, events, r = ask(client, conv, "Hallo Welt")
    assert status == 200 and r.headers["content-type"].startswith("text/event-stream")
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta" and kinds[-1] == "done" and set(kinds[1:-1]) == {"delta"}
    meta = events[0][1]
    assert meta["model"] == "small" and meta["node"] == "node-a"
    assert meta["fallback_from"] is None
    assert meta["user_message"]["content"] == "Hallo Welt"
    assert meta["context"]["trimmed"] == 0 and meta["context"]["length"] == 8192
    text = answer_text(events)
    assert text == f"Antwort von {node.node_id[:8]}: Hallo Welt"

    done = events[-1][1]["message"]
    assert done["content"] == text and done["role"] == "assistant"
    assert (done["node"], done["model"], done["status"]) == ("node-a", "small", "complete")
    assert done["prompt_tokens"] and done["completion_tokens"] == len(text.split())
    assert done["first_token_ms"] is not None and done["duration_ms"] >= done["first_token_ms"]

    full = client.get(f"/api/conversations/{conv}", headers=AUTH).json()
    assert full["title"] == "Hallo Welt"  # from the first message
    assert [m["role"] for m in full["messages"]] == ["user", "assistant"]
    assert full["messages"][1]["id"] == done["id"]

    # What the model saw: the language rule and the history, streamed with usage.
    sent = sent_messages(node)
    assert sent[0]["role"] == "system"
    assert_system(sent[0]["content"])
    assert sent[-1] == {"role": "user", "content": "Hallo Welt"}

    status, events, _ = ask(client, conv, "Und jetzt?")
    assert status == 200
    sent = sent_messages(node)
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[2]["content"] == text


def test_default_model_order_and_settings(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small", "big"])
    settings = client.get("/api/chat/settings", headers=AUTH).json()
    # nothing chosen: the default of the pool is the best model it offers
    assert settings["effective_model"] == "big"
    assert settings["user"] == {
        "system_prompt": None,
        "temperature": None,
        "address": "du",
        "memory_enabled": True,
        "quality": "balanced",
        "internet": True,
        "persona_id": None,
    }

    use_model(client, "small")  # hub default
    conv = new_conv(client)
    _, events, _ = ask(client, conv, "eins")
    assert events[0][1]["model"] == "small"

    r = client.put(
        "/api/chat/settings",
        headers=AUTH,
        json={"system_prompt": "Antworte kurz.", "temperature": 0.2},
    )
    assert r.status_code == 200 and r.json()["effective_model"] == "small"
    conv2 = new_conv(client)
    _, events, _ = ask(client, conv2, "zwei")  # a person has no default model of their own
    assert events[0][1]["model"] == "small"
    # the old per-person field is gone: the API refuses it
    refused = client.put("/api/chat/settings", headers=AUTH, json={"default_model": "big"})
    assert refused.status_code == 422
    sent = sent_messages(node)
    assert_system(sent[0]["content"], contains=("Antworte kurz.",))
    body = chat_requests(node)[-1]
    assert body["temperature"] == 0.2 and body["stream_options"] == {"include_usage": True}

    conv3 = new_conv(client, model="big")  # the conversation's own choice beats the default
    _, events, _ = ask(client, conv3, "drei")
    assert events[0][1]["model"] == "big"

    # A pinned node other than the one that has the model: nothing to answer with (when no row of
    # the table offers another node).
    client.put("/api/scheduling/applications", headers=AUTH, json={"rows": {}})
    conv4 = new_conv(client, model="small", node="node-zzz")
    status, _, r = ask(client, conv4, "vier")
    assert status == 404 and r.json()["detail"]["code"] == "node_not_found"

    # Resetting: null / missing values fall back to the defaults.
    r = client.put("/api/chat/settings", headers=AUTH, json={})
    assert r.json()["user"] == {
        "system_prompt": None,
        "temperature": None,
        "address": "du",
        "memory_enabled": True,
        "quality": "balanced",
        "internet": True,
        "persona_id": None,
    }
    assert (
        client.put("/api/chat/settings", headers=AUTH, json={"temperature": 3}).status_code == 422
    )
    assert (
        client.put(
            "/api/chat/settings", headers=AUTH, json={"system_prompt": "x" * 4001}
        ).status_code
        == 422
    )


def test_there_is_no_global_default_model_to_set(client: TestClient) -> None:
    """Models are released per node and chosen per application: the old endpoint is gone and the
    pools view names no setting behind the automatic fallback."""
    url = "/api/scheduling/default-model"
    assert client.put(url, headers=AUTH, json={"model": "small"}).status_code in (404, 405)
    assert "default_model_setting" not in client.get("/api/scheduling/pools", headers=AUTH).json()


def test_context_is_trimmed_visibly(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"], context_length=700)
    use_model(client)
    conv = new_conv(client)
    long = "wort " * 60  # 300 characters
    trimmed = []
    for i in range(5):
        status, events, _ = ask(client, conv, f"{i}: {long}")
        assert status == 200
        meta = events[0][1]
        assert meta["context"]["length"] == 700
        trimmed.append(meta["context"]["trimmed"])
        assert events[-1][1]["message"]["context_trimmed"] == meta["context"]["trimmed"]
    assert trimmed[0] == 0 and trimmed[-1] > 0
    assert trimmed == sorted(trimmed)  # the history only grows
    sent = sent_messages(node)
    assert sent[-1]["content"].startswith("4: ")  # the newest message is always there
    assert len(sent) - 1 < 9  # the 9 stored messages before the last did not all fit


def test_regenerate_replaces_the_last_answer(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    conv = new_conv(client)
    _, events, _ = ask(client, conv, "Hallo")
    first_id = events[-1][1]["message"]["id"]
    r = client.post(f"/api/conversations/{conv}/regenerate", headers=AUTH)
    assert r.status_code == 200
    events = sse_events(r.text)
    assert events[0][1]["user_message"] is None
    new_id = events[-1][1]["message"]["id"]
    assert new_id != first_id
    msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["id"] == new_id
    assert [m["role"] for m in sent_messages(node)] == ["system", "user"]  # the old answer is gone

    empty = new_conv(client)
    assert client.post(f"/api/conversations/{empty}/regenerate", headers=AUTH).status_code == 409


def test_unavailable_model_is_a_clear_503_and_the_message_is_kept(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.state = "busy_gaming"
    node.heartbeat()
    eventually(lambda: _state(client, "node-a", "busy_gaming"))
    conv = new_conv(client)
    status, _, r = ask(client, conv, "Hallo?")
    assert status == 503
    assert r.headers["retry-after"] == "30"
    assert r.json()["detail"] == {
        "code": "model_unavailable",
        "message": r.json()["detail"]["message"],
        "retry_after": 30,
    }
    msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [m["role"] for m in msgs] == ["user"]  # nothing lost, can be retried

    node.state = "available"
    node.heartbeat()
    eventually(lambda: _state(client, "node-a", "available"))
    r = client.post(f"/api/conversations/{conv}/regenerate", headers=AUTH)
    events = sse_events(r.text)
    assert events[-1][0] == "done"
    assert [
        m["role"] for m in client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    ] == [
        "user",
        "assistant",
    ]


def test_a_503_comes_after_the_placeholder_is_gone(
    client: TestClient, connect, monkeypatch
) -> None:
    """The run that failed settles after it reported the error; the 503 waits for that, so a
    look at the conversation right after it never shows the empty placeholder (#107)."""
    import asyncio

    from topcoat_hub.api import chat as chat_api

    settle = chat_api._settle

    async def slow_settle(*args: Any) -> Any:
        await asyncio.sleep(0.3)
        return await settle(*args)

    monkeypatch.setattr(chat_api, "_settle", slow_settle)
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.state = "busy_gaming"
    node.heartbeat()
    eventually(lambda: _state(client, "node-a", "busy_gaming"))
    conv = new_conv(client)
    status, _, _ = ask(client, conv, "Hallo?")
    assert status == 503
    msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [m["role"] for m in msgs] == ["user"]


def _state(client: TestClient, name: str, state: str) -> None:
    nodes = {n["name"]: n["state"] for n in client.get("/api/nodes", headers=AUTH).json()}
    assert nodes[name] == state


def test_fallback_is_shown_and_stored(client: TestClient, connect) -> None:
    gaming = online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b", loaded=["small"])
    use_model(client)
    conv = new_conv(client)
    _, events, _ = ask(client, conv, "eins")
    assert events[0][1]["node"] == "node-a" and events[0][1]["fallback_from"] is None
    gaming.state = "busy_gaming"
    gaming.heartbeat()
    eventually(lambda: _state(client, "node-a", "busy_gaming"))
    _, events, _ = ask(client, conv, "zwei")
    meta = events[0][1]
    assert (meta["node"], meta["fallback_from"], meta["fallback_state"]) == (
        "node-b",
        "node-a",
        "busy_gaming",
    )
    last = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"][-1]
    assert (last["node"], last["fallback_from"]) == ("node-b", "node-a")


def test_chat_models_lists_the_picker_options(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    models = {m["id"]: m for m in client.get("/api/chat/models", headers=AUTH).json()}
    assert set(models) == {"small", "big"}
    assert models["small"]["topcoat"]["nodes"][0]["node"] == "node-a"


def test_a_broken_stream_keeps_the_partial_answer(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.drop_stream_after = 3  # the node ends the stream without finishing
    conv = new_conv(client)
    status, events, _ = ask(client, conv, "eins zwei drei vier fünf sechs")
    assert status == 200
    assert events[-1][0] == "error" and events[-1][1]["code"] == "stream_broken"
    row = events[-1][1]["message_row"]
    assert row["status"] == "error" and row["content"].startswith("Antwort von")
    assert "lost" in row["error"]
    msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [m["status"] for m in msgs] == ["complete", "error"]
    # An unfinished answer does not confuse the next turn: it is part of the history as said.
    assert msgs[1]["content"] == row["content"]


def test_address_form_setting_reaches_the_system_prompt(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    r = client.put("/api/chat/settings", headers=AUTH, json={"address": "sie"})
    assert r.status_code == 200 and r.json()["user"]["address"] == "sie"
    ask(client, new_conv(client), "Guten Tag")
    system = sent_messages(node)[0]["content"]
    assert_system(system, contains=('formally with "Sie"',))

    # null / missing resets to the default "du"; anything else is refused
    assert client.put("/api/chat/settings", headers=AUTH, json={}).json()["user"]["address"] == "du"
    assert (
        client.put("/api/chat/settings", headers=AUTH, json={"address": "ihr"}).status_code == 422
    )


def test_first_token_time_is_measured_from_the_request(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.first_chunk_delay = 0.3  # prompt processing before the first byte
    _, events, _ = ask(client, new_conv(client), "Hallo")
    done = events[-1][1]["message"]
    assert 300 <= done["first_token_ms"] <= done["duration_ms"]
    stored = client.get(f"/api/conversations/{events[0][1]['conversation_id']}", headers=AUTH)
    assert stored.json()["messages"][1]["first_token_ms"] == done["first_token_ms"]


def test_an_answer_that_hit_the_length_limit_is_marked_truncated(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.finish_reason = "length"
    conv = new_conv(client)
    _, events, _ = ask(client, conv, "eins zwei drei")
    assert events[-1][0] == "done"
    done = events[-1][1]["message"]
    assert done["status"] == "truncated" and done["error"] is None
    msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert msgs[1]["status"] == "truncated"

    node.finish_reason = "stop"  # a normal end stays "complete"
    _, events, _ = ask(client, new_conv(client), "vier")
    assert events[-1][1]["message"]["status"] == "complete"


def test_a_truncated_answer_is_continued_in_the_same_message(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.finish_reason = "length"
    conv = new_conv(client)
    _, events, _ = ask(client, conv, "erzähl")
    first = events[-1][1]["message"]
    assert first["status"] == "truncated"

    node.finish_reason = "stop"
    r = client.post(f"/api/conversations/{conv}/continue", headers=AUTH)
    assert r.status_code == 200
    events = sse_events(r.text)
    meta = events[0][1]
    assert meta["continues"] == first["id"] and meta["user_message"] is None
    assert events[-1][0] == "done"
    merged = events[-1][1]["message"]
    assert merged["id"] == first["id"]  # the same message, not a new one
    assert merged["content"] == first["content"] + answer_text(events)
    assert merged["status"] == "complete"
    assert merged["completion_tokens"] > first["completion_tokens"]
    assert merged["duration_ms"] > first["duration_ms"]

    # the model saw the cut-off answer, then the instruction to go on (nothing of it is stored)
    sent = sent_messages(node)
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[2]["content"] == first["content"]
    assert sent[3]["content"] == chat.CONTINUE_INSTRUCTION
    msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == merged["content"]

    # only a truncated answer can be continued
    r = client.post(f"/api/conversations/{conv}/continue", headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "nothing_to_continue"
    assert (
        client.post(f"/api/conversations/{uuid.uuid4()}/continue", headers=AUTH).status_code == 404
    )


def test_continuing_can_be_truncated_again(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.finish_reason = "length"
    conv = new_conv(client)
    ask(client, conv, "lang")
    r = client.post(f"/api/conversations/{conv}/continue", headers=AUTH)
    assert sse_events(r.text)[-1][1]["message"]["status"] == "truncated"
    r = client.post(f"/api/conversations/{conv}/continue", headers=AUTH)  # and again
    assert r.status_code == 200


def test_a_regenerated_answer_replaces_a_truncated_one(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.finish_reason = "length"
    conv = new_conv(client)
    ask(client, conv, "lang")
    node.finish_reason = "stop"
    r = client.post(f"/api/conversations/{conv}/regenerate", headers=AUTH)
    assert sse_events(r.text)[-1][1]["message"]["status"] == "complete"
    msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [m["status"] for m in msgs] == ["complete", "complete"]


def test_continue_keeps_the_end_of_a_long_cut_off_answer(client: TestClient, connect) -> None:
    """Hardware test C15: a 1024-token context, the story stopped mid-word, and the model was
    asked to continue without having seen it. The tail of the text must stay in the prompt."""
    node = online_node(client, connect, "node-a", loaded=["small"], context_length=1024)
    use_model(client)
    node.finish_reason = "length"
    node.answer_padding = " " + " ".join(f"satz{i}" for i in range(400)) + " Die zwe"
    conv = new_conv(client)
    _, events, _ = ask(client, conv, "Erzähle eine Geschichte")
    cut = events[-1][1]["message"]
    assert cut["status"] == "truncated" and cut["content"].endswith("Die zwe")

    node.finish_reason, node.answer_padding = "stop", ""
    r = client.post(f"/api/conversations/{conv}/continue", headers=AUTH)
    assert r.status_code == 200
    sent = sent_messages(node)
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[1]["content"] == "Erzähle eine Geschichte"
    tail = sent[2]["content"]
    assert tail.startswith("[…] ") and tail.endswith("Die zwe")  # the END, marked as shortened
    assert len(tail) < len(cut["content"]) and cut["content"].endswith(tail[4:])
    assert sent[3]["content"] == chat.CONTINUE_INSTRUCTION
    assert_system(sent[0]["content"], compact=True)
    est = sum(chat.estimate_tokens(m["content"]) for m in sent)
    assert est <= chat.prompt_budget(1024)  # and the tail is a big part of it
    assert chat.estimate_tokens(tail) >= 0.4 * (
        chat.prompt_budget(1024) - est + chat.estimate_tokens(tail)
    )
    merged = sse_events(r.text)[-1][1]["message"]
    assert merged["content"].startswith(cut["content"]) and merged["status"] == "complete"


def test_continue_says_so_when_the_context_is_too_small(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"], context_length=1024)
    use_model(client)
    node.finish_reason = "length"
    conv = new_conv(client)
    ask(client, conv, "Erzähle")
    # a long personal system prompt is capped at 400 tokens of the core: it can no longer eat the
    # window, but the core and the fixed rules still leave no room for the tail in a 1k window
    # once the conversation moves to the persona with the long text
    client.put("/api/chat/settings", headers=AUTH, json={"system_prompt": "Wort. " * 650})
    persona = client.get("/api/personas", headers=AUTH).json()["own"][0]
    assert persona["truncated"] and persona["tokens"] <= 400
    client.patch(f"/api/conversations/{conv}", headers=AUTH, json={"persona_ref": persona["id"]})
    calls = len(chat_requests(node))
    r = client.post(f"/api/conversations/{conv}/continue", headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "context_too_small"
    assert "too small to continue" in r.json()["detail"]["message"]
    assert len(chat_requests(node)) == calls  # nothing was sent to the model

    def still_continuable() -> None:  # the end of the first answer is written a moment after it
        msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
        assert msgs[1]["status"] == "truncated" and len(msgs) == 2  # still there, continuable

    eventually(still_continuable)


def test_small_contexts_get_the_short_system_prompt(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"], context_length=2048)
    use_model(client)
    ask(client, new_conv(client), "Hallo")
    assert_system(sent_messages(node)[0]["content"], compact=True)
    node.close()
    node2 = online_node(client, connect, "node-b", loaded=["big"], context_length=8192)
    use_model(client, "big")
    ask(client, new_conv(client), "Hallo")
    assert_system(sent_messages(node2)[0]["content"])


def test_a_paused_node_is_not_a_timeout_and_the_chat_is_told(client: TestClient, connect) -> None:
    # The person at the node's machine came back (E2-05 B): the node freezes the model, says so,
    # and the hub waits far longer than its idle timeout for the answer to go on.
    object.__setattr__(client.app.state.settings, "gateway_idle_timeout_seconds", 0.3)
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.pause_after = 2
    node.pause_seconds = 1.0
    node.pause_reasons = ["input", "battery"]
    conv = new_conv(client)
    status, events, _ = ask(client, conv, "eins zwei drei vier fünf sechs")
    assert status == 200
    kinds = [e for e, _ in events]
    assert events[-1][0] == "done" and events[-1][1]["message"]["status"] == "complete"
    states = [(d["state"], d.get("reasons")) for e, d in events if e == "status"]
    assert states == [("paused", ["input", "battery"]), ("running", [])]
    assert kinds.index("status") < len(kinds) - 1
    assert answer_text(events).startswith("Antwort von") and "sechs" in answer_text(events)


def test_an_answer_the_node_stopped_by_unloading_can_be_continued(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.drop_stream_after = 3
    node.drop_error_code = "interrupted"  # the node unloaded the model after a long pause
    conv = new_conv(client)
    status, events, _ = ask(client, conv, "eins zwei drei vier fünf sechs")
    assert status == 200
    assert [e for e, _ in events][-2:] == ["status", "done"]
    assert events[-2][1]["state"] == "interrupted"
    done = events[-1][1]["message"]
    assert done["status"] == "truncated" and done["error"] is None
    assert done["content"].startswith("Antwort von")
    # the Continue logic carries on in the same message
    node.drop_stream_after = None
    r = client.post(f"/api/conversations/{conv}/continue", headers=AUTH)
    cont = sse_events(r.text)
    assert cont[-1][0] == "done" and cont[-1][1]["message"]["status"] == "complete"
    msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"].startswith(done["content"])


def test_an_answer_that_broke_without_a_reason_is_still_an_error(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.drop_stream_after = 3  # a crash: code runner_error
    _, events, _ = ask(client, new_conv(client), "eins zwei drei vier fünf sechs")
    assert events[-1][0] == "error" and events[-1][1]["message_row"]["status"] == "error"


def test_token_counts_survive_pauses_unloads_and_continuing(client: TestClient, connect) -> None:
    # The first hardware test of E2-05 B saw completion_tokens = None on an answer that went on
    # after a pause: every stored answer keeps a count (the node's, else an estimate).
    object.__setattr__(client.app.state.settings, "gateway_idle_timeout_seconds", 0.3)
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.pause_after = 2
    node.pause_seconds = 1.0
    node.pause_reasons = ["input"]
    conv = new_conv(client)
    _, events, _ = ask(client, conv, "eins zwei drei vier fünf sechs")
    done = events[-1][1]["message"]
    assert done["status"] == "complete"
    assert done["completion_tokens"] and done["prompt_tokens"], done
    # a model unloaded during the answer, then continued
    node.pause_after = None
    node.drop_stream_after = 3
    node.drop_error_code = "interrupted"
    conv2 = new_conv(client)
    _, events, _ = ask(client, conv2, "eins zwei drei vier fünf sechs")
    cut = events[-1][1]["message"]
    assert cut["status"] == "truncated" and cut["completion_tokens"], cut
    node.drop_stream_after = None
    sse_events(client.post(f"/api/conversations/{conv2}/continue", headers=AUTH).text)
    msgs = client.get(f"/api/conversations/{conv2}", headers=AUTH).json()["messages"]
    assert msgs[1]["status"] == "complete"
    assert msgs[1]["completion_tokens"] > cut["completion_tokens"]
    stored = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
    assert stored[1]["completion_tokens"] == done["completion_tokens"]


def test_a_full_context_window_has_its_own_error_code(client: TestClient, connect) -> None:
    # The shared window of llama-server ran full mid-answer ("Context size has been exceeded"):
    # a clear, retryable error instead of a broken stream.
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.stream_error = "Context size has been exceeded."
    conv = new_conv(client)
    _, events, _ = ask(client, conv, "eins zwei drei vier fünf sechs")
    kind, data = events[-1]
    assert kind == "error" and data["code"] == "context_full" and data["retry"] is True
    assert "context is full" in data["message"] and "n_parallel" in data["message"]
    assert data["message_row"]["status"] == "error"
    assert "context is full" in data["message_row"]["error"]
    # the same error before the answer starts (the request alone is too long for a slot)
    node.stream_error = None
    node.model_error = (
        400,
        "request (4552 tokens) exceeds the available context size (512 tokens), try increasing it",
    )
    r = client.post(
        f"/api/conversations/{new_conv(client)}/messages", headers=AUTH, json={"content": "hallo"}
    )
    assert r.status_code == 503 and r.headers.get("Retry-After")
    assert r.json()["detail"]["code"] == "context_full"
    # other refusals stay model_error
    node.model_error = (400, "something else")
    r = client.post(
        f"/api/conversations/{new_conv(client)}/messages", headers=AUTH, json={"content": "hallo"}
    )
    assert r.status_code == 502 and r.json()["detail"]["code"] == "model_error"
