"""SCH-03 step 2: the node is chosen, and the model loaded, before the prompt is built."""

import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import eventually, online_node
from topcoat_hub.gateway import Gateway, GatewayError

CHAT = {"model": "small", "messages": [{"role": "user", "content": "Hallo Welt"}]}


def test_reserve_names_the_node_and_its_context_length(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"], context_length=4096)
    gateway: Gateway = client.app.state.gateway
    placement = client.portal.call(gateway.reserve, "small")
    assert placement.cand.name == "node-a" and placement.context_length == 4096
    assert placement.model == "small"


def test_reserve_loads_the_model_and_reports_what_the_node_has_then(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a")  # on disk, not loaded
    eventually(lambda: _known(client, "small"))
    gateway: Gateway = client.app.state.gateway
    placement = client.portal.call(gateway.reserve, "small")
    assert node.loads == 1  # loaded before any prompt exists
    assert placement.cand.name == "node-a"


def test_forward_runs_the_request_on_the_reserved_node(client: TestClient, connect) -> None:
    node_a = online_node(client, connect, "node-a", loaded=["small"], context_length=2048)
    node_b = online_node(client, connect, "node-b", loaded=["small"], context_length=8192)
    gateway: Gateway = client.app.state.gateway

    async def scenario() -> str:
        placement = await gateway.reserve("small@node-b")
        up = await gateway.forward(
            "small@node-b", "POST", "/v1/chat/completions", CHAT, placement=placement
        )
        async for _ in up.body:
            pass
        return placement.cand.name

    assert client.portal.call(scenario) == "node-b"
    assert len(node_b.proxy_requests) == 1 and not node_a.proxy_requests
    sent = json.loads(__import__("base64").b64decode(node_b.proxy_requests[0]["body"]["body"]))
    assert sent["model"] == "small"


def test_reserve_holds_no_place_in_the_queue(client: TestClient, connect) -> None:
    """Other model calls of the same answer (the memory search, a check) may need the node while
    the prompt is built: a reservation must not take its only place."""
    online_node(client, connect, "node-a", loaded=["small"])
    gateway: Gateway = client.app.state.gateway

    async def scenario() -> tuple[int, int]:
        await gateway.reserve("small")
        queue = next(iter(gateway._slots.values()), None)
        free_after_reserve = queue._value if queue is not None else 1
        up = await gateway.forward("small", "POST", "/v1/chat/completions", CHAT)
        up.close()
        return free_after_reserve, next(iter(gateway._slots.values())).capacity

    free, capacity = client.portal.call(scenario)
    assert free == capacity  # nothing was held between reserve and forward


def test_reserve_fails_like_forward_for_a_model_nobody_has(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    gateway: Gateway = client.app.state.gateway
    with pytest.raises(GatewayError) as err:
        client.portal.call(gateway.reserve, "no-such-model")
    assert err.value.status == 404 and err.value.code == "model_not_found"


def _known(client: TestClient, model: str) -> None:
    assert model in [m["id"] for m in client.get("/v1/models", headers=AUTH).json()["data"]]
