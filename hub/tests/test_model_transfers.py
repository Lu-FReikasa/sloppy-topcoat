"""Direct model transfers between nodes (E2-07, D51): the hub side, against two fake nodes."""

import copy
import json
import logging
import threading
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH
from tests.fakenode import INVENTORY, FakeNode, eventually, msg, pair
from tests.users import make_user
from topcoat_hub.config import Settings
from topcoat_hub.main import create_app
from topcoat_hub.model_transfers import ModelTransferStore
from topcoat_hub.models import DEV_ADMIN_ID, ModelTransfer

GIB = 1024**3
SHA = "a" * 64
CERT = "b" * 64


class Peer:
    """A fake node that answers actions from a table and records everything it is asked."""

    def __init__(self, client: TestClient, node: FakeNode, node_id: str) -> None:
        self.client, self.node, self.node_id = client, node, node_id
        self.actions: list[dict[str, Any]] = []
        self.configs: list[dict[str, Any]] = []
        # action name -> action_result body
        self.answers: dict[str, dict[str, Any]] = {
            "models.send": {"ok": True, "result": {"started": True}},
            "models.receive": {"ok": True, "result": {"started": True}},
            "models.delete": {"ok": True, "result": {}},
        }
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                m = self.node.recv()
            except Exception:  # socket closed
                return
            if m["type"] == "config":
                self.configs.append(m)
            elif m["type"] == "action":
                self.actions.append(m)
                body = self.answers.get(m["body"]["name"], {"ok": True, "result": {}})
                self.node.send(msg("action_result", body, re=m["id"]))

    def calls(self, name: str) -> list[dict[str, Any]]:
        return [a["body"]["args"] for a in self.actions if a["body"]["name"] == name]

    def first(self, name: str) -> dict[str, Any]:
        return eventually(lambda: self._one(name))

    def _one(self, name: str) -> dict[str, Any]:
        found = self.calls(name)
        assert found, f"no {name} yet"
        return found[0]

    def emit(self, kind: str, data: dict[str, Any], message: dict[str, Any] | None = None) -> None:
        self.node.send(message or msg("event", {"kind": kind, "data": data}))

    def ready(self, transfer_id: str, **over: Any) -> None:
        data = {
            "transfer_id": transfer_id,
            "model": "small",
            "size": 2 * GIB,
            "sha256": SHA,
            "port": 47000,
            "addresses": ["10.0.0.20", "fd00::20"],
            "cert_sha256": CERT,
        }
        self.emit("transfer_ready", {**data, **over})

    def progress(self, transfer_id: str, role: str, status: str, **over: Any) -> dict[str, Any]:
        data = {
            "transfer_id": transfer_id,
            "role": role,
            "status": status,
            "transferred_bytes": 100,
            "total_bytes": 2 * GIB,
            **over,
        }
        message = msg("event", {"kind": "transfer_progress", "data": data})
        self.emit("transfer_progress", data, message)
        return message


class World:
    def __init__(self, client: TestClient, connect: Callable[..., FakeNode]) -> None:
        self.client, self.connect = client, connect
        self.nodes: dict[str, tuple[str, Peer]] = {}

    def add(
        self,
        name: str,
        models: list[str],
        free_mb: int | None = 100_000,
        addresses: list[str] | None = None,
    ) -> tuple[str, Peer]:
        node_id, key = pair(self.client, name)
        node = self.connect(node_id, key)
        inventory = copy.deepcopy(INVENTORY)
        inventory["models"] = [{"name": m, "size_bytes": 2 * GIB, "format": "gguf"} for m in models]
        if free_mb is not None:
            inventory["hardware"]["disk_free_mb"] = free_mb
            inventory["hardware"]["disk_total_mb"] = 1_000_000
        inventory["hardware"]["addresses"] = addresses or []
        node.inventory = inventory
        node.handshake()

        def synced() -> None:
            detail = self.client.get(f"/api/nodes/{node_id}", headers=AUTH).json()
            assert detail["connected"]
            assert [m["name"] for m in detail["models"]] == models
            assert detail["addresses"] == (addresses or [])

        eventually(synced)
        peer = Peer(self.client, node, node_id)
        self.nodes[name] = (node_id, peer)
        return node_id, peer

    def start(self, src: str, dst: str, **body: Any):
        return self.client.post(
            "/api/model-transfers",
            headers=AUTH,
            json={
                "source_node_id": self.nodes[src][0],
                "target_node_id": self.nodes[dst][0],
                "model": "small",
                **body,
            },
        )

    def get(self, transfer_id: str) -> dict[str, Any]:
        r = self.client.get(f"/api/model-transfers/{transfer_id}", headers=AUTH)
        assert r.status_code == 200, r.text
        return r.json()

    def wait_status(self, transfer_id: str, status: str) -> dict[str, Any]:
        def check() -> dict[str, Any]:
            row = self.get(transfer_id)
            assert row["status"] == status, row
            return row

        return eventually(check)


@pytest.fixture
def world(client: TestClient, connect) -> World:
    w = World(client, connect)
    w.add("node-amd", ["small", "big"], addresses=["10.0.0.20"])
    w.add("node-laptop", [], free_mb=100_000, addresses=["10.0.0.30"])
    return w


def _started(world: World, **body: Any) -> dict[str, Any]:
    r = world.start("node-amd", "node-laptop", **body)
    assert r.status_code == 201, r.text
    return r.json()


# --- the happy path --------------------------------------------------------------------------


def test_copy_happy_path_dispatches_send_then_receive_with_endpoints_token_and_pin(
    world: World,
) -> None:
    amd, laptop = world.nodes["node-amd"][1], world.nodes["node-laptop"][1]
    # the address the hub sees the source connect from comes last among the endpoints
    world.client.app.state.sessions.get(uuid.UUID(world.nodes["node-amd"][0])).peer_ip = "10.0.0.5"
    row = _started(world)
    assert row["status"] == "preparing" and row["move"] is False
    assert row["model"] == "small" and row["target_name"] == "small"
    assert row["size"] == 2 * GIB and row["sha256"] is None
    assert row["user_id"] == str(DEV_ADMIN_ID)
    tid = row["id"]

    send = amd.first("models.send")
    assert send["transfer_id"] == tid and send["model"] == "small"
    assert send["peer_addresses"] == [
        "10.0.0.30"
    ]  # the target's candidates ("testclient" is no IP)
    assert len(send["token"]) == 43
    assert not laptop.calls("models.receive")  # nothing to receive before the source is ready

    amd.ready(tid)
    recv = laptop.first("models.receive")
    assert recv == {
        "transfer_id": tid,
        "model": "small",
        "size": 2 * GIB,
        "sha256": SHA,
        "token": send["token"],
        "cert_sha256": CERT,
        "endpoints": ["10.0.0.20:47000", "[fd00::20]:47000", "10.0.0.5:47000"],
    }
    ready = world.get(tid)
    assert ready["sha256"] == SHA and ready["status"] in ("ready", "transferring")

    amd.progress(tid, "send", "running")
    laptop.progress(tid, "receive", "running", transferred_bytes=GIB)
    world.wait_status(tid, "transferring")
    laptop.progress(tid, "receive", "done", transferred_bytes=2 * GIB)
    done = world.wait_status(tid, "done")
    assert done["error"] is None and done["error_code"] is None
    assert not amd.calls("models.delete")  # a copy deletes nothing
    assert "token" not in json.dumps(done)


def test_target_name_is_used_on_the_target(world: World) -> None:
    amd, laptop = world.nodes["node-amd"][1], world.nodes["node-laptop"][1]
    row = _started(world, target_name="renamed")
    assert row["target_name"] == "renamed"
    amd.ready(row["id"])
    assert laptop.first("models.receive")["model"] == "renamed"


def test_status_changes_reach_the_gui_stream_for_the_owner_only(world: World) -> None:
    bus = world.client.app.state.bus
    queue = bus.subscribe()
    amd, laptop = world.nodes["node-amd"][1], world.nodes["node-laptop"][1]
    row = _started(world)
    amd.ready(row["id"])
    laptop.first("models.receive")
    laptop.progress(row["id"], "receive", "running", transferred_bytes=7)
    laptop.progress(row["id"], "receive", "done")
    world.wait_status(row["id"], "done")

    events = []
    while not queue.empty():
        name, data = queue.get_nowait()
        if name == "model_transfer":
            events.append(
                {**data["transfer"], **{k: v for k, v in data.items() if k != "transfer"}}
            )
    bus.unsubscribe(queue)
    assert next(e["status"] for e in events if e["id"] == row["id"]) == "pending"
    assert events[-1]["status"] == "done"
    assert any(e.get("transferred_bytes") == 7 for e in events)  # progress is shown
    assert {e["_user"] for e in events} == {str(DEV_ADMIN_ID)}  # sse_stream strips it and filters


# --- move ------------------------------------------------------------------------------------


def test_move_deletes_the_source_only_after_the_target_is_done(world: World) -> None:
    amd, laptop = world.nodes["node-amd"][1], world.nodes["node-laptop"][1]
    row = _started(world, move=True)
    tid = row["id"]
    assert row["move"] is True
    amd.ready(tid)
    laptop.first("models.receive")
    amd.progress(tid, "send", "done")  # the source finished sending: not yet the target's word
    laptop.progress(tid, "receive", "running")
    world.wait_status(tid, "transferring")
    assert not amd.calls("models.delete")

    message = laptop.progress(tid, "receive", "done")
    laptop.emit("", {}, message)  # a replayed delivery of the same event
    world.wait_status(tid, "done")
    eventually(lambda: amd.first("models.delete"))
    assert amd.calls("models.delete") == [{"model": "small", "confirm": True}]
    assert world.get(tid)["error"] is None


def test_a_failed_delete_is_a_warning_and_the_transfer_stays_done(world: World) -> None:
    amd, laptop = world.nodes["node-amd"][1], world.nodes["node-laptop"][1]
    amd.answers["models.delete"] = {
        "ok": False,
        "error": {"code": "internal", "message": "file is locked"},
    }
    tid = _started(world, move=True)["id"]
    amd.ready(tid)
    laptop.first("models.receive")
    laptop.progress(tid, "receive", "done")

    def warned() -> dict[str, Any]:
        row = world.get(tid)
        assert row["error"] and "file is locked" in row["error"], row
        return row

    row = eventually(warned)
    assert row["status"] == "done" and row["error_code"] is None


def test_move_deletes_nothing_when_the_transfer_failed(world: World) -> None:
    amd, laptop = world.nodes["node-amd"][1], world.nodes["node-laptop"][1]
    tid = _started(world, move=True)["id"]
    amd.ready(tid)
    laptop.first("models.receive")
    laptop.progress(tid, "receive", "failed", error_code="hash_mismatch", error="bad bytes")
    row = world.wait_status(tid, "failed")
    assert (row["error_code"], row["error"]) == ("hash_mismatch", "bad bytes")
    assert not amd.calls("models.delete")


# --- failures --------------------------------------------------------------------------------


def test_no_route_reported_by_the_target_fails_the_transfer(world: World) -> None:
    amd, laptop = world.nodes["node-amd"][1], world.nodes["node-laptop"][1]
    tid = _started(world)["id"]
    amd.ready(tid)
    laptop.first("models.receive")
    laptop.progress(
        tid, "receive", "failed", error_code="no_route", error="no candidate address answered"
    )
    row = world.wait_status(tid, "failed")
    assert row["error_code"] == "no_route" and "no candidate" in row["error"]
    # the transfer is over: a late event changes nothing and the nodes are free again
    amd.progress(tid, "send", "running")
    assert world.get(tid)["status"] == "failed"
    assert world.start("node-amd", "node-laptop", target_name="again").status_code == 201


def test_the_source_failing_fails_the_transfer(world: World) -> None:
    amd = world.nodes["node-amd"][1]
    tid = _started(world)["id"]
    amd.progress(tid, "send", "failed", error_code="timeout", error="no peer connected")
    row = world.wait_status(tid, "failed")
    assert row["error_code"] == "timeout"


def test_an_unknown_error_code_from_a_node_is_stored_as_internal(world: World) -> None:
    amd = world.nodes["node-amd"][1]
    tid = _started(world)["id"]
    amd.progress(tid, "send", "failed", error_code="made_up", error="x")
    assert world.wait_status(tid, "failed")["error_code"] == "internal"


def test_the_target_refusing_receive_fails_the_transfer(world: World) -> None:
    amd, laptop = world.nodes["node-amd"][1], world.nodes["node-laptop"][1]
    laptop.answers["models.receive"] = {
        "ok": False,
        "error": {"code": "busy", "message": "gaming"},
    }
    tid = _started(world)["id"]
    amd.ready(tid)
    row = eventually(lambda: _failed(world, tid))
    assert row["error_code"] == "busy" and row["error"] == "gaming"


def _failed(world: World, tid: str) -> dict[str, Any]:
    row = world.get(tid)
    assert row["status"] == "failed", row
    return row


def test_the_source_refusing_send_answers_409_and_keeps_a_failed_row(world: World) -> None:
    amd = world.nodes["node-amd"][1]
    amd.answers["models.send"] = {"ok": False, "error": {"code": "busy", "message": "gaming"}}
    r = world.start("node-amd", "node-laptop")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "busy"
    [row] = world.client.get("/api/model-transfers", headers=AUTH).json()["transfers"]
    assert row["status"] == "failed" and row["error_code"] == "busy"
    # not stuck: the nodes are free for the next try
    amd.answers["models.send"] = {"ok": True, "result": {"started": True}}
    assert world.start("node-amd", "node-laptop").status_code == 201


def test_a_malformed_transfer_ready_fails_the_transfer(world: World) -> None:
    amd = world.nodes["node-amd"][1]
    tid = _started(world)["id"]
    amd.ready(tid, port=0)
    assert world.wait_status(tid, "failed")["error_code"] == "internal"


def test_a_source_without_any_address_is_no_route(world: World) -> None:
    amd = world.nodes["node-amd"][1]
    tid = _started(world)["id"]
    amd.ready(tid, addresses=["not-an-ip"])  # nothing usable, and "testclient" is not an IP
    assert world.wait_status(tid, "failed")["error_code"] == "no_route"


def test_events_of_other_transfers_or_nodes_are_ignored(world: World) -> None:
    amd, laptop = world.nodes["node-amd"][1], world.nodes["node-laptop"][1]
    tid = _started(world)["id"]
    amd.progress(str(uuid.uuid4()), "send", "failed")  # unknown transfer
    laptop.ready(tid)  # only the source can be ready
    laptop.progress(tid, "send", "failed")  # the target speaks as the receiver only
    amd.progress(tid, "receive", "done")  # and the source as the sender only
    assert not laptop.calls("models.receive")
    assert world.get(tid)["status"] == "preparing"


# --- refusals at the start -------------------------------------------------------------------


def test_refusals_not_found_offline_not_allowed_missing_exists_disk(
    world: World, client: TestClient
) -> None:
    amd = world.nodes["node-amd"][1]
    ghost, _ = pair(client, "node-nvidia")  # paired, never connected
    unknown = str(uuid.uuid4())

    def code(r) -> tuple[int, str]:
        return r.status_code, r.json()["detail"]["code"]

    amd_id = world.nodes["node-amd"][0]
    lap_id = world.nodes["node-laptop"][0]
    post = lambda **b: client.post(  # noqa: E731
        "/api/model-transfers",
        headers=AUTH,
        json={"source_node_id": amd_id, "target_node_id": lap_id, "model": "small", **b},
    )
    assert code(post(target_node_id=unknown)) == (404, "not_found")
    assert code(post(target_node_id=ghost)) == (409, "node_offline")
    assert code(post(source_node_id=ghost)) == (409, "node_offline")
    assert code(post(model="nope")) == (409, "model_missing")
    assert code(post(target_node_id=amd_id)) == (400, "invalid_request")
    assert post(extra=1).status_code == 422
    # the target already has the (target) name
    world.add("node-full", ["small"])
    assert code(post(target_node_id=world.nodes["node-full"][0])) == (409, "model_exists")
    assert post(target_node_id=world.nodes["node-full"][0], target_name="other").status_code == 201
    # nothing was started by any refusal but the last one
    assert len(amd.calls("models.send")) == 1


def test_not_allowed_when_either_node_lacks_its_action(world: World, client: TestClient) -> None:
    amd_id, amd = world.nodes["node-amd"]
    lap_id, laptop = world.nodes["node-laptop"]

    def without(node_id: str, action: str) -> None:
        config = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config"]
        config["allowed_actions"].remove(action)
        assert (
            client.put(f"/api/nodes/{node_id}/config", headers=AUTH, json=config).status_code == 200
        )

    without(amd_id, "models.send")
    r = world.start("node-amd", "node-laptop")
    assert (r.status_code, r.json()["detail"]["code"]) == (403, "not_allowed")
    assert not amd.calls("models.send")

    world.add("node-ok", [], addresses=["10.0.0.40"])
    r = world.start("node-ok", "node-laptop")  # node-ok has no model: but allowed
    assert r.json()["detail"]["code"] == "model_missing"
    without(lap_id, "models.receive")
    world.add("node-src2", ["small"])
    r = world.start("node-src2", "node-laptop")
    assert (r.status_code, r.json()["detail"]["code"]) == (403, "not_allowed")
    assert not laptop.calls("models.receive")


@pytest.mark.parametrize(
    ("free_mb", "status"),
    [(3 * 1024 - 1, 409), (3 * 1024, 201), (None, 201)],  # size 2 GiB + 1 GiB reserve
)
def test_the_target_disk_must_cover_size_plus_one_gib(
    client: TestClient, connect, free_mb: int | None, status: int
) -> None:
    w = World(client, connect)
    w.add("node-amd", ["small"])
    w.add("node-laptop", [], free_mb=free_mb)
    r = w.start("node-amd", "node-laptop")
    assert r.status_code == status, r.text
    if status == 409:
        assert r.json()["detail"]["code"] == "insufficient_disk"


def test_only_one_transfer_per_source_and_per_target(world: World) -> None:
    world.add("node-nvidia", ["small"])
    first = _started(world)

    def refused(src: str, dst: str) -> None:
        r = world.start(src, dst, target_name="x")
        assert (r.status_code, r.json()["detail"]["code"]) == (409, "busy")

    refused("node-nvidia", "node-laptop")  # same target
    refused("node-amd", "node-nvidia")  # same source
    refused("node-nvidia", "node-amd")  # a source that is already sending is not a target either
    world.client.post(f"/api/model-transfers/{first['id']}/cancel", headers=AUTH)
    assert world.start("node-nvidia", "node-laptop", target_name="x").status_code == 201


# --- cancel, timeout, restart ----------------------------------------------------------------


def test_cancel_marks_the_transfer_and_frees_the_nodes(world: World) -> None:
    amd, laptop = world.nodes["node-amd"][1], world.nodes["node-laptop"][1]
    tid = _started(world)["id"]
    r = world.client.post(f"/api/model-transfers/{tid}/cancel", headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "canceled"
    assert r.json()["error_code"] == "canceled"
    # a late ready of the source is ignored: nothing is sent to the target
    amd.ready(tid)
    again = world.client.post(f"/api/model-transfers/{tid}/cancel", headers=AUTH)
    assert (again.status_code, again.json()["detail"]["code"]) == (409, "not_active")
    assert world.get(tid)["status"] == "canceled"
    assert not laptop.calls("models.receive")
    unknown = world.client.post(f"/api/model-transfers/{uuid.uuid4()}/cancel", headers=AUTH)
    assert unknown.status_code == 404


def test_no_progress_for_too_long_fails_with_timeout(world: World) -> None:
    world.client.app.state.transfers.timeout_seconds = 0.4
    amd, laptop = world.nodes["node-amd"][1], world.nodes["node-laptop"][1]
    tid = _started(world)["id"]
    amd.ready(tid)
    laptop.first("models.receive")
    row = eventually(lambda: _failed(world, tid))
    assert row["error_code"] == "timeout"


def test_progress_keeps_a_transfer_alive_past_the_timeout(world: World) -> None:
    import time

    world.client.app.state.transfers.timeout_seconds = 0.6
    laptop = world.nodes["node-laptop"][1]
    tid = _started(world)["id"]
    for _ in range(5):
        time.sleep(0.25)
        laptop.progress(tid, "receive", "running")
    assert world.get(tid)["status"] != "failed"
    laptop.progress(tid, "receive", "done")
    world.wait_status(tid, "done")


def test_rows_left_running_by_a_dead_hub_are_failed_as_interrupted_at_startup(
    world: World, client: TestClient, settings: Settings
) -> None:
    amd_id, lap_id = world.nodes["node-amd"][0], world.nodes["node-laptop"][0]
    ids = {}
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        for status in ("pending", "preparing", "ready", "transferring", "done", "canceled"):
            ids[status] = str(uuid.uuid4())
            conn.execute(
                text(
                    "INSERT INTO model_transfers (id, user_id, source_node_id, target_node_id, "
                    "model, target_name, move, status) VALUES (:id, :u, :s, :t, 'small', 'small', "
                    "false, :status)"
                ),
                {
                    "id": ids[status],
                    "u": str(DEV_ADMIN_ID),
                    "s": amd_id,
                    "t": lap_id,
                    "status": status,
                },
            )
    engine.dispose()
    with TestClient(create_app(settings)) as restarted:
        for status, tid in ids.items():
            row = restarted.get(f"/api/model-transfers/{tid}", headers=AUTH).json()
            if status in ("done", "canceled"):
                assert row["status"] == status and row["error_code"] is None
            else:
                assert row["status"] == "failed" and row["error_code"] == "interrupted"
                assert "restarted" in row["error"]


# --- list / read / isolation -----------------------------------------------------------------


def test_list_is_newest_first_and_filters_by_node(world: World) -> None:
    amd = world.nodes["node-amd"][1]
    world.add("node-nvidia", [])
    first = _started(world)
    world.client.post(f"/api/model-transfers/{first['id']}/cancel", headers=AUTH)
    second = world.start("node-amd", "node-nvidia", target_name="b").json()
    assert amd.first("models.send")
    listing = world.client.get("/api/model-transfers", headers=AUTH).json()["transfers"]
    assert [t["id"] for t in listing] == [second["id"], first["id"]]
    for node, expected in (
        ("node-laptop", [first["id"]]),
        ("node-nvidia", [second["id"]]),
        ("node-amd", [second["id"], first["id"]]),
    ):
        r = world.client.get(
            "/api/model-transfers", headers=AUTH, params={"node_id": world.nodes[node][0]}
        )
        assert [t["id"] for t in r.json()["transfers"]] == expected
    assert world.client.get(f"/api/model-transfers/{uuid.uuid4()}", headers=AUTH).status_code == 404


def test_the_rest_api_is_admin_only(world: World, client: TestClient, clean_db: str) -> None:
    tid = _started(world)["id"]
    _, key = make_user(clean_db, "user-b", role="admin")  # an API key never opens the admin API
    assert client.get("/api/model-transfers").status_code in (401, 403)
    for call in (
        lambda h: client.get("/api/model-transfers", headers=h),
        lambda h: client.get(f"/api/model-transfers/{tid}", headers=h),
        lambda h: client.post(f"/api/model-transfers/{tid}/cancel", headers=h),
    ):
        assert call(key).status_code == 403


def test_the_store_only_sees_the_rows_of_its_own_user(world: World, clean_db: str) -> None:
    other, _ = make_user(clean_db, "user-b")
    tid = uuid.UUID(_started(world)["id"])
    coordinator = world.client.app.state.transfers

    async def probe() -> None:
        async with coordinator.sessionmaker() as db:
            mine, theirs = ModelTransferStore(db, DEV_ADMIN_ID), ModelTransferStore(db, other)
            assert (await mine.get(tid)) is not None
            assert (await theirs.get(tid)) is None
            assert await theirs.list() == [] and await theirs.list(node_id=uuid.uuid4()) == []
            assert await theirs.update(tid, status="failed") is None  # not theirs: untouched
            assert len(await mine.list()) == 1
            row = await theirs.add(
                ModelTransfer(
                    source_node_id=(await mine.get(tid)).source_node_id,
                    target_node_id=(await mine.get(tid)).target_node_id,
                    model="m",
                    target_name="m",
                )
            )
            assert row.user_id == other  # the store stamps its own user, whatever the caller set
            await db.commit()
            assert [r.id for r in await mine.list()] == [tid]
        assert await coordinator.get(other, tid) is None
        assert [t.id for t in await coordinator.list(DEV_ADMIN_ID, None)] == [tid]

    world.client.portal.call(probe)
    from topcoat_hub.model_transfers import TransferError

    async def cancel_foreign() -> None:
        with pytest.raises(TransferError) as err:
            await coordinator.cancel(other, tid)
        assert err.value.code == "not_found"

    world.client.portal.call(cancel_foreign)
    assert world.get(str(tid))["status"] == "preparing"


# --- the token is a secret -------------------------------------------------------------------


def test_the_token_is_only_ever_on_the_two_node_sessions(
    world: World, client: TestClient, caplog: pytest.LogCaptureFixture, settings: Settings
) -> None:
    caplog.set_level(logging.DEBUG)
    amd, laptop = world.nodes["node-amd"][1], world.nodes["node-laptop"][1]
    seen: list[str] = []
    row = _started(world, move=True)
    seen.append(json.dumps(row))
    tid = row["id"]
    token = amd.first("models.send")["token"]
    amd.ready(tid)
    assert laptop.first("models.receive")["token"] == token
    laptop.progress(tid, "receive", "running")
    laptop.progress(tid, "receive", "done")
    eventually(lambda: amd.first("models.delete"))
    seen.append(json.dumps(world.get(tid)))
    seen.append(client.get("/api/model-transfers", headers=AUTH).text)
    seen.append(client.get(f"/api/nodes/{world.nodes['node-amd'][0]}", headers=AUTH).text)
    seen.append(client.get(f"/api/nodes/{world.nodes['node-laptop'][0]}", headers=AUTH).text)

    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        for table in ("model_transfers", "node_events", "node_inventory", "nodes"):
            query = text(f"SELECT t::text FROM {table} t")  # noqa: S608
            seen.append(repr(conn.execute(query).scalars().all()))
    engine.dispose()
    assert token not in caplog.text
    for blob in seen:
        assert token not in blob
    # a fresh token per transfer
    assert world.start("node-amd", "node-laptop", target_name="again").status_code == 201
    assert amd.calls("models.send")[1]["token"] != token
    assert repr(client.app.state.transfers._active) and token not in repr(
        client.app.state.transfers._active
    )


def test_the_node_summary_lists_addresses_and_allowed_actions(world: World) -> None:
    node_id = world.nodes["node-amd"][0]
    detail = world.client.get(f"/api/nodes/{node_id}", headers=AUTH).json()
    assert detail["addresses"] == ["10.0.0.20"]
    assert (
        "models.send" in detail["allowed_actions"] and "models.receive" in detail["allowed_actions"]
    )
    listed = world.client.get("/api/nodes", headers=AUTH).json()
    rows = listed["nodes"] if isinstance(listed, dict) else listed
    assert all(isinstance(n["allowed_actions"], list) for n in rows)
