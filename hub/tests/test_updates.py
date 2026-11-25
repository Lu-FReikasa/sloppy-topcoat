"""The signed update channel on the hub (E2-06, D50): release store, admin and node API."""

import base64
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import auth_header, pair
from topcoat_hub.config import Settings
from topcoat_hub.main import create_app
from topcoat_hub.updates import (
    UpdateStore,
    is_newer,
    key_id,
    node_channel,
    parse_keys,
    parse_manifest,
    version_key,
)

DATA = Path(__file__).parent / "data" / "update"


class Signer:
    """What topcoat-sign does, in Python."""

    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.pub = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.pub_b64 = base64.b64encode(self.pub).decode()

    def build(
        self,
        version: str = "0.2.0",
        channel: str = "stable",
        os_name: str = "linux",
        arch: str = "amd64",
        binary: bytes | None = None,
        **override: object,
    ) -> dict[str, object]:
        binary = binary if binary is not None else f"program {version} {os_name}".encode()
        manifest = {
            "format": 1,
            "product": "topcoat-node",
            "channel": channel,
            "version": version,
            "os": os_name,
            "arch": arch,
            "size": len(binary),
            "sha256": hashlib.sha256(binary).hexdigest(),
            "created": "2026-10-07T20:00:00Z",
            **override,
        }
        text = json.dumps(manifest, separators=(",", ":"))
        sig = self.key.sign(b"topcoat-update-v1\n" + text.encode())
        line = f"topcoat-sig-v1 {key_id(self.pub)} {base64.b64encode(sig).decode()}"
        return {"manifest": text, "signature": line, "binary": binary, "plat": f"{os_name}-{arch}"}


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


def publish(client: TestClient, b: dict[str, object], headers: dict[str, str] = AUTH):
    manifest = json.loads(str(b["manifest"]))
    base = f"/api/updates/{manifest['channel']}/{manifest['version']}/{b['plat']}"
    r = client.put(
        f"{base}/manifest",
        json={"manifest": b["manifest"], "signature": b["signature"]},
        headers=headers,
    )
    if r.status_code != 204:
        return r
    return client.put(f"{base}/binary", content=b["binary"], headers=headers)


# --- pure helpers ---------------------------------------------------------------


def test_version_order_matches_the_node() -> None:
    ordered = [
        "0.1.0-dev",
        "0.1.0",
        "0.1.1",
        "0.2.0-rc.1",
        "0.2.0-rc.2",
        "0.2.0",
        "0.10.0",
        "1.0.0",
    ]
    assert sorted(ordered, key=version_key) == ordered
    assert sorted(reversed(ordered), key=version_key) == ordered


def test_newer_treats_a_dev_build_as_zero() -> None:
    assert is_newer("0.0.1", "dev-build")
    assert is_newer("0.2.0", None)
    assert is_newer("0.2.0", "0.1.0")
    assert not is_newer("0.2.0", "0.2.0")
    assert not is_newer("0.2.0-rc.1", "0.2.0")


def test_node_channel_defaults_to_stable() -> None:
    assert node_channel(None) == "stable"
    assert node_channel({}) == "stable"
    assert node_channel({"update": {"channel": "test"}}) == "test"
    assert node_channel({"update": "x"}) == "stable"


def test_parse_keys() -> None:
    s = Signer()
    assert parse_keys("") == []
    assert parse_keys(s.pub_b64) == [(key_id(s.pub), s.pub)]
    with pytest.raises(ValueError):
        parse_keys("not-a-key")
    with pytest.raises(ValueError):
        parse_keys(",".join(Signer().pub_b64 for _ in range(3)))


def test_the_manifest_is_read_strictly(signer: Signer) -> None:
    good = str(signer.build()["manifest"])
    assert parse_manifest(good)["version"] == "0.2.0"
    m = json.loads(good)
    for bad in (
        json.dumps({**m, "extra": 1}),
        json.dumps({k: v for k, v in m.items() if k != "sha256"}),
        json.dumps({**m, "size": True}),
        json.dumps({**m, "version": "latest"}),
        json.dumps({**m, "channel": "../x"}),
        json.dumps({**m, "product": "other"}),
        good.replace('"size"', '"size":1,"size"', 1),  # duplicate field
        "[]",
        "",
    ):
        with pytest.raises(Exception) as exc:
            parse_manifest(bad)
        assert getattr(exc.value, "code", "") == "bad_manifest", bad


def test_a_manifest_signed_by_the_go_tool_verifies() -> None:
    """The fixture was made by topcoat-sign: both sides must agree on the signed bytes."""
    pub = (DATA / "pub.txt").read_text().strip()
    store = UpdateStore(None, parse_keys(pub))
    manifest = (DATA / "manifest.json").read_text()
    signature = (DATA / "manifest.sig").read_text().strip()
    store.check_signature(manifest, signature)
    m = parse_manifest(manifest)
    binary = (DATA / "topcoat-node").read_bytes()
    assert m["size"] == len(binary)
    assert m["sha256"] == hashlib.sha256(binary).hexdigest()
    with pytest.raises(Exception) as exc:
        store.check_signature(manifest.replace("0.3.0", "0.3.1"), signature)
    assert exc.value.code == "bad_signature"  # type: ignore[attr-defined]


# --- admin API ------------------------------------------------------------------


def test_publish_list_and_prune(client: TestClient, signer: Signer) -> None:
    for v in ("0.2.0", "0.3.0", "0.4.0"):
        assert publish(client, signer.build(v)).status_code == 204
    r = client.get("/api/updates", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] and body["signature_check"]
    assert body["key_ids"] == [key_id(signer.pub)]
    # the store keeps the newest two
    assert [x["version"] for x in body["releases"]] == ["0.4.0", "0.3.0"]


def test_the_admin_api_is_for_the_admin(client: TestClient, signer: Signer) -> None:
    assert client.get("/api/updates").status_code == 401
    b = signer.build()
    r = client.put(
        "/api/updates/stable/0.2.0/linux-amd64/manifest",
        json={"manifest": b["manifest"], "signature": b["signature"]},
    )
    assert r.status_code == 401
    created = client.post("/api/keys", json={"name": "ci"}, headers=AUTH)
    assert created.status_code == 201
    # an API key never opens the admin API
    r = client.get("/api/updates", headers={"Authorization": f"Bearer {created.json()['key']}"})
    assert r.status_code == 403


def test_refused_uploads(client: TestClient, signer: Signer) -> None:
    other = Signer()
    # another key
    r = publish(client, other.build())
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_signature"
    # the path disagrees with the signed manifest
    b = signer.build("0.2.0")
    r = client.put(
        "/api/updates/stable/0.9.9/linux-amd64/manifest",
        json={"manifest": b["manifest"], "signature": b["signature"]},
        headers=AUTH,
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "mismatch"
    # a program that is not the one in the manifest
    b = signer.build("0.2.0")
    assert (
        client.put(
            "/api/updates/stable/0.2.0/linux-amd64/manifest",
            json={"manifest": b["manifest"], "signature": b["signature"]},
            headers=AUTH,
        ).status_code
        == 204
    )
    same_size = b"X" * len(b["binary"])  # type: ignore[arg-type]
    r = client.put("/api/updates/stable/0.2.0/linux-amd64/binary", content=same_size, headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "hash_mismatch"
    r = client.put("/api/updates/stable/0.2.0/linux-amd64/binary", content=b"short", headers=AUTH)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "size_mismatch"
    r = client.put(
        "/api/updates/stable/0.2.0/linux-amd64/binary", content=b["binary"] + b"+", headers=AUTH
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "size_mismatch"
    # nothing of the failed uploads is visible
    assert client.get("/api/updates", headers=AUTH).json()["releases"] == []
    # the right program completes the release; a second publish is refused
    r = client.put(
        "/api/updates/stable/0.2.0/linux-amd64/binary", content=b["binary"], headers=AUTH
    )
    assert r.status_code == 204
    assert publish(client, signer.build("0.2.0")).status_code == 409


def test_a_program_needs_its_manifest_first(client: TestClient) -> None:
    r = client.put("/api/updates/stable/0.2.0/linux-amd64/binary", content=b"x", headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_manifest"


def test_bad_paths_are_refused(client: TestClient) -> None:
    for path in (
        "/api/updates/Stable/0.2.0/linux-amd64/binary",
        "/api/updates/stable/0.2.0/linux/binary",
        "/api/updates/stable/..%2f..%2fx/linux-amd64/binary",
        "/api/updates/stable/latest/linux-amd64/binary",
    ):
        r = client.put(path, content=b"x", headers=AUTH)
        assert r.status_code in (400, 404, 409, 422), (path, r.status_code)
        assert r.status_code != 204


def test_delete_a_release(client: TestClient, signer: Signer) -> None:
    assert publish(client, signer.build("0.2.0")).status_code == 204
    assert client.delete("/api/updates/stable/0.2.0/linux-amd64", headers=AUTH).status_code == 204
    assert client.delete("/api/updates/stable/0.2.0/linux-amd64", headers=AUTH).status_code == 404
    assert client.get("/api/updates", headers=AUTH).json()["releases"] == []


def test_the_store_is_off_without_a_directory(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as c:
        body = c.get("/api/updates", headers=AUTH).json()
        assert body == {"enabled": False, "signature_check": False, "key_ids": [], "releases": []}
        r = c.put("/api/updates/stable/0.2.0/linux-amd64/binary", content=b"x", headers=AUTH)
        assert r.status_code in (409, 503)


# --- node API -------------------------------------------------------------------


def fetch(client: TestClient, node_id: str, key: Ed25519PrivateKey, name: str, **params: str):
    return client.get(
        f"/api/node/update/{name}", params=params, headers=auth_header(client, node_id, key)
    )


def test_a_node_gets_the_newest_release_of_its_channel_and_platform(
    client: TestClient, signer: Signer
) -> None:
    node_id, key = pair(client, "node-a", "linux")
    win_id, win_key = pair(client, "node-w", "windows")
    publish(client, signer.build("0.2.0"))
    publish(client, signer.build("0.3.0"))
    publish(client, signer.build("0.4.0", channel="test"))
    publish(client, signer.build("0.3.5", os_name="windows"))

    r = fetch(client, node_id, key, "manifest")
    assert r.status_code == 200
    got = r.json()
    assert json.loads(got["manifest"])["version"] == "0.3.0"
    assert got["signature"].startswith("topcoat-sig-v1 ")
    b = fetch(client, node_id, key, "binary")
    assert b.status_code == 200
    assert b.content == b"program 0.3.0 linux"
    assert b.headers["content-length"] == str(len(b.content))

    # a pinned version
    r = fetch(client, node_id, key, "manifest", version="0.2.0")
    assert json.loads(r.json()["manifest"])["version"] == "0.2.0"
    assert fetch(client, node_id, key, "manifest", version="9.9.9").status_code == 404

    # another platform gets its own build
    r = fetch(client, win_id, win_key, "manifest")
    assert json.loads(r.json()["manifest"])["version"] == "0.3.5"


def test_the_channel_comes_from_the_node_config(client: TestClient, signer: Signer) -> None:
    node_id, key = pair(client, "node-a", "linux")
    publish(client, signer.build("0.3.0"))
    publish(client, signer.build("0.4.0", channel="test"))
    cfg = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config"]
    cfg["update"] = {"channel": "test"}
    assert client.put(f"/api/nodes/{node_id}/config", json=cfg, headers=AUTH).status_code == 200
    r = fetch(client, node_id, key, "manifest")
    assert json.loads(r.json()["manifest"])["version"] == "0.4.0"


def test_node_downloads_need_a_valid_single_use_signature(
    client: TestClient, signer: Signer
) -> None:
    node_id, key = pair(client, "node-a", "linux")
    publish(client, signer.build("0.3.0"))
    assert client.get("/api/node/update/manifest").status_code == 401
    assert client.get("/api/node/update/manifest", headers=AUTH).status_code == 401
    headers = auth_header(client, node_id, key)
    assert client.get("/api/node/update/manifest", headers=headers).status_code == 200
    # the nonce is spent
    assert client.get("/api/node/update/manifest", headers=headers).status_code == 401
    # a signature of another key
    stranger = Ed25519PrivateKey.generate()
    bad = auth_header(client, node_id, stranger)
    assert client.get("/api/node/update/binary", headers=bad).status_code == 401


def test_no_release_is_a_404_with_a_code(client: TestClient) -> None:
    node_id, key = pair(client, "node-a", "linux")
    r = fetch(client, node_id, key, "manifest")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "no_release"


def test_node_downloads_without_a_store(settings: Settings) -> None:
    with TestClient(create_app(settings)) as c:
        node_id, key = pair(c, "node-a", "linux")
        r = fetch(c, node_id, key, "manifest")
        assert r.status_code == 404 and r.json()["detail"]["code"] == "no_release"


# --- GUI view -------------------------------------------------------------------


def test_the_node_summary_offers_a_newer_release(
    client: TestClient, signer: Signer, connect: Callable
) -> None:
    node_id, key = pair(client, "node-a", "linux")
    node = connect(node_id, key)
    node.handshake()
    publish(client, signer.build("0.5.0"))
    summary = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()
    up = summary["update"]
    assert up["available"] == "0.5.0"
    assert up["newer"] is True  # the fake node reports a development version
    assert up["channel"] == "stable"
    assert up["action_allowed"] is True  # node.update is part of the default actions
    listed = [n for n in client.get("/api/nodes", headers=AUTH).json() if n["id"] == node_id]
    assert listed[0]["update"]["available"] == "0.5.0"


def test_new_nodes_may_run_node_update(client: TestClient) -> None:
    node_id, _ = pair(client, "node-a", "linux")
    cfg = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config"]
    assert "node.update" in cfg["allowed_actions"]


def test_an_updater_older_than_the_node_is_shown() -> None:
    # live 2026-11-09: an old updater with a fresh heartbeat counted as "ok", and the update
    # failed until the program was installed once by hand
    from types import SimpleNamespace

    from topcoat_hub.views import _update_view, updater_behind

    assert updater_behind("0.2.3", "0.2.5")
    assert not updater_behind("0.2.6", "0.2.6") and not updater_behind("0.2.6", "0.2.5")
    assert not updater_behind(None, "0.2.5") and not updater_behind("9828731", "0.2.5")
    node = SimpleNamespace(config={}, agent_version="0.2.6")
    view = _update_view(node, {"update": {"updater": "ok", "updater_version": "0.2.4"}}, None)
    assert view["updater_version"] == "0.2.4" and view["updater_behind"] is True
    old = _update_view(node, {"update": {"updater": "ok"}}, None)  # a node before 0.2.6
    assert old["updater_version"] is None and old["updater_behind"] is False
