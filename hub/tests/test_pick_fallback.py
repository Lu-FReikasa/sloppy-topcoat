"""A model or node the person picked wins; but a pick that cannot be served now does not end in an
error. The row of the program in the table of applications answers instead, and the answer says
whom it replaces (the label of any hand-over to the next node)."""

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import eventually, online_node
from tests.test_chat import new_conv, use_model
from tests.test_segments import ask


def put_chat_row(client: TestClient, **row: object) -> None:
    r = client.put("/api/scheduling/applications", headers=AUTH, json={"rows": {"chat": row}})
    assert r.status_code == 200, r.text


def node_state(client: TestClient, name: str, state: str) -> None:
    nodes = {n["name"]: n["state"] for n in client.get("/api/nodes", headers=AUTH).json()}
    assert nodes[name] == state


def test_a_pick_that_is_served_is_not_replaced(client: TestClient, connect) -> None:
    online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b", loaded=["small"])
    use_model(client, "small")
    put_chat_row(client, models=["small"], nodes=["node-b", "node-a"])
    conv = new_conv(client, model="small", node="node-a")
    _, events, _ = ask(client, conv, "eins")
    meta = events[0][1]
    assert (meta["node"], meta["fallback_from"]) == ("node-a", None)


def test_a_pick_whose_machine_is_gone_is_answered_by_the_row_of_the_table(
    client: TestClient, connect
) -> None:
    gone = online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b", loaded=["small"])
    use_model(client, "small")
    put_chat_row(client, models=["small"], nodes=["node-a", "node-b"])
    conv = new_conv(client, model="small", node="node-a")
    gone.close()
    eventually(lambda: node_state(client, "node-a", "offline"))
    status, events, _ = ask(client, conv, "zwei")
    meta = events[0][1]
    assert status == 200
    assert (meta["node"], meta["fallback_from"], meta["fallback_state"]) == (
        "node-b",
        "node-a",
        "offline",
    )


def test_without_a_row_a_pick_that_cannot_be_served_is_still_an_error(
    client: TestClient, connect
) -> None:
    gone = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client, "small")
    # the table of an earlier test is still in this worker's database: no row at all
    assert (
        client.put("/api/scheduling/applications", headers=AUTH, json={"rows": {}}).status_code
        == 200
    )
    conv = new_conv(client, model="small", node="node-a")
    gone.close()
    eventually(lambda: node_state(client, "node-a", "offline"))
    status, _, _r = ask(client, conv, "drei")
    assert status == 503

    def only_the_question() -> None:
        # the 503 reaches the client while the pipeline is still tidying up: its progress line
        # (an assistant message) is removed a moment after the answer was refused
        msgs = client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"]
        assert [m["role"] for m in msgs] == ["user"], msgs  # nothing lost, can be retried

    eventually(only_the_question)


def test_a_pick_outside_the_row_of_the_program_is_answered_by_the_row(
    client: TestClient, connect
) -> None:
    """A machine that is not in the row of the program is kept free of its work, a pick included
    (D85); the answer comes from the row and says so."""
    online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b", loaded=["small"])
    use_model(client, "small")
    put_chat_row(client, models=["small"], nodes=["node-b"])
    conv = new_conv(client, model="small", node="node-a")
    status, events, _ = ask(client, conv, "vier")
    meta = events[0][1]
    assert status == 200 and meta["node"] == "node-b" and meta["fallback_from"] == "node-a"
