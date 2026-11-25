#!/usr/bin/env python3
"""Run one set of questions over models and request-parameter variants and score the answers by rules.

The question this answers: "does answer quality or speed change when I change a setting?" - the thinking
switch, the sampling, the effort level, a different quantisation of the same model (just another model
name). It goes through the hub's /v1 gateway, writes no chat and no note, and uses no model as judge.

    param_sweep.py run --base-url https://127.0.0.1:8443/v1 --insecure --plan sweep.json --out run.json
    param_sweep.py compare OLD.json NEW.json
    param_sweep.py selftest

A plan (JSON, see ``sweep-example.json``):

    {"label": "thinking switch",
     "cases": "quality_cases.json",              # relative to the plan file
     "models": ["gemma-4-12B@node-amd"],         # name or name@node, as the gateway knows it
     "variants": [{"name": "base", "extra": {}},
                  {"name": "nothink", "extra": {"chat_template_kwargs": {"enable_thinking": false}}}],
     "repeats": 2, "max_tokens": 1200, "temperature": 0.2}

Server arguments of llama-server (ngl, ubatch, flash attention, cache types) are not request parameters: they
are changed in the node's configuration for the model, and the run is repeated with a new ``--label`` (the
results file records the label and the date, not the node's configuration; write it into the label).

Cases (JSON): {"cases": [{"id", "question", "system"?, "checks": {...}, "tags"?}]}. Checks (all that are given
must hold): ``must`` (strings or lists of alternatives, case-insensitive), ``must_not`` (strings),
``regex`` (a pattern that has to match), ``max_words`` / ``min_words``, ``json`` (the answer, or the first
JSON object in it, parses; with ``keys`` a list of required keys). Scoring is deterministic.

Run one model at a time on a node and unload the node's other models first (``run-sweep.sh`` does it):
two big models in memory made the timings of 2026-10-31 ten times slower.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import statistics
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


# --- scoring ------------------------------------------------------------------------------------


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def _first_json_object(text: str) -> Any | None:
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
            return obj
        except ValueError:
            start = text.find("{", start + 1)
    return None


def score(answer: str, checks: dict[str, Any]) -> dict[str, Any]:
    """Which checks hold for ``answer``; ``passed`` when all do."""
    failed: list[str] = []
    text = _norm(answer)
    for item in checks.get("must", []):
        options = [item] if isinstance(item, str) else list(item)
        if not any(_norm(o) in text for o in options):
            failed.append(f"must:{options[0]}")
    for item in checks.get("must_not", []):
        if _norm(item) in text:
            failed.append(f"must_not:{item}")
    pattern = checks.get("regex")
    if pattern and not re.search(pattern, answer, re.IGNORECASE | re.DOTALL):
        failed.append(f"regex:{pattern}")
    words = len(answer.split())
    if "max_words" in checks and words > checks["max_words"]:
        failed.append(f"max_words:{words}>{checks['max_words']}")
    if "min_words" in checks and words < checks["min_words"]:
        failed.append(f"min_words:{words}<{checks['min_words']}")
    if "json" in checks:
        obj = _first_json_object(answer)
        keys = checks["json"].get("keys", []) if isinstance(checks["json"], dict) else []
        if not isinstance(obj, dict):
            failed.append("json:not an object")
        else:
            failed.extend(f"json:key {k}" for k in keys if k not in obj)
    if not answer.strip():
        failed.append("empty")
    return {"passed": not failed, "failed": failed}


# --- running ------------------------------------------------------------------------------------

Ask = Callable[[str, list[dict[str, str]], dict[str, Any]], dict[str, Any]]


def http_ask(base: str, key: str, insecure: bool, timeout: float) -> Ask:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    def ask(model: str, messages: list[dict[str, str]], params: dict[str, Any]) -> dict[str, Any]:
        body = {"model": model, "messages": messages, **params}
        req = urllib.request.Request(
            base.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            return json.loads(resp.read())

    return ask


def load_plan(path: Path) -> dict[str, Any]:
    plan = json.loads(path.read_text(encoding="utf-8"))
    for field in ("models", "variants", "cases"):
        if not plan.get(field):
            raise SystemExit(f"the plan has no {field}")
    plan["_cases"] = json.loads((path.parent / plan["cases"]).read_text(encoding="utf-8"))["cases"]
    for case in plan["_cases"]:
        if not case.get("id") or not case.get("question") or not case.get("checks"):
            raise SystemExit(f"a case needs id, question and checks: {case.get('id')}")
    names = [v["name"] for v in plan["variants"]]
    if len(set(names)) != len(names):
        raise SystemExit("variant names must differ")
    return plan


def run_plan(plan: dict[str, Any], ask: Ask, pause: float = 0.0, log: Callable[[str], None] = print) -> dict[str, Any]:
    base_params = {
        "temperature": plan.get("temperature", 0.2),
        "max_tokens": plan.get("max_tokens", 1200),
        "stream": False,
    }
    rows: list[dict[str, Any]] = []
    for model in plan["models"]:
        for variant in plan["variants"]:
            for case in plan["_cases"]:
                for rep in range(int(plan.get("repeats", 1))):
                    messages = ([{"role": "system", "content": case["system"]}] if case.get("system") else []) + [
                        {"role": "user", "content": case["question"]}
                    ]
                    row: dict[str, Any] = {
                        "model": model,
                        "variant": variant["name"],
                        "case": case["id"],
                        "rep": rep,
                        "tags": case.get("tags", []),
                    }
                    t0 = time.time()
                    try:
                        data = ask(model, messages, {**base_params, **variant.get("extra", {})})
                        seconds = time.time() - t0
                        choice = data["choices"][0]
                        answer = (choice.get("message") or {}).get("content") or ""
                        tokens = (data.get("usage") or {}).get("completion_tokens")
                        row.update(
                            seconds=round(seconds, 2),
                            completion_tokens=tokens,
                            tps=round(tokens / seconds, 1) if tokens and seconds > 0 else None,
                            finish=choice.get("finish_reason"),
                            answer=answer.strip()[:1500],
                            **score(answer, case["checks"]),
                        )
                    except Exception as exc:  # a node that is gone: the reason is the result
                        row.update(
                            seconds=round(time.time() - t0, 2),
                            error=str(exc)[:200],
                            passed=False,
                            failed=["error"],
                        )
                    rows.append(row)
                    log(f"{model} {variant['name']} {case['id']}#{rep} {'ok' if row['passed'] else 'FAIL'} {row['seconds']}s")
                    if pause:
                        time.sleep(pause)
    return {
        "schema_version": SCHEMA_VERSION,
        "label": plan.get("label", ""),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "plan": {k: v for k, v in plan.items() if not k.startswith("_")},
        "rows": rows,
        "summary": summarize(rows),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per model and variant: pass rate, errors, median seconds, mean tokens per second, tokens."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(f"{r['model']} | {r['variant']}", []).append(r)
    out: dict[str, Any] = {}
    for key, group in groups.items():
        secs = [r["seconds"] for r in group if "error" not in r]
        tps = [r["tps"] for r in group if r.get("tps")]
        toks = [r["completion_tokens"] for r in group if r.get("completion_tokens")]
        by_tag: dict[str, list[bool]] = {}
        for r in group:
            for tag in r.get("tags", []):
                by_tag.setdefault(tag, []).append(r["passed"])
        out[key] = {
            "runs": len(group),
            "pass_rate": round(sum(r["passed"] for r in group) / len(group), 3),
            "errors": sum("error" in r for r in group),
            "median_seconds": round(statistics.median(secs), 2) if secs else None,
            "mean_tps": round(statistics.mean(tps), 1) if tps else None,
            "mean_tokens": round(statistics.mean(toks)) if toks else None,
            "by_tag": {t: round(sum(v) / len(v), 3) for t, v in sorted(by_tag.items())},
            "cut_off": sum(r.get("finish") == "length" for r in group),
        }
    return out


def compare(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Lines that say what changed between two result files (same plan or not: keys that match)."""
    lines = [f"old: {old.get('label')} ({old.get('timestamp')})", f"new: {new.get('label')} ({new.get('timestamp')})"]
    keys = sorted(set(old["summary"]) | set(new["summary"]))
    for key in keys:
        a, b = old["summary"].get(key), new["summary"].get(key)
        if a is None or b is None:
            lines.append(f"{key}: only in {'new' if a is None else 'old'}")
            continue
        parts = []
        for field in ("pass_rate", "median_seconds", "mean_tps", "mean_tokens", "errors", "cut_off"):
            if a.get(field) != b.get(field):
                parts.append(f"{field} {a.get(field)} -> {b.get(field)}")
        lines.append(f"{key}: " + ("; ".join(parts) if parts else "no change"))
    old_rows = {(r["model"], r["variant"], r["case"], r["rep"]): r["passed"] for r in old["rows"]}
    flips = [
        f"  regression: {k[0]} | {k[1]} | {k[2]}#{k[3]}"
        for r in new["rows"]
        if (k := (r["model"], r["variant"], r["case"], r["rep"])) in old_rows and old_rows[k] and not r["passed"]
    ]
    return lines + (["single cases that passed before and fail now:", *flips] if flips else [])


# --- selftest -----------------------------------------------------------------------------------


def selftest() -> int:
    """The whole run against a scripted gateway: scoring, a thinking variant that answers worse and
    slower, an error row, compare."""
    cases = {
        "cases": [
            {"id": "yes", "question": "Is 2+2=4? say yes or no", "checks": {"must": [["yes", "ja"]]}, "tags": ["math"]},
            {"id": "json", "question": "Reply with JSON {\"a\":1}", "checks": {"json": {"keys": ["a"]}}},
        ]
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            think = "chat_template_kwargs" not in body
            q = body["messages"][-1]["content"]
            if body["model"] == "gone":
                self.send_response(500)
                self.end_headers()
                return
            text = ("Yes." if "2+2" in q else '{"a": 1}') if not think else ("hmm" if "2+2" in q else "{}")
            payload = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": {"completion_tokens": 10 if not think else 100}}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    plan = {
        "label": "selftest",
        "models": ["m", "gone"],
        "variants": [{"name": "think", "extra": {}}, {"name": "nothink", "extra": {"chat_template_kwargs": {"enable_thinking": False}}}],
        "_cases": cases["cases"],
        "repeats": 1,
    }
    ask = http_ask(base, "k", False, 5)
    first = run_plan(plan, ask, log=lambda s: None)
    s = first["summary"]
    assert s["m | think"]["pass_rate"] == 0.0 and s["m | nothink"]["pass_rate"] == 1.0, s
    assert s["m | nothink"]["by_tag"] == {"math": 1.0}, s
    assert s["gone | think"]["errors"] == 2, s
    second = json.loads(json.dumps(first))
    for r in second["rows"]:
        if r["model"] == "m" and r["variant"] == "nothink" and r["case"] == "yes":
            r["passed"] = False
    assert any("regression: m | nothink | yes#0" in line for line in compare(first, second))
    assert score("Ja, klar", {"must": [["yes", "ja"]], "max_words": 3})["passed"]
    assert not score("x " * 10, {"max_words": 3})["passed"]
    assert score('text {"a": 1} text', {"json": {"keys": ["a"]}})["passed"]
    assert not score("", {"must_not": ["x"]})["passed"]
    server.shutdown()
    print("selftest ok")
    return 0


# --- command line -------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--base-url", required=True)
    run.add_argument("--api-key-env", default="TOPCOAT_API_KEY")
    run.add_argument("--insecure", action="store_true")
    run.add_argument("--plan", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--label", default=None, help="replaces the label of the plan (write the server arguments into it)")
    run.add_argument("--timeout", type=float, default=900)
    run.add_argument("--pause", type=float, default=0.0)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("old")
    cmp_.add_argument("new")
    sub.add_parser("selftest")
    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "compare":
        old, new = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (args.old, args.new))
        print("\n".join(compare(old, new)))
        return 0
    key = os.environ.get(args.api_key_env, "")
    if not key:
        sys.exit(f"{args.api_key_env} is not set")
    plan = load_plan(Path(args.plan))
    if args.label:
        plan["label"] = args.label
    result = run_plan(plan, http_ask(args.base_url, key, args.insecure, args.timeout), args.pause)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
