"""Block programs through the hub: validation, a run on fake nodes, rights."""

import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import online_node

PROGRAM: dict[str, Any] = {
    "name": "write_and_check",
    "inputs": {"question": "text"},
    "stack": [
        {"b": "ask", "prompt": "{{question}}", "pick": {"rank": "largest"}, "out": "draft"},
        {"b": "check", "in": "draft", "question": "question", "out": "v"},
        {
            "b": "repeat_until",
            "cond": "v.ok",
            "stack": [
                {"b": "revise", "draft": "draft", "issues": "v", "out": "draft"},
                {"b": "check", "in": "draft", "question": "question", "out": "v"},
            ],
        },
    ],
}


def wait_done(client: TestClient, run_id: str, seconds: float = 20.0) -> dict[str, Any]:
    end = time.time() + seconds
    while time.time() < end:
        view = client.get(f"/api/blocks/runs/{run_id}", headers=AUTH).json()
        if view["status"] != "running":
            return view  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError("the run did not finish")


def test_catalogue_programs_and_validation(client: TestClient) -> None:
    cat = client.get("/api/blocks/catalogue", headers=AUTH).json()
    assert "compare" in cat["blocks"] and "text" in cat["types"]
    names = [p["name"] for p in client.get("/api/blocks/programs", headers=AUTH).json()]
    assert "research_report" in names
    ok = client.post("/api/blocks/validate", headers=AUTH, json={"program": PROGRAM}).json()
    assert ok["ok"] is True
    bad = {**PROGRAM, "stack": [{"b": "check", "in": "nothing", "out": "v"}]}
    res = client.post("/api/blocks/validate", headers=AUTH, json={"program": bad}).json()
    assert res["ok"] is False and "nothing" in res["error"]


def test_a_program_runs_on_the_pool_and_revises_until_the_check_is_satisfied(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small", "big"])
    seen: list[str] = []

    def answer(req: dict[str, Any]) -> str | None:
        system = req["messages"][0]["content"]
        user = req["messages"][-1]["content"]
        seen.append(req.get("model", ""))
        if system.startswith("You check"):
            return json.dumps(
                {"ok": "second" in user, "issues": [] if "second" in user else ["too thin"]}
            )
        if system.startswith("You revise"):
            return "second version"
        return "first version"

    node.answer_fn = answer
    node.stream_text_fn = answer  # the hub streams its own requests
    started = client.post(
        "/api/blocks/runs", headers=AUTH, json={"program": PROGRAM, "inputs": {"question": "why?"}}
    )
    assert started.status_code == 202, started.text
    view = wait_done(client, started.json()["id"])
    assert view["status"] == "ok", view
    assert view["variables"]["draft"] == "second version"
    assert view["variables"]["v"]["ok"] is True
    assert view["authors"]["draft"] == "big"  # the reviser is the author's model
    events = [s["event"] for s in view["steps"]]
    assert events[0] == "step" and events[-1] == "run"
    # a stranger cannot read the run
    assert client.get(f"/api/blocks/runs/{view['id']}").status_code in (401, 403)


def test_a_bad_program_or_name_is_refused(client: TestClient) -> None:
    r = client.post("/api/blocks/runs", headers=AUTH, json={"name": "nope", "inputs": {}})
    assert r.status_code == 404
    r = client.post("/api/blocks/runs", headers=AUTH, json={"inputs": {}})
    assert r.status_code == 422
    r = client.post(
        "/api/blocks/runs",
        headers=AUTH,
        json={
            "program": {"name": "x", "inputs": {"q": "text"}, "stack": [{"b": "nope"}]},
            "inputs": {"q": "a"},
        },
    )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_program"


def test_a_run_without_a_pool_model_fails_with_a_reason(client: TestClient) -> None:
    started = client.post(
        "/api/blocks/runs", headers=AUTH, json={"program": PROGRAM, "inputs": {"question": "q"}}
    )
    view = wait_done(client, started.json()["id"])
    assert view["status"] == "failed" and "no model" in view["error"]


def test_an_exclusive_pick_unloads_the_other_models_of_the_node(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small", "big"])
    node.answer_fn = lambda req: "an answer"
    node.stream_text_fn = lambda req: "an answer"
    program = {
        "name": "exclusive",
        "inputs": {"question": "text"},
        "stack": [
            {
                "b": "ask",
                "prompt": "{{question}}",
                "pick": {"fixed": "big@node-a", "exclusive": True},
                "out": "a",
            }
        ],
    }
    r = client.post(
        "/api/blocks/runs", headers=AUTH, json={"program": program, "inputs": {"question": "q"}}
    )
    view = wait_done(client, r.json()["id"])
    assert view["status"] == "ok", view
    loaded = {
        m["name"]: m["loaded"]
        for m in client.get("/api/scheduling/pools", headers=AUTH).json()["nodes"][0]["models"]
    }
    assert loaded["small"] is False and loaded["big"] is True


def test_programs_are_saved_listed_run_by_name_and_deleted(client: TestClient) -> None:
    copy = {**PROGRAM, "name": "my_copy", "title": "My copy"}
    r = client.put("/api/blocks/programs/my_copy", headers=AUTH, json={"program": copy})
    assert r.status_code == 200, r.text
    listed = client.get("/api/blocks/programs", headers=AUTH).json()
    mine = next(p for p in listed if p["name"] == "my_copy")
    assert mine["builtin"] is False and mine["title"] == "My copy"
    assert next(p for p in listed if p["name"] == "research_report")["builtin"] is True
    # it runs by name (no pool here, so it fails for want of a model, but it started)
    started = client.post(
        "/api/blocks/runs", headers=AUTH, json={"name": "my_copy", "inputs": {"question": "q"}}
    )
    assert started.status_code == 202
    # a shipped program is not overwritten, a name must match, an invalid program is refused
    assert (
        client.put(
            "/api/blocks/programs/research_report", headers=AUTH, json={"program": copy}
        ).status_code
        == 409
    )
    assert (
        client.put("/api/blocks/programs/other", headers=AUTH, json={"program": copy}).status_code
        == 422
    )
    bad = {**copy, "stack": [{"b": "nope"}]}
    assert (
        client.put("/api/blocks/programs/my_copy", headers=AUTH, json={"program": bad}).status_code
        == 422
    )
    assert client.delete("/api/blocks/programs/my_copy", headers=AUTH).status_code == 200
    assert client.delete("/api/blocks/programs/my_copy", headers=AUTH).status_code == 404
    assert all(
        p["name"] != "my_copy" for p in client.get("/api/blocks/programs", headers=AUTH).json()
    )


def test_the_catalogue_tells_the_editor_what_it_needs_for_typed_fields(client: TestClient) -> None:
    cat = client.get("/api/blocks/catalogue", headers=AUTH).json()
    assert cat["hook_booleans"] == ["step_check", "claims", "second_reader"]
    assert cat["hook_numbers"]["repeat_guard"] == [1, 20]
    assert "research" in cat["applications"] and "thorough" in cat["depths"]
    assert cat["blocks"]["check"] == {
        "required": ["in", "out"],
        "optional": ["question", "pick", "think", "lint", "decides", "max_tokens"],
    }


# The editor of the web page applies the same typing rules (web/src/blocks/edit.ts, `analyze`) so
# that a move which cannot work is refused before it snaps into place. These cases are in both test
# suites (edit.test.ts, "agrees with the hub"): the same program, the same verdict.
RULE_CASES: list[tuple[dict[str, Any], str | None]] = [
    ({"inputs": {"q": "text"}, "stack": [{"b": "set", "value": "{{q}}", "out": "a"}]}, None),
    (
        {"inputs": {"q": "text"}, "stack": [{"b": "check", "in": "nothing", "out": "v"}]},
        "nothing is not defined",
    ),
    (
        {
            "inputs": {"q": "text"},
            "stack": [
                {"b": "check", "in": "q", "out": "v"},
                {"b": "file_report", "in": "v"},
            ],
        },
        "v is verdict, this block needs text",
    ),
    (
        {
            "inputs": {"q": "text"},
            "stack": [
                {"b": "check", "in": "q", "out": "v"},
                {
                    "b": "repeat_until",
                    "cond": "v",
                    "stack": [{"b": "set", "value": "{{q}}", "out": "x"}],
                },
            ],
        },
        "use v.ok here",
    ),
    (
        {
            "inputs": {"q": "text"},
            "stack": [
                {"b": "check", "in": "q", "out": "v"},
                {"b": "repeat_until", "cond": "v.ok", "stack": []},
            ],
        },
        "must write a variable",
    ),
    (
        {
            "inputs": {"q": "text"},
            "stack": [
                {
                    "b": "parallel",
                    "lanes": [
                        [{"b": "set", "value": "{{q}}", "out": "a"}],
                        [{"b": "set", "value": "{{q}}", "out": "a"}],
                    ],
                }
            ],
        },
        "both write a",
    ),
]


@pytest.mark.parametrize(("program", "problem"), RULE_CASES)
def test_the_hub_judges_the_shared_rule_cases(
    client: TestClient, program: dict[str, Any], problem: str | None
) -> None:
    res = client.post(
        "/api/blocks/validate", headers=AUTH, json={"program": {"name": "case", **program}}
    ).json()
    if problem is None:
        assert res["ok"] is True, res
    else:
        assert res["ok"] is False and problem in res["error"], res
