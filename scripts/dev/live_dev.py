#!/usr/bin/env python3
"""Live test of the Development application with one model, on the hub host.

    live_dev.py MODEL NODE[,NODE] [MAX_MINUTES]

The counterpart of ``live_research.py`` and it takes the same parameters, so that one model on one node is judged
for both applications (``live_suite.py`` runs the two). What it does: opens a chat of the application ``dev`` pinned to
MODEL@NODE (the table of applications is not touched), titled ``TEST dev live``, asks one read-only question that needs
the repository tools, waits for the answer and prints the numbers. It changes nothing in a repository: the question only
asks to read. The chat stays (title starts with TEST).

Judged (``live_judge.judge_dev``): the answer is ``complete``, came from MODEL on one of NODE, names the version in the
file VERSION, does not say it was cut off, and the agent task behind it is ``done`` without a stop reason and called at
least one tool. Exit code 0 when all of that holds. Same environment variables as ``live_research.py``.
"""

import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_judge  # noqa: E402

ENV = Path(os.environ.get("TOPCOAT_ENV_FILE", "~/topcoat/deploy/hub.env")).expanduser()
URL = os.environ.get("TOPCOAT_HUB_URL", "https://127.0.0.1:8443")
LIVE = Path(os.environ.get("TOPCOAT_LIVE_DIR", "~/topcoat/livetest")).expanduser()
QUESTION = (
    "Look into the repository sloppy-topcoat (read only, change nothing) and tell me in three short points: the main "
    "language of the project, the content of the file VERSION, and what the newest commit changed."
)
CTX = ssl.create_default_context()
CTX.check_hostname, CTX.verify_mode = False, ssl.CERT_NONE


def _token() -> str:
    for line in ENV.read_text().splitlines():
        if line.startswith("TOPCOAT_DEV_ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("TOPCOAT_DEV_ADMIN_TOKEN not found in the env file")


def api(method: str, path: str, body: object | None = None, timeout: int = 120) -> bytes:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:  # noqa: S310
        return r.read()


def judge(answer: dict, task: dict | None, model: str, nodes: list[str]) -> list[str]:
    return live_judge.judge_dev(answer, task, model, nodes)


def task_of(pin: str, since: str) -> dict | None:
    """The agent task that answered the chat. A chat check carries no conversation id, so it is found
    by what the test pinned: origin ``chat``, the model ``MODEL@NODE``, started after the test began (newest first)."""
    tasks = json.loads(api("GET", "/api/agent/tasks?include_chat=true&limit=100"))
    return next((t for t in tasks if t.get("origin") == "chat" and t.get("model") == pin and t["created_at"] >= since), None)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    model, nodes = argv[1], argv[2].split(",")
    minutes = int(argv[3]) if len(argv) > 3 else 15
    LIVE.mkdir(parents=True, exist_ok=True)
    code = 1
    # the chat is pinned to MODEL@NODE: the table is not touched, so nodes can be tested side by side
    started = time.time()
    since = datetime.fromtimestamp(started - 5, timezone.utc).isoformat()
    body = {"title": f"TEST dev live {nodes[0]}", "application": "dev", "model": f"{model}@{nodes[0]}"}
    conv = json.loads(api("POST", "/api/conversations", body))
    raw = api(
        "POST",
        f"/api/conversations/{conv['id']}/messages",
        {"content": QUESTION, "tools": "auto"},
        timeout=minutes * 60,
    )
    events = [line[7:] for line in raw.decode("utf-8", "replace").splitlines() if line.startswith("event: ")]
    detail = json.loads(api("GET", f"/api/conversations/{conv['id']}"))
    answer = next((m for m in reversed(detail.get("messages", [])) if m.get("role") == "assistant"), {})
    task = task_of(f"{model}@{nodes[0]}", since)
    problems = judge(answer, task, model, nodes)
    print(
        json.dumps(
            {
                "conversation": conv["id"],
                "seconds": int(time.time() - started),
                "events": sorted(set(events)),
                "tool_events": sum(1 for e in events if e.startswith("tool")),
            "tool_calls": task.get("tool_calls") if task else None,
            "stop_reason": task.get("stop_reason") if task else None,
                "status": answer.get("status"),
                "model": answer.get("model"),
                "node": answer.get("node"),
                "completion_tokens": answer.get("completion_tokens"),
                "text": (answer.get("content") or "")[:300],
                "problems": problems,
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    code = 0 if not problems else 1
    print("dev live test:", "PASS" if code == 0 else "FAIL")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
