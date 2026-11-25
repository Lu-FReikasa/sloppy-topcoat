#!/usr/bin/env python3
"""Run the model probes (tool call, embedding) for one model on one or more nodes and print what they proved.

    live_probes.py MODEL NODE[,NODE...]

Runs on the hub host like the other live scripts (same environment variables as ``live_dev.py``). Background
work of the hub is paused meanwhile, and every node is left as it was found (the model is unloaded again).
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live_dev  # noqa: E402  (its api() reads the token and talks to the hub)


def api(method: str, path: str, body: object | None = None) -> object:
    raw = live_dev.api(method, path, body)
    return json.loads(raw) if raw else None


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    model, nodes = argv[1], argv[2].split(",")
    api("POST", "/api/scheduling/pause", {"minutes": 20})
    try:
        for node in nodes:
            try:
                out = api("POST", "/api/scheduling/model-probes", {"model": model, "node": node})
                print(node, json.dumps(out["results"], sort_keys=True))  # type: ignore[index]
            except Exception as exc:  # one node failing must not hide the other
                print(node, "FAILED", exc)
        for pool in api("GET", "/api/scheduling/pools")["nodes"]:  # type: ignore[index]
            if pool["node"] in nodes:
                for m in pool["models"]:
                    if m["name"] == model:
                        print(pool["node"], "kinds", m["kinds"], m["kinds_source"], m.get("capabilities"))
    finally:
        known = {n["name"]: n["id"] for n in api("GET", "/api/nodes")}  # type: ignore[union-attr]
        for node in nodes:
            if node in known:
                try:
                    api("POST", f"/api/nodes/{known[node]}/actions", {"name": "models.unload", "args": {"model": model}})
                except Exception:
                    pass
        api("POST", "/api/scheduling/pause", {"minutes": 0})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
