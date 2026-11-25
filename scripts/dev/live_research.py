#!/usr/bin/env python3
"""Live test of a thorough research with one model, on the hub host.

    live_research.py fresh  MODEL NODE[,NODE] [MAX_MINUTES]            a new report in the Ablage
    live_research.py revise MODEL NODE[,NODE] REPORT_ID [MAX_MINUTES]  revise (or re-research) a report

What it does: starts the task pinned to MODEL@NODE (the table is not touched; ``revise`` has no model field and still
sets the Research row to MODEL on NODE and puts it back), waits for it, prints the numbers and writes the report to ``<MODEL>.md`` in the live directory, and puts the
row back (also after an error). Nothing is pinned, no conversation is created. Unload the model afterwards
(``models.unload``), and keep node-amd out of NODE unless the maintainer said so.

The hub's admin token is read from its env file (``TOPCOAT_DEV_ADMIN_TOKEN``) and goes into one request header.
Environment: ``TOPCOAT_ENV_FILE`` (default ``~/topcoat/deploy/hub.env``), ``TOPCOAT_HUB_URL`` (default
``https://127.0.0.1:8443``, the hub's own CA is not verified for loopback), ``TOPCOAT_LIVE_DIR`` (default
``~/topcoat/livetest``, where the row backup and the reports go).
"""

import json
import os
import ssl
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_judge  # noqa: E402

ENV = Path(os.environ.get("TOPCOAT_ENV_FILE", "~/topcoat/deploy/hub.env")).expanduser()
URL = os.environ.get("TOPCOAT_HUB_URL", "https://127.0.0.1:8443")
LIVE = Path(os.environ.get("TOPCOAT_LIVE_DIR", "~/topcoat/livetest")).expanduser()
QUESTION = (
    "Please write a report on working with the ls command on Ubuntu. Which options do I have, how can I "
    "filter the output (grep etc.), and what else might be important."
)
CTX = ssl.create_default_context()
CTX.check_hostname, CTX.verify_mode = False, ssl.CERT_NONE


def _token() -> str:
    for line in ENV.read_text().splitlines():
        if line.startswith("TOPCOAT_DEV_ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("TOPCOAT_DEV_ADMIN_TOKEN not found in the env file")


def api(method: str, path: str, body: object | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, context=CTX, timeout=120) as r:  # noqa: S310
        raw = r.read()
    return json.loads(raw) if raw else None


def wait(task_id: str, minutes: int) -> dict:
    start = time.time()
    while time.time() - start < minutes * 60:
        task = api("GET", f"/api/agent/tasks/{task_id}")
        if task["status"] not in ("queued", "running"):
            return task
        time.sleep(20)
    api("POST", f"/api/agent/tasks/{task_id}/cancel")
    print("TIMEOUT: cancelled", flush=True)
    return api("GET", f"/api/agent/tasks/{task_id}")


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[1] not in ("fresh", "revise") or (argv[1] == "revise" and len(argv) < 5):
        print(__doc__)
        return 2
    mode, model, nodes = argv[1], argv[2], argv[3]
    report_id = argv[4] if mode == "revise" else None
    minutes = int(argv[5 if mode == "revise" else 4]) if len(argv) > (5 if mode == "revise" else 4) else 25
    LIVE.mkdir(parents=True, exist_ok=True)
    rows = api("GET", "/api/scheduling/applications")["rows"]
    backup = LIVE / "research_row_backup.json"
    if not backup.exists():  # never overwrite the first backup with a test row
        backup.write_text(json.dumps(rows["research"]))
    original = json.loads(backup.read_text())
    code = 0 if mode == "revise" else 1
    try:
        # fresh: the task is pinned to MODEL@NODE, the table is not touched, so several nodes can be tested at the
        # same time and a killed script leaves nothing behind. A revision has no model field: it needs the row.
        if mode == "revise":
            rows["research"] = {**original, "models": [model], "nodes": nodes.split(",")}
            api("PUT", "/api/scheduling/applications", {"rows": rows})
        started = time.time()
        if mode == "fresh":
            task_id = api(
                "POST",
                "/api/agent/tasks",
                {
                    "prompt": QUESTION,
                    "application": "research",
                    "depth": "thorough",
                    "model": f"{model}@{nodes.split(',')[0]}",
                },
            )["id"]
        else:
            task_id = api("POST", f"/api/reports/{report_id}/revise", {"note": "live test"})["task_id"]
        print("task", task_id, flush=True)
        task = wait(task_id, minutes)
        print(
            f"state={task['status']} stop={task.get('stop_reason')} steps={task.get('step_count')} "
            f"calls={task.get('tool_calls')} seconds={int(time.time() - started)} model={model}@{nodes}"
        )
        time.sleep(3)
        found = api("GET", f"/api/reports?task_id={task_id}")["reports"]
        rid = found[0]["id"] if found else report_id
        if rid:
            shown = api("GET", f"/api/reports/{rid}").get("shown") or {}
            body, meta = shown.get("body") or "", shown.get("meta") or {}
            out = LIVE / f"{mode}_{model}.md"
            out.write_text(body)
            print("report", rid, "chars", len(body), "->", out)
            if mode == "fresh":
                problems = live_judge.judge_research(task, meta, body)
                shown_meta = {k: meta.get(k) for k in ("chapters", "searches", "pages", "sources", "hosts", "claims_total", "claims_backed")}
                print("numbers", json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in shown_meta.items()}))
                for p in problems:
                    print("problem:", p)
                code = 0 if not problems else 1
                print("research live test:", "PASS" if code == 0 else "FAIL")
        else:
            print("no report filed")
    finally:
        if mode == "revise":
            rows["research"] = original
            api("PUT", "/api/scheduling/applications", {"rows": rows})
            print("research row restored")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
