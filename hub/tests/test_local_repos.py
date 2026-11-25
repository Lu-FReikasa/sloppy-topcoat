"""Local repositories of the dev workshop (ARCHITECTURE D76): a repository is external (it lives
on GitHub, the assistant reads it) or local (its origin is in the workshop of a dev node, the
assistant edits and tests it there). The person creates one, clones an external one, makes
branches, takes a branch into the default branch, publishes and pulls — each a click on the hub's
page that runs on the node through ``workshop.manage``, which no assistant can reach."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import FakeNode, eventually, pair
from tests.test_dev_workshop import TOKEN, _schema, dev_inventory
from tests.test_tools import set_config
from tests.users import make_user

MANAGE = [
    (
        "workshop.create",
        _schema(
            ["repo"],
            branch={"type": "string"},
            title={"type": "string"},
            description={"type": "string"},
        ),
    ),
    ("workshop.clone", _schema(["repo"])),
    (
        "workshop.branch",
        _schema(["repo", "name"], name={"type": "string"}, **{"from": {"type": "string"}}),
    ),
    (
        "workshop.merge",
        _schema(
            ["repo", "branch"],
            branch={"type": "string"},
            into={"type": "string"},
            message={"type": "string"},
        ),
    ),
    ("workshop.publish", _schema(["repo", "branch"], branch={"type": "string"})),
    ("workshop.pull", _schema(["repo", "branch"], branch={"type": "string"})),
    ("workshop.remove", _schema(["repo", "confirm"], confirm={"const": True})),
]
CAPS = [
    "repo.read", "repo.sync", "repo.edit", "repo.diff", "repo.push", "repo.branches", "repo.commit",
    *[name for name, _ in MANAGE],
]  # fmt: skip


def local_inventory() -> dict[str, Any]:
    inv = dev_inventory()
    inv["capabilities"] += [
        {
            "name": "repo.branches",
            "description": "branches",
            "read_only": True,
            "input_schema": _schema(["repo"]),
        },
        {
            "name": "repo.commit",
            "description": "commit",
            "read_only": False,
            "scratch": True,
            "input_schema": _schema(["repo", "message"], message={"type": "string"}),
        },
        *[
            {"name": n, "description": n, "read_only": False, "manage": True, "input_schema": s}
            for n, s in MANAGE
        ],
    ]
    return inv


def dev_node(client: TestClient, connect: Any, name: str = "dev-alpha") -> tuple[str, FakeNode]:
    node_id, key = pair(client, name, role="dev")
    node = connect(node_id, key)
    node.inventory = local_inventory()
    node.capability_answers.update(
        {
            "workshop.create": lambda i: {
                "repo": i["repo"],
                "default_branch": i.get("branch") or "main",
                "commit": "c0ffee",
            },
            "workshop.clone": lambda i: {
                "repo": i["repo"],
                "default_branch": "trunk",
                "branches": 3,
            },
            "workshop.branch": lambda i: {"repo": i["repo"], "branch": i["name"], "created": True},
            "workshop.merge": lambda i: {"repo": i["repo"], "merged": True, "into": i.get("into")},
            "workshop.publish": lambda i: {
                "repo": i["repo"],
                "published": True,
                "branch": i["branch"],
            },
            "workshop.pull": lambda i: {"repo": i["repo"], "updated": True, "branch": i["branch"]},
            "workshop.remove": lambda i: {"repo": i["repo"], "removed": True},
            "repo.branches": {
                "repo": "notes",
                "default_branch": "main",
                "branches": [
                    {"name": "main", "default": True, "work": False, "ahead": 0, "behind": 0},
                    {
                        "name": "topcoat/todo",
                        "default": False,
                        "work": True,
                        "ahead": 1,
                        "behind": 0,
                    },
                ],
            },
            "repo.sync": {"head": "abc123"},
        }
    )
    node.handshake()
    node.serve()

    def stored() -> None:
        assert client.get(f"/api/nodes/{node_id}/capabilities", headers=AUTH).json()["offered"]

    eventually(stored)
    set_config(client, node_id, lambda cfg: cfg.update(capabilities=CAPS))
    return node_id, node


@pytest.fixture
def external(client: TestClient):
    """An external repository straight in the database (the add dialog needs GitHub)."""
    box = client.app.state.devrepos.box
    engine = create_engine(TEST_DB_URL)
    rid = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO dev_repos (id, user_id, name, full_name, default_branch, mode, "
                "test_template, token_enc) VALUES (:i, :u, 'upstream-app', 'acme/app', 'trunk', "
                "'read', 'pytest', :k)"
            ),
            {"i": rid, "u": "00000000-0000-4000-8000-000000000001", "k": box.encrypt(TOKEN)},
        )
    engine.dispose()
    return rid


def repos(client: TestClient) -> dict[str, dict[str, Any]]:
    r = client.get("/api/dev/repos", headers=AUTH)
    assert r.status_code == 200, r.text
    return {x["name"]: x for x in r.json()["repos"]}


def test_a_new_local_repository_lives_in_the_workshop_of_its_dev_node(
    client: TestClient, connect
) -> None:
    node_id, node = dev_node(client, connect)
    r = client.post(
        "/api/dev/repos/local",
        headers=AUTH,
        json={
            "name": "notes",
            "title": "Notes",
            "description": "A small tool",
            "test_template": "pytest",
        },
    )
    assert r.status_code == 201, r.text
    made = r.json()
    assert made["kind"] == "local" and made["node_id"] == node_id and made["full_name"] == ""
    (call,) = node.manage_calls
    assert call["capability"] == "workshop.create"
    assert call["input"] == {
        "repo": "notes",
        "branch": "main",
        "title": "Notes",
        "description": "A small tool",
    }
    listed = client.get("/api/dev/repos", headers=AUTH).json()
    assert listed["nodes"] == [{"id": node_id, "name": "dev-alpha", "online": True}]
    assert repos(client)["notes"]["node"] == "dev-alpha"
    again = client.post("/api/dev/repos/local", headers=AUTH, json={"name": "notes"})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "name_taken"


def test_without_a_dev_node_there_is_no_local_repository(client: TestClient) -> None:
    r = client.post("/api/dev/repos/local", headers=AUTH, json={"name": "notes"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_dev_node"


def test_a_local_clone_of_an_external_repository_keeps_it_as_its_upstream(
    client: TestClient, connect, external: str
) -> None:
    _, node = dev_node(client, connect)
    r = client.post(f"/api/dev/repos/{external}/clone", headers=AUTH, json={"name": "app"})
    assert r.status_code == 201, r.text
    clone = r.json()
    assert clone["kind"] == "local" and clone["upstream_id"] == external
    assert clone["default_branch"] == "trunk" and clone["test_template"] == "pytest"
    (call,) = node.manage_calls
    # the node gets the remote's address and token with this call only ...
    assert call["repo"] == {
        "url": "https://github.com/acme/app.git",
        "branch": "trunk",
        "token": TOKEN,
    }
    # ... and the audit keeps the input, never the token
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT input::text FROM tool_calls WHERE capability = 'workshop.clone'")
        ).all()
    engine.dispose()
    assert rows and TOKEN not in rows[0][0]
    listed = repos(client)
    assert listed["upstream-app"]["clones"] == [clone["id"]]
    # a local repository is not cloned again
    r = client.post(f"/api/dev/repos/{clone['id']}/clone", headers=AUTH, json={"name": "app2"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_external"


def test_branches_merge_publish_and_pull_are_the_persons_steps(
    client: TestClient, connect, external: str
) -> None:
    _, node = dev_node(client, connect)
    client.post("/api/dev/repos/local", headers=AUTH, json={"name": "notes"})
    got = client.get("/api/dev/repos/notes/branches", headers=AUTH).json()
    assert [b["name"] for b in got["branches"]] == ["main", "topcoat/todo"]
    r = client.post(
        "/api/dev/repos/notes/branches", headers=AUTH, json={"name": "feature", "from": "main"}
    )
    assert r.status_code == 201, r.text
    r = client.post("/api/dev/repos/notes/merge", headers=AUTH, json={"branch": "topcoat/todo"})
    assert r.status_code == 200 and r.json()["merged"] is True
    steps = [(c["capability"], c["input"]) for c in node.manage_calls]
    assert steps[1:] == [
        ("workshop.branch", {"repo": "notes", "name": "feature", "from": "main"}),
        ("workshop.merge", {"repo": "notes", "branch": "topcoat/todo", "into": "main"}),
    ]
    # publishing needs a connected remote
    r = client.post("/api/dev/repos/notes/publish", headers=AUTH, json={"branch": "main"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_upstream"
    r = client.put("/api/dev/repos/notes/upstream", headers=AUTH, json={"upstream": "notes"})
    assert r.status_code == 422  # a local repository is no remote
    r = client.put("/api/dev/repos/notes/upstream", headers=AUTH, json={"upstream": external})
    assert r.status_code == 200 and r.json()["upstream_id"] == external
    r = client.post("/api/dev/repos/notes/publish", headers=AUTH, json={"branch": "main"})
    assert r.status_code == 200 and r.json()["published"] is True
    assert node.manage_calls[-1]["repo"]["token"] == TOKEN
    r = client.post("/api/dev/repos/notes/pull", headers=AUTH, json={})
    assert r.status_code == 200 and node.manage_calls[-1]["input"] == {
        "repo": "notes",
        "branch": "main",
    }
    # an external repository has no branches of the workshop
    r = client.get(f"/api/dev/repos/{external}/branches", headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_local"


def test_a_removed_local_repository_is_deleted_in_its_workshop(client: TestClient, connect) -> None:
    _, node = dev_node(client, connect)
    client.post("/api/dev/repos/local", headers=AUTH, json={"name": "tmp"})
    assert client.delete("/api/dev/repos/tmp", headers=AUTH).status_code == 204
    assert node.manage_calls[-1]["input"] == {"repo": "tmp", "confirm": True}
    assert "tmp" not in repos(client)


def test_no_assistant_and_no_api_key_reaches_a_workshop_step(
    client: TestClient, connect, clean_db: str
) -> None:
    from tests.test_tools import run
    from topcoat_hub.tools import Caller

    node_id, node = dev_node(client, connect)
    admin = Caller(user_id=uuid.UUID("00000000-0000-4000-8000-000000000001"), admin=True, via="gui")
    tools = client.app.state.tools

    async def offered() -> tuple[set[str], set[str]]:
        usable = {t["capability"] for t in await tools.usable_tools(admin)}
        proposable = {t["capability"] for t in await tools.proposable_tools(admin)}
        return usable, proposable

    usable, proposable = client.portal.call(offered)
    steps = {n for n, _ in MANAGE}
    assert not steps & usable and not steps & proposable
    assert {"repo.branches", "repo.commit"} <= usable
    # a plain call of a step is refused before it reaches the node
    r = run(client, node_id, "workshop.create", {"repo": "x"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "needs_confirmation"
    assert node.manage_calls == []
    # another user may not manage repositories (the page is the admin's)
    _, other = make_user(clean_db, "user-b")
    assert client.post("/api/dev/repos/local", headers=other, json={"name": "x"}).status_code == 403


def test_the_agent_works_on_a_local_repository_on_its_own_node(client: TestClient, connect) -> None:
    from tests.test_tools import run

    node_id, node = dev_node(client, connect)
    other_id, _ = dev_node(client, connect, "dev-beta")
    r = client.post(
        "/api/dev/repos/local", headers=AUTH, json={"name": "notes", "node_id": node_id}
    )
    assert r.status_code == 201, r.text
    r = run(client, node_id, "repo.sync", {"repo": "notes"})
    assert r.status_code == 200, r.text
    call = node.capability_calls[-1]
    assert call["repo"] == {
        "url": "",
        "branch": "main",
        "template": "none",
        "token": "",
        "local": True,
    }
    wrong = run(client, other_id, "repo.sync", {"repo": "notes"})
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "wrong_node"
    # the agent learns the local repository from the node that holds it
    from topcoat_hub.tools import Caller

    admin = Caller(user_id=uuid.UUID("00000000-0000-4000-8000-000000000001"), admin=True, via="gui")

    async def described() -> dict[str, str]:
        tools = await client.app.state.agents.tools.available(admin)
        return {t["node_id"]: t["description"] for t in tools if t["capability"] == "repo.sync"}

    sync = client.portal.call(described)
    assert "notes" in sync[node_id] and "notes" not in sync[other_id]


def test_a_dev_node_is_paired_with_the_workshop_steps(client: TestClient) -> None:
    node_id, _ = pair(client, "dev-new", role="dev")
    cfg = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()
    config = cfg.get("config") or cfg.get("desired_config") or {}
    assert "workshop.manage" in config["allowed_actions"]
    assert {"workshop.create", "repo.branches", "repo.commit"} <= set(config["capabilities"])
