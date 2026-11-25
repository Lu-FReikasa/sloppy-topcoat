"""Model gateway (E1-05): /v1 endpoints, routing, loading, streaming, API keys."""

import json
import threading
import time
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import eventually, online_node
from topcoat_hub.gateway import Gateway, GatewayError
from topcoat_hub.node_policy import AppRow, NodePolicy

CHAT = {"model": "small", "messages": [{"role": "user", "content": "Hallo Welt"}]}


def _chat(client: TestClient, body: dict[str, Any], headers: dict[str, str] = AUTH):
    return client.post("/v1/chat/completions", headers=headers, json=body)


def test_chat_on_loaded_model_non_streaming(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    r = _chat(client, CHAT)
    assert r.status_code == 200, r.text
    assert r.headers["x-topcoat-node"] == "node-a"
    assert r.json()["choices"][0]["message"]["content"].endswith("Hallo Welt")
    sent = json.loads(__import__("base64").b64decode(node.proxy_requests[0]["body"]["body"]))
    assert sent["model"] == "small"
    assert node.proxy_requests[0]["body"]["path"] == "/v1/chat/completions"
    assert node.loads == 0


def test_streaming_answer(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    with client.stream(
        "POST", "/v1/chat/completions", headers=AUTH, json={**CHAT, "stream": True}
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        lines = [line for line in r.iter_lines() if line]
    assert lines[-1] == "data: [DONE]"
    deltas = [json.loads(line[6:]) for line in lines[:-1]]
    words = [
        d["choices"][0]["delta"]["content"]
        for d in deltas
        if d["choices"] and d["choices"][0]["delta"]
    ]
    assert [w.strip() for w in words[-2:]] == ["Hallo", "Welt"]


def test_loads_on_demand_once_for_concurrent_requests(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a")
    node.load_delay = 0.3
    results: list[int] = []

    def ask() -> None:
        results.append(_chat(client, CHAT).status_code)

    threads = [threading.Thread(target=ask) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results == [200, 200, 200]
    assert node.loads == 1
    assert len([a for a in node.received if a["body"]["name"] == "models.load"]) == 1


def test_routes_around_busy_nodes_and_answers_503(client: TestClient, connect) -> None:
    gaming = online_node(client, connect, "node-a", loaded=["small"])
    gaming.state = "busy_gaming"
    gaming.heartbeat()
    other = online_node(client, connect, "node-b")  # has "small" on disk only

    def a_gaming() -> None:
        assert client.get("/api/nodes", headers=AUTH).json()[0]["state"] == "busy_gaming"

    eventually(a_gaming)
    r = _chat(client, CHAT)
    assert r.status_code == 200, r.text
    assert r.headers["x-topcoat-node"] == "node-b"
    assert other.loads == 1
    assert not gaming.proxy_requests

    other.state = "yielded"
    other.heartbeat()
    eventually(lambda: _states(client) == {"node-a": "busy_gaming", "node-b": "yielded"})
    r = _chat(client, CHAT)
    assert r.status_code == 503
    assert r.headers["retry-after"] == "30"
    assert r.json()["error"]["code"] == "model_unavailable"


def _states(client: TestClient) -> dict[str, str]:
    return {n["name"]: n["state"] for n in client.get("/api/nodes", headers=AUTH).json()}


def test_node_order_and_pinning(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b", loaded=["small"])
    assert _chat(client, CHAT).headers["x-topcoat-node"] == "node-a"  # alphabetical
    state = client.app.state
    state.gateway.set_policy(NodePolicy(applications={"api": AppRow(nodes=("node-b", "all"))}))
    assert _chat(client, CHAT).headers["x-topcoat-node"] == "node-b"
    r = _chat(client, {**CHAT, "model": "small@node-a"})
    assert r.headers["x-topcoat-node"] == "node-a"
    r = _chat(client, {**CHAT, "model": "small@node-x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "node_not_found"


def test_errors(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    r = _chat(client, {**CHAT, "model": "nope"})
    assert r.status_code == 404
    assert r.json()["error"] == {
        "message": "model 'nope' is not on any node",
        "type": "not_found_error",
        "code": "model_not_found",
    }
    assert _chat(client, {"messages": []}).status_code == 400
    r = client.post("/v1/chat/completions", headers=AUTH, content=b"{no json")
    assert r.status_code == 400
    big = {**CHAT, "pad": "x" * (1024 * 1024)}
    assert _chat(client, big).status_code == 413
    assert _chat(client, CHAT, headers={}).status_code == 401


def test_reloads_when_the_node_unloaded_meanwhile(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    node.not_loaded_once.add("small")
    r = _chat(client, CHAT)
    assert r.status_code == 200, r.text
    assert len(node.proxy_requests) == 2
    assert [a["body"]["name"] for a in node.received] == ["models.load"]


def test_permission_is_required(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    cfg = client.get(f"/api/nodes/{node.node_id}", headers=AUTH).json()["config"]
    cfg["allowed_actions"].remove("models.proxy")
    assert (
        client.put(f"/api/nodes/{node.node_id}/config", headers=AUTH, json=cfg).status_code == 200
    )
    r = _chat(client, CHAT)
    assert r.status_code == 503
    assert not node.proxy_requests
    assert client.get("/v1/models", headers=AUTH).json()["data"] == []


def test_client_disconnect_cancels_the_stream(client: TestClient, connect) -> None:
    """TestClient buffers streamed responses, so the disconnect is simulated the way
    Starlette does it: the response body iterator is closed early."""
    node = online_node(client, connect, "node-a", loaded=["small"])
    node.chunk_delay = 0.2
    gateway: Gateway = client.app.state.gateway

    async def read_one_and_leave() -> bytes:
        up = await gateway.forward(
            "small", "POST", "/v1/chat/completions", {**CHAT, "stream": True}
        )
        first = await anext(up.body)
        await up.body.aclose()
        return first

    assert client.portal.call(read_one_and_leave).startswith(b"data: ")
    stream_id = node.proxy_requests[0]["body"]["stream_id"]
    eventually(lambda: _assert_in(stream_id, node.cancels))


def _assert_in(item: str, items: list[str]) -> None:
    assert item in items


def test_models_list(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b")
    r = client.get("/v1/models", headers=AUTH)
    assert r.status_code == 200
    data = {m["id"]: m for m in r.json()["data"]}
    assert set(data) == {"small", "big"}
    assert data["small"]["object"] == "model"
    # the order of the nodes is not promised
    assert sorted(data["small"]["topcoat"]["nodes"], key=lambda n: n["node"]) == [
        {"node": "node-a", "loaded": True, "state": "available"},
        {"node": "node-b", "loaded": False, "state": "available"},
    ]


def test_api_keys(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    r = client.post("/api/keys", headers=AUTH, json={"name": "editor"})
    assert r.status_code == 201, r.text
    created = r.json()
    key = created["key"]
    assert key.startswith("tc_") and len(key) > 30
    assert created["prefix"] == key[:10]
    listed = client.get("/api/keys", headers=AUTH).json()
    assert [k["name"] for k in listed] == ["editor"]
    assert "key" not in listed[0]
    bearer = {"Authorization": f"Bearer {key}"}
    assert _chat(client, CHAT, headers=bearer).status_code == 200
    assert client.get("/api/keys", headers=AUTH).json()[0]["last_used_at"] is not None
    # A gateway key does not open the admin API.
    assert client.get("/api/nodes", headers=bearer).status_code == 403
    assert client.delete(f"/api/keys/{created['id']}", headers=AUTH).status_code == 204
    assert _chat(client, CHAT, headers=bearer).status_code == 401
    assert client.get("/api/keys", headers=AUTH).json() == []
    assert client.delete(f"/api/keys/{created['id']}", headers=AUTH).status_code == 404
    assert client.post("/api/keys", headers=AUTH, json={"name": ""}).status_code == 422


def test_node_gone_mid_stream_ends_the_answer(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    node.chunk_delay = 0.3
    gateway: Gateway = client.app.state.gateway

    async def start():
        up = await gateway.forward(
            "small", "POST", "/v1/chat/completions", {**CHAT, "stream": True}
        )
        await anext(up.body)
        return up

    up = client.portal.call(start)
    node.close()

    async def rest() -> list[bytes]:
        return [chunk async for chunk in up.body]

    begin = time.monotonic()
    remaining = client.portal.call(rest)
    assert b"data: [DONE]" not in b"".join(remaining)
    assert time.monotonic() - begin < 5


def test_cancelled_response_task_still_cancels_and_frees_the_slot(
    client: TestClient, connect
) -> None:
    """uvicorn ends a response by cancelling its task group when the client leaves; awaiting
    in the stream's cleanup would be cancelled again there (found while building E2-01)."""
    import anyio

    node = online_node(client, connect, "node-a", loaded=["small"])
    node.chunk_delay = 0.2
    gateway: Gateway = client.app.state.gateway

    async def scenario() -> int:
        up = await gateway.forward(
            "small", "POST", "/v1/chat/completions", {**CHAT, "stream": True}
        )

        async def consume() -> None:
            async for _ in up.body:
                pass

        async with anyio.create_task_group() as tg:

            async def stopper() -> None:
                await anyio.sleep(0.3)
                tg.cancel_scope.cancel()

            tg.start_soon(consume)
            tg.start_soon(stopper)
        slot = next(iter(gateway._slots.values()))
        return slot._value, slot.capacity

    free, capacity = client.portal.call(scenario)
    assert free == capacity
    stream_id = node.proxy_requests[0]["body"]["stream_id"]
    eventually(lambda: _assert_in(stream_id, node.cancels))


def test_response_close_before_the_body_runs_frees_the_slot(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    node.chunk_delay = 0.2
    gateway: Gateway = client.app.state.gateway

    async def scenario() -> int:
        up = await gateway.forward(
            "small", "POST", "/v1/chat/completions", {**CHAT, "stream": True}
        )
        up.close()  # the response never iterated the body
        up.close()  # idempotent
        slot = next(iter(gateway._slots.values()))
        return slot._value, slot.capacity

    free, capacity = client.portal.call(scenario)
    assert free == capacity
    eventually(lambda: _assert_in(node.proxy_requests[0]["body"]["stream_id"], node.cancels))


def test_fallback_is_reported(client: TestClient, connect) -> None:
    gaming = online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b", loaded=["small"])
    gateway: Gateway = client.app.state.gateway

    async def ask() -> tuple[str, str | None, str | None]:
        up = await gateway.forward("small", "POST", "/v1/chat/completions", CHAT)
        try:
            return up.node, up.fallback_from, up.fallback_state
        finally:
            up.close()

    assert client.portal.call(ask) == ("node-a", None, None)
    gaming.state = "busy_gaming"
    gaming.heartbeat()
    eventually(lambda: _assert_state(client, "node-a", "busy_gaming"))
    assert client.portal.call(ask) == ("node-b", "node-a", "busy_gaming")


def _assert_state(client: TestClient, name: str, state: str) -> None:
    assert _states(client)[name] == state


def test_a_paused_node_gets_no_new_work(client: TestClient, connect) -> None:
    # E2-05 B: the person at that machine comes first; the model is on another node too
    paused = online_node(client, connect, "node-a", loaded=["small"])
    paused.state = "paused"
    paused.heartbeat()
    other = online_node(client, connect, "node-b", loaded=["small"])
    eventually(lambda: _states(client) == {"node-a": "paused", "node-b": "available"})
    r = _chat(client, CHAT)
    assert r.status_code == 200 and r.headers["x-topcoat-node"] == "node-b"
    assert not paused.proxy_requests

    other.state = "paused"
    other.heartbeat()
    eventually(lambda: _states(client) == {"node-a": "paused", "node-b": "paused"})
    r = _chat(client, CHAT)
    assert r.status_code == 503 and r.json()["error"]["code"] == "model_unavailable"


def test_the_reason_a_load_was_refused_reaches_the_caller(client: TestClient, connect) -> None:
    from topcoat_hub.gateway import GatewayError

    node = online_node(client, connect, "node-a", loaded=["small"])
    node.not_loaded_once.update({"small"})  # unloaded meanwhile: the hub loads it again …
    node.load_error = ("insufficient_memory", "not enough free RAM: needs ~21000 MB, 9000 MB free")
    r = _chat(client, CHAT)  # … and the node refuses
    assert r.status_code == 503 and r.json()["error"]["code"] == "model_unavailable"
    assert "not enough free RAM" in r.json()["error"]["message"]
    assert "node-a" in r.json()["error"]["message"]
    # the structured reason for callers that act on it (the agent)
    node.not_loaded_once.update({"small"})
    gateway = client.app.state.gateway
    try:
        client.portal.call(gateway.call, "small", "/v1/chat/completions", {"messages": []})
    except GatewayError as exc:
        assert exc.reason == "insufficient_memory" and exc.status == 503
    else:
        raise AssertionError("no error")


def _load_args(node: Any) -> list[dict[str, Any]]:
    return [a["body"]["args"] for a in node.received if a["body"]["name"] == "models.load"]


def _ask_with(client: TestClient, min_context: int) -> None:
    gateway: Gateway = client.app.state.gateway

    def known() -> None:
        assert "small" in [m["id"] for m in client.get("/v1/models", headers=AUTH).json()["data"]]

    eventually(known)
    client.portal.call(
        lambda: gateway.call("small", "/v1/chat/completions", CHAT, min_context=min_context)
    )


def test_an_agent_load_asks_for_the_context_tools_need(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a")  # "small" is on disk, not loaded
    _ask_with(client, 16384)
    # a floor, not a size: the node sizes the context itself (test_gateway_context.py)
    assert _load_args(node) == [{"model": "small", "min_context_length": 16384}]


def test_a_load_without_a_minimum_leaves_the_context_to_the_node(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a")
    _ask_with(client, 0)
    assert _load_args(node) == [{"model": "small"}]


def test_an_older_node_with_a_large_enough_context_is_left_alone(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a")
    node.legacy = True  # from before min_context_length: the hub decides by the node's config
    cfg = client.get(f"/api/nodes/{node.node_id}", headers=AUTH).json()["config"]
    cfg["runner"]["model_context_lengths"] = {"small": 32768}
    assert (
        client.put(f"/api/nodes/{node.node_id}/config", headers=AUTH, json=cfg).status_code == 200
    )
    _ask_with(client, 16384)
    # the first try with the floor is refused, the second one carries nothing: the node's own
    # 32768 is never lowered
    assert _load_args(node)[-1] == {"model": "small"}


def test_an_older_node_with_a_too_small_context_is_asked_for_more(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a")
    node.legacy = True
    cfg = client.get(f"/api/nodes/{node.node_id}", headers=AUTH).json()["config"]
    cfg["runner"]["default_context_length"] = 4096
    assert (
        client.put(f"/api/nodes/{node.node_id}/config", headers=AUTH, json=cfg).status_code == 200
    )
    _ask_with(client, 16384)
    assert _load_args(node)[-1] == {"model": "small", "context_length": 16384}


def _bench(client: TestClient, node_name: str, model: str, tg: float) -> None:
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy import select

    from topcoat_hub.models import Node, NodeBenchmark

    state = client.app.state

    async def go() -> None:
        async with state.sessionmaker() as db:
            node = (await db.execute(select(Node).where(Node.name == node_name))).scalar_one()
            db.add(
                NodeBenchmark(
                    node_id=node.id,
                    benchmark_id=uuid.uuid4(),
                    model=model,
                    backend="vulkan",
                    threads=4,
                    n_gpu_layers=99,
                    pp_tps=100.0,
                    tg_tps=tg,
                    ts=datetime.now(UTC),
                )
            )
            await db.commit()

    client.portal.call(go)


def test_a_fast_node_that_must_load_beats_a_slow_loaded_one(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b")  # "small" only on disk
    client.app.state.settings.gateway_load_mb_per_second = 1e-4  # a load takes about 10 s
    gateway: Gateway = client.app.state.gateway

    async def first() -> str:
        return (await gateway.route("small")).candidates[0].name

    # No measured speed: the loaded node answers first, as before.
    assert client.portal.call(first) == "node-a"
    _bench(client, "node-a", "small", 5.0)
    assert client.portal.call(first) == "node-a"  # one side is still unmeasured
    _bench(client, "node-b", "small", 60.0)
    assert client.portal.call(first) == "node-b"
    # The loaded node is faster to a reply once its speed is close: the load time decides.
    _bench(client, "node-a", "small", 59.0)
    assert client.portal.call(first) == "node-a"


def test_idle_ok_needs_the_target_node_not_just_any_node(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    state = client.app.state

    async def ok() -> bool:
        return await state.memory.idle_ok()

    def background_is(model: str) -> None:
        state.gateway.set_policy(
            NodePolicy(applications={"background": AppRow(models=(model,), nodes=("node-a",))})
        )

    background_is("small")
    assert client.portal.call(ok) is True
    # A node is up, but the model of the background work is on none of them.
    background_is("nowhere")
    assert client.portal.call(ok) is False


def test_measured_speeds_are_averaged_and_beat_the_benchmark(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b")  # "small" only on disk
    state = client.app.state
    state.settings.gateway_load_mb_per_second = 1e-4  # a load takes about 10 s by default
    gateway: Gateway = state.gateway

    async def first() -> str:
        return (await gateway.route("small")).candidates[0].name

    async def speeds(name: str) -> tuple[float | None, float | None]:
        from sqlalchemy import select

        from topcoat_hub.models import Node, NodeSpeed

        async with state.sessionmaker() as db:
            node = (await db.execute(select(Node).where(Node.name == name))).scalar_one()
            row = await db.scalar(select(NodeSpeed).where(NodeSpeed.node_id == node.id))
            return (row.tg_tps, row.load_mb_s) if row else (None, None)

    # short or quick answers say nothing
    client.portal.call(gateway.note_speed, "node-a", "small", 10, 2.0)
    client.portal.call(gateway.note_speed, "node-a", "small", 100, 0.1)
    assert client.portal.call(speeds, "node-a") == (None, None)
    # real answers: 100 tokens in 20 s = 5 tok/s on a, 100 tokens in 2 s = 50 tok/s on b
    client.portal.call(gateway.note_speed, "node-a", "small", 100, 20.0)
    client.portal.call(gateway.note_speed, "node-b", "small", 100, 2.0)
    assert client.portal.call(speeds, "node-a")[0] == 5.0
    assert client.portal.call(first) == "node-b"  # 10 s load + 2 s beats 20 s
    # a is faster than it first looked: the average moves (0.7 * 5 + 0.3 * 100 = 33.5 tok/s)
    client.portal.call(gateway.note_speed, "node-a", "small", 100, 1.0)
    assert round(client.portal.call(speeds, "node-a")[0], 1) == 33.5
    assert client.portal.call(first) == "node-a"  # 3 s on a beats a 10 s load on b
    # a real load is noted (size 1000 bytes in 4 s) and used in place of the default load speed
    node_b = next(n for n in client.portal.call(gateway._nodes) if n.name == "node-b")
    client.portal.call(gateway.note_load, node_b.id, "small", 1.0, 0.5)  # too quick: ignored
    assert client.portal.call(speeds, "node-b")[1] is None
    client.portal.call(gateway.note_load, node_b.id, "small", 400.0, 2.0)
    assert client.portal.call(speeds, "node-b")[1] == 200.0


def test_background_work_prefers_its_nodes(client: TestClient, connect) -> None:
    from topcoat_hub.gateway import background_work

    online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b")  # has the model on disk, not loaded
    state = client.app.state
    state.gateway.set_policy(
        NodePolicy(applications={"background": AppRow(nodes=("node-b", "all"))})
    )
    gw = state.gateway

    @background_work
    async def background_route() -> str:
        return (await gw.route("small")).candidates[0].name

    async def chat_route() -> str:
        return (await gw.route("small")).candidates[0].name

    # chat keeps the normal order (loaded first), background work takes the preferred node
    assert client.portal.call(chat_route) == "node-a"
    assert client.portal.call(background_route) == "node-b"
    # with no preference nothing changes for background work either
    gw.set_policy(NodePolicy())
    assert client.portal.call(background_route) == "node-a"


def test_a_node_that_times_out_is_followed_by_the_next_one(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b", loaded=["small"])
    gateway: Gateway = client.app.state.gateway
    real_start = gateway._start

    async def start(cand, *args, **kwargs):  # type: ignore[no-untyped-def]
        if cand.name == "node-a":
            raise GatewayError(504, "timeout", "node node-a did not answer in time")
        return await real_start(cand, *args, **kwargs)

    gateway._start = start  # type: ignore[method-assign]

    async def ask() -> str:
        up = await gateway.forward("small", "POST", "/v1/chat/completions", CHAT)
        try:
            return up.node
        finally:
            up.close()

    assert client.portal.call(ask) == "node-b"


def test_a_timeout_on_the_only_node_stays_a_504(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    gateway: Gateway = client.app.state.gateway

    async def start(cand, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise GatewayError(504, "timeout", "node node-a did not answer in time")

    gateway._start = start  # type: ignore[method-assign]

    async def ask() -> int:
        try:
            await gateway.forward("small", "POST", "/v1/chat/completions", CHAT)
        except GatewayError as exc:
            return exc.status
        return 0

    assert client.portal.call(ask) == 504


def test_background_chat_is_answered_by_the_background_model(client: TestClient, connect) -> None:
    from topcoat_hub.gateway import background_work

    node = online_node(client, connect, "node-a", loaded=["small", "big"])
    state = client.app.state
    state.gateway.set_policy(
        NodePolicy(applications={"background": AppRow(models=("big",), nodes=("node-a",))})
    )
    gw = state.gateway
    body = {"messages": [{"role": "user", "content": "hi"}]}

    async def ask(model: str) -> None:
        await gw.call(model, "/v1/chat/completions", body)

    @background_work
    async def ask_in_background(model: str) -> None:
        await ask(model)

    def sent_models() -> list[str]:
        raw = [
            json.loads(__import__("base64").b64decode(r["body"]["body"]))
            for r in node.proxy_requests
        ]
        return [r["model"] for r in raw]

    client.portal.call(ask, "small")  # a chat request keeps the model it asked for
    client.portal.call(ask_in_background, "small")  # background work gets the background model
    client.portal.call(ask_in_background, "small@node-a")  # a pinned request is left alone
    assert sent_models() == ["small", "big", "small"]
    assert gw.background_model("small", None) == "small"  # outside background work


# --- one queue for the nodes of a pool (R2-Rest): the node is chosen late -----------------------


def test_a_second_request_goes_to_the_idle_node_instead_of_waiting_behind_the_busy_one(
    client: TestClient, connect
) -> None:
    a = online_node(client, connect, "node-a", loaded=["small"])
    b = online_node(client, connect, "node-b", loaded=["small"])
    a.chunk_delay = b.chunk_delay = 0.4  # both answers last long enough to overlap
    nodes: list[str] = []

    def ask() -> None:
        nodes.append(_chat(client, {**CHAT, "stream": True}).headers["x-topcoat-node"])

    threads = [threading.Thread(target=ask) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert sorted(nodes) == ["node-a", "node-b"]


def test_a_full_queue_refuses_at_once_with_a_hint(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    gateway = client.app.state.gateway
    gateway.settings.gateway_retry_after_seconds = 7

    async def full(*args: Any, **kwargs: Any) -> Any:
        from topcoat_hub.pool_queue import QueueFull

        raise QueueFull("source", 20, 20)

    gateway._pool.take = full
    r = _chat(client, CHAT)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "queue_full"
    assert r.headers["retry-after"] == "7"


def test_a_request_for_several_named_nodes_goes_to_whichever_is_free(
    client: TestClient, connect
) -> None:
    a = online_node(client, connect, "node-a", loaded=["small"])
    b = online_node(client, connect, "node-b", loaded=["small"])
    c = online_node(client, connect, "node-c", loaded=["small"])
    for n in (a, b, c):
        n.chunk_delay = 0.4
    nodes: list[str] = []

    def ask() -> None:
        body = {**CHAT, "model": "small@node-a,node-b", "stream": True}
        nodes.append(_chat(client, body).headers["x-topcoat-node"])

    threads = [threading.Thread(target=ask) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert sorted(nodes) == ["node-a", "node-b"]  # node-c was not named
    assert not c.proxy_requests
