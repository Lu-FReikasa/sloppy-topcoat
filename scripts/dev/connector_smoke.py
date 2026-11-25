#!/usr/bin/env python3
"""Call every read-only capability of the given families once, on every node that offers it, and
print what came back. Run it on the hub host after a node update and before a tool counts as done:
a capability that never ran against the real service fails here, not in a person's diagnosis
(2026-11-17: omada.events had answered HTTP 400 on every call since it was written).

    connector_smoke.py                        omada., kuma., keycloak. on every node
    connector_smoke.py omada. logs.           other families (name prefixes)
    connector_smoke.py --all                  every read-only capability that needs no input
    connector_smoke.py --node tools-node

Only capabilities that run without a click (read-only) are called. A capability with required
input is called only when SAMPLES below has an input for it; otherwise it is listed as skipped.
Token and URL as in hubapi.py (hub.env on the host, loopback address).
"""

import argparse
import json
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_dev  # noqa: E402

DEFAULT_FAMILIES = ("omada.", "kuma.", "keycloak.")

# inputs for capabilities that need some, and narrower inputs where the empty one is the wrong test
SAMPLES: dict[str, dict] = {
    "omada.events": {"since": "1d", "limit": 5},
    "omada.alerts": {"since": "7d", "limit": 5},
    "omada.audit_logs": {"since": "7d", "limit": 5},
    "omada.clients": {"limit": 5},
    "logs.search": {"pattern": "error", "since": "1h", "max_matches": 5},
}
# inputs a schema does not mark as required but the capability cannot do without
SKIP = {"omada.firmware": "needs a device MAC", "keycloak.user": "needs a user name"}


def api(method: str, path: str, body: object | None = None, timeout: int = 120) -> dict:
    return json.loads(live_dev.api(method, path, body, timeout) or b"{}")


def needs_input(info: dict) -> bool:
    schema = info.get("input_schema") or info.get("schema") or {}
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except ValueError:
            return False
    return bool(schema.get("required"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("families", nargs="*", help="capability name prefixes (default: omada. kuma. keycloak.)")
    ap.add_argument("--all", action="store_true", help="every read-only capability")
    ap.add_argument("--node", action="append", help="only this node (name); repeatable")
    args = ap.parse_args()
    families = tuple(args.families) or DEFAULT_FAMILIES

    failed = 0
    for node in api("GET", "/api/nodes"):
        if args.node and node["name"] not in args.node:
            continue
        if node.get("status") not in (None, "online"):
            print(f"{node['name']}: {node.get('status')}, skipped")
            continue
        caps = api("GET", f"/api/nodes/{node['id']}/capabilities")
        offered = {c["name"]: c for c in caps.get("offered", [])}
        names = [n for n in sorted(caps.get("usable", [])) if args.all or n.startswith(families)]
        for name in names:
            sample = SAMPLES.get(name)
            if name in SKIP:
                print(f"  skip {node['name']:<18} {name:<22} {SKIP[name]}")
                continue
            if sample is None and needs_input(offered.get(name, {})):
                print(f"  skip {node['name']:<18} {name:<22} needs input (add it to SAMPLES)")
                continue
            body = sample or {}
            t0 = time.monotonic()
            try:
                res = api("POST", f"/api/nodes/{node['id']}/capabilities/{name}", {"input": body})
            except urllib.error.HTTPError as e:
                res = {"ok": False, "error": {"code": f"http_{e.code}", "message": e.read().decode()[:200]}}
            ms = int((time.monotonic() - t0) * 1000)
            if res.get("ok"):
                out = res.get("output")
                size = len(json.dumps(out))
                extra = ""
                if isinstance(out, dict):
                    extra = " ".join(f"{k}={out[k]}" for k in ("total", "shown", "matched") if k in out)
                print(f"  ok   {node['name']:<18} {name:<22} {ms:>5} ms {size:>7} B  {extra}")
            else:
                failed += 1
                err = res.get("error") or {}
                print(f"  FAIL {node['name']:<18} {name:<22} {ms:>5} ms  {err.get('code')}: {err.get('message')}")
    print("all ok" if not failed else f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
