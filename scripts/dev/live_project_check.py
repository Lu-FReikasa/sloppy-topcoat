#!/usr/bin/env python3
"""Ask the project agent of the live hub for changes in the TEST project and say what it prepared.

Runs on the hub host next to hubapi.py. For each request it opens a new chat in the project
application (title TEST ...), sends the request, and then looks at BOTH tables that can hold what the
agent prepared: single changes (project_changes) and change sets (project_change_sets). Every pending
row of the test project is refused at the end of each request, so nothing stays open and the project
data is never changed. Only the TEST project is touched (its id is a required argument).

    live_project_check.py PROJECT_ID --model NAME@NODE [--case status|tree|two_drops|all]
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_chat_check as lc  # noqa: E402

CASES = {
    "status": "Setze die Aufgabe 'Test: Aufgabe A' auf in_review.",
    "tree": "Lege ein Thema 'Rollout' an mit drei Aufgaben: Staging, Freigabe, Produktion. Produktion hängt von Freigabe ab.",
    "two_drops": "Verwirf die Aufgaben 'Test: Aufgabe B' und 'Test: Aufgabe C'.",
}


def psql(sql: str) -> list[list[str]]:
    out = subprocess.run(
        ["docker", "exec", "-i", "topcoat-db-1", "psql", "-U", "topcoat", "topcoat", "-tA", "-F", "|", "-c", sql],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.split("|") for line in out.splitlines() if line.strip()]


def pending(project_id: str, since: str) -> tuple[list[list[str]], list[list[str]]]:
    """What is pending and was made after ``since`` (the start of this run): single changes have no
    project column, so the time is what keeps this to the rows the run itself caused."""
    changes = psql(
        f"select id, capability from project_changes where status='pending' and created_at >= '{since}'"
    )
    sets = psql(
        f"select id, (select count(*) from jsonb_array_elements(operations)) from project_change_sets "
        f"where status='pending' and project_id='{project_id}' and created_at >= '{since}'"
    )
    return changes, sets


def refuse_all(project_id: str, since: str) -> int:
    changes, sets = pending(project_id, since)
    for cid, _ in changes:
        lc.call("POST", f"/api/project/changes/{cid}/refuse", {})
    for sid, _ in sets:
        lc.call("POST", f"/api/project/changesets/{sid}/refuse", {})
    return len(changes) + len(sets)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_id")
    ap.add_argument("--model", required=True)
    ap.add_argument("--case", default="all", choices=[*CASES, "all"])
    args = ap.parse_args()
    names = list(CASES) if args.case == "all" else [args.case]
    since = psql("select now()")[0][0]
    before = psql("select (select count(*) from project_changes where status='pending'), "
                  "(select count(*) from project_change_sets where status='pending')")[0]
    if before != ["0", "0"]:
        sys.exit(f"something is pending already ({before}): decide it first, this run refuses only its own")
    results = []
    for name in names:
        t0 = time.time()
        conv = json.loads(
            lc.call(
                "POST",
                "/api/conversations",
                {
                    "title": f"TEST night project {name}",
                    "model": args.model,
                    "application": "project",
                    "tool_scope": {"nodes": ["project"], "groups": None, "internet": False},
                },
            )
        )
        text = f"Im Projekt 'TEST Wegwerf-Projekt (nur Tests)': {CASES[name]}"
        lc.call("POST", f"/api/conversations/{conv['id']}/messages", {"content": text, "tools": "on"})
        detail = json.loads(lc.call("GET", f"/api/conversations/{conv['id']}"))
        answer = next((m for m in reversed(detail.get("messages", [])) if m.get("role") == "assistant"), {})
        changes, sets = pending(args.project_id, since)
        results.append(
            {
                "case": name,
                "seconds": round(time.time() - t0, 1),
                "status": answer.get("status"),
                "model": answer.get("model"),
                "pending_single": [c[1] for c in changes],
                "pending_sets": [f"{s[1]} operations" for s in sets],
                "answer": (answer.get("content") or "")[:220],
            }
        )
        refuse_all(args.project_id, since)
    left = pending(args.project_id, since)
    print(json.dumps({"results": results, "left_pending_after_refusal": [len(left[0]), len(left[1])]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
