"""Who does what on which node: the admin's scheduling choices (node policy) and their effect on
the gateway's choice of node."""

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import online_node, pair
from topcoat_hub.gateway import Gateway, GatewayError, background_work
from topcoat_hub.node_policy import NodePolicy, PolicyError, load_policy, parse_policy


def _put(client: TestClient, body: dict):
    return client.put("/api/scheduling", headers=AUTH, json=body)


def _rows(client: TestClient, rows: dict):
    return client.put("/api/scheduling/applications", headers=AUTH, json={"rows": rows})


def test_the_policy_is_admin_only(client: TestClient) -> None:
    assert client.get("/api/scheduling").status_code == 401
    assert client.put("/api/scheduling", json={}).status_code == 401


def test_the_view_lists_the_nodes_and_the_rules_that_stay(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b")
    view = client.get("/api/scheduling", headers=AUTH).json()
    assert [n["name"] for n in view["nodes"]] == ["node-a", "node-b"]
    assert view["nodes"][0]["loaded"] == ["small"]
    assert view["paused_until"] is None and view["customised"] is False
    # the lists of the older view are gone: the table has its own call
    assert not {"roles", "app_roles", "defaults", "order", "tasks"} & set(view)


def test_a_node_that_is_not_in_the_row_of_the_gateway_is_not_used_for_it(
    client: TestClient, connect
) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b", loaded=["small"])
    r = _rows(client, {"api": {"nodes": ["node-b"]}})
    assert r.status_code == 200, r.text
    body = {"model": "small", "messages": [{"role": "user", "content": "Hi"}]}
    chat = client.post("/v1/chat/completions", headers=AUTH, json=body)
    assert chat.status_code == 200, chat.text
    assert chat.headers["x-topcoat-node"] == "node-b"
    # no node that has the model is in the row (its one node is registered but not connected):
    # a clear 503, not "model not found"
    pair(client, "node-c")
    assert _rows(client, {"api": {"nodes": ["node-c"]}}).status_code == 200
    gone = client.post("/v1/chat/completions", headers=AUTH, json=body)
    assert gone.status_code == 503
    assert gone.json()["error"]["code"] == "node_kept_free"


def test_background_work_skips_nodes_that_are_not_in_its_row(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b")
    gw: Gateway = client.app.state.gateway

    @background_work
    async def background_route() -> str:
        return (await gw.route("small")).candidates[0].name

    assert client.portal.call(background_route) == "node-a"
    _rows(client, {"background": {"nodes": ["node-b"]}})
    assert client.portal.call(background_route) == "node-b"

    # chat is not affected
    async def chat_route() -> str:
        return (await gw.route("small")).candidates[0].name

    assert client.portal.call(chat_route) == "node-a"


def test_the_nodes_of_the_rows_background_and_api_are_what_the_gateway_prefers(
    client: TestClient, connect
) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b")
    assert client.app.state.gateway.background_nodes == []
    r = _rows(
        client,
        {"background": {"nodes": ["node-b", "all"]}, "api": {"nodes": ["node-b", "node-a"]}},
    )
    assert r.status_code == 200, r.text
    gw = client.app.state.gateway
    assert gw.background_nodes == ["node-b"] and gw.order == ["node-b", "node-a"]
    # reset: nothing stored, no preference
    reset = client.delete("/api/scheduling", headers=AUTH).json()
    assert reset["customised"] is False
    assert gw.background_nodes == [] and gw.order == []


def test_pause_stops_background_work_and_resume_restores_it(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    gw: Gateway = client.app.state.gateway

    @background_work
    async def background_route() -> str:
        return (await gw.route("small")).candidates[0].name

    r = client.post("/api/scheduling/pause", headers=AUTH, json={"minutes": 30})
    assert r.status_code == 200 and r.json()["paused_until"]
    try:
        client.portal.call(background_route)
        raise AssertionError("background work must wait")
    except GatewayError as exc:
        assert exc.code == "background_paused"
        assert exc.status == 503
    assert client.portal.call(gw.any_node_available) is False
    # chat is not paused
    assert (
        client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "small", "messages": [{"role": "user", "content": "Hi"}]},
        ).status_code
        == 200
    )
    # a change of the table keeps the pause
    assert _rows(client, {"background": {"nodes": ["node-a"]}}).status_code == 200
    assert client.get("/api/scheduling", headers=AUTH).json()["paused_until"]
    resumed = client.post("/api/scheduling/pause", headers=AUTH, json={"minutes": 0}).json()
    assert resumed["paused_until"] is None
    assert client.portal.call(background_route) == "node-a"


def test_a_table_with_an_unknown_node_is_refused(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a")
    r = _rows(client, {"background": {"nodes": ["node-typo"]}})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_table"
    assert _rows(client, {"api": {"nodes": ["node-a", "node-a"]}}).status_code == 400
    assert (
        client.post("/api/scheduling/pause", headers=AUTH, json={"minutes": -1}).status_code == 422
    )
    # the older keys are not accepted any more
    assert _put(client, {"no_chat": ["node-a"]}).status_code == 422


def test_the_policy_survives_a_restart_and_a_broken_value_counts_as_unset() -> None:
    policy = parse_policy(
        {
            "applications": {"api": {"nodes": ["a", "all"]}},
            "paused_until": "2999-01-01T00:00:00+00:00",
        }
    )
    again = load_policy(policy.to_json())
    assert again == policy and again.paused()
    assert load_policy({"applications": "not a dict"}) == NodePolicy()
    assert load_policy(["junk"]) == NodePolicy()
    try:
        parse_policy({"surprise": 1})
        raise AssertionError("unknown keys are refused")
    except PolicyError:
        pass


def test_the_history_records_changes_pauses_and_manual_starts(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a")
    _rows(client, {"background": {"nodes": ["node-a"]}})
    client.post("/api/scheduling/pause", headers=AUTH, json={"minutes": 30})
    history = client.get("/api/scheduling", headers=AUTH).json()["history"]
    assert [h["kind"] for h in history] == ["pause", "table"]  # newest first
    assert "background" in history[1]["detail"] and "node-a" in history[1]["detail"]
    assert all(h["ok"] and h["actor"] for h in history)


def test_a_background_task_can_be_started_by_hand(client: TestClient, connect, monkeypatch) -> None:
    from topcoat_hub.api import scheduling

    online_node(client, connect, "node-a")
    started = []

    async def fake(request, db, user):
        started.append(user.name)
        return {"started": True}

    monkeypatch.setitem(scheduling.RUNNABLE, "care", fake)
    ok = client.post("/api/scheduling/run/care", headers=AUTH)
    assert ok.status_code == 202 and ok.json() == {"started": True} and started
    # paused: refused with the reason, and the refusal is in the history
    client.post("/api/scheduling/pause", headers=AUTH, json={"minutes": 30})
    paused = client.post("/api/scheduling/run/care", headers=AUTH)
    assert paused.status_code == 409
    assert paused.json()["detail"]["code"] == "background_paused"
    assert len(started) == 1
    history = client.get("/api/scheduling", headers=AUTH).json()["history"]
    runs = [h for h in history if h["kind"] == "run"]
    assert [(h["detail"], h["ok"]) for h in runs] == [("care", False), ("care", True)]


def test_manual_start_failures_are_logged_and_unknown_tasks_refused(
    client: TestClient,
) -> None:
    assert client.post("/api/scheduling/run/care").status_code == 401
    assert client.post("/api/scheduling/run/nope", headers=AUTH).status_code == 404
    r = client.post("/api/scheduling/run/consolidate", headers=AUTH)
    assert r.status_code == 409  # no node is connected
    history = client.get("/api/scheduling", headers=AUTH).json()["history"]
    assert history[0]["ok"] is False and history[0]["detail"].startswith("consolidate:")


def test_the_embedding_model_is_chosen_stored_and_offered(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a")
    view = client.get("/api/scheduling", headers=AUTH).json()
    assert view["embedding_model"] == "" and view["embedding_choices"] == []
    r = _put(client, {"embedding_model": "bge-m3-q8_0"})
    assert r.status_code == 200, r.text
    assert r.json()["embedding_model"] == "bge-m3-q8_0"
    assert "bge-m3-q8_0" in r.json()["embedding_choices"]
    assert client.app.state.gateway.policy.embedding_model == "bge-m3-q8_0"
    # a save that does not mention it keeps it; an empty string clears it
    assert _put(client, {}).json()["embedding_model"] == "bge-m3-q8_0"
    assert _put(client, {"embedding_model": ""}).json()["embedding_model"] == ""
    assert _put(client, {"embedding_model": "x@node-a"}).status_code == 400
