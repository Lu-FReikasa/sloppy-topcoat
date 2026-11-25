#!/usr/bin/env python3
"""Bring the fixes the hub rolled out itself (issue_fixes.py, ARCHITECTURE D104) into this checkout.

A fix of an issue is made in the dev node's workshop and rolled out onto the hub's overlay; this
checkout does not have it, and scripts/dev/deploy-overlay.sh refuses to push over it. This asks the
hub (through scripts/dev/hubapi.py on the hub host, so the token never leaves it) for the fixes that
run, applies each one's diff here and commits it with the trailer ``Topcoat-Fix: <label>``, which
is what deploy-overlay.sh looks for.

    python scripts/dev/pull-fixes.py            apply and commit what is missing
    python scripts/dev/pull-fixes.py --list     only list

Environment (or .dev/hosts.env): TOPCOAT_OVERLAY_HOST, the ssh target of the hub host.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def hosts() -> dict[str, str]:
    out = dict(os.environ)
    env = ROOT / ".dev" / "hosts.env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip("'\""))
    return out


def git(*args: str, **kw: object) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True, **kw).stdout  # type: ignore[call-overload]


def main(argv: list[str]) -> int:
    host = hosts().get("TOPCOAT_OVERLAY_HOST")
    if not host:
        sys.exit("TOPCOAT_OVERLAY_HOST is not set")
    helper = (ROOT / "scripts" / "dev" / "hubapi.py").read_bytes()
    remote = (
        "d=$(mktemp -d) && cat > $d/hubapi.py && "
        "TOPCOAT_ENV_FILE=$HOME/topcoat/deploy/hub.env python3 $d/hubapi.py GET /api/project/fixes; "
        "rm -rf $d"
    )
    raw = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, remote], input=helper, capture_output=True, check=True
    ).stdout
    fixes = json.loads(raw)["fixes"]
    have = git("log", "--format=%B")
    todo = []
    for f in fixes:
        label = (f.get("rollout") or {}).get("label") or ""
        issue = f.get("issue") or {}
        mark = f"Topcoat-Fix: {label}"
        state = "here" if label and mark in have.splitlines() else "missing"
        print(f"{label or '?':<20} {issue.get('number', '?'):<10} {state:<8} {f.get('summary', '')[:70]}")
        if state == "missing" and label and f.get("diff"):
            todo.append(f)
    if "--list" in argv or not todo:
        return 0
    if git("status", "--porcelain", "--untracked-files=no").strip():
        sys.exit("uncommitted changes: commit or stash first")
    for f in todo:
        label = f["rollout"]["label"]
        issue = f.get("issue") or {}
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as tmp:
            tmp.write(f["diff"] if f["diff"].endswith("\n") else f["diff"] + "\n")
        try:
            git("apply", "--3way", tmp.name)
        except subprocess.CalledProcessError as exc:
            sys.exit(f"{label} does not apply here: {exc.stderr.strip()}")
        finally:
            os.unlink(tmp.name)
        message = (
            f"Fix {issue.get('number', '?')}: {f.get('summary') or issue.get('title', '')}\n\n"
            f"Made and rolled out by Topcoat (issue {issue.get('number', '?')}, fix {f['id']}).\n\n"
            f"Topcoat-Fix: {label}\n"
        )
        git("add", "-A")
        git("commit", "-q", "-F", "-", input=message)
        print(f"committed {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
