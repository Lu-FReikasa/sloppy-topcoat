"""The dev workshop, hub side (E6-01 S1, D52): pairing with role dev, scratch capabilities that run
without a click for an admin only, and the repository a call hands to the dev node (token included,
the audit row not)."""

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import FakeNode, eventually, pair
from tests.test_tools import audit_rows, grant, run, set_config
from tests.users import make_user

ADMIN_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")  # the dev-token admin
TOKEN = "ghp_workshopTokenForTests0123456789abcdef"


def _schema(required: list[str], **props: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": {"repo": {"type": "string", "maxLength": 63}, **props},
    }


def dev_inventory() -> dict[str, Any]:
    caps = [
        ("repo.read", True, False, _schema(["repo", "path"], path={"type": "string"})),
        ("repo.sync", False, True, _schema(["repo"])),
        ("repo.edit", False, True, _schema(["repo", "path"], path={"type": "string"})),
        # offered, but a test must switch them on (the default config below leaves them out)
        ("repo.list", True, False, _schema(["repo"])),
        ("repo.status", True, False, _schema(["repo"])),
        ("repo.start", False, True, _schema(["repo", "topic"], topic={"type": "string"})),
        ("repo.reset", False, True, _schema(["repo"])),
        (
            "repo.write",
            False,
            True,
            _schema(
                ["repo", "path", "content"], path={"type": "string"}, content={"type": "string"}
            ),
        ),
        ("repo.test", False, True, _schema(["repo"])),
        ("repo.test_start", False, True, _schema(["repo"])),
        (
            "repo.test_status",
            True,
            False,
            _schema(
                ["repo", "job_id"], job_id={"type": "string"}, wait_seconds={"type": "integer"}
            ),
        ),
        ("repo.test_cancel", False, True, _schema(["repo", "job_id"], job_id={"type": "string"})),
        ("repo.diff", True, False, _schema(["repo"])),
        ("repo.build_node", False, True, _schema(["repo", "version"], version={"type": "string"})),
        (
            "repo.push",  # a state change outside the workshop
            False,
            False,
            _schema(
                ["repo", "message", "branch", "diff_sha256"],
                message={"type": "string"},
                branch={"type": "string"},
                diff_sha256={"type": "string", "pattern": "^[0-9a-f]{64}$"},
            ),
        ),
    ]
    return {
        "hardware": {"cpu": {"model": "Fake VM CPU", "cores": 4}, "ram_total_mb": 8192, "gpus": []},
        "runner": {"type": "none", "available": True},
        "models": [],
        "loaded": [],
        "capabilities": [
            {
                "name": n,
                "description": f"{n} (test)",
                "read_only": ro,
                **({"scratch": True} if scratch else {}),
                "input_schema": schema,
            }
            for n, ro, scratch, schema in caps
        ],
    }


DIGEST = "a" * 64
DIFF: dict[str, Any] = {
    "repo": "app",
    "files": ["a.txt"],
    "diff": "diff --git a/a.txt b/a.txt\n+hello\n",
    "empty": False,
    "truncated": False,
    "masked": 0,
    "sha256": DIGEST,
    "bytes": 40,
    "branch": "topcoat/fix",
}


def dev_node(client: TestClient, connect: Any, name: str = "dev-alpha") -> tuple[str, FakeNode]:
    node_id, key = pair(client, name, role="dev")
    node = connect(node_id, key)
    node.inventory = dev_inventory()
    node.capability_answers.update(
        {
            "repo.read": {"text": "hello", "path": "README.md"},
            "repo.sync": {"head": "abc123"},
            "repo.edit": {"bytes": 5},
            "repo.test": {"passed": True},
            "repo.push": {"pushed": True, "branch": "topcoat/fix", "commit": "abc123"},
            "repo.diff": DIFF,
        }
    )
    node.handshake()
    node.serve()

    def stored() -> None:
        assert client.get(f"/api/nodes/{node_id}/capabilities", headers=AUTH).json()["offered"]

    eventually(stored)
    set_config(
        client,
        node_id,
        lambda cfg: cfg.update(
            capabilities=[
                "repo.read",
                "repo.sync",
                "repo.edit",
                "repo.test",
                "repo.test_start",
                "repo.test_status",
                "repo.test_cancel",
                "repo.diff",
                "repo.build_node",
                "repo.push",
            ]
        ),
    )
    return node_id, node


@pytest.fixture
def repos(client: TestClient):
    """Register repositories straight in the database (the add dialog needs GitHub)."""
    box = client.app.state.devrepos.box
    engine = create_engine(TEST_DB_URL)

    def add(name: str, full: str, mode: str, template: str = "make-check") -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO dev_repos (id, user_id, name, full_name, default_branch, mode, "
                    "test_template, token_enc) VALUES (:i, :u, :n, :f, 'trunk', :m, :t, :k)"
                ),
                {
                    "i": uuid.uuid4(),
                    "u": ADMIN_ID,
                    "n": name,
                    "f": full,
                    "m": mode,
                    "t": template,
                    "k": box.encrypt(TOKEN),
                },
            )

    yield add
    engine.dispose()


def test_a_node_paired_as_dev_gets_the_workshop_config(client: TestClient) -> None:
    node_id, _ = pair(client, "dev-box", role="dev")
    cfg = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config"]
    assert cfg["roles"] == ["dev"] and cfg["runner"] == {"type": "none"}
    # capability.execute is there for repo.push only: the one step that leaves the workshop
    assert cfg["allowed_actions"] == ["capability.invoke", "capability.execute", "workshop.manage"]
    assert "repo.push" in cfg["capabilities"]
    assert "repo.edit" in cfg["capabilities"] and "docker.logs" not in cfg["capabilities"]
    assert cfg["yield"] == {"enabled": False}


def test_the_call_hands_the_node_its_repository_and_the_audit_row_has_no_token(
    client: TestClient, connect, repos
) -> None:
    repos("app", "acme/app", "test-branch", "pytest")
    node_id, node = dev_node(client, connect)
    r = run(client, node_id, "repo.edit", {"repo": "acme/app", "path": "a.txt"})
    assert r.status_code == 200, r.text
    call = node.capability_calls[-1]
    assert call["input"]["repo"] == "app"  # the hub's own name, the node's directory
    assert call["repo"] == {
        "url": "https://github.com/acme/app.git",
        "branch": "trunk",
        "template": "pytest",
        "token": TOKEN,
    }
    rows = audit_rows(node_id)
    assert rows[-1]["capability"] == "repo.edit" and rows[-1]["ok"] is True
    assert TOKEN not in json.dumps(rows, default=str)
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        stored = conn.scalar(text("SELECT input::text FROM tool_calls ORDER BY id DESC LIMIT 1"))
    engine.dispose()
    assert TOKEN not in stored and "github.com" not in stored


def test_a_read_only_repository_can_be_read_but_not_changed(
    client: TestClient, connect, repos
) -> None:
    repos("docs", "acme/docs", "read")
    node_id, node = dev_node(client, connect)
    assert run(client, node_id, "repo.sync", {"repo": "docs"}).status_code == 200
    assert (
        run(client, node_id, "repo.read", {"repo": "docs", "path": "README.md"}).status_code == 200
    )
    for cap, body in (
        ("repo.edit", {"repo": "docs", "path": "a.txt"}),
        ("repo.test", {"repo": "docs"}),
        ("repo.test_start", {"repo": "docs"}),
        ("repo.test_cancel", {"repo": "docs", "job_id": "x"}),
        ("repo.build_node", {"repo": "docs", "version": "1"}),
    ):
        r = run(client, node_id, cap, body)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "repo_read_only", r.text
    assert [c["capability"] for c in node.capability_calls] == ["repo.sync", "repo.read"]
    refused = [r for r in audit_rows(node_id) if not r["ok"]]
    assert {r["code"] for r in refused} == {"repo_read_only"}


def test_an_unknown_repository_names_the_registered_ones(
    client: TestClient, connect, repos
) -> None:
    repos("app", "acme/app", "test-branch")
    node_id, node = dev_node(client, connect)
    r = run(client, node_id, "repo.read", {"repo": "nope", "path": "x"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "unknown_repo"
    assert "app" in r.json()["detail"]["message"]
    assert node.capability_calls == []


def test_scratch_capabilities_run_for_an_admin_but_not_for_a_key_or_a_user(
    client: TestClient, connect, repos, clean_db: str
) -> None:
    repos("app", "acme/app", "test-branch")
    node_id, node = dev_node(client, connect)
    assert run(client, node_id, "repo.sync", {"repo": "app"}).status_code == 200
    # a push is not a scratch capability: it needs the confirmation path
    r = run(client, node_id, "repo.push", {"repo": "app"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "needs_confirmation"
    # another person, even with a grant for everything, gets neither scratch nor named-only repo.*
    user_id, key = make_user(clean_db, "guest", role="user")
    grant(client, node_id, user_id, ["*"])
    r = run(client, node_id, "repo.read", {"repo": "app", "path": "a"}, who=key)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_granted"
    grant(client, node_id, user_id, ["repo.edit", "repo.read"])
    r = run(client, node_id, "repo.edit", {"repo": "app", "path": "a"}, who=key)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "needs_confirmation"
    assert [c["capability"] for c in node.capability_calls] == ["repo.sync"]


def test_the_admin_sees_the_scratch_capabilities_as_usable_but_not_the_push(
    client: TestClient, connect, repos
) -> None:
    repos("app", "acme/app", "test-branch")
    node_id, _ = dev_node(client, connect)
    usable = set(client.get(f"/api/nodes/{node_id}/capabilities", headers=AUTH).json()["usable"])
    assert {"repo.read", "repo.sync", "repo.edit", "repo.test"} <= usable
    assert "repo.push" not in usable


@pytest.fixture
def proposals_on(client: TestClient):
    client.app.state.settings.proposals_enabled = True
    yield
    client.app.state.settings.proposals_enabled = False


def push_proposal(client: TestClient, message: str = "Fix paging", ref: str = "app"):
    return client.post(
        f"/api/dev/repos/{ref}/push-proposal", headers=AUTH, json={"message": message}
    )


def test_a_push_is_proposed_with_its_diff_and_runs_only_after_the_click(
    client: TestClient, connect, proposals_on, repos
) -> None:
    repos("app", "acme/app", "test-branch")
    node_id, node = dev_node(client, connect)
    r = push_proposal(client)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["capability"] == "repo.push" and p["status"] == "pending"
    assert p["preview"].startswith("diff --git") and "topcoat/fix" in p["effect"]
    assert p["input"] == {
        "repo": "app",
        "message": "Fix paging",
        "branch": "topcoat/fix",
        "diff_sha256": DIGEST,
    }
    assert node.execute_calls == [] and node.capability_calls[-1]["capability"] == "repo.diff"
    # the card is there to be clicked; the node has not been asked to push yet
    shown = client.get(f"/api/agent/proposals/{p['id']}", headers=AUTH).json()
    assert shown["status"] == "pending" and shown["preview"] == p["preview"]

    done = client.post(f"/api/agent/proposals/{p['id']}/execute", headers=AUTH)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "executed"
    call = node.execute_calls[-1]
    assert call["capability"] == "repo.push" and call["hash"] == p["hash"]
    assert call["input"]["diff_sha256"] == DIGEST and call["input"]["repo"] == "app"
    assert call["repo"]["token"] == TOKEN and call["repo"]["url"].endswith("acme/app.git")
    rows = audit_rows(node_id)
    assert rows[-1]["capability"] == "repo.push" and rows[-1]["via"] == "proposal"
    assert TOKEN not in json.dumps(rows, default=str)


def test_the_diff_is_part_of_the_hash(
    client: TestClient, connect, proposals_on, repos, clean_db: str
) -> None:
    repos("app", "acme/app", "test-branch")
    dev_node(client, connect)
    p = push_proposal(client).json()
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_proposals SET preview = preview || 'x' WHERE id = :i"),
            {"i": p["id"]},
        )
    engine.dispose()
    r = client.post(f"/api/agent/proposals/{p['id']}/execute", headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "content_changed"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"empty": True}, "no_change"),
        ({"truncated": True}, "too_big"),
        ({"masked": 2}, "secrets_found"),
        ({"branch": "main"}, "not_on_work_branch"),
        ({"sha256": ""}, "node_too_old"),
    ],
)
def test_a_change_that_must_not_leave_is_not_even_proposed(
    client: TestClient, connect, proposals_on, repos, change: dict[str, Any], code: str
) -> None:
    repos("app", "acme/app", "test-branch")
    _, node = dev_node(client, connect)
    node.capability_answers["repo.diff"] = {**DIFF, **change}
    r = push_proposal(client)
    assert r.status_code == 422 and r.json()["detail"]["code"] == code, r.text
    assert node.execute_calls == []


def test_only_a_test_branch_repository_and_a_connected_dev_node_can_push(
    client: TestClient, connect, proposals_on, repos
) -> None:
    repos("docs", "acme/docs", "read")
    r = push_proposal(client, ref="docs")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "repo_read_only"
    r = push_proposal(client, ref="nope")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "unknown_repo"
    repos("app", "acme/app", "test-branch")
    r = push_proposal(client)  # no dev node connected yet
    assert r.status_code == 404 and r.json()["detail"]["code"] == "no_dev_node"
    dev_node(client, connect)
    assert push_proposal(client, message=" ").status_code == 422


def test_an_api_key_cannot_ask_for_a_push(
    client: TestClient, connect, proposals_on, repos, clean_db: str
) -> None:
    repos("app", "acme/app", "test-branch")
    dev_node(client, connect)
    _, key = make_user(clean_db, "guest2", role="user")
    r = client.post("/api/dev/repos/app/push-proposal", headers=key, json={"message": "Fix paging"})
    assert r.status_code in (401, 403)


def test_a_confirmed_file_change_runs_as_an_invoke_with_only_the_keys_the_node_accepts(
    client: TestClient, connect, repos
) -> None:
    """The node runs a scratch capability with capability.invoke and refuses it under
    capability.execute (seen live, 2026-11-04); the hub's confirmation has been checked before."""
    from topcoat_hub.tools import Caller

    repos("app", "acme/app", "test-branch")
    node_id, node = dev_node(client, connect)
    caller = Caller(user_id=ADMIN_ID, admin=True, via="gui")
    pid = uuid.uuid4()

    async def go() -> dict[str, Any]:
        return await client.app.state.tools.execute_confirmed(
            caller, uuid.UUID(node_id), "repo.edit",
            {"repo": "app", "path": "VERSION"},
            pid, "a" * 64,
        )  # fmt: skip

    out = client.portal.call(go)
    assert out["ok"] is True and out["capability"] == "repo.edit"
    assert node.execute_calls == []  # not the execute action
    call = node.capability_calls[-1]
    assert call["capability"] == "repo.edit" and call["input"]["repo"] == "app"
    assert set(call) == {
        "capability",
        "input",
        "repo",
    }  # no proposal id, no hash: invoke refuses them
    rows = audit_rows(node_id)
    assert rows[-1]["capability"] == "repo.edit" and rows[-1]["via"] == "proposal"


def test_a_confirmed_push_still_runs_as_an_execute_with_the_click_in_it(
    client: TestClient, connect, proposals_on, repos
) -> None:
    repos("app", "acme/app", "test-branch")
    _, node = dev_node(client, connect)
    p = push_proposal(client).json()
    assert client.post(f"/api/agent/proposals/{p['id']}/execute", headers=AUTH).status_code == 200
    call = node.execute_calls[-1]
    assert call["proposal"] == p["id"] and call["hash"] == p["hash"]
