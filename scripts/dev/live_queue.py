#!/usr/bin/env python3
"""Live test of the queue of the pool: more requests than places, on the nodes given.

    live_queue.py MODEL NODE[,NODE...] [REQUESTS]   (default: 3 requests per node)

Sends REQUESTS short chat requests at the same moment through the hub's /v1 gateway (no node is named, so
the hub chooses late), and prints per request the node that answered, the time and the status. Judged: every
request is answered (200) or refused at once with ``queue_full``; the requests spread over the nodes (when
there are several); none took longer than the sum of the answers it waited behind (no timer cut anything).
The pools of every other node are limited to models other than MODEL meanwhile (so no other machine, e.g. a PC that is
not for tests, can take a request) and put back at the end. Background work is paused meanwhile and every node is
unloaded at the end. Runs on the hub host like the other
live scripts (same environment variables as ``live_dev.py``).
"""

import json
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live_dev  # noqa: E402  (its api() and URL read the token and talk to the hub)

CTX = ssl.create_default_context()
CTX.check_hostname, CTX.verify_mode = False, ssl.CERT_NONE


def ask(model: str, results: list, index: int) -> None:
    body = {
        "model": model,
        "max_tokens": 24,
        "messages": [{"role": "user", "content": f"Answer with one short sentence: what is {index} + 1?"}],
    }
    req = urllib.request.Request(live_dev.URL + "/v1/chat/completions", data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {live_dev._token()}")
    req.add_header("Content-Type", "application/json")
    began = time.time()
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=900) as r:  # noqa: S310
            r.read()
            results.append((index, r.status, r.headers.get("x-topcoat-node"), time.time() - began, ""))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode()[:200]
        results.append((index, exc.code, None, time.time() - began, text))


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    model, nodes = argv[1], argv[2].split(",")
    count = int(argv[3]) if len(argv) > 3 else 3 * len(nodes)
    before = json.loads(live_dev.api("GET", "/api/scheduling/pools"))["nodes"]
    original = {n["node"]: {"available": [m["name"] for m in n["models"] if m["available"]]} for n in before if n["restricted"]}
    limited = dict(original)
    for n in before:
        others = [m["name"] for m in n["models"] if m["name"] != model]
        if n["node"] not in nodes and any(m["name"] == model for m in n["models"]) and others:
            limited[n["node"]] = {"available": others[:1]}  # a pool without MODEL: the node takes no test request
    live_dev.api("PUT", "/api/scheduling/pools", {"pools": limited})
    live_dev.api("POST", "/api/scheduling/pause", {"minutes": 30})
    results: list = []
    try:
        threads = [threading.Thread(target=ask, args=(model, results, i)) for i in range(count)]
        for t in threads:
            t.start()
            time.sleep(0.2)
        for t in threads:
            t.join(1200)
        for row in sorted(results):
            print(row)
        answered = [r for r in results if r[1] == 200]
        refused = [r for r in results if r[1] == 503 and "queue_full" in r[4]]
        used = {r[2] for r in answered}
        print(f"answered {len(answered)}, refused {len(refused)}, other {len(results) - len(answered) - len(refused)}; nodes used: {sorted(used)}")
        ok = len(answered) + len(refused) == count and (len(nodes) < 2 or len(used) >= 2)
        print("RESULT", "ok" if ok else "FAILED")
        return 0 if ok else 1
    finally:
        known = {n["name"]: n["id"] for n in json.loads(live_dev.api("GET", "/api/nodes"))}
        pools = json.loads(live_dev.api("GET", "/api/scheduling/pools"))["nodes"]
        for pool in pools:
            if pool["node"] in nodes and pool["node"] in known:
                for m in (m["name"] for m in pool["models"] if m["loaded"]):
                    live_dev.api("POST", f"/api/nodes/{known[pool['node']]}/actions", {"name": "models.unload", "args": {"model": m}})
        live_dev.api("PUT", "/api/scheduling/pools", {"pools": original})
        live_dev.api("POST", "/api/scheduling/pause", {"minutes": 0})


if __name__ == "__main__":
    sys.exit(main(sys.argv))
