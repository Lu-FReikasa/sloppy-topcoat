"""The context of a load is sized by the node (consolidation step 2): the hub says how much a use
needs, the node picks the rest, an older node is asked the old way, and a loaded model with too
little context is reloaded, but only while its node does nothing else."""

import contextlib
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import FakeNode, eventually, online_node
from topcoat_hub.gateway import Gateway

CHAT: dict[str, Any] = {"messages": [{"role": "user", "content": "Hallo"}]}


def _lists(client: TestClient, model: str) -> None:
    assert model in {m["id"] for m in client.get("/v1/models", headers=AUTH).json()["data"]}


def ask(client: TestClient, model: str, min_context: int = 0) -> str:
    gateway: Gateway = client.app.state.gateway
    eventually(lambda: _lists(client, model))  # the node's inventory has arrived

    async def go() -> str:
        up = await gateway.forward(
            model, "POST", "/v1/chat/completions", CHAT, min_context=min_context
        )
        try:
            return up.node
        finally:
            up.close()

    return client.portal.call(go)


def loaded_context(client: TestClient, node: FakeNode, model: str) -> int | None:
    got = client.app.state.gateway
    views = client.portal.call(got._nodes)
    view = next(v for v in views if str(v.id) == node.node_id)
    return view.contexts.get(model)


def test_a_load_asks_for_at_least_and_the_node_sizes_it(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a")
    node.auto_context = 32768
    assert ask(client, "small", min_context=16384) == "node-a"
    assert node.load_args == [{"model": "small", "min_context_length": 16384}]
    eventually(lambda: _assert_context(client, node, "small", 32768))


def _assert_context(client: TestClient, node: FakeNode, model: str, want: int) -> None:
    assert loaded_context(client, node, model) == want


def test_a_load_without_a_need_asks_for_nothing(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a")
    assert ask(client, "small") == "node-a"
    assert node.load_args == [{"model": "small"}]


def test_an_older_node_is_asked_the_old_way_and_remembered(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a")
    node.legacy = True
    assert ask(client, "small", min_context=16384) == "node-a"
    # the first try was refused, the second one carried an explicit context
    assert node.load_args[0] == {"model": "small", "min_context_length": 16384}
    assert node.load_args[-1] == {"model": "small", "context_length": 16384}
    eventually(lambda: _assert_context(client, node, "small", 16384))
    gateway: Gateway = client.app.state.gateway
    assert len(gateway._legacy_nodes) == 1
    # another model on the same node goes the old way at once
    before = len(node.load_args)
    assert ask(client, "big", min_context=16384) == "node-a"
    assert node.load_args[before:] == [{"model": "big", "context_length": 16384}]


def test_a_loaded_model_with_too_little_context_is_loaded_again_when_the_node_is_idle(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"], context_length=4096)
    node.auto_context = 16384
    assert loaded_context(client, node, "small") == 4096
    assert ask(client, "small", min_context=16384) == "node-a"
    assert node.unloaded == ["small"]
    assert node.load_args and node.load_args[-1]["min_context_length"] == 16384
    eventually(lambda: _assert_context(client, node, "small", 16384))
    # enough now: the next request leaves it alone
    assert ask(client, "small", min_context=16384) == "node-a"
    assert node.unloaded == ["small"]


def test_a_node_that_is_working_keeps_its_small_context(client: TestClient, connect) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"], context_length=4096)
    gateway: Gateway = client.app.state.gateway

    async def busy() -> str:
        key = gateway._track("node-a", "small")  # somebody is being answered
        try:
            up = await gateway.forward(
                "small", "POST", "/v1/chat/completions", CHAT, min_context=16384
            )
            try:
                return up.node
            finally:
                up.close()
        finally:
            gateway._untrack(key)

    assert client.portal.call(busy) == "node-a"
    assert node.unloaded == [] and node.load_args == []
    assert loaded_context(client, node, "small") == 4096


def test_a_reload_that_cannot_be_done_is_not_tried_on_every_request(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"], context_length=4096)
    node.load_error = ("insufficient_memory", "no room")
    # the reload fails: the model is asked for again as it was (which fails here too), the
    # request itself still finds a way or reports it, and the next one does not unload again
    for _ in range(2):
        with contextlib.suppress(Exception):  # the failure of the request is not what is tested
            ask(client, "small", min_context=16384)
    assert node.unloaded == ["small"]
