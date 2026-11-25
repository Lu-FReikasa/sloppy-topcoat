"""The record of model requests (SCH-03 step 3): one row per request, never content, never fatal."""

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH
from tests.fakenode import eventually, online_node
from topcoat_hub.job_log import JobLog, set_job_context
from topcoat_hub.models import JobRecord

CHAT = {"model": "small", "messages": [{"role": "user", "content": "Hallo Welt"}]}


@pytest.fixture(autouse=True)
def _empty_record(migrated_db: str) -> None:
    """The records of earlier tests of this worker are not part of what a test counts."""
    engine = create_engine(migrated_db)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM job_records"))
    engine.dispose()


def _rows(db_url: str) -> list[dict[str, Any]]:
    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM job_records ORDER BY created_at")).mappings().all()
    engine.dispose()
    return [dict(r) for r in rows]


def test_a_served_request_leaves_one_record(client: TestClient, connect, migrated_db: str) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    r = client.post("/v1/chat/completions", headers=AUTH, json=CHAT)
    assert r.status_code == 200
    eventually(lambda: _assert(len(_rows(migrated_db)) == 1))
    row = _rows(migrated_db)[0]
    assert row["chosen_node"] == "node-a" and row["chosen_model"] == "small"
    assert row["outcome"] == "done" and row["error_code"] is None
    assert row["model_loaded"] is True and row["place"] == 0
    assert row["waited_ms"] >= 0 and row["duration_ms"] >= 0
    assert row["created_at"] <= row["finished_at"]


def test_the_record_names_a_request_that_loaded_the_model(
    client: TestClient, connect, migrated_db: str
) -> None:
    online_node(client, connect, "node-a")  # on disk, not loaded
    eventually(
        lambda: _assert(
            "small" in [m["id"] for m in client.get("/v1/models", headers=AUTH).json()["data"]]
        )
    )
    assert client.post("/v1/chat/completions", headers=AUTH, json=CHAT).status_code == 200
    eventually(lambda: _assert(len(_rows(migrated_db)) == 1))
    assert _rows(migrated_db)[0]["model_loaded"] is False


def test_the_context_of_the_caller_ends_up_in_the_record(
    client: TestClient, connect, migrated_db: str
) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    gateway = client.app.state.gateway
    conv, task = uuid.uuid4(), uuid.uuid4()

    async def ask() -> None:
        set_job_context(conversation_id=conv, task_id=task, application="projects")
        up = await gateway.forward("small", "POST", "/v1/chat/completions", CHAT)
        async for _ in up.body:
            pass

    client.portal.call(ask)
    eventually(lambda: _assert(len(_rows(migrated_db)) == 1))
    row = _rows(migrated_db)[0]
    assert (row["conversation_id"], row["task_id"], row["application"]) == (conv, task, "projects")
    assert row["user_id"] is None  # nobody named a person: hub work


def test_a_cancelled_request_is_recorded_as_cancelled(
    client: TestClient, connect, migrated_db: str
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    node.chunk_delay = 0.3
    gateway = client.app.state.gateway

    async def ask() -> None:
        up = await gateway.forward(
            "small", "POST", "/v1/chat/completions", {**CHAT, "stream": True}
        )
        up.close()  # the client went away before the answer ended

    client.portal.call(ask)
    eventually(lambda: _assert(len(_rows(migrated_db)) == 1))
    assert _rows(migrated_db)[0]["outcome"] == "cancelled"


def test_a_record_that_cannot_be_written_does_not_break_the_answer(
    client: TestClient, connect, migrated_db: str
) -> None:
    online_node(client, connect, "node-a", loaded=["small"])

    class Broken:
        def __call__(self) -> Any:
            raise RuntimeError("the database is gone")

    client.app.state.gateway.job_log = JobLog(Broken())  # type: ignore[arg-type]
    r = client.post("/v1/chat/completions", headers=AUTH, json=CHAT)
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"].endswith("Hallo Welt")


def test_the_record_holds_no_content() -> None:
    names = {c.name for c in JobRecord.__table__.columns}
    assert not names & {"content", "prompt", "body", "messages", "text", "answer"}
    assert json.dumps(sorted(names))  # nothing but metadata: who, where, how long, how it ended


def _assert(cond: bool) -> None:
    assert cond


def test_the_live_view_names_the_context_of_running_and_waiting_requests(
    client: TestClient, connect, migrated_db: str
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    node.chunk_delay = 0.4
    gateway = client.app.state.gateway
    conv = uuid.uuid4()
    seen: dict[str, Any] = {}

    async def scenario() -> None:
        set_job_context(conversation_id=conv, application="chat")
        first = await gateway.forward(
            "small", "POST", "/v1/chat/completions", {**CHAT, "stream": True}
        )
        seen["running"] = gateway.activity()
        # the node has one place: the second request waits in the pool queue (the view says why)
        import asyncio

        second = asyncio.ensure_future(
            gateway.forward("small", "POST", "/v1/chat/completions", {**CHAT, "stream": True})
        )
        await asyncio.sleep(0.2)
        seen["waiting"] = gateway.pool_waiting_jobs()
        first.close()
        (await second).close()

    client.portal.call(scenario)
    run = seen["running"][0]
    assert run["node"] == "node-a" and run["conversation_id"] == str(conv)
    assert run["application"] == "chat"
    waiting = seen["waiting"]
    assert len(waiting) == 1 and waiting[0]["conversation_id"] == str(conv)
    assert waiting[0]["kind"] == "chat" and waiting[0]["seconds"] >= 0 and waiting[0]["place"] == 0


def test_an_incognito_request_leaves_no_record(
    client: TestClient, connect, migrated_db: str
) -> None:
    """An incognito conversation is never written: a row that says who asked and when would be a
    trace of it."""
    online_node(client, connect, "node-a", loaded=["small"])
    gateway = client.app.state.gateway

    async def ask() -> None:
        await gateway.call("small", "/v1/chat/completions", CHAT, label="incognito")

    client.portal.call(ask)
    client.portal.call(__import__("asyncio").sleep, 0.3)  # a write would have landed by now
    assert _rows(migrated_db) == []


# --- what the model reported about itself (tokens, cache, times, finish) ------------------------

LLAMA_END = (
    b'data: {"choices":[{"finish_reason":"stop","index":0,"delta":{}}],'
    b'"usage":{"completion_tokens":212,'
    b'"prompt_tokens":3186,"total_tokens":3398,"prompt_tokens_details":{"cached_tokens":2048}},'
    b'"timings":{"cache_n":2048,"prompt_n":1138,"prompt_ms":812.5,"predicted_n":212,"predicted_ms":6020.25}}\n\n'
    b"data: [DONE]\n\n"
)


def test_the_tail_reads_the_numbers_llama_cpp_ends_an_answer_with() -> None:
    from topcoat_hub.gateway import StatsTail

    tail = StatsTail()
    tail.feed(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
    tail.feed(LLAMA_END)
    assert tail.stats() == {
        "tokens": 212,
        "prompt_tokens": 3186,
        "cached_tokens": 2048,
        "prompt_ms": 812,
        "predicted_ms": 6020,
        "finish_reason": "stop",
    }


def test_the_tail_keeps_only_the_end_and_never_grows() -> None:
    from topcoat_hub.gateway import TAIL_BYTES, StatsTail

    tail = StatsTail()
    for _ in range(500):
        tail.feed(b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n')
    tail.feed(LLAMA_END)
    assert len(tail.buf) <= TAIL_BYTES
    assert tail.stats()["tokens"] == 212


def test_an_answer_without_numbers_leaves_them_empty() -> None:
    from topcoat_hub.gateway import StatsTail

    tail = StatsTail()
    tail.feed(b"no json at all")
    assert tail.stats() == {}


def test_a_served_request_records_tokens_and_how_it_ended(
    client: TestClient, connect, migrated_db: str
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    node.answer_fn = lambda req: "an answer"  # a whole answer, with the usage of the fake node
    assert client.post("/v1/chat/completions", headers=AUTH, json=CHAT).status_code == 200
    eventually(lambda: _assert(len(_rows(migrated_db)) == 1))
    row = _rows(migrated_db)[0]
    assert row["tokens"] == 20 and row["prompt_tokens"] == 50  # the numbers of the fake node
    streamed = {**CHAT, "stream": True}
    assert client.post("/v1/chat/completions", headers=AUTH, json=streamed).status_code == 200
    eventually(lambda: _assert(len(_rows(migrated_db)) == 2))
    row = _rows(migrated_db)[1]
    assert row["tokens"] is not None and row["finish_reason"] == "stop"
