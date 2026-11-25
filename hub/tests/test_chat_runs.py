"""Answers that outlive the connection (E3-07 D1): the browser leaves, the answer is finished and
stored; a subscriber attaches again from an offset; "stop" is its own call; two tabs see the same
answer; a hub that goes down ends the answer as interrupted."""

import asyncio
import json
import threading
import uuid
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH
from tests.fakenode import FakeNode, eventually, online_node
from tests.test_chat import new_conv, sse_events, use_model
from tests.test_memory import memory_items, model, note
from topcoat_hub.chat_runs import sweep_interrupted

WORDS = " ".join(f"wort{i}" for i in range(12))


async def drive(
    app: Any, path: str, body: dict[str, Any] | None, *, leave_after: int | None = None
) -> list[str]:
    """Call the app at the ASGI level the way a browser does: send a POST (or a GET when ``body``
    is ``None``), read the body chunks, and — with ``leave_after`` — disconnect after that many
    chunks (Starlette then cancels the response, which is what a closed tab does)."""
    sent: list[str] = []
    gone = asyncio.Event()
    payload = json.dumps(body or {}).encode()
    asked = False

    async def receive() -> dict[str, Any]:
        nonlocal asked
        if not asked:
            asked = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            sent.append(message["body"].decode())
            if leave_after is not None and len(sent) >= leave_after:
                gone.set()

    route, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET" if body is None else "POST",
        "scheme": "http",
        "path": route,
        "raw_path": route.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [
            (b"authorization", AUTH["Authorization"].encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    return sent


def parse(text_: str) -> list[tuple[str, dict[str, Any]]]:
    """The events of an SSE body; the ``id:`` lines (the numbering) and comments are dropped."""
    kept = [ln for ln in text_.split("\n") if not ln.startswith(("id: ", ":"))]
    return sse_events("\n".join(kept))


def events_of(chunks: list[str]) -> list[tuple[str, dict[str, Any]]]:
    return parse("".join(chunks))


def leave(client: TestClient, conv: str, content: str, after: int = 2) -> list[str]:
    """Ask a question and close the stream after ``after`` chunks (a tab that goes away)."""
    return client.portal.call(
        lambda: drive(
            client.app,
            f"/api/conversations/{conv}/messages",
            {"content": content},
            leave_after=after,
        )
    )


def conversation(client: TestClient, conv: str) -> dict[str, Any]:
    return client.get(f"/api/conversations/{conv}", headers=AUTH).json()


def answer_of(client: TestClient, conv: str) -> dict[str, Any] | None:
    return next(
        (m for m in conversation(client, conv)["messages"] if m["role"] == "assistant"), None
    )


def message_id_of(chunks: list[str]) -> int:
    meta = next(d for e, d in events_of(chunks) if e == "meta")
    return int(meta["message_id"])


def slow_node(client: TestClient, connect: Any, delay: float = 0.15) -> FakeNode:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.chunk_delay = delay
    node.answer_padding = " " + WORDS  # the streamed answer ends with these words
    return node


def attach(
    client: TestClient, message_id: int, offset: int = 0
) -> list[tuple[str, dict[str, Any]]]:
    r = client.get(f"/api/chat/messages/{message_id}/stream?offset={offset}", headers=AUTH)
    assert r.status_code == 200, r.text
    return parse(r.text)


# --- the browser leaves, the answer does not ----------------------------------------------------


def test_closing_the_stream_does_not_stop_the_answer(client: TestClient, connect: Any) -> None:
    node = slow_node(client, connect)
    conv = new_conv(client)
    got = leave(client, conv, "Zähle bis zwölf")
    assert any("event: meta" in c for c in got)  # the stream was open, then closed
    message_id = message_id_of(got)
    # the tab is gone; the hub finishes the answer and stores it whole
    eventually(lambda: _complete(client, conv), timeout=15)
    answer = answer_of(client, conv)
    assert answer is not None and answer["status"] == "complete" and answer["id"] == message_id
    assert answer["content"].endswith(WORDS)  # nothing missing
    assert answer["prompt_hash"] and answer["completion_tokens"]
    detail = conversation(client, conv)
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]  # one answer, no copy
    assert detail["running"] is False and not node.cancels  # the node was never told to stop


def _complete(client: TestClient, conv: str) -> None:
    answer = answer_of(client, conv)
    assert answer is not None and answer["status"] == "complete"


def test_a_running_answer_is_marked_and_its_text_so_far_is_stored(
    client: TestClient, connect: Any
) -> None:
    slow_node(client, connect, delay=0.3)
    conv = new_conv(client)
    message_id = message_id_of(leave(client, conv, "Zähle bis zwölf"))
    assert conversation(client, conv)["running"] is True
    listed = {c["id"]: c for c in client.get("/api/conversations", headers=AUTH).json()}
    assert listed[conv]["running"] is True  # the list says "runs in the background"

    def partial() -> None:
        answer = answer_of(client, conv)
        assert answer is not None and answer["id"] == message_id
        assert answer["status"] == "running" and "wort0" in answer["content"]

    eventually(partial, timeout=10)  # the text so far is in the database (every second)
    eventually(lambda: _complete(client, conv), timeout=20)
    assert {c["id"]: c for c in client.get("/api/conversations", headers=AUTH).json()}[conv][
        "running"
    ] is False


# --- attach again ----------------------------------------------------------------------------


def test_attaching_again_gives_the_rest_and_the_finished_state(
    client: TestClient, connect: Any
) -> None:
    slow_node(client, connect)
    conv = new_conv(client)
    first = leave(client, conv, "Zähle bis zwölf", after=3)
    message_id = message_id_of(first)
    seen = len(events_of(first))
    rest = attach(client, message_id, offset=seen)
    kinds = [e for e, _ in rest]
    assert kinds[-1] == "done" and "meta" not in kinds  # only what the first stream had not seen
    text_before = "".join(d["text"] for e, d in events_of(first) if e == "delta")
    text_after = "".join(d["text"] for e, d in rest if e == "delta")
    stored = answer_of(client, conv)
    assert stored is not None and (text_before + text_after).strip().endswith(WORDS)
    assert rest[-1][1]["message"]["id"] == message_id
    # from the start, the whole run is replayed (a reload attaches like that)
    everything = attach(client, message_id, offset=0)
    assert everything[0][0] == "meta" and everything[-1][0] == "done"


def test_a_finished_answer_that_is_gone_from_memory_is_served_as_a_snapshot(
    client: TestClient, connect: Any
) -> None:
    slow_node(client, connect, delay=0.02)
    conv = new_conv(client)
    message_id = message_id_of(leave(client, conv, "Zähle bis zwölf"))
    eventually(lambda: _complete(client, conv), timeout=15)
    client.app.state.runs._by_message.clear()  # kept a few minutes in reality; here: gone
    [(kind, data)] = attach(client, message_id, offset=5)
    assert kind == "snapshot" and data["message"]["status"] == "complete"
    assert data["message"]["content"].endswith(WORDS) and data["conversation_id"] == conv


def test_only_the_owner_can_attach_or_stop(client: TestClient, connect: Any, clean_db: str) -> None:
    from tests.users import make_user

    slow_node(client, connect, delay=0.3)
    conv = new_conv(client)
    message_id = message_id_of(leave(client, conv, "Zähle bis zwölf"))
    _b_id, b = make_user(clean_db, "user-b")
    assert client.get(f"/api/chat/messages/{message_id}/stream", headers=b).status_code == 404
    assert client.post(f"/api/chat/messages/{message_id}/stop", headers=b).status_code == 404
    assert answer_of(client, conv)["status"] in ("running", "complete")  # type: ignore[index]
    client.post(f"/api/chat/messages/{message_id}/stop", headers=AUTH)


def test_two_tabs_see_the_same_answer(client: TestClient, connect: Any) -> None:
    slow_node(client, connect)
    conv = new_conv(client)
    message_id = message_id_of(leave(client, conv, "Zähle bis zwölf", after=2))
    results: list[list[tuple[str, dict[str, Any]]]] = []

    def tab() -> None:
        results.append(attach(client, message_id))

    threads = [threading.Thread(target=tab) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 2 and results[0] == results[1]
    assert [e for e, _ in results[0]][-1] == "done"
    text_ = "".join(d["text"] for e, d in results[0] if e == "delta")
    assert text_.strip().endswith(WORDS)


# --- stop is its own call ---------------------------------------------------------------------


def test_stop_ends_the_answer_for_real_and_keeps_what_was_said(
    client: TestClient, connect: Any
) -> None:
    node = slow_node(client, connect, delay=0.3)
    conv = new_conv(client)
    message_id = message_id_of(leave(client, conv, "Zähle bis zwölf"))
    run = client.app.state.runs.live(message_id)
    eventually(lambda: _has_text(run), timeout=10)
    follower: list[tuple[str, dict[str, Any]]] = []
    t = threading.Thread(target=lambda: follower.extend(attach(client, message_id)))
    t.start()
    r = client.post(f"/api/chat/messages/{message_id}/stop", headers=AUTH)
    assert r.status_code == 200, r.text
    t.join(timeout=15)
    answer = answer_of(client, conv)
    assert answer is not None and answer["status"] == "stopped" and answer["id"] == message_id
    assert answer["content"] and not answer["content"].endswith(WORDS)
    stopped = [d for e, d in follower if e == "stopped"]
    assert stopped and stopped[0]["message"]["status"] == "stopped"
    assert stopped[0]["interrupted"] is False
    assert node.cancels  # the model was told to stop
    kept = answer["content"]
    eventually(lambda: None, timeout=1)
    assert answer_of(client, conv)["content"] == kept  # type: ignore[index]  # nothing grows any more
    assert conversation(client, conv)["running"] is False
    assert client.post(f"/api/chat/messages/{message_id}/stop", headers=AUTH).status_code == 409


def _has_text(run: Any) -> None:
    assert run is not None and run.text


def test_stop_before_the_first_word_leaves_no_empty_answer(
    client: TestClient, connect: Any
) -> None:
    node = slow_node(client, connect, delay=0.0)
    node.first_chunk_delay = 30.0  # the model is still reading the prompt: no byte yet
    conv = new_conv(client)
    pending = client.portal.start_task_soon(
        lambda: drive(client.app, f"/api/conversations/{conv}/messages", {"content": "Hallo"})
    )

    def registered() -> None:
        assert client.app.state.runs.running(uuid.UUID(conv)) is not None

    eventually(registered, timeout=10)
    # nobody knows the message yet (no event has been sent): the conversation is named
    assert client.post(f"/api/conversations/{conv}/stop", headers=AUTH).status_code == 200
    chunks = pending.result(timeout=15)
    assert [e for e, _ in events_of(chunks)] == ["stopped"]  # the reader hears the end
    detail = conversation(client, conv)
    assert [m["role"] for m in detail["messages"]] == ["user"]  # no empty bubble
    assert detail["running"] is False
    assert client.post(f"/api/conversations/{conv}/stop", headers=AUTH).status_code == 409


# --- extraction and checks keep running -----------------------------------------------------------


def test_the_note_is_still_written_when_the_tab_is_gone(client: TestClient, connect: Any) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    node.chunk_delay = 0.05
    node.answer_fn = model({"Hamburg": [note("Wohnt in Hamburg")]})
    conv = new_conv(client)
    leave(client, conv, "Ich wohne in Hamburg, nur zur Info.")
    eventually(
        lambda: _note_there(client),
        timeout=20,
    )


def _note_there(client: TestClient) -> None:
    assert [i["text"] for i in memory_items(client)] == ["Wohnt in Hamburg"]


# --- the hub goes down ------------------------------------------------------------------------


def test_a_hub_that_goes_down_in_the_middle_ends_the_answer_as_interrupted(
    client: TestClient, connect: Any
) -> None:
    slow_node(client, connect, delay=0.3)
    conv = new_conv(client)
    message_id = message_id_of(leave(client, conv, "Zähle bis zwölf"))
    run = client.app.state.runs.live(message_id)
    eventually(lambda: _has_text(run), timeout=10)
    ended = client.portal.call(client.app.state.runs.shutdown)  # what the lifespan does at the end
    assert ended == 1
    answer = answer_of(client, conv)
    assert answer is not None and answer["status"] == "interrupted" and answer["content"]
    assert conversation(client, conv)["running"] is False
    assert run is not None and run.finished  # no hanger: the task is over


def test_a_message_left_running_by_a_killed_hub_is_interrupted_at_the_next_start(
    client: TestClient, connect: Any, clean_db: str
) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    conv = new_conv(client)
    engine = create_engine(clean_db)
    with engine.begin() as conn:
        user = conn.execute(
            text("SELECT user_id FROM conversations WHERE id = :c"), {"c": conv}
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO messages (conversation_id, user_id, role, content, status) "
                "VALUES (:c, :u, 'assistant', 'Halbe Antwort', 'running')"
            ),
            {"c": conv, "u": user},
        )
    engine.dispose()
    assert client.portal.call(sweep_interrupted, client.app.state.sessionmaker) == 1
    answer = answer_of(client, conv)
    assert answer is not None and answer["status"] == "interrupted"
    assert answer["content"] == "Halbe Antwort"  # what was saved is kept
    assert client.portal.call(sweep_interrupted, client.app.state.sessionmaker) == 0


_: Callable[..., Any] | None = None
