#!/usr/bin/env python3
"""Ask the live hub a question in a new chat titled TEST ... and say what came back.

Runs on the hub host next to hubapi.py (same token handling: read from hub.env, never printed).
It starts a conversation, sends one message, follows the answer until the stream ends and prints
the stored answer's model, node, status and the first characters. The chat stays (title starts
with TEST), nothing else is touched.

    live_chat_check.py "Wie viel Arbeitsspeicher hat node-amd?" [--model NAME@NODE] [--tools auto|on|off]
    live_chat_check.py "ja bitte" --conversation ID      (the next message of that conversation)
"""

import argparse
import json
import ssl
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hubapi  # noqa: E402  (the token reader lives there)

BASE = "https://127.0.0.1:8443"


def _ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def call(method: str, path: str, body: dict | None = None, timeout: int = 900) -> bytes:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode() if body is not None else None, method=method
    )
    req.add_header("Authorization", "Bearer " + hubapi._token())
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, context=_ctx(), timeout=timeout) as resp:
        return resp.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--model", default=None)
    ap.add_argument("--tools", default="off", choices=["auto", "on", "off"])
    ap.add_argument("--title", default="TEST night check")
    ap.add_argument("--application", default=None, help="workspace of the chat (default: chat, with its tool ceiling)")
    ap.add_argument("--conversation", default=None, help="go on in this conversation instead of a new one")
    args = ap.parse_args()
    t0 = time.time()
    body = {"title": args.title, "model": args.model, **({"application": args.application} if args.application else {})}
    cid = args.conversation or json.loads(call("POST", "/api/conversations", body))["id"]
    raw = call("POST", f"/api/conversations/{cid}/messages", {"content": args.question, "tools": args.tools})
    events = [line[7:] for line in raw.decode("utf-8", "replace").splitlines() if line.startswith("event: ")]
    detail = json.loads(call("GET", f"/api/conversations/{cid}"))
    msgs = detail.get("messages", [])
    answer = next((m for m in reversed(msgs) if m.get("role") == "assistant"), {})
    out = {
        "conversation": cid,
        "seconds": round(time.time() - t0, 1),
        "events": sorted(set(events)),
        "status": answer.get("status"),
        "model": answer.get("model"),
        "node": answer.get("node"),
        "first_token_ms": answer.get("first_token_ms"),
        "completion_tokens": answer.get("completion_tokens"),
        "text": (answer.get("content") or "")[:300],
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if answer.get("status") == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
