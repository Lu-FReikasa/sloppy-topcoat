#!/usr/bin/env python3
"""Run the shipped bench suites for one model on one or more nodes at the same time and print the ratings.

    live_bench.py MODEL NODE[,NODE...] [MAX_MINUTES] [--suites key,key]  (default: every suite)

Runs on the hub host like the other live scripts (same environment variables as ``live_dev.py``). Background
work of the hub is paused for the run (its rows would load other models next to MODEL) and the pause is
lifted at the end, also after Ctrl-C; a run that is still going when the time is up is stopped.
"""

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live_dev  # noqa: E402  (its api() reads the token and talks to the hub)


def api(method: str, path: str, body: object | None = None) -> object:
    raw = live_dev.api(method, path, body)
    return json.loads(raw) if raw else None


def unload_all(nodes: list[str]) -> None:
    """Leave every node as it was found: a bench must not keep a big model in someone's GPU."""
    known = {n["name"]: n["id"] for n in api("GET", "/api/nodes")}
    for pool in api("GET", "/api/scheduling/pools")["nodes"]:
        if pool["node"] in nodes and pool["node"] in known:
            for model in (m["name"] for m in pool["models"] if m["loaded"]):
                api("POST", f"/api/nodes/{known[pool['node']]}/actions", {"name": "models.unload", "args": {"model": model}})


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        return 2
    model, nodes = args[0], args[1].split(",")
    minutes = int(args[2]) if len(args) > 2 else 30
    suites = [s["key"] for s in api("GET", "/api/bench/suites")]  # type: ignore[union-attr]
    if "--suites" in argv:
        suites = argv[argv.index("--suites") + 1].split(",")
    api("POST", "/api/scheduling/pause", {"minutes": minutes + 5})
    runs: dict[str, str] = {}
    try:
        for node in nodes:
            started = api("POST", "/api/bench/runs", {"model": model, "node": node, "suites": suites})
            runs[node] = started["run_id"]  # type: ignore[index]
        end = time.time() + minutes * 60
        views: dict[str, dict] = {}
        while time.time() < end:
            views = {n: api("GET", f"/api/bench/runs/{rid}") for n, rid in runs.items()}  # type: ignore[misc]
            if all(v["status"] != "running" for v in views.values()):
                break
            time.sleep(10)
        for node, rid in runs.items():
            if views.get(node, {}).get("status") == "running":
                api("DELETE", f"/api/bench/runs/{rid}")
        views = {n: api("GET", f"/api/bench/runs/{rid}") for n, rid in runs.items()}  # type: ignore[misc]
    finally:
        api("POST", "/api/scheduling/pause", {"minutes": 0})
        unload_all(nodes)
    ok = True
    for node, view in views.items():
        print(f"{node}: {view['status']}" + (f" ({view['error']})" if view["error"] else ""))
        for r in view["results"]:
            fails = ", ".join(f["case"] for f in r["failed"]) or "-"
            print(f"  {r['suite']:9} {r['passed']}/{r['total']}  {r['tps']} tok/s  failed: {fails}")
        ok = ok and view["status"] == "done"
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
