#!/usr/bin/env python3
"""Does a model call the project tools right? A bench for the role ``agent`` of the Projekte program.

The model gets a short system prompt, a small project (ids and revisions as the agent would have read
them) and a request. A reading call (project.list, project.get ...) is answered from that project,
up to three turns; the first writing call is scored, without a model as judge: is the call valid
against the hub's own schemas, does it do what was asked, and does it leave out what was not asked (the 12B model prepared an unasked ``project.node.complete`` and wrote ``node_id`` where an
operation takes ``node``). Nothing is changed anywhere: the call is never run.

    changeset_bench.py export OUT.json          needs the hub package (run it with `uv run` in hub/)
    changeset_bench.py run --tools OUT.json --base-url https://127.0.0.1:8443/v1 \\
        --model NAME@NODE --api-key-env TOPCOAT_API_KEY --insecure [--repeat 3] [--out RESULT.json]

``run`` uses the standard library and ``jsonschema`` (present on a hub host).
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.request
from typing import Any

# fixed ids so that the cases can say which node a call must name
PROJECT = "11111111-1111-4111-8111-111111111111"
T_TESTS = "22222222-2222-4222-8222-222222222221"
T_DOCS = "22222222-2222-4222-8222-222222222222"
T_OLD = "22222222-2222-4222-8222-222222222223"
T_SPIKE = "22222222-2222-4222-8222-222222222224"
CONTEXT = {
    "project": {"id": PROJECT, "title": "Release 2.0", "revision": 4},
    "nodes": [
        {"id": T_TESTS, "ref": "Release 2.0 #1", "title": "Tests schreiben", "kind": "task", "status": "todo", "revision": 3},
        {"id": T_DOCS, "ref": "Release 2.0 #2", "title": "Doku", "kind": "task", "status": "in_progress", "revision": 5},
        {"id": T_OLD, "ref": "Release 2.0 #3", "title": "Altlasten", "kind": "task", "status": "todo", "revision": 1},
        {"id": T_SPIKE, "ref": "Release 2.0 #4", "title": "Spike", "kind": "task", "status": "todo", "revision": 2},
    ],
}
SYSTEM = (
    "You are the project assistant of Topcoat. The person talks to you about one project; you read "
    "it with tools and prepare changes with tools. A change is only prepared: the person confirms it "
    "with a click. Answer in the language of the person. The project has already been read:\n"
    + json.dumps(CONTEXT, ensure_ascii=False)
)
WRITE = {"project.node.update", "project.node.complete", "project.node.create", "project.changeset.propose"}


def export(out: str) -> None:
    from topcoat_hub import agent_prompts, project_changesets, project_tools

    defs, index = agent_prompts.native_tools(project_tools.tool_entries())
    json.dump(
        {"tools": defs, "index": index, "schemas": project_changesets.SCHEMAS},
        open(out, "w", encoding="utf-8"),
        ensure_ascii=False,
    )
    print(f"{len(defs)} tool definitions, {len(json.dumps(defs))} characters -> {out}")


# --- simulated reads ---------------------------------------------------------------------------


def read_result(cap: str, args: dict[str, Any]) -> str:
    """What the hub would answer a reading call, from the small project above."""
    if cap == "project.list":
        return json.dumps([{**CONTEXT["project"], "status": "active"}])
    if cap in ("project.get", "project.agenda"):
        return json.dumps(CONTEXT)
    if cap == "project.node.get":
        node = next((n for n in CONTEXT["nodes"] if n["id"] == args.get("node_id")), None)
        return json.dumps(node or {"error": "not_found"})
    if cap == "project.node.search":
        return json.dumps(CONTEXT["nodes"])
    return json.dumps({"error": "not available in this test"})


READS = {"project.list", "project.get", "project.agenda", "project.node.get", "project.node.search"}


# --- scoring -----------------------------------------------------------------------------------


def _ops_of(cap: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    """The changes a call stands for, as change-set operations (so one rule scores both ways)."""
    if cap == "project.changeset.propose":
        ops = args.get("operations")
        return ops if isinstance(ops, list) else []
    if cap == "project.node.update":
        op: dict[str, Any] = {"op": "update_node", "node": args.get("node_id")}
        if "status" in args:
            op = {"op": "set_status", "node": args.get("node_id"), "status": args["status"]}
        op.update({k: v for k, v in args.items() if k in ("due_at", "priority", "title")})
        return [op]
    if cap == "project.node.complete":
        return [{"op": "set_status", "node": args.get("node_id"), "status": "done", "via": "complete"}]
    return []


def _valid(ops: list[dict[str, Any]], schemas: dict[str, Any]) -> str | None:
    import jsonschema

    for i, op in enumerate(ops):
        name = op.get("op") if isinstance(op, dict) else None
        if name not in schemas:
            return f"operation {i + 1}: op {name!r} is not one of the known operations"
        body = {k: v for k, v in op.items() if k != "via"}
        try:
            jsonschema.validate(body, schemas[name])
        except jsonschema.ValidationError as exc:
            return f"operation {i + 1}: {exc.message[:120]}"
    return None


def _has(ops: list[dict[str, Any]], node: str, **fields: Any) -> bool:
    return any(o.get("node") == node and all(o.get(k) == v for k, v in fields.items()) for o in ops)


def score(case: dict[str, Any], calls: list[tuple[str, dict[str, Any]]], text: str, schemas: dict[str, Any]) -> dict[str, Any]:
    writes = [(c, a) for c, a in calls if c in WRITE]
    ops = [o for c, a in writes for o in _ops_of(c, a)]
    res: dict[str, Any] = {"calls": [c for c, _ in calls], "valid": None, "ok": False, "why": ""}
    if case["expect"] == "no_write":
        res["ok"] = not writes
        res["why"] = "" if res["ok"] else "wrote without being asked what to clean"
        return res
    if not writes:
        res["why"] = "no writing call"
        return res
    res["valid"] = _valid(ops, schemas) is None
    if not res["valid"]:
        res["why"] = _valid(ops, schemas) or ""
        return res
    if any(c == "project.node.complete" for c, _ in writes) and not case.get("complete_ok"):
        res["why"] = "unasked project.node.complete"
        return res
    res["ok"], res["why"] = case["check"](ops, writes)
    return res


def _single_status(ops: list[dict[str, Any]], writes: Any) -> tuple[bool, str]:
    ok = len(ops) == 1 and _has(ops, T_TESTS, status="in_review")
    return ok, "" if ok else "expected exactly one change: Tests schreiben -> in_review"


def _close(ops: list[dict[str, Any]], writes: Any) -> tuple[bool, str]:
    ok = len(ops) == 1 and _has(ops, T_DOCS, status="done")
    return ok, "" if ok else "expected one change: Doku -> done"


def _tree(ops: list[dict[str, Any]], writes: Any) -> tuple[bool, str]:
    creates = [o for o in ops if o.get("op") == "create_node"]
    kinds = [o.get("kind") for o in creates]
    has_dep = any(o.get("depends_on") for o in creates)
    ok = len(creates) >= 4 and kinds.count("task") >= 3 and "topic" in kinds and has_dep
    return ok, "" if ok else f"expected a topic, three tasks and a dependency; got {len(creates)} creates"


def _two_drops(ops: list[dict[str, Any]], writes: Any) -> tuple[bool, str]:
    ok = _has(ops, T_OLD, status="dropped") and _has(ops, T_SPIKE, status="dropped") and len(ops) == 2
    return ok, "" if ok else "expected exactly two changes: Altlasten and Spike -> dropped"


def _due(ops: list[dict[str, Any]], writes: Any) -> tuple[bool, str]:
    merged: dict[str, Any] = {}
    for o in ops:
        if o.get("node") == T_TESTS:
            merged.update(o)
    ok = str(merged.get("due_at", "")).startswith("2026-11-15") and merged.get("priority") == 3
    return ok, "" if ok else "expected due 2026-11-15 and priority 3 on Tests schreiben"


CASES: list[dict[str, Any]] = [
    {"id": "single_status_de", "say": "Setze die Aufgabe 'Tests schreiben' auf in_review.", "expect": "write", "check": _single_status},
    {"id": "close_de", "say": "Die Doku ist fertig, schließ die Aufgabe bitte ab.", "expect": "write", "check": _close, "complete_ok": True},
    {"id": "tree_de", "say": "Lege ein Thema 'Rollout' an mit drei Aufgaben: Staging, Freigabe, Produktion. Produktion hängt von Freigabe ab.", "expect": "write", "check": _tree},
    {"id": "two_drops_de", "say": "Verwirf die Aufgaben 'Altlasten' und 'Spike'.", "expect": "write", "check": _two_drops},
    {"id": "due_priority_en", "say": "Move 'Tests schreiben' to due date 2026-11-15 and set the priority to 3.", "expect": "write", "check": _due},
    {"id": "vague_de", "say": "Räum das Projekt mal auf.", "expect": "no_write"},
]


# --- running -----------------------------------------------------------------------------------


def chat(
    base: str,
    key: str,
    model: str,
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    insecure: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = {
        "model": model,
        # as the agent asks (agent_service.GatewayModel.complete): a function must be called
        "temperature": 0.1,
        "max_tokens": 2048,
        "tools": tools,
        "tool_choice": "required",
        "parallel_tool_calls": True,
        "messages": messages,
        **(extra or {}),
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, context=ctx, timeout=900) as resp:
        return json.loads(resp.read())


def play(case: dict[str, Any], bundle: dict[str, Any], args: argparse.Namespace, key: str) -> dict[str, Any]:
    """Up to three turns: reads are answered, the turn with a writing call (or a finish) is scored."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": case["say"]},
    ]
    seen: list[str] = []
    for turn in range(3):
        data = chat(
            args.base_url, key, args.model, bundle["tools"], messages, args.insecure,
            json.loads(args.extra) if args.extra else None,
        )
        msg = data["choices"][0]["message"]
        raw_calls = msg.get("tool_calls") or []
        calls = []
        for tc in raw_calls:
            fn = tc.get("function") or {}
            try:
                parsed = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                parsed = {}
            calls.append((bundle["index"].get(fn.get("name", ""), fn.get("name", "")), parsed if isinstance(parsed, dict) else {}))
        seen += [c for c, _ in calls]
        reads_only = calls and all(c in READS for c, _ in calls)
        if not reads_only or turn == 2:
            res = score(case, calls, msg.get("content") or "", bundle["schemas"])
            res["calls"] = seen
            res["turns"] = turn + 1
            return res
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": raw_calls})
        for tc, (cap, parsed) in zip(raw_calls, calls, strict=False):
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": read_result(cap, parsed)})
    raise AssertionError("unreachable")


def run(args: argparse.Namespace) -> int:
    bundle = json.load(open(args.tools, encoding="utf-8"))
    key = os.environ.get(args.api_key_env, "")
    if not key:
        sys.exit(f"{args.api_key_env} is not set")
    results = []
    for case in CASES:
        for n in range(args.repeat):
            t0 = time.time()
            try:
                res = play(case, bundle, args, key)
            except Exception as exc:  # a node that is gone, a timeout: counts as a failure with the reason
                res = {"calls": [], "valid": None, "ok": False, "why": f"request failed: {str(exc)[:100]}", "turns": 0}
            res.update(case=case["id"], run=n, seconds=round(time.time() - t0, 1))
            results.append(res)
            print(f"{case['id']:18} #{n} {'ok  ' if res['ok'] else 'FAIL'} {res['seconds']:6.1f}s t{res.get('turns', 0)} {','.join(res['calls']) or '-'} {res['why']}", flush=True)
    ok = sum(r["ok"] for r in results)
    writes = [r for r in results if r["valid"] is not None]
    summary = {
        "model": args.model,
        "passed": ok,
        "of": len(results),
        "writes_valid": f"{sum(bool(r['valid']) for r in writes)}/{len(writes)}",
        "unasked_complete": sum(r["why"] == "unasked project.node.complete" for r in results),
    }
    print(json.dumps(summary))
    if args.out:
        json.dump({"summary": summary, "results": results}, open(args.out, "w", encoding="utf-8"), indent=1)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("out")
    r = sub.add_parser("run")
    r.add_argument("--tools", required=True)
    r.add_argument("--base-url", required=True)
    r.add_argument("--model", required=True)
    r.add_argument("--api-key-env", default="TOPCOAT_API_KEY")
    r.add_argument("--insecure", action="store_true")
    r.add_argument("--repeat", type=int, default=3)
    r.add_argument("--extra", default="", help="JSON merged into every request, e.g. a thinking switch")
    r.add_argument("--out")
    args = ap.parse_args()
    if args.cmd == "export":
        export(args.out)
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
