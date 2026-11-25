#!/usr/bin/env python3
"""The live tests of the applications that depend on the same parameters, with one model on one or more nodes.

    live_suite.py MODEL NODE[,NODE...] [MAX_MINUTES] [--only research|dev|blocks]

Every node of the list is tested **on its own and at the same time as the others** (the tasks and the chat are pinned to
MODEL@NODE, the table of applications is not touched, so nodes do not get in each other's way), every test with the same
single model; on one node the tests run one after the other. For each node it runs
  research  ``live_research.py fresh``   a thorough research and its report
  dev       ``live_dev.py``              a read-only question in the Development chat
  blocks    the shipped block program ``research_report`` with agent, check and revise all pinned to MODEL on that node
Isolation, so that the result says something about MODEL and not about the rest of the hub: background work of the hub
(memory care, summaries) is **paused** for the whole run, because its rows name other models and would load them next to
MODEL; the node's other models are unloaded before each node and everything is unloaded at the end. The Research and
table of applications stays as it is. Prints one line per test and exits with 0 only when all passed.
`node-amd` is allowed (the maintainer said so on 2026-11-05); it is his PC, so run it when he is not playing.
"""

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live_dev  # noqa: E402  (its api() reads the token and talks to the hub)
import live_judge  # noqa: E402

QUESTION = "What are the main differences between llama.cpp and vLLM for serving a single user on an 8 GB GPU?"


def api(method: str, path: str, body: object | None = None) -> object:
    raw = live_dev.api(method, path, body)
    return json.loads(raw) if raw else None


def run_script(name: str, *args: str) -> tuple[bool, str]:
    proc = subprocess.run([sys.executable, str(HERE / name), *args], capture_output=True, text=True, check=False)  # noqa: S603
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-6:])
    return proc.returncode == 0, tail


def block_program(model: str, node: str) -> dict:
    pin = {"fixed": f"{model}@{node}"}
    return {
        "name": "live_suite_report",
        "inputs": {"question": "text"},
        "stack": [
            {"b": "agent", "app": "research", "depth": "thorough", "prompt": "{{question}}", "pick": pin, "out": "draft"},
            {"b": "check", "in": "draft", "question": "question", "pick": pin, "out": "v"},
            {"b": "repeat_until", "cond": "v.ok", "max": 1, "stack": [
                {"b": "revise", "draft": "draft", "issues": "v", "pick": pin, "out": "draft"},
                {"b": "check", "in": "draft", "question": "question", "pick": pin, "out": "v"},
            ]},
            {"b": "file_report", "in": "draft", "title": "{{question}}"},
        ],
    }  # fmt: skip


def run_blocks(model: str, node: str, minutes: int) -> tuple[bool, str]:
    started = api("POST", "/api/blocks/runs", {"program": block_program(model, node), "inputs": {"question": QUESTION}})
    end = time.time() + minutes * 60
    while time.time() < end:
        view = api("GET", f"/api/blocks/runs/{started['id']}")
        if view["status"] != "running":
            problems = live_judge.judge_blocks(view)
            return not problems, f"status={view['status']} filed={view['filed']} notes={view['notes']} problems={problems}"
        time.sleep(15)
    api("POST", f"/api/blocks/runs/{started['id']}/stop")
    return False, "timeout: the run was stopped"


def unload_all(node: str) -> None:
    """Every model of the node that is loaded (a test must start and end on a node that holds nothing)."""
    found = next((n for n in api("GET", "/api/nodes") if n["name"] == node), None)
    if not found:
        return
    for m in api("GET", "/api/scheduling/pools")["nodes"]:
        if m["node"] == node:
            for model in (x["name"] for x in m["models"] if x["loaded"]):
                api("POST", f"/api/nodes/{found['id']}/actions", {"name": "models.unload", "args": {"model": model}})


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    only = None
    if "--only" in args:
        i = args.index("--only")
        only = args[i + 1]
        del args[i : i + 2]
    if len(args) < 2:
        print(__doc__)
        return 2
    model, nodes = args[0], args[1].split(",")
    minutes = int(args[2]) if len(args) > 2 else 25

    def test_node(node: str) -> list[tuple[str, bool, str]]:
        found: list[tuple[str, bool, str]] = []
        unload_all(node)
        if only in (None, "research"):
            ok, tail = run_script("live_research.py", "fresh", model, node, str(minutes))
            found.append((f"research on {node}", ok and "no report filed" not in tail, tail))
        if only in (None, "dev"):
            ok, tail = run_script("live_dev.py", model, node, str(minutes))
            found.append((f"dev on {node}", ok, tail))
        if only in (None, "blocks"):
            ok, tail = run_blocks(model, node, minutes)
            found.append((f"blocks on {node}", ok, tail))
        unload_all(node)
        return found

    api("POST", "/api/scheduling/pause", {"minutes": minutes * 3 + 10})  # background work waits
    try:
        with ThreadPoolExecutor(max_workers=len(nodes)) as pool:
            results = [r for part in pool.map(test_node, nodes) for r in part]
    finally:
        api("POST", "/api/scheduling/pause", {"minutes": 0})  # background work goes on
    for name, ok, tail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print("      " + tail.replace("\n", "\n      "))
    return 0 if results and all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
