"""Releases built inside Topcoat and signed in the browser (NS-01, phases 3 and 4).

The build node is a fake that answers the named build steps; the file upload goes through the
real node-signed route. The browser's part (Web Crypto) is played here by the same Ed25519 the
Go tool uses, over the same bytes."""

import base64
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.fakenode import FakeNode, auth_header, eventually, pair
from tests.test_updates import DATA, Signer
from tests.users import make_user
from topcoat_hub.config import Settings
from topcoat_hub.main import create_app
from topcoat_hub.releases import manifest_text, mask_secrets
from topcoat_hub.updates import key_id

COMMIT = "a" * 40
SCHEMA = {"type": "object", "additionalProperties": True}


def build_inventory() -> dict[str, Any]:
    caps = [
        ("build.fetch", {"ref": {"type": "string"}, "source": {"type": "string"}}),
        (
            "build.node_release",
            {
                "commit": {"type": "string"},
                "version": {"type": "string"},
                "build_id": {"type": "string"},
            },
        ),
        ("build.hub_image", {"commit": {"type": "string"}, "build_id": {"type": "string"}}),
        ("build.smoke_test", {"image": {"type": "string"}}),
    ]
    return {
        "hardware": {"cpu": {"model": "Fake", "cores": 6}, "ram_total_mb": 11000, "gpus": []},
        "runner": {"type": "none", "available": True},
        "models": [],
        "loaded": [],
        "capabilities": [
            {
                "name": n,
                "description": n,
                "read_only": False,
                "manage": True,
                "input_schema": {"type": "object", "properties": p, "additionalProperties": False},
            }
            for n, p in caps
        ],
    }


@pytest.fixture
def signer() -> Signer:
    return Signer()


@pytest.fixture
def app(settings: Settings, tmp_path: Path, signer: Signer) -> FastAPI:
    return create_app(
        settings.model_copy(
            update={"update_dir": tmp_path / "updates", "update_pubkeys": signer.pub_b64}
        )
    )


class Builder:
    """A connected build node and the files its steps leave on the hub."""

    def __init__(self, client: TestClient, connect: Any) -> None:
        self.client = client
        self.node_id, self.key = pair(client, "build-vm", role="build")
        self.node: FakeNode = connect(self.node_id, self.key)
        self.node.inventory = build_inventory()
        self.node.handshake()
        self.node.serve()
        eventually(
            lambda: _assert(
                client.get(f"/api/nodes/{self.node_id}/capabilities", headers=AUTH).json()[
                    "offered"
                ]
            )
        )
        self.files = {
            "topcoat-node-linux-amd64": b"linux program",
            "topcoat-node-windows-amd64.exe": b"windows program",
        }
        self.upload_ok = True
        self.node.capability_answers.update(
            {
                "build.fetch": lambda i: {
                    "commit": COMMIT,
                    "subject": "Add paging",
                    "changes": "a1b2c3 Add paging\nd4e5f6 Fix the footer",
                    "log": "fetched",
                },
                "build.node_release": self.release,
                "build.hub_image": lambda i: {
                    "image": "topcoat-hub:local-aaaaaaa",
                    "digest": "sha256:" + "b" * 64,
                    "size": 123,
                    "log": "built",
                },
                "build.smoke_test": lambda i: {"ok": True, "summary": "health ok", "log": "smoke"},
            }
        )

    def release(self, tool_input: dict[str, Any]) -> dict[str, Any]:
        out = []
        for name, data in self.files.items():
            os_name = "windows" if "windows" in name else "linux"
            r = self.client.put(
                f"/api/node/builds/{tool_input['build_id']}/artifacts/{name}",
                params={"os": os_name, "arch": "amd64"},
                content=data,
                headers=auth_header(self.client, self.node_id, self.key),
            )
            assert r.status_code == 204, r.text
            sha = hashlib.sha256(data).hexdigest() if self.upload_ok else "0" * 64
            out.append(
                {"name": name, "os": os_name, "arch": "amd64", "size": len(data), "sha256": sha}
            )
        return {"artifacts": out, "log": "token=ghp_" + "x" * 30 + " compiled"}


def _assert(value: Any) -> None:
    assert value


@pytest.fixture
def builder(client: TestClient, connect: Any) -> Builder:
    return Builder(client, connect)


def start(client: TestClient, **body: Any):
    return client.post(
        "/api/releases/builds",
        headers=AUTH,
        json={"kind": "node_release", "ref": "main", "version": "0.3.0", **body},
    )


def wait(client: TestClient, build_id: str, *states: str) -> dict[str, Any]:
    def check() -> dict[str, Any]:
        got = client.get(f"/api/releases/builds/{build_id}", headers=AUTH).json()
        assert got["state"] in states, got
        return got

    return eventually(check, timeout=15)


def sign_in_browser(signer: Signer, listed: dict[str, Any]) -> dict[str, str]:
    """What the page does: sign 'topcoat-update-v1\\n' + the manifest bytes, per platform."""
    out = {}
    for m in listed["manifests"]:
        sig = signer.key.sign(b"topcoat-update-v1\n" + m["manifest"].encode())
        out[m["platform"]] = f"topcoat-sig-v1 {key_id(signer.pub)} {base64.b64encode(sig).decode()}"
    return out


# --- the manifest is the one the Go tool writes ---


def test_the_manifest_text_is_what_the_go_tool_writes() -> None:
    raw = (DATA / "manifest.json").read_text(encoding="utf-8").strip()
    m = json.loads(raw)
    again = manifest_text(
        channel=m["channel"], version=m["version"], os_name=m["os"], arch=m["arch"],
        size=m["size"], sha256=m["sha256"], created=m["created"],
    )  # fmt: skip
    assert again == raw


def test_secrets_are_masked_in_a_log() -> None:
    log = (
        "using ghp_" + "a" * 30 + " and Bearer abcdefghijklmnop1234 then password=hunter22x\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n"
        "topcoat-update-key-v1 " + "A" * 44 + "\nplain line"
    )
    masked = mask_secrets(log)
    for secret in ("ghp_", "abcdefghijklmnop1234", "hunter22x", "AAAA", "topcoat-update-key-v1 A"):
        assert secret not in masked
    assert "plain line" in masked and masked.count("[masked]") >= 4


# --- who may do what ---


def test_only_the_signed_in_admin_reaches_the_releases(
    client: TestClient, builder: Builder
) -> None:
    _, key = make_user(TEST_DB_URL, "release key", role="admin")
    _, plain = make_user(TEST_DB_URL, "release user", role="user")
    for who in (key, plain, {}):
        assert client.get("/api/releases", headers=who).status_code in (401, 403)
        assert client.post(
            "/api/releases/builds", headers=who, json={"kind": "node_release", "ref": "main"}
        ).status_code in (401, 403)
    assert client.get("/api/releases", headers=AUTH).status_code == 200


def test_the_build_node_is_paired_with_the_build_role_and_nothing_more(client: TestClient) -> None:
    node_id, _ = pair(client, "build-box", role="build")
    cfg = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config"]
    assert cfg["roles"] == ["build"] and cfg["allowed_actions"] == ["build.manage"]
    assert cfg["runner"] == {"type": "none"}
    assert "build.node_release" in cfg["capabilities"] and "repo.push" not in cfg["capabilities"]


def test_no_build_node_means_no_build(client: TestClient) -> None:
    r = start(client)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_build_node"


# --- a build from start to publication ---


def test_a_node_release_is_built_signed_in_the_browser_and_published(
    client: TestClient, builder: Builder, signer: Signer
) -> None:
    r = start(client)
    assert r.status_code == 201, r.text
    done = wait(client, r.json()["id"], "built")
    assert done["commit"] == COMMIT and "Add paging" in done["changes"]
    assert {a["name"] for a in done["artifacts"]} == set(builder.files)
    sums = {a["name"]: a["sha256"] for a in done["artifacts"]}
    assert sums["topcoat-node-linux-amd64"] == hashlib.sha256(b"linux program").hexdigest()
    assert "ghp_" not in done["log"] and "[masked]" in done["log"]
    # the build node was asked in order, with named steps only
    assert [c["capability"] for c in builder.node.build_calls] == [
        "build.fetch",
        "build.node_release",
    ]
    assert builder.node.build_calls[0]["input"] == {"ref": "main", "source": "release_repo"}
    assert builder.node.build_calls[1]["input"]["commit"] == COMMIT

    listed = client.get(
        f"/api/releases/builds/{done['id']}/manifests", params={"channel": "stable"}, headers=AUTH
    ).json()
    assert listed["key_ids"] == [key_id(signer.pub)] and listed["previous_version"] is None
    assert {m["platform"] for m in listed["manifests"]} == {"linux-amd64", "windows-amd64"}
    sigs = sign_in_browser(signer, listed)
    pub = client.post(
        f"/api/releases/builds/{done['id']}/publish",
        headers=AUTH,
        json={"channel": "stable", "created": listed["created"], "signatures": sigs},
    )
    assert pub.status_code == 200, pub.text
    assert pub.json()["state"] == "published" and pub.json()["key_id"] == key_id(signer.pub)
    published = client.get("/api/updates", headers=AUTH).json()["releases"]
    assert {(r["os"], r["version"], r["sha256"]) for r in published} == {
        ("linux", "0.3.0", sums["topcoat-node-linux-amd64"]),
        ("windows", "0.3.0", sums["topcoat-node-windows-amd64.exe"]),
    }
    again = client.post(
        f"/api/releases/builds/{done['id']}/publish",
        headers=AUTH,
        json={"channel": "stable", "created": listed["created"], "signatures": sigs},
    )
    assert again.status_code == 409


def test_a_signature_of_other_bytes_or_another_key_publishes_nothing(
    client: TestClient, builder: Builder, signer: Signer
) -> None:
    done = wait(client, start(client).json()["id"], "built")
    listed = client.get(f"/api/releases/builds/{done['id']}/manifests", headers=AUTH).json()
    good = sign_in_browser(signer, listed)

    def post(sigs: dict[str, str], **more: Any):
        return client.post(
            f"/api/releases/builds/{done['id']}/publish",
            headers=AUTH,
            json={"channel": "stable", "created": listed["created"], "signatures": sigs, **more},
        )

    # signed for the other channel's bytes
    other = client.get(
        f"/api/releases/builds/{done['id']}/manifests", params={"channel": "test"}, headers=AUTH
    ).json()
    wrong_bytes = sign_in_browser(signer, {**other, "manifests": other["manifests"]})
    assert post(wrong_bytes).json()["detail"]["code"] == "bad_signature"
    # a key the hub does not know
    stranger = Signer()
    assert post(sign_in_browser(stranger, listed)).json()["detail"]["code"] == "bad_signature"
    # one platform missing, an extra one, two different keys
    one = {k: v for k, v in list(good.items())[:1]}
    assert post(one).json()["detail"]["code"] == "signatures"
    assert post({**good, "linux-arm64": "x"}).json()["detail"]["code"] == "signatures"
    # a time that is not the one in the preview
    assert post(good, created="2020-01-01T00:00:00Z").status_code == 422
    assert client.get("/api/updates", headers=AUTH).json()["releases"] == []
    assert wait(client, done["id"], "built")["state"] == "built"
    assert post(good).status_code == 200  # and the right one still works


def test_the_hub_without_update_keys_cannot_publish(client: TestClient, builder: Builder) -> None:
    done = wait(client, start(client).json()["id"], "built")
    listed = client.get(f"/api/releases/builds/{done['id']}/manifests", headers=AUTH).json()
    client.app.state.updates.keys = []  # as a hub without TOPCOAT_UPDATE_PUBKEYS
    r = client.post(
        f"/api/releases/builds/{done['id']}/publish",
        headers=AUTH,
        json={"channel": "stable", "created": listed["created"], "signatures": {}},
    )
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_update_keys"


def test_what_the_node_reports_must_be_what_it_uploaded(
    client: TestClient, builder: Builder
) -> None:
    builder.upload_ok = False  # the output carries another checksum than the upload
    failed = wait(client, start(client).json()["id"], "failed")
    assert "artifact_mismatch" in (failed["error"] or "") or "not what the node reported" in (
        failed["error"] or ""
    )
    r = client.get(f"/api/releases/builds/{failed['id']}/manifests", headers=AUTH)
    assert r.status_code == 409


def test_a_step_that_fails_ends_the_build_as_failed_and_frees_the_node(
    client: TestClient, builder: Builder
) -> None:
    builder.node.capability_answers["build.fetch"] = {
        "error": {"code": "capability_failed", "message": "no such ref"}
    }
    failed = wait(client, start(client, ref="nope").json()["id"], "failed")
    assert "capability_failed" in failed["error"] and "no such ref" in failed["error"]
    builder.node.capability_answers["build.fetch"] = lambda i: {"commit": "not-a-commit"}
    bad = wait(client, start(client).json()["id"], "failed")
    assert "commit" in bad["error"]


def test_one_build_at_a_time(client: TestClient, builder: Builder) -> None:
    gate = {"open": False}

    def slow(i: dict[str, Any]) -> dict[str, Any]:
        import time

        while not gate["open"]:
            time.sleep(0.02)
        return {"commit": COMMIT, "changes": ""}

    builder.node.capability_answers["build.fetch"] = slow
    first = start(client).json()
    busy = start(client)
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "busy"
    gate["open"] = True
    wait(client, first["id"], "built")
    assert start(client, version="0.3.1").status_code == 201


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "shell", "ref": "main"},
        {"kind": "node_release", "ref": "main"},  # no version
        {"kind": "node_release", "ref": "main", "version": "latest"},
        {"kind": "node_release", "ref": "../etc/passwd", "version": "0.3.0"},
        {"kind": "node_release", "ref": "main; rm -rf /", "version": "0.3.0"},
        {"kind": "node_release", "ref": "main", "version": "0.3.0", "source": "anywhere"},
        {"kind": "node_release", "ref": "main", "version": "0.3.0", "command": "ls"},
    ],
)
def test_a_build_request_is_checked(
    client: TestClient, builder: Builder, body: dict[str, Any]
) -> None:
    r = client.post("/api/releases/builds", headers=AUTH, json=body)
    assert r.status_code in (422,)
    assert builder.node.build_calls == []


def test_a_hub_image_is_built_and_smoke_tested_but_not_published_through_the_channel(
    client: TestClient, builder: Builder
) -> None:
    done = wait(
        client, start(client, kind="hub_image", version=None, ref="main").json()["id"], "built"
    )
    assert [c["capability"] for c in builder.node.build_calls] == [
        "build.fetch", "build.hub_image", "build.smoke_test",
    ]  # fmt: skip
    assert done["artifacts"][0]["digest"] == "sha256:" + "b" * 64
    assert done["smoke"] == {"ok": True, "summary": "health ok"}
    r = client.get(f"/api/releases/builds/{done['id']}/manifests", headers=AUTH)
    assert r.status_code == 409  # only a node release has manifests
    builder.node.capability_answers["build.smoke_test"] = lambda i: {"ok": False, "summary": "down"}
    failed = wait(
        client, start(client, kind="hub_image", version=None, ref="main").json()["id"], "failed"
    )
    assert failed["smoke"]["ok"] is False


# --- the upload route ---


def test_only_the_build_node_may_upload_and_only_while_the_build_runs(
    client: TestClient, builder: Builder, connect: Any
) -> None:
    other_id, other_key = pair(client, "tools-other", role="tools")
    gate = {"open": False}

    def slow(i: dict[str, Any]) -> dict[str, Any]:
        import time

        while not gate["open"]:
            time.sleep(0.02)
        return builder.release(i)

    builder.node.capability_answers["build.node_release"] = slow
    build = start(client).json()
    eventually(lambda: _assert(builder.node.build_calls and len(builder.node.build_calls) >= 1))
    url = f"/api/node/builds/{build['id']}/artifacts/topcoat-node-linux-amd64"
    stranger = client.put(
        url, params={"os": "linux", "arch": "amd64"}, content=b"x",
        headers=auth_header(client, other_id, other_key),
    )  # fmt: skip
    assert stranger.status_code == 404
    unsigned = client.put(url, params={"os": "linux", "arch": "amd64"}, content=b"x")
    assert unsigned.status_code == 401
    bad_name = client.put(
        f"/api/node/builds/{build['id']}/artifacts/..%2Fevil",
        params={"os": "linux", "arch": "amd64"},
        content=b"x", headers=auth_header(client, builder.node_id, builder.key),
    )  # fmt: skip
    assert bad_name.status_code in (404, 422)
    gate["open"] = True
    done = wait(client, build["id"], "built")
    late = client.put(
        url, params={"os": "linux", "arch": "amd64"}, content=b"late",
        headers=auth_header(client, builder.node_id, builder.key),
    )  # fmt: skip
    assert late.status_code == 409
    assert {a["name"] for a in done["artifacts"]} == set(builder.files)


def test_builds_are_private_to_the_admin_who_started_them(
    client: TestClient, builder: Builder
) -> None:
    build = wait(client, start(client).json()["id"], "built")
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE release_builds SET user_id = :u WHERE id = :i"),
            {
                "u": str(uuid.uuid4())
                if False
                else make_user(TEST_DB_URL, "other admin", role="admin")[0],
                "i": build["id"],
            },
        )
    engine.dispose()
    assert client.get(f"/api/releases/builds/{build['id']}", headers=AUTH).status_code == 404
    listed = client.get("/api/releases", headers=AUTH).json()["builds"]
    assert build["id"] not in [b["id"] for b in listed]


def test_the_page_is_off_without_a_directory(settings: Settings, connect: Any) -> None:
    with TestClient(create_app(settings.model_copy(update={"update_dir": None}))) as client:
        assert client.get("/api/releases", headers=AUTH).json()["enabled"] is False
        r = client.post(
            "/api/releases/builds", headers=AUTH, json={"kind": "hub_image", "ref": "main"}
        )
        assert r.status_code == 503


# --- the roll-out of a hub image (phase 5) ---

DIGEST = "sha256:" + "b" * 64


class ProdHost:
    """The node of the production host: it offers hub.update as the admin's own step."""

    def __init__(self, client: TestClient, connect: Any) -> None:
        self.node_id, self.key = pair(client, "prod-host", role="tools")
        client.put(
            f"/api/nodes/{self.node_id}/config",
            headers=AUTH,
            json={
                **client.get(f"/api/nodes/{self.node_id}", headers=AUTH).json()["config"],
                "allowed_actions": ["hub.manage"],
                "capabilities": ["hub.update"],
            },
        )
        self.node: FakeNode = connect(self.node_id, self.key)
        inv = build_inventory()
        inv["capabilities"] = [
            {
                "name": "hub.update",
                "description": "roll out",
                "read_only": False,
                "manage": True,
                "input_schema": {"type": "object", "additionalProperties": True},
            }
        ]
        self.node.inventory = inv
        self.node.handshake()
        self.node.serve()
        eventually(
            lambda: _assert(
                client.get(f"/api/nodes/{self.node_id}/capabilities", headers=AUTH).json()[
                    "offered"
                ]
            )
        )
        self.node.capability_answers["hub.update"] = lambda i: {
            "ok": True,
            "dry_run": i["dry_run"],
            "output": "== checks ok token=ghp_" + "y" * 30,
        }


@pytest.fixture
def prod(client: TestClient, connect: Any) -> ProdHost:
    client.app.state.releases.rollout_node = "prod-host"
    return ProdHost(client, connect)


def hub_image(client: TestClient) -> dict[str, Any]:
    return wait(client, start(client, kind="hub_image", version=None).json()["id"], "built")


def rollout(client: TestClient, build_id: str, **body: Any):
    return client.post(f"/api/releases/builds/{build_id}/rollout", headers=AUTH, json=body)


def test_a_hub_image_is_rolled_out_after_a_dry_run_and_only_by_the_admin(
    client: TestClient, builder: Builder, prod: ProdHost
) -> None:
    image = hub_image(client)
    _, key = make_user(TEST_DB_URL, "rollout key", role="admin")
    assert rollout(client, image["id"], dry_run=True).status_code == 200
    assert client.post(
        f"/api/releases/builds/{image['id']}/rollout", headers=key, json={}
    ).status_code in (401, 403)
    done = rollout(client, image["id"], dry_run=False)
    assert done.status_code == 200, done.text
    attempts = done.json()["rollout"]
    assert [(a["dry_run"], a["ok"]) for a in attempts] == [(True, True), (False, True)]
    assert "ghp_" not in attempts[1]["output"] and attempts[1]["node"] == "prod-host"
    first, second = prod.node.hub_calls
    assert first["capability"] == second["capability"] == "hub.update"
    assert second["input"] == {
        "image": "topcoat-hub:local-aaaaaaa",
        "digest": DIGEST,
        "dry_run": False,
    }
    assert first["input"]["dry_run"] is True


def test_a_real_roll_out_wants_a_passed_dry_run_of_the_same_image_first(
    client: TestClient, builder: Builder, prod: ProdHost
) -> None:
    image = hub_image(client)
    r = rollout(client, image["id"], dry_run=False)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dry_run_first"
    assert prod.node.hub_calls == []
    # the dry run is the default
    assert rollout(client, image["id"]).json()["rollout"][-1]["dry_run"] is True


def test_a_failed_check_is_recorded_and_does_not_open_the_real_run(
    client: TestClient, builder: Builder, prod: ProdHost
) -> None:
    image = hub_image(client)
    # (a key "error" at the top of a fake node's answer would be taken for a protocol error)
    prod.node.capability_answers["hub.update"] = lambda i: {
        "ok": False,
        "output": "revision missing",
    }
    got = rollout(client, image["id"], dry_run=True).json()
    assert got["rollout"][-1]["ok"] is False and "revision missing" in got["rollout"][-1]["output"]
    assert rollout(client, image["id"], dry_run=False).json()["detail"]["code"] == "dry_run_first"
    prod.node.capability_answers["hub.update"] = {
        "error": {"code": "capability_failed", "message": "no such image"}
    }
    refused = rollout(client, image["id"], dry_run=True).json()["rollout"][-1]
    assert refused["ok"] is False and "no such image" in refused["error"]


def test_only_a_tested_hub_image_is_rolled_out_and_only_to_the_named_node(
    client: TestClient, builder: Builder, prod: ProdHost
) -> None:
    node_build = wait(client, start(client).json()["id"], "built")
    assert rollout(client, node_build["id"]).json()["detail"]["code"] == "not_built"
    builder.node.capability_answers["build.smoke_test"] = lambda i: {"ok": False, "summary": "down"}
    failed = wait(client, start(client, kind="hub_image", version=None).json()["id"], "failed")
    assert rollout(client, failed["id"]).json()["detail"]["code"] == "not_built"
    builder.node.capability_answers["build.smoke_test"] = lambda i: {"ok": True, "summary": "ok"}
    image = hub_image(client)
    client.app.state.releases.rollout_node = ""
    assert rollout(client, image["id"]).json()["detail"]["code"] == "no_rollout_node"
    client.app.state.releases.rollout_node = "no-such-node"
    assert rollout(client, image["id"]).json()["detail"]["code"] == "no_rollout_node"
    assert prod.node.hub_calls == []
