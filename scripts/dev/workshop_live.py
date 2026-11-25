#!/usr/bin/env python3
"""Move the workshop's clone onto the live commit, through Topcoat's own steps (run on the hub host).

After push-live-to-devnode.sh put the hub's commit into the local origin of the dev node, the clone
the assistant reads still sits where it was. This runs ``repo.sync`` (fetch the default branch) and
``repo.start`` with the topic ``live`` (``topcoat/live`` = ``origin/<default>``), then prints the
head. ``repo.start`` refuses when the worktree has unsaved changes, so no work is thrown away.

    ssh <hub host> "TOPCOAT_ENV_FILE=\\$HOME/topcoat/deploy/hub.env python3 -" < scripts/dev/workshop_live.py

Environment: TOPCOAT_ENV_FILE (hub.env with TOPCOAT_DEV_ADMIN_TOKEN), TOPCOAT_HUB_URL (default
https://127.0.0.1:8443), TOPCOAT_LIVE_REPO (default sloppy-topcoat-local), TOPCOAT_DEVNODE_NAME
(default: the one connected node that offers repo.start). The token goes into one request header,
never onto a command line or into the output.
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HUB = os.environ.get("TOPCOAT_HUB_URL", "https://127.0.0.1:8443")
REPO = os.environ.get("TOPCOAT_LIVE_REPO", "sloppy-topcoat-local")


def token() -> str:
    env = Path(os.environ.get("TOPCOAT_ENV_FILE", Path.home() / "topcoat" / "deploy" / "hub.env"))
    for line in env.read_text().splitlines():
        if line.startswith("TOPCOAT_DEV_ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("TOPCOAT_DEV_ADMIN_TOKEN not found in the env file")


def call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(HUB + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False  # loopback of the hub host, the hub's own CA
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=300) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path}: HTTP {e.code} {e.read()[:300]!r}")


def step(node_id: str, capability: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    res = call("POST", f"/api/nodes/{node_id}/capabilities/{capability}", {"input": {"repo": REPO, **(extra or {})}})
    if not res.get("ok"):
        sys.exit(f"{capability} failed: {json.dumps(res.get('error'))}")
    return res["output"]


def dev_nodes() -> list[dict[str, Any]]:
    nodes = call("GET", "/api/nodes")
    nodes = nodes.get("nodes", nodes) if isinstance(nodes, dict) else nodes
    wanted = os.environ.get("TOPCOAT_DEVNODE_NAME")
    return [
        n
        for n in nodes
        if n.get("connected") and "repo.start" in (n.get("capabilities") or []) and (not wanted or n["name"] == wanted)
    ]


def main() -> int:
    # right after a deploy the hub has just restarted and the nodes are still connecting again
    deadline = time.monotonic() + float(os.environ.get("TOPCOAT_DEVNODE_WAIT", "90"))
    dev = dev_nodes()
    while len(dev) != 1 and time.monotonic() < deadline:
        time.sleep(5)
        dev = dev_nodes()
    if len(dev) != 1:
        sys.exit(f"expected one dev node with repo.start, found {[n['name'] for n in dev]}")
    node_id = dev[0]["id"]
    step(node_id, "repo.sync")
    step(node_id, "repo.start", {"topic": "live"})
    status = step(node_id, "repo.status")
    print(f"workshop {REPO}: {status.get('branch')} at {status.get('head')} (clean={status.get('clean')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
