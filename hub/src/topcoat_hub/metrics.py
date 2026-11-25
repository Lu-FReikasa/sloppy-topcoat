"""Short history of what the nodes report in their heartbeats (CPU and GPU load, RAM, VRAM), kept in
memory for the sparklines of the nodes overview. Nothing is stored in the database: a hub restart
starts the curves anew, and a node that is not connected has no curve at all."""

import time
import uuid
from collections import deque
from typing import Any

WINDOW_SECONDS = 30 * 60
MAX_POINTS = 360  # 30 minutes at the usual 10 s heartbeat; more frequent beats are thinned out
MIN_GAP_SECONDS = 4.0


class NodeMetrics:
    def __init__(self, clock: Any = time.time, min_gap: float = MIN_GAP_SECONDS) -> None:
        self._clock = clock
        self._min_gap = min_gap
        self._series: dict[uuid.UUID, deque[dict[str, float | int | None]]] = {}
        # the exact time of the last kept point: the stored "t" is rounded (up to 0.05 s too late),
        # and comparing against it dropped a beat that came right after another even with no gap
        self._last: dict[uuid.UUID, float] = {}

    def record(self, node_id: uuid.UUID, heartbeat: dict[str, Any]) -> None:
        """Remember the numbers of one heartbeat (ignored when the last one is very recent)."""
        now = self._clock()
        points = self._series.setdefault(node_id, deque(maxlen=MAX_POINTS))
        if points and now - self._last.get(node_id, 0.0) < self._min_gap:
            return
        self._last[node_id] = now
        gpus = heartbeat.get("gpus") or []
        utils = [g["gpu_util"] for g in gpus if isinstance(g.get("gpu_util"), int | float)]
        vram = [g["vram_used_mb"] for g in gpus if isinstance(g.get("vram_used_mb"), int | float)]
        points.append(
            {
                "t": round(now, 1),
                "ram_used_mb": heartbeat.get("ram_used_mb"),
                "cpu_util": heartbeat.get("cpu_util"),
                "gpu_util": max(utils) if utils else None,
                "vram_used_mb": sum(vram) if vram else None,
            }
        )

    def forget(self, node_id: uuid.UUID) -> None:
        """A node that went offline shows no history (not even an old one)."""
        self._series.pop(node_id, None)
        self._last.pop(node_id, None)

    def points(self, node_id: uuid.UUID) -> list[dict[str, float | int | None]]:
        horizon = self._clock() - WINDOW_SECONDS
        return [p for p in self._series.get(node_id, ()) if float(p["t"] or 0) >= horizon]
