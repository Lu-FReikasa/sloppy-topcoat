#!/usr/bin/env python3
"""A block program with agent hooks, run live on one node (PRG-00 R5).

    live_hooks.py MODEL NODE [MAX_MINUTES]

Starts a quick Research agent block pinned to MODEL@NODE with ``hooks`` that switch the repeat guard to 2
and the step check off, waits for the run and prints how it ended. Runs on the hub host like the other
live scripts. Background work of the hub is paused for the run and the pause is lifted at the end.
"""

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live_dev  # noqa: E402  (its api() reads the token and talks to the hub)


def api(method: str, path: str, body: object | None = None) -> dict:
    raw = live_dev.api(method, path, body)
    return json.loads(raw) if raw else {}


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    model, node = argv[1], argv[2]
    minutes = int(argv[3]) if len(argv) > 3 else 15
    program = {
        "name": "live_hooks",
        "inputs": {"question": "text"},
        "stack": [
            {
                "b": "agent",
                "app": "research",
                "depth": "quick",
                "prompt": "{{question}}",
                "pick": {"fixed": f"{model}@{node}"},
                "hooks": {"repeat_guard": 2, "step_check": False, "claims": True},
                "out": "draft",
            }
        ],
    }
    api("POST", "/api/scheduling/pause", {"minutes": minutes + 5})
    try:
        started = api(
            "POST",
            "/api/blocks/runs",
            {"program": program, "inputs": {"question": "What does the command ls do on Linux? Two sentences."}},
        )
        end = time.time() + minutes * 60
        view = started
        while time.time() < end:
            view = api("GET", f"/api/blocks/runs/{started['id']}")
            if view["status"] != "running":
                break
            time.sleep(5)
        else:
            api("POST", f"/api/blocks/runs/{started['id']}/stop")
    finally:
        api("POST", "/api/scheduling/pause", {"minutes": 0})
    draft = str(view.get("variables", {}).get("draft", ""))
    print(f"status={view['status']} error={view.get('error')} draft_chars={len(draft)}")
    print(draft[:300].replace("\n", " "))
    return 0 if view["status"] == "ok" and len(draft) > 100 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
