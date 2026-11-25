"""Benchmark results (node_benchmarks), recommended threads and the admin config editor."""

import copy
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import FakeNode, eventually, msg, pair
from topcoat_hub.config import REPO_ROOT
from topcoat_hub.models import Node, NodeInventory
from topcoat_hub.protocol import config_hash, load_schemas
from topcoat_hub.service import benchmark_row
from topcoat_hub.views import node_summary

SCHEMAS = load_schemas(REPO_ROOT / "proto" / "v1")
BENCH_ID = "7b1e9f00-0000-4000-8000-0000000000aa"


def _result(model: str, backend: str, threads: int, tg: float, **extra: Any) -> dict[str, Any]:
    data = {
        "benchmark_id": BENCH_ID,
        "model": model,
        "backend": backend,
        "threads": threads,
        "n_gpu_layers": 0 if backend == "cpu" else 99,
        "pp_tps": 100.5,
        "tg_tps": tg,
        **extra,
    }
    return msg("event", {"kind": "benchmark_result", "data": data})


def _events(client: TestClient, node_id: str) -> list[dict[str, Any]]:
    return client.get(f"/api/nodes/{node_id}/events", headers=AUTH).json()


def _wait_for_event(client: TestClient, node_id: str, kind: str, count: int = 1) -> None:
    def check() -> None:
        assert [e["kind"] for e in _events(client, node_id)].count(kind) >= count

    eventually(check)


def _connected(client: TestClient, connect, name: str = "node-a") -> tuple[str, FakeNode]:
    node_id, cred = pair(client, name)
    node = connect(node_id, cred)
    node.handshake()
    return node_id, node


def _config_message(node: FakeNode) -> dict[str, Any]:
    """Next config message the node receives."""
    while True:
        m = node.recv()
        if m["type"] == "config":
            return m


# --- node_benchmarks ------------------------------------------------------------------


def test_benchmark_results_are_stored_once(client: TestClient, connect) -> None:
    node_id, node = _connected(client, connect)
    live = _result("small", "vulkan", 6, 40.0)
    live["ts"] = "2026-01-01T10:00:00Z"
    queued = _result("small", "cpu", 2, 14.2)
    queued["ts"] = "2026-01-01T09:00:00Z"
    node.send(live)
    node.send(live)  # duplicate message id
    node.send(msg("queued_events", {"events": [queued]}))
    node.send(msg("queued_events", {"events": [queued]}))  # replay after a lost ack
    node.send(msg("event", {"kind": "game_ended", "data": {}}))
    _wait_for_event(client, node_id, "game_ended")

    r = client.get(f"/api/nodes/{node_id}/benchmarks", headers=AUTH)
    assert r.status_code == 200
    rows = r.json()
    assert rows == [
        {
            "benchmark_id": BENCH_ID,
            "model": "small",
            "backend": "vulkan",
            "threads": 6,
            "n_gpu_layers": 99,
            "pp_tps": 100.5,
            "tg_tps": 40.0,
            "ts": "2026-01-01T10:00:00+00:00",
        },
        {
            "benchmark_id": BENCH_ID,
            "model": "small",
            "backend": "cpu",
            "threads": 2,
            "n_gpu_layers": 0,
            "pp_tps": 100.5,
            "tg_tps": 14.2,
            "ts": "2026-01-01T09:00:00+00:00",
        },
    ]
    detail = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()
    assert detail["benchmarks"] == rows
    node.close()


def test_malformed_benchmark_result_keeps_event_but_no_row(client: TestClient, connect) -> None:
    node_id, node = _connected(client, connect)
    bad = msg("event", {"kind": "benchmark_result", "data": {"model": "small", "tg_tps": "x"}})
    node.send(bad)
    _wait_for_event(client, node_id, "benchmark_result")
    assert client.get(f"/api/nodes/{node_id}/benchmarks", headers=AUTH).json() == []
    node.close()


def test_benchmarks_endpoint_auth_and_unknown(client: TestClient) -> None:
    node_id, _ = pair(client, "node-a")
    assert client.get(f"/api/nodes/{node_id}/benchmarks").status_code == 401
    unknown = f"/api/nodes/{uuid.uuid4()}/benchmarks"
    assert client.get(unknown, headers=AUTH).status_code == 404


def test_benchmark_row_validation() -> None:
    node_id = uuid.uuid4()
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    good = _result("m", "cpu", 4, 10)["body"]["data"]
    row = benchmark_row(node_id, good, ts)
    assert row is not None
    assert (row.model, row.threads, row.tg_tps, row.n_gpu_layers) == ("m", 4, 10.0, 0)
    assert benchmark_row(node_id, {**good, "n_gpu_layers": None}, ts) is not None
    for broken in (
        {k: v for k, v in good.items() if k != "model"},
        {**good, "benchmark_id": "not-a-uuid"},
        {**good, "threads": 0},
        {**good, "threads": True},
        {**good, "tg_tps": float("nan")},
        {**good, "pp_tps": "fast"},
        {**good, "backend": "x" * 21},
        {**good, "n_gpu_layers": 1.5},
    ):
        assert benchmark_row(node_id, broken, ts) is None, broken


# --- recommended threads ----------------------------------------------------------------


def test_benchmark_finished_sets_threads_and_pushes_config(client: TestClient, connect) -> None:
    node_id, node = _connected(client, connect)
    finished = msg(
        "event",
        {
            "kind": "benchmark_finished",
            "data": {"benchmark_id": BENCH_ID, "ok": True, "recommended_threads": 6},
        },
    )
    node.send(finished)
    pushed = _config_message(node)["body"]
    assert pushed["config"]["runner"]["threads"] == 6
    assert pushed["config_hash"] == config_hash(pushed["config"])
    SCHEMAS.validate("node_config", pushed["config"])

    _wait_for_event(client, node_id, "config_changed")
    detail = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()
    assert detail["config"]["runner"]["threads"] == 6
    assert detail["config_hash"] == pushed["config_hash"]

    # A replayed event and an unchanged recommendation push nothing and log nothing.
    node.send(msg("queued_events", {"events": [finished]}))
    same = copy.deepcopy(finished)
    same["id"] = str(uuid.uuid4())
    node.send(same)
    node.send(msg("event", {"kind": "game_ended", "data": {}}))
    _wait_for_event(client, node_id, "game_ended")
    kinds = [e["kind"] for e in _events(client, node_id)]
    assert kinds.count("config_changed") == 1
    assert kinds.count("benchmark_finished") == 2
    node.close()


@pytest.mark.parametrize("threads", [0, "6", 2000, None])
def test_benchmark_finished_ignores_invalid_threads(
    client: TestClient, connect, threads: Any
) -> None:
    node_id, node = _connected(client, connect)
    data: dict[str, Any] = {"benchmark_id": BENCH_ID, "ok": True, "recommended_threads": threads}
    node.send(msg("event", {"kind": "benchmark_finished", "data": data}))
    node.send(msg("event", {"kind": "game_ended", "data": {}}))
    _wait_for_event(client, node_id, "game_ended")
    detail = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()
    assert "threads" not in detail["config"]["runner"]
    assert "config_changed" not in [e["kind"] for e in detail["events"]]
    node.close()


# --- PUT /api/nodes/{id}/config ---------------------------------------------------------


def _edited_config(client: TestClient, node_id: str) -> dict[str, Any]:
    config = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config"]
    config["runner"]["backend"] = "vulkan"
    config["runner"]["llamacpp_release"] = "b11223"
    config["watcher"]["rules"] = [
        {"process": "game.exe"},
        {"path_prefix": "D:\\SteamLibrary\\steamapps\\common\\"},
    ]
    return config


def test_put_config_validates_stores_and_pushes(client: TestClient, connect) -> None:
    node_id, node = _connected(client, connect)
    config = _edited_config(client, node_id)
    r = client.put(f"/api/nodes/{node_id}/config", headers=AUTH, json=config)
    assert r.status_code == 200, r.text
    assert r.json() == {"config": config, "config_hash": config_hash(config)}

    pushed = _config_message(node)["body"]
    assert pushed == {"config_hash": config_hash(config), "config": config}

    detail = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()
    assert detail["config"] == config
    audit = next(e for e in detail["events"] if e["kind"] == "config_changed")
    assert audit["data"] == {"user_id": "00000000-0000-4000-8000-000000000001"}
    node.close()


def test_put_config_accepts_context_lengths_and_rejects_bad_ones(client: TestClient) -> None:
    node_id, _ = pair(client, "node-a")
    url = f"/api/nodes/{node_id}/config"
    config = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config"]
    config["runner"]["default_context_length"] = 16384
    config["runner"]["model_context_lengths"] = {"Qwen2.5-7B-Instruct-Q4_K_M": 32768}
    r = client.put(url, headers=AUTH, json=config)
    assert r.status_code == 200, r.text
    assert r.json()["config"]["runner"]["default_context_length"] == 16384

    for runner in (
        {"default_context_length": 100},
        {"model_context_lengths": {"a": 10}},
        {"model_context_lengths": {"": 4096}},
        {"model_context_lengths": {"a": "big"}},
    ):
        bad = copy.deepcopy(config)
        bad["runner"] = {**bad["runner"], **runner}
        assert client.put(url, headers=AUTH, json=bad).status_code == 400, runner


def test_put_config_rejects_invalid(client: TestClient) -> None:
    node_id, _ = pair(client, "node-a")
    url = f"/api/nodes/{node_id}/config"
    original = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config"]

    bad = copy.deepcopy(original)
    bad["runner"]["backend"] = "rocm"
    r = client.put(url, headers=AUTH, json=bad)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_config"
    assert "runner/backend" in r.json()["detail"]["message"]

    empty_rule = copy.deepcopy(original)
    empty_rule["watcher"]["rules"] = [{}]
    assert client.put(url, headers=AUTH, json=empty_rule).status_code == 400
    assert client.put(url, headers=AUTH, json=["not", "an", "object"]).status_code == 400
    r = client.put(url, headers={**AUTH, "Content-Type": "application/json"}, content=b"{nope")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_config"

    assert client.get(f"/api/nodes/{node_id}", headers=AUTH).json()["config"] == original
    assert client.put(url, json=original).status_code == 401
    assert (
        client.put(f"/api/nodes/{uuid.uuid4()}/config", headers=AUTH, json=original).status_code
        == 404
    )


def test_put_config_while_offline_is_sent_on_next_hello(client: TestClient, connect) -> None:
    node_id, cred = pair(client, "node-a")
    config = _edited_config(client, node_id)
    r = client.put(f"/api/nodes/{node_id}/config", headers=AUTH, json=config)
    assert r.status_code == 200
    node = connect(node_id, cred)
    hello_config = node.handshake()
    assert hello_config["body"]["config"] == config
    node.close()


# --- views ------------------------------------------------------------------------------


def test_node_summary_merges_igpu_and_process_details() -> None:
    inventory = json.loads(
        (REPO_ROOT / "proto/examples/v1/valid/inventory.llamaserver.json").read_text()
    )["body"]
    heartbeat = json.loads((REPO_ROOT / "proto/examples/v1/valid/heartbeat.igpu.json").read_text())[
        "body"
    ]
    heartbeat["loaded"] = inventory["loaded"]
    node = Node(id=uuid.uuid4(), name="node-laptop", os="windows", arch="amd64", state="available")
    inv = NodeInventory(node_id=node.id, inventory=inventory, status=heartbeat)
    summary = node_summary(node, inv, connected=True)

    assert summary["cpu"]["performance_cores"] == 2
    assert summary["runner"]["backend"] == "vulkan"
    assert summary["gpus"] == [
        {
            "index": 0,
            "name": "Intel(R) Iris(R) Xe Graphics",
            "vendor": "intel",
            "vram_total_mb": 128,
            "integrated": True,
            "shared_total_mb": 16305,
            "vram_used_mb": 90,
            "shared_used_mb": 1500,
        }
    ]
    assert summary["models"] == [
        {
            "name": "qwen2.5-1.5b-instruct-q4_k_m",
            "size_bytes": 1117320736,
            "format": "gguf",
            "loaded": True,
            "vram_used_mb": 1400,
            "ram_used_mb": 350,
            "backend": "vulkan",
            "port": 41001,
            "context_length": 4096,
        }
    ]
