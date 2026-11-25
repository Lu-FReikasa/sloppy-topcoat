#!/usr/bin/env python3
"""What fits completely into a node's VRAM, and how fast it is (run on the hub host).

    vram_fit.py NODE MODEL[,MODEL...] [--ctx 8192,16384,32768] [--slots 1,2] [--out results/vram_<node>.json]

With ``--slots`` each model is also loaded with that many llama-server slots (``n_parallel``; the context is per slot,
the node starts the server with ctx x slots) and, besides the speed of one request, N requests run at the same time:
``agg_tps`` is what the card makes in total, ``per_req_tps`` what each of them gets.

For every model and context length: unload the node, load the model with that context (``models.load``), read what the node
reports (VRAM and RAM of the model's process, the card's free VRAM), then measure through the hub's /v1 route pinned to
MODEL@NODE: the prefill speed (a prompt of ~3000 tokens, one token answer) and the generation speed (200 tokens). A model
"fits" when its process holds at least 90 % of the file in VRAM (all layers on the card) and the card keeps 300 MB free;
otherwise layers run on the CPU and generation drops. Unloads the node at the end. Token from hub.env, never printed.
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ENV = Path(os.environ.get("TOPCOAT_ENV_FILE", "~/topcoat/deploy/hub.env")).expanduser()
URL = os.environ.get("TOPCOAT_HUB_URL", "https://127.0.0.1:8443")
OUT = Path(os.environ.get("TOPCOAT_YARD_DIR", "~/topcoat/livetest/results")).expanduser()
CTX = ssl.create_default_context()
CTX.check_hostname, CTX.verify_mode = False, ssl.CERT_NONE
FILLER = "Der Hub verteilt Aufgaben an Nodes, die Modelle laden und Werkzeuge ausführen. " * 230  # ~3000 tokens


def _token() -> str:
    for line in ENV.read_text().splitlines():
        if line.startswith("TOPCOAT_DEV_ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("TOPCOAT_DEV_ADMIN_TOKEN not found")


def api(method: str, path: str, body: object | None = None, timeout: int = 600) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for attempt in range(3):  # a long load can drop the TLS connection: ask again
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:  # noqa: S310
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return {"http_error": e.code, "body": e.read().decode("utf-8", "replace")[:300]}
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            if attempt == 2:
                return {"error": str(e)[:200]}
            time.sleep(5)
    return {}


def node_of(name: str) -> dict:
    nodes = api("GET", "/api/nodes")
    nodes = nodes if isinstance(nodes, list) else nodes.get("nodes", [])
    return next(n for n in nodes if n["name"] == name)


def action(node_id: str, name: str, args: dict, timeout: int = 600) -> dict:
    return api("POST", f"/api/nodes/{node_id}/actions", {"name": name, "args": args}, timeout)


def ask(pin: str, prompt: str, max_tokens: int) -> tuple[float, int, int]:
    t0 = time.time()
    r = api("POST", "/v1/chat/completions", {
        "model": pin, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
        "temperature": 0.7, "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }, timeout=600)
    usage = r.get("usage") or {}
    return time.time() - t0, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


STORY = "Erzähle eine kurze Geschichte über eine Büroklammer, etwa 200 Wörter."


def side_by_side(pin: str, n: int) -> tuple[float | None, float | None]:
    """N generations of 200 tokens at the same time: tokens per second in total and per request."""
    with ThreadPoolExecutor(max_workers=n) as pool:
        t0 = time.time()
        results = list(pool.map(lambda _: ask(pin, STORY, 200), range(n)))
        wall = time.time() - t0
    total = sum(ctok for _, _, ctok in results)
    per = [ctok / secs for secs, _, ctok in results if secs and ctok]
    return (round(total / wall, 1) if wall and total else None, round(sum(per) / len(per), 1) if per else None)


def measure(node: str, model: str, ctx: int, slots: int = 1) -> dict:
    n = node_of(node)
    size = next((m.get("size_bytes") for m in n.get("models", []) if m["name"] == model), None)
    action(n["id"], "models.unload", {"all": True}, 120)
    load = {"model": model, "context_length": ctx, **({"n_parallel": slots} if slots > 1 else {})}
    t0 = time.time()
    res = action(n["id"], "models.load", load, 900)
    load_s = round(time.time() - t0, 1)
    row: dict = {"node": node, "model": model, "ctx": ctx, "slots": slots, "size_mb": round((size or 0) / 2**20),
                 "load_s": load_s}
    if not res.get("ok"):
        row["error"] = json.dumps(res.get("error") or res)[:200]
        return row
    n = node_of(node)
    others = [m["name"] for m in n.get("loaded", []) if m["name"] != model]
    if others:  # only the measured model may be on the card, or the numbers say nothing
        for other in others:
            action(n["id"], "models.unload", {"model": other}, 120)
        row["others_unloaded"] = others
        action(n["id"], "models.unload", {"all": True}, 120)
        action(n["id"], "models.load", load, 900)
        n = node_of(node)
    loaded = next((m for m in n.get("loaded", []) if m["name"] == model), {})
    gpu = (n.get("gpus") or [{}])[0]
    row.update({
        "vram_mb": loaded.get("vram_used_mb"), "ram_mb": loaded.get("ram_used_mb"),
        "card_free_mb": (gpu.get("vram_total_mb") or 0) - (gpu.get("vram_used_mb") or 0),
        "ctx_loaded": loaded.get("context_length"),
    })
    row["fits"] = bool(row["vram_mb"] and size and row["vram_mb"] * 2**20 >= 0.9 * size and row["card_free_mb"] >= 300)
    pin = f"{model}@{node}"
    ask(pin, "Sag Hallo.", 8)  # warm up
    secs, ptok, _ = ask(pin, FILLER + "\nWie viele Wörter hat der erste Satz? Nur die Zahl.", 1)
    row["prefill_tps"] = round(ptok / secs, 1) if secs and ptok else None
    secs, _, ctok = ask(pin, STORY, 200)
    row["gen_tps"] = round(ctok / secs, 1) if secs and ctok else None
    if slots > 1:
        row["agg_tps"], row["per_req_tps"] = side_by_side(pin, slots)
    after = [m["name"] for m in node_of(node).get("loaded", []) if m["name"] != model]
    if after:
        row["others_during"] = after  # something else came while measuring: the speed is not clean
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("node")
    ap.add_argument("models")
    ap.add_argument("--ctx", default="8192,16384,32768")
    ap.add_argument("--slots", default="1", help="llama-server slots to try, e.g. 1,2,3")
    args = ap.parse_args()
    out = OUT / f"vram_{args.node}.json"
    rows = json.loads(out.read_text()) if out.exists() else []
    models = args.models.split(",")
    # the gateway serves only models of the node's pool: add the test models for the run, put it back after
    # PUT replaces every pool: send all restricted nodes, or the others lose their limits (2026-11-11)
    pools = api("GET", "/api/scheduling/pools")["nodes"]
    original = {p["node"]: {"available": [m["name"] for m in p["models"] if m["available"]]} for p in pools if p["restricted"]}
    if args.node not in original:
        sys.exit(f"{args.node} has no pool of its own: every model is available there, nothing to add")
    test = {k: {"available": list(v["available"])} for k, v in original.items()}
    # while measuring, the node's pool holds the measured models only: nothing else is loaded there
    test[args.node]["available"] = sorted(set(models))
    api("PUT", "/api/scheduling/pools", {"pools": test})
    try:
        for model, slots in [(m, int(s)) for m in models for s in args.slots.split(",")]:
            first_gen = None
            for ctx in [int(c) for c in args.ctx.split(",")]:
                row = measure(args.node, model, ctx, slots)
                # layers moved to the CPU keep the VRAM full but cut the speed: that is no fit either
                first_gen = first_gen or row.get("gen_tps")
                if row.get("fits") and first_gen and (row.get("gen_tps") or 0) < 0.7 * first_gen:
                    row["fits"] = False
                    row["note"] = "slower than at the smallest context: layers run on the CPU"
                same = (model, ctx, slots)
                rows = [r for r in rows if (r["model"], r["ctx"], r.get("slots", 1)) != same] + [row]
                OUT.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(rows, indent=1))
                print(json.dumps(row), flush=True)
                if not row.get("fits") and "error" not in row:
                    break  # a bigger context will not fit either
    finally:
        api("PUT", "/api/scheduling/pools", {"pools": original})
        action(node_of(args.node)["id"], "models.unload", {"all": True}, 120)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
