#!/usr/bin/env python3
"""Rate many models with every bench suite and print one table: models x suites.

    live_bench_matrix.py PLAN.json [--suites key,key]

PLAN.json maps a node to the models it works through, one after the other; the nodes run at the same
time:  {"node-nvidia": ["Llama-3.2-3B-Instruct-Q4_K_M", ...], "node-amd": [...]}

Runs on the hub host like the other live scripts. Background work of the hub is paused for the whole
run and the pause is lifted at the end. A model that cannot be rated (not in the pool, a node that
goes away) is skipped and shown as such; the run goes on with the next one.
"""

import json
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live_dev  # noqa: E402  (its api() reads the token and talks to the hub)

URLERR = (OSError, ValueError)
POLL_SECONDS = 15
PER_MODEL_MINUTES = 40


def api(method: str, path: str, body: object | None = None):  # type: ignore[no-untyped-def]
    raw = live_dev.api(method, path, body)
    return json.loads(raw) if raw else None


def unload_all(nodes: list[str]) -> None:
    """Leave every node as it was found: a bench must not keep a big model in someone's GPU."""
    known = {n["name"]: n["id"] for n in api("GET", "/api/nodes")}
    for pool in api("GET", "/api/scheduling/pools")["nodes"]:
        if pool["node"] in nodes and pool["node"] in known:
            for model in (m["name"] for m in pool["models"] if m["loaded"]):
                api("POST", f"/api/nodes/{known[pool['node']]}/actions", {"name": "models.unload", "args": {"model": model}})


def run_node(node: str, models: list[str], suites: list[str], out: dict, variant: str = "") -> None:
    for model in models:
        key = (model, node)
        try:
            started = api("POST", "/api/bench/runs", {"model": model, "node": node, "suites": suites, "variant": variant})
        except Exception as exc:  # a refusal (not in the pool, node gone): note it, go on
            out[key] = {"status": f"skipped ({exc})", "results": []}
            continue
        end = time.time() + PER_MODEL_MINUTES * 60
        view = started
        while time.time() < end:
            time.sleep(POLL_SECONDS)
            view = api("GET", f"/api/bench/runs/{started['run_id']}")
            if view["status"] != "running":
                break
        else:
            api("DELETE", f"/api/bench/runs/{started['run_id']}")
            view = api("GET", f"/api/bench/runs/{started['run_id']}")
        out[key] = view
        print(f"{node} {model}: {view['status']} {view.get('error') or ''}", flush=True)


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")][:1]
    if not args:
        print(__doc__)
        return 2
    plan = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    suites = [s["key"] for s in api("GET", "/api/bench/suites")]
    if "--suites" in argv:
        suites = argv[argv.index("--suites") + 1].split(",")
    variant = argv[argv.index("--variant") + 1] if "--variant" in argv else ""
    api("POST", "/api/scheduling/pause", {"minutes": 60 * 5})
    out: dict = {}
    try:
        threads = [
            threading.Thread(target=run_node, args=(node, models, suites, out, variant))
            for node, models in plan.items()
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        api("POST", "/api/scheduling/pause", {"minutes": 0})
        unload_all(list(plan))
    print(f"variant: {variant or 'default'}")
    header = "model@node".ljust(64) + "".join(s[:9].rjust(10) for s in suites)
    print("\n" + header)
    for (model, node), view in sorted(out.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        scored = {r["suite"]: f"{r['passed']}/{r['total']}" for r in view.get("results", [])}
        cells = "".join(scored.get(s, "-").rjust(10) for s in suites)
        note = "" if view["status"] == "done" else f"  [{view['status']}]"
        print(f"{model}@{node}".ljust(64) + cells + note)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
