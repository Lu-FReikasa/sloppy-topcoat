"""The Projekte REST API (brief PPH-01-planning-app, 01b; rework 2026-10-27).

Scope: the routes work end to end; every write carries the revision and a stale one answers 409
and changes nothing; a project or node of another person looks like a missing one (404); the
tree rules answer 422; the scope is set through its own route and is validated there.
"""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub.agent_service import AgentError
from topcoat_hub.message_service import MessageService
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.project_store import ProjectStore


def _project(client: TestClient, who: dict[str, str] = AUTH, **body: Any) -> dict[str, Any]:
    r = client.post("/api/project/projects", json={"title": "Topcoat", **body}, headers=who)
    assert r.status_code == 201, r.text
    return r.json()


def _node(client: TestClient, project_id: str, who: dict[str, str] = AUTH, **body: Any) -> Any:
    return client.post(
        f"/api/project/projects/{project_id}/nodes",
        json={"kind": "task", "title": "Step", **body},
        headers=who,
    )


def test_create_read_and_list_a_project(client: TestClient) -> None:
    project = _project(client, description="Development of Topcoat")
    assert project["revision"] == 1
    assert project["status"] == "active"
    assert project["scope"] == {}

    listed = client.get("/api/project/projects", headers=AUTH).json()["projects"]
    assert [p["id"] for p in listed] == [project["id"]]

    detail = client.get(f"/api/project/projects/{project['id']}", headers=AUTH).json()
    assert detail["project"]["description"] == "Development of Topcoat"
    assert detail["nodes"] == []


def test_a_second_project_with_the_same_title_is_refused(client: TestClient) -> None:
    _project(client)
    again = client.post("/api/project/projects", json={"title": "Topcoat"}, headers=AUTH)
    assert again.status_code == 422


def test_nodes_are_numbered_and_refs_are_given(client: TestClient) -> None:
    project = _project(client)
    topic = _node(client, project["id"], kind="topic", title="Repo").json()
    child = _node(client, project["id"], parent_id=topic["id"], title="Check").json()
    assert topic["number"] == "1"
    assert child["number"] == "1.1"
    assert child["ref"] == "Topcoat 1.1"


def test_a_move_keeps_the_number(client: TestClient) -> None:
    project = _project(client)
    a = _node(client, project["id"], kind="topic", title="A").json()
    b = _node(client, project["id"], kind="topic", title="B").json()
    task = _node(client, project["id"], parent_id=a["id"]).json()
    moved = client.post(
        f"/api/project/nodes/{task['id']}/move",
        json={"revision": task["revision"], "parent_id": b["id"]},
        headers=AUTH,
    )
    assert moved.status_code == 200
    assert moved.json()["number"] == "1.1"
    assert moved.json()["parent_id"] == b["id"]


def test_a_stale_revision_answers_409_and_changes_nothing(client: TestClient) -> None:
    project = _project(client)
    first = client.patch(
        f"/api/project/projects/{project['id']}",
        json={"revision": 1, "description": "new"},
        headers=AUTH,
    )
    assert first.status_code == 200
    late = client.patch(
        f"/api/project/projects/{project['id']}",
        json={"revision": 1, "description": "late"},
        headers=AUTH,
    )
    assert late.status_code == 409
    detail = client.get(f"/api/project/projects/{project['id']}", headers=AUTH).json()
    assert detail["project"]["description"] == "new"


def test_a_question_is_answered_on_the_page(client: TestClient) -> None:
    project = _project(client)
    question = client.post(
        f"/api/project/projects/{project['id']}/nodes",
        json={"kind": "report", "report_kind": "question", "title": "Which node?"},
        headers=AUTH,
    ).json()
    answered = client.post(
        f"/api/project/nodes/{question['id']}/answer",
        json={"answer": "node-amd"},
        headers=AUTH,
    )
    assert answered.status_code == 200
    assert answered.json()["answer"] == "node-amd"


def test_a_report_kind_is_refused_for_a_topic(client: TestClient) -> None:
    project = _project(client)
    r = _node(client, project["id"], kind="topic", title="T", report_kind="note")
    assert r.status_code == 422


def test_the_scope_is_set_through_its_own_route_and_validated(client: TestClient) -> None:
    project = _project(client)
    ok = client.put(
        f"/api/project/projects/{project['id']}/scope",
        json={
            "revision": 1,
            "scope": {"dev": {"create_branch": "confirm", "request_report": "free"}},
        },
        headers=AUTH,
    )
    assert ok.status_code == 200
    assert ok.json()["scope"]["dev"]["create_branch"] == "confirm"
    assert ok.json()["revision"] == 2

    never = client.put(
        f"/api/project/projects/{project['id']}/scope",
        json={"revision": 2, "scope": {"dev": {"push": "free"}}},
        headers=AUTH,
    )
    assert never.status_code == 422


def test_a_project_of_another_person_looks_missing(client: TestClient) -> None:
    project = _project(client)
    stranger, who = make_user(TEST_DB_URL, "stranger api")
    assert stranger is not None
    got = client.get(f"/api/project/projects/{project['id']}", headers=who)
    assert got.status_code == 404
    edit = client.patch(
        f"/api/project/projects/{project['id']}",
        json={"revision": 1, "description": "mine now"},
        headers=who,
    )
    assert edit.status_code == 404
    assert client.get("/api/project/projects", headers=who).json()["projects"] == []


def test_the_routes_need_a_login(client: TestClient) -> None:
    assert client.get("/api/project/projects").status_code == 401


async def test_the_person_confirms_a_request_awaiting_a_confirm_grant(client: TestClient) -> None:
    project = _project(client)
    scoped = client.put(
        f"/api/project/projects/{project['id']}/scope",
        json={"revision": 1, "scope": {"dev": {"request_report": "confirm"}}},
        headers=AUTH,
    ).json()
    assert scoped["revision"] == 2
    # the agent makes the request (the REST API cannot make one: a report kind "request" is refused)
    refused = _node(client, project["id"], kind="report", report_kind="request", title="x")
    assert refused.status_code == 422
    store = ProjectStore(client.app.state.sessionmaker)
    request, _ = await MessageService(store, None).send_request(
        DEV_ADMIN_ID,
        True,
        uuid.UUID(project["id"]),
        target="dev",
        payload={"topic": "Branch for the trace tool"},
    )
    assert request.msg_state == "awaiting_click"
    confirmed = client.post(
        f"/api/project/nodes/{request.id}/decide", json={"confirm": True}, headers=AUTH
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["message"]["state"] in ("sent", "queued")
    again = client.post(
        f"/api/project/nodes/{request.id}/decide", json={"confirm": False}, headers=AUTH
    )
    assert again.status_code == 422
    stranger, who = make_user(TEST_DB_URL, "stranger decide")
    assert stranger is not None
    other = client.post(
        f"/api/project/nodes/{request.id}/decide", json={"confirm": True}, headers=who
    )
    assert other.status_code == 404


def test_a_run_starts_with_the_digest_of_the_project(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt of a run holds the project's id and the code-built digest (04d), then the ask."""
    seen: dict[str, Any] = {}

    async def start(user: Any, prompt: str, model: Any, application: str = "chat") -> Any:
        seen.update(prompt=prompt, application=application)
        raise AgentError(409, "busy", "stop here")  # the prompt is what is looked at

    monkeypatch.setattr(client.app.state.agents, "start", start)
    project = _project(client, description="Digest check")
    _node(client, project["id"], title="Write the plan")
    r = client.post(
        f"/api/project/projects/{project['id']}/runs",
        json={"prompt": "Plan the next step"},
        headers=AUTH,
    )
    assert r.status_code == 409
    assert seen["application"] == "project"
    head, _, ask = seen["prompt"].rpartition("\n\n")
    assert head.startswith(f"[project {project['id']}: Topcoat]")
    assert "data" in head  # the digest says that node text is data, not an instruction
    assert ask == "Plan the next step"


def test_a_node_goes_back_to_an_earlier_version_as_a_new_revision(client: TestClient) -> None:
    project = _project(client)
    node = _node(client, project["id"], title="First title", body="first text").json()
    changed = client.patch(
        f"/api/project/nodes/{node['id']}",
        json={"revision": node["revision"], "title": "Second title", "body": "second text"},
        headers=AUTH,
    ).json()
    back = client.post(
        f"/api/project/nodes/{node['id']}/revert",
        json={"revision": changed["revision"], "to_rev": node["revision"]},
        headers=AUTH,
    )
    assert back.status_code == 200, back.text
    out = back.json()
    assert (out["title"], out["body"]) == ("First title", "first text")
    assert out["revision"] > changed["revision"]  # a new revision, not an overwrite
    versions = client.get(f"/api/project/nodes/{node['id']}/history", headers=AUTH).json()[
        "versions"
    ]
    assert [v["rev"] for v in versions][-1] == out["revision"]  # the history keeps both ways
    # a stale revision is refused and changes nothing
    stale = client.post(
        f"/api/project/nodes/{node['id']}/revert",
        json={"revision": changed["revision"], "to_rev": node["revision"]},
        headers=AUTH,
    )
    assert stale.status_code == 409
    # a revision that never existed, and a node of somebody else
    assert (
        client.post(
            f"/api/project/nodes/{node['id']}/revert",
            json={"revision": out["revision"], "to_rev": 99},
            headers=AUTH,
        ).status_code
        == 404
    )
    _, other = make_user(TEST_DB_URL, "Other revert")
    assert (
        client.post(
            f"/api/project/nodes/{node['id']}/revert",
            json={"revision": out["revision"], "to_rev": 1},
            headers=other,
        ).status_code
        == 404
    )
