"""The table of applications (SCH-04, D85): one row per application decides model and node.

A row that has models or nodes decides for its application; a row that says nothing leaves the
choice to the older lists (roles), which stay until the page edits the table (UI-02)."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import FakeNode
from tests.test_model_selection_baseline import world as world  # the fixture, shared
from topcoat_hub.job_log import set_job_context
from topcoat_hub.model_resolver import Choice
from topcoat_hub.node_policy import AppRow, PolicyError, parse_policy

# --- the table as data ----------------------------------------------------------------------


def table(**rows: dict[str, Any]) -> dict[str, AppRow]:
    return parse_policy({"applications": rows}).applications


def test_a_row_has_a_priority_models_and_nodes() -> None:
    rows = table(
        research={
            "priority": "normal",
            "models": ["big", "small"],
            "nodes": ["node-amd", "all"],
        },
        check={"models": ["small", "same_as_answer"]},
    )
    assert rows["research"] == AppRow("normal", ("big", "small"), ("node-amd", "all"))
    assert rows["check"].models == ("small", "same_as_answer")
    # it comes back as it went in
    again = parse_policy(
        parse_policy({"applications": {"research": rows["research"].to_json()}}).to_json()
    )
    assert again.applications["research"] == rows["research"]


def test_a_row_without_any_setting_is_no_row() -> None:
    assert table(dev={}, chat={"models": []}) == {}


@pytest.mark.parametrize(
    "bad",
    [
        {"nope": {"models": ["a"]}},  # not an application
        {"dev": {"priority": "urgent"}},
        {"dev": {"models": ["a", "a"]}},
        {"dev": {"models": ["a@node-amd"]}},  # a model is a name, the node is its own list
        {"dev": {"models": ["same_as_answer"]}},  # only the system rows follow the answer
        {"dev": {"nodes": ["all", "node-amd"]}},  # all stands for the rest, so it comes last
        {"dev": {"colour": "red"}},
    ],
)
def test_a_row_that_does_not_make_sense_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(PolicyError):
        parse_policy({"applications": bad})


# --- the choice ------------------------------------------------------------------------------


def put_table(client: TestClient, rows: dict[str, Any]) -> Any:
    return client.put("/api/scheduling/applications", headers=AUTH, json={"rows": rows})


def resolve(client: TestClient, role: str, **kw: Any) -> Choice:
    return client.portal.call(lambda: client.app.state.gateway.resolver.resolve(role, **kw))


def test_a_row_decides_for_its_application_and_an_application_without_one_gets_the_default(
    client: TestClient, world: dict[str, FakeNode]
) -> None:
    r = put_table(client, {"research": {"models": ["small"], "nodes": ["node-laptop"]}})
    assert r.status_code == 200, r.text
    got = resolve(client, "answer", application="research")
    assert (got.node, got.model) == ("node-laptop", "small")
    assert "the row research of the table" in got.reason
    # an application without a row has no list of the hub any more: the default model answers
    got = resolve(client, "answer", application="dev")
    assert got.node is None and "no row of the table decides" in got.reason
    assert resolve(client, "answer").node is None  # work of no application is the chat's
    # the agent of the application uses its row as well (D85: no role agent of its own)
    assert resolve(client, "agent", application="research").node == "node-laptop"


def test_the_best_model_of_a_row_comes_first_on_any_node_it_allows(
    client: TestClient, world: dict[str, FakeNode]
) -> None:
    put_table(client, {"dev": {"models": ["big", "small"], "nodes": ["node-laptop", "node-amd"]}})
    got = resolve(client, "answer", application="dev")
    # the best model first, on the first node of the row that can take it
    assert (got.node, got.model) == ("node-laptop", "big")
    put_table(client, {"dev": {"models": ["big", "small"], "nodes": ["node-amd", "node-laptop"]}})
    assert resolve(client, "answer", application="dev").node == "node-amd"


def test_a_row_without_nodes_means_all_nodes_and_without_models_the_best_by_speed(
    client: TestClient, world: dict[str, FakeNode]
) -> None:
    put_table(client, {"admin": {"models": ["small"]}})
    assert resolve(client, "answer", application="admin").model == "small"
    put_table(client, {"admin": {"nodes": ["node-laptop"], "priority": "high"}})
    got = resolve(client, "answer", application="admin")
    assert got.node == "node-laptop"


def test_a_row_of_a_priority_only_decides_nothing(
    client: TestClient, world: dict[str, FakeNode]
) -> None:
    put_table(client, {"dev": {"priority": "high"}})
    got = resolve(client, "answer", application="dev")
    assert got.node is None and "no row of the table decides" in got.reason


def test_a_check_may_use_the_model_of_the_answer(
    client: TestClient, world: dict[str, FakeNode]
) -> None:
    put_table(client, {"check": {"models": ["same_as_answer"]}})
    got = resolve(client, "check", same_as="big@node-amd")
    assert got.model == "big" and "the row check of the table" in got.reason
    # nobody told the answer's model: the row has nothing to offer, the default model answers
    assert "no row of the table decides" in resolve(client, "check").reason


def test_the_agent_task_of_an_application_asks_for_its_row(
    client: TestClient, world: dict[str, FakeNode]
) -> None:
    put_table(client, {"research": {"models": ["small"], "nodes": ["node-laptop"]}})

    async def ask(application: str | None) -> str:
        set_job_context(application=application)
        return await client.app.state.agents.loop.model._model(None)  # type: ignore[no-any-return]

    assert client.portal.call(ask, "research") == "small@node-laptop"
    assert (
        client.portal.call(ask, None) == "big"
    )  # the default of the pool (the env name is not served)


def test_research_asks_the_node_for_the_context_of_a_research_run(
    client: TestClient, world: dict[str, FakeNode]
) -> None:
    settings = client.app.state.settings
    assert (
        resolve(client, "agent", application="research").min_context
        == settings.research_min_context
    )
    assert resolve(client, "agent", application="dev").min_context == settings.agent_min_context


# --- the API ---------------------------------------------------------------------------------


def test_the_table_is_stored_and_shown_with_what_each_row_comes_to(
    client: TestClient, world: dict[str, FakeNode]
) -> None:
    view = put_table(client, {"research": {"models": ["small"], "nodes": ["node-laptop"]}}).json()
    assert view["rows"] == {"research": {"models": ["small"], "nodes": ["node-laptop"]}}
    assert view["keys"][-3:] == ["check", "background", "api"]
    assert view["preview"]["research"]["node"] == "node-laptop"
    assert "table" in view["preview"]["research"]["reason"]
    assert client.get("/api/scheduling/applications", headers=AUTH).json()["rows"] == view["rows"]


def test_a_table_with_a_node_that_does_not_exist_is_refused_and_nothing_changes(
    client: TestClient, world: dict[str, FakeNode]
) -> None:
    put_table(client, {"research": {"models": ["small"]}})
    r = put_table(client, {"dev": {"nodes": ["node-nowhere"]}})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_table"
    r = put_table(client, {"nope": {"models": ["x"]}})
    assert r.status_code == 400
    assert list(client.get("/api/scheduling/applications", headers=AUTH).json()["rows"]) == [
        "research"
    ]


def test_changing_the_older_settings_keeps_the_table(
    client: TestClient, world: dict[str, FakeNode]
) -> None:
    put_table(client, {"research": {"models": ["small"], "nodes": ["node-laptop"]}})
    assert (
        client.put("/api/scheduling", headers=AUTH, json={"embedding_model": ""}).status_code == 200
    )
    rows = client.get("/api/scheduling/applications", headers=AUTH).json()["rows"]
    assert rows["research"]["models"] == ["small"]
    # an empty table (a reset of everything) is empty
    assert client.delete("/api/scheduling", headers=AUTH).status_code == 200
    assert client.get("/api/scheduling/applications", headers=AUTH).json()["rows"] == {}


def test_only_an_admin_reads_or_changes_the_table(client: TestClient) -> None:
    assert client.get("/api/scheduling/applications").status_code in (401, 403)
    assert client.put("/api/scheduling/applications", json={"rows": {}}).status_code in (401, 403)


def test_the_preview_of_a_check_follows_the_answer_and_names_a_row_nobody_can_serve(
    client: TestClient, world: dict[str, FakeNode]
) -> None:
    put_table(
        client,
        {
            "chat": {"models": ["big"], "nodes": ["node-amd"]},
            "check": {"models": ["same_as_answer"]},
        },
    )
    view = client.get("/api/scheduling/applications", headers=AUTH).json()
    # the chat's answer is "big" on the amd node: the check follows it
    assert view["preview"]["check"]["model"] == "big"
    assert "the row check of the table" in view["preview"]["check"]["reason"]
    # a row whose node is kept away from by the game: the first entry, with the reason
    put_table(client, {"dev": {"models": ["big"], "nodes": ["node-amd"]}})
    amd = world["node-amd"]
    amd.state = "busy_gaming"
    amd.heartbeat()
    from tests.fakenode import eventually
    from tests.test_model_selection_baseline import _is_in_state

    eventually(lambda: _is_in_state(client, "node-amd", "busy_gaming"))
    shown = client.get("/api/scheduling/applications", headers=AUTH).json()["preview"]["dev"]
    assert (shown["node"], shown["model"]) == ("node-amd", "big")
    assert "can take it now" in shown["reason"]


def test_the_priority_of_a_row_moves_its_requests_one_place(
    client: TestClient, world: dict[str, FakeNode]
) -> None:
    from topcoat_hub.job_log import set_job_context
    from topcoat_hub.job_queue import PRIORITY_ANSWER, PRIORITY_CHAT, PRIORITY_IDLE

    put_table(
        client,
        {
            "chat": {"priority": "low"},
            "research": {"priority": "high"},
            "dev": {"priority": "normal"},
            "background": {"priority": "high"},
        },
    )
    gateway = client.app.state.gateway

    def place(application: str | None, base: int) -> int:
        async def ask() -> int:
            set_job_context(application=application)
            return gateway._place(base)  # type: ignore[no-any-return]

        return client.portal.call(ask)

    assert place("chat", PRIORITY_CHAT) == PRIORITY_ANSWER  # one worse
    assert place("research", PRIORITY_ANSWER) == PRIORITY_CHAT  # one better
    assert place("research", PRIORITY_CHAT) == PRIORITY_CHAT  # never above the first place
    assert place("dev", PRIORITY_ANSWER) == PRIORITY_ANSWER  # normal leaves it
    assert place("admin", PRIORITY_IDLE) == PRIORITY_IDLE  # no row, no change
    assert place(None, PRIORITY_CHAT) == PRIORITY_CHAT


def test_a_stored_waiting_time_is_dropped_not_refused() -> None:
    """Rows stored before the waiting time left the table still carry it; they must still load
    (a policy that does not parse counts as unset and would wipe the admin's table)."""
    rows = table(research={"models": ["big"], "wait_seconds": 600}, chat={"wait_seconds": -1})
    assert rows["research"] == AppRow(None, ("big",), ())
    assert "chat" not in rows  # nothing else in the row: no row
    assert "wait_seconds" not in rows["research"].to_json()
