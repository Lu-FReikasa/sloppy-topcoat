"""The coordinator (E3-08, D44): one question, one read-only check per machine, one report.

Real hub, real tool service, protocol-level fake tool nodes; only the model is a script that tells
the three kinds of call apart (plan, a check's loop, the merge)."""

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import FakeNode, eventually
from tests.test_agent import _assert, audit, finish, tool, wait_for
from tests.test_tools import grant, tools_node
from tests.users import make_user
from topcoat_hub.agent_loop import Completion
from topcoat_hub.models import DEV_ADMIN_ID  # noqa: F401

PS = {"containers": [{"name": "web", "state": "running"}], "total": 1, "truncated": False}


class CoordModel:
    """Answers by kind: ``plan`` (JSON or a function), the checks (per machine, each its own
    script of replies) and ``merge``."""

    def __init__(self, plan: Any, merge: Any = None, checks: dict[str, list[Any]] | None = None):
        self.plan, self.merge = plan, merge
        self.checks = {k: list(v) for k, v in (checks or {}).items()}
        self.kinds: list[str] = []
        self.inflight = self.peak = 0
        self.delay = 0.0
        self.block = False

    async def complete(self, messages: list[dict[str, str]], model: str | None, max_tokens: int):
        system = messages[0]["content"]
        if system.startswith("You split a question"):
            self.kinds.append("plan")
            reply = self.plan
        elif system.startswith("You merge the reports"):
            self.kinds.append("merge")
            reply = (
                self.merge
                if self.merge is not None
                else json.dumps({"summary": "merged", "next": "nothing"})
            )
        else:
            node = next(n for n in self.checks if f"- {n}:" in system)
            self.kinds.append(f"check:{node}")
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            try:
                while self.block:  # noqa: ASYNC110 - set from the test's thread
                    await asyncio.sleep(0.02)
                if self.delay:
                    await asyncio.sleep(self.delay)
                replies = self.checks[node]
                reply = replies.pop(0) if replies else finish("(script ended)")
            finally:
                self.inflight -= 1
        if isinstance(reply, Exception):
            raise reply
        return Completion(text=reply, tokens=10, model="scripted")


def check_script(node: str, claim: str) -> list[Any]:
    return [
        tool(node, "docker.ps"),
        finish(
            f"{node} is fine.",
            [{"claim": claim, "tag": "live", "evidence": ["E1"]}],
            nxt=f"look at {node} again tomorrow",
        ),
    ]


@pytest.fixture
def lab(client: TestClient, connect) -> dict[str, tuple[str, FakeNode]]:
    nodes = {}
    for name in ("vm-a", "vm-b"):
        node_id, node = tools_node(client, connect, name)
        node.capability_answers["docker.ps"] = PS
        nodes[name] = (node_id, node)
    return nodes


def use_model(client: TestClient, model: CoordModel, **limits: Any) -> CoordModel:
    import dataclasses

    agents = client.app.state.agents
    agents.loop.model = model
    agents.loop.limits = dataclasses.replace(
        agents.loop.limits, model_retry_seconds=0, tools_wait_seconds=0, **limits
    )
    s = client.app.state.settings
    s.coordinator_poll_seconds = 0.02
    return model


def start_coord(client: TestClient, prompt: str = "Is everything ok?", who=AUTH):
    return client.post(
        "/api/agent/tasks", headers=who, json={"prompt": prompt, "coordinator": True}
    )


def get(client: TestClient, tid: str, who=AUTH) -> dict[str, Any]:
    return client.get(f"/api/agent/tasks/{tid}", headers=who).json()


PLAN = json.dumps(
    {
        "checks": [
            {"node": "vm-a", "question": "Are the containers on vm-a healthy?"},
            {"node": "vm-b", "question": "Are the containers on vm-b healthy?"},
        ]
    }
)


def test_one_check_per_machine_and_one_merged_report(client: TestClient, lab) -> None:
    model = use_model(
        client,
        CoordModel(
            PLAN,
            checks={
                "vm-a": check_script("vm-a", "vm-a runs web"),
                "vm-b": check_script("vm-b", "vm-b runs web"),
            },
        ),
    )
    r = start_coord(client)
    assert r.status_code == 201 and r.json()["kind"] == "coordinator"
    t = wait_for(client, r.json()["id"], "done")
    assert model.kinds.count("plan") == 1 and model.kinds.count("merge") == 1
    # the children: ordinary tasks of the same user, one machine each, no internet, hidden
    kids = t["children"]
    assert sorted(c["node"] for c in kids) == ["vm-a", "vm-b"]
    assert all(c["status"] == "done" and c["tool_calls"] == 1 for c in kids)
    assert {c["question"] for c in kids} == {
        "Are the containers on vm-a healthy?",
        "Are the containers on vm-b healthy?",
    }
    child = get(client, kids[0]["id"])
    assert (
        child["origin"] == "child" and child["parent_id"] == t["id"] and child["kind"] == "single"
    )
    listed = client.get("/api/agent/tasks", headers=AUTH).json()
    assert [x["id"] for x in listed] == [t["id"]]  # the checks are not on the Tasks page
    # the merged diagnosis: the model's summary, the children's findings with their machine, and
    # the evidence copied so that every finding still points at what it rests on
    d = t["diagnosis"]
    assert d["summary"] == "merged"
    assert sorted((f["node"], f["claim"], f["tag"]) for f in d["findings"]) == [
        ("vm-a", "vm-a runs web", "live"),
        ("vm-b", "vm-b runs web", "live"),
    ]
    refs = {f["node"]: f["evidence"] for f in d["findings"]}
    assert sorted(r for rs in refs.values() for r in rs) == ["E1", "E2"]
    ev = {e["ref"]: e for e in t["evidence"]}
    for node, rs in refs.items():
        assert ev[rs[0]]["source"]["node"] == node and ev[rs[0]]["tag"] == "live"
    assert "vm-a: look at vm-a again tomorrow" in d["next"] or d["next"] == "nothing"
    # the coordinator looked at nothing itself: every audited call belongs to a check
    child_ids = {c["id"] for c in kids}
    for node_id, _ in lab.values():
        assert {str(r["task_id"]) for r in audit(node_id)} <= child_ids
    assert t["proposals"] == []


def test_a_plan_that_names_no_known_machine_falls_back_to_one_check_each(
    client: TestClient, lab
) -> None:
    model = use_model(
        client,
        CoordModel(
            json.dumps({"checks": [{"node": "mars", "question": "?"}]}),
            checks={"vm-a": check_script("vm-a", "a"), "vm-b": check_script("vm-b", "b")},
        ),
    )
    t = wait_for(client, start_coord(client).json()["id"], "done")
    assert sorted(c["node"] for c in t["children"]) == ["vm-a", "vm-b"]
    assert any("one check per machine" in s["data"].get("text", "") for s in t["steps"])
    assert model.kinds.count("plan") == 1


def test_a_plan_is_cut_to_the_machines_that_exist_and_the_limit(client: TestClient, lab) -> None:
    client.app.state.settings.coordinator_max_children = 1
    use_model(
        client,
        CoordModel(
            PLAN, checks={"vm-a": check_script("vm-a", "a"), "vm-b": check_script("vm-b", "b")}
        ),
    )
    t = wait_for(client, start_coord(client).json()["id"], "done")
    assert [c["node"] for c in t["children"]] == ["vm-a"]


def test_one_machine_needs_no_plan(client: TestClient, connect) -> None:
    _, node = tools_node(client, connect, "vm-a")
    node.capability_answers["docker.ps"] = PS
    model = use_model(client, CoordModel("unused", checks={"vm-a": check_script("vm-a", "a")}))
    t = wait_for(client, start_coord(client).json()["id"], "done")
    assert "plan" not in model.kinds and [c["node"] for c in t["children"]] == ["vm-a"]


def test_a_check_that_fails_is_reported_and_the_rest_still_merge(client: TestClient, lab) -> None:
    use_model(
        client,
        CoordModel(
            PLAN,
            checks={"vm-a": check_script("vm-a", "a"), "vm-b": ["no json", "no json", "no json"]},
        ),
    )
    t = wait_for(client, start_coord(client).json()["id"], "done")
    status = {c["node"]: c["status"] for c in t["children"]}
    assert status == {"vm-a": "done", "vm-b": "failed"}
    assert any(n.startswith("vm-b: no result") for n in t["diagnosis"]["notes"])
    assert [f["node"] for f in t["diagnosis"]["findings"]] == ["vm-a"]


def test_when_every_check_fails_the_coordinator_fails_too(client: TestClient, lab) -> None:
    use_model(
        client,
        CoordModel(PLAN, checks={"vm-a": ["x", "x", "x"], "vm-b": ["x", "x", "x"]}),
    )
    t = wait_for(client, start_coord(client).json()["id"], "failed")
    assert t["stop_reason"] == "children_failed" and t["diagnosis"]["findings"] == []


def test_a_model_that_cannot_merge_still_gives_a_report(client: TestClient, lab) -> None:
    use_model(
        client,
        CoordModel(
            PLAN,
            merge="not json",
            checks={"vm-a": check_script("vm-a", "a"), "vm-b": check_script("vm-b", "b")},
        ),
    )
    t = wait_for(client, start_coord(client).json()["id"], "done")
    assert "[vm-a] vm-a is fine." in t["summary"] and "[vm-b] vm-b is fine." in t["summary"]


def test_only_a_few_checks_run_at_a_time(client: TestClient, lab) -> None:
    client.app.state.settings.coordinator_parallel = 1
    model = use_model(
        client,
        CoordModel(
            PLAN, checks={"vm-a": check_script("vm-a", "a"), "vm-b": check_script("vm-b", "b")}
        ),
    )
    model.delay = 0.05
    wait_for(client, start_coord(client).json()["id"], "done")
    assert model.peak == 1
    # and with two at a time they do overlap
    client.app.state.settings.coordinator_parallel = 2
    model2 = use_model(
        client,
        CoordModel(
            PLAN, checks={"vm-a": check_script("vm-a", "a"), "vm-b": check_script("vm-b", "b")}
        ),
    )
    model2.delay = 0.1
    wait_for(client, start_coord(client).json()["id"], "done")
    assert model2.peak == 2


def test_canceling_the_coordinator_cancels_every_check(client: TestClient, lab) -> None:
    model = use_model(
        client,
        CoordModel(
            PLAN, checks={"vm-a": check_script("vm-a", "a"), "vm-b": check_script("vm-b", "b")}
        ),
    )
    model.block = True
    tid = start_coord(client).json()["id"]
    eventually(lambda: _assert(model.inflight == 2))
    r = client.post(f"/api/agent/tasks/{tid}/cancel", headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "canceled"
    t = get(client, tid)
    eventually(
        lambda: _assert(all(c["status"] == "canceled" for c in get(client, tid)["children"]))
    )
    assert t["status"] == "canceled"
    eventually(lambda: _assert(not client.app.state.agents._running))
    model.block = False


def test_the_time_budget_stops_the_checks_and_reports_what_there_is(
    client: TestClient, lab
) -> None:
    client.app.state.settings.coordinator_time_budget_seconds = 1.0
    model = use_model(
        client,
        CoordModel(
            PLAN, checks={"vm-a": check_script("vm-a", "a"), "vm-b": check_script("vm-b", "b")}
        ),
    )
    model.block = True
    t = wait_for(client, start_coord(client).json()["id"], "failed", "done")
    assert t["stop_reason"] in ("time_budget", "children_failed")
    assert all(c["status"] == "canceled" for c in t["children"])
    model.block = False


def test_a_user_only_gets_checks_on_machines_they_may_use(
    client: TestClient, lab, settings
) -> None:
    uid, key = make_user(settings.database_url, "anna")
    grant(client, lab["vm-a"][0], uid, ["docker.ps"])
    model = use_model(
        client,
        CoordModel(
            PLAN, checks={"vm-a": check_script("vm-a", "a"), "vm-b": check_script("vm-b", "b")}
        ),
    )
    r = start_coord(client, who=key)
    assert r.status_code == 201
    t = wait_for(client, r.json()["id"], "done", who=key)
    assert [c["node"] for c in t["children"]] == ["vm-a"]  # vm-b is not hers: not even planned
    assert "check:vm-b" not in model.kinds
    # another user sees none of it
    _, other = make_user(settings.database_url, "bob")
    assert client.get(f"/api/agent/tasks/{t['id']}", headers=other).status_code == 404
    assert (
        client.get(f"/api/agent/tasks/{t['children'][0]['id']}", headers=other).status_code == 404
    )


def test_the_coordinator_can_be_switched_off(client: TestClient, lab) -> None:
    client.app.state.settings.coordinator_enabled = False
    r = start_coord(client)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "coordinator_disabled"
