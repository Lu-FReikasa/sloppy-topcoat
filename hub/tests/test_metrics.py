"""The short history behind the sparklines of the nodes overview."""

import uuid

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import eventually, online_node
from topcoat_hub.metrics import MIN_GAP_SECONDS, WINDOW_SECONDS, NodeMetrics


def test_a_connected_node_has_a_history_and_an_offline_node_none(
    client: TestClient, connect
) -> None:
    assert client.get("/api/nodes/metrics").status_code == 401
    client.app.state.metrics = NodeMetrics(min_gap=0)  # keep every beat of this short test
    node = online_node(client, connect, "node-a", loaded=["small"])
    node.gpu_util = 37.5
    node.cpu_util = 12.5
    node.heartbeat()

    def has_point() -> None:
        nodes = client.get("/api/nodes/metrics", headers=AUTH).json()["nodes"]
        assert nodes
        (points,) = nodes.values()
        assert points[-1]["ram_used_mb"] == 1000
        assert points[-1]["gpu_util"] == 37.5
        assert points[-1]["cpu_util"] == 12.5
        (summary,) = [n for n in client.get("/api/nodes", headers=AUTH).json()]
        assert summary["cpu_util"] == 12.5

    eventually(has_point, timeout=10)
    node.close()

    def gone() -> None:
        assert client.get("/api/nodes/metrics", headers=AUTH).json()["nodes"] == {}

    eventually(gone)


def test_the_series_is_thinned_windowed_and_forgotten() -> None:
    now = [1000.0]
    metrics = NodeMetrics(clock=lambda: now[0])
    node = uuid.uuid4()
    beat = {"ram_used_mb": 2000, "gpus": [{"index": 0, "vram_used_mb": 300, "gpu_util": 12}]}
    metrics.record(node, beat)
    now[0] += MIN_GAP_SECONDS / 2
    metrics.record(node, beat)  # too soon: ignored
    assert len(metrics.points(node)) == 1
    assert metrics.points(node)[0]["gpu_util"] == 12
    now[0] += WINDOW_SECONDS + 10
    assert metrics.points(node) == []  # older than the window
    metrics.record(node, {"ram_used_mb": 1, "gpus": []})
    assert metrics.points(node)[-1]["gpu_util"] is None
    metrics.forget(node)
    assert metrics.points(node) == []


def test_two_beats_right_after_each_other_are_both_kept_without_a_gap() -> None:
    # the stored time is rounded to 0.1 s and may lie after "now": 100.06 is stored as 100.1
    now = [100.06]
    metrics = NodeMetrics(clock=lambda: now[0], min_gap=0)
    node = uuid.uuid4()
    metrics.record(node, {"ram_used_mb": 1, "gpus": []})
    now[0] = 100.08
    metrics.record(node, {"ram_used_mb": 2, "gpus": []})
    assert [p["ram_used_mb"] for p in metrics.points(node)] == [1, 2]
