#!/usr/bin/env python3
"""The yardstick: fixed questions from real chats, asked in TEST chats, judged by rules, measured from the database.

    live_cases.py ROUND MODEL@NODE[,MODEL@NODE...] [--only F1,F5] [--project TEST_PROJECT_ID]

Runs on the hub host next to hubapi.py (token from hub.env, never printed). Every lane (MODEL@NODE) runs its cases one after
the other; the lanes run at the same time. Each case is a conversation of one application with one or more turns; every turn
is judged by plain rules (no model as judge) and measured from the agent tasks behind its answers: stop reason, model calls,
tool calls and how many failed, prompt tokens per call (mean, max), total prompt tokens, seconds.

Nothing stays changed: chats are titled ``TEST yard …``; a theme is only checked (``/api/ui/themes/check``), never stored;
F7 works in the TEST project only and every pending change of that project made by this run is refused at the end.
Results: ``results/ROUND_<lane>.json`` and one table on stdout.
"""

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.request

# the node the state questions name (the real name comes from the environment, never the repo)
NODE = os.environ.get("TOPCOAT_LIVE_NODE", "node-nvidia")
from pathlib import Path

ENV = Path(os.environ.get("TOPCOAT_ENV_FILE", "~/topcoat/deploy/hub.env")).expanduser()
URL = os.environ.get("TOPCOAT_HUB_URL", "https://127.0.0.1:8443")
OUT = Path(os.environ.get("TOPCOAT_YARD_DIR", "~/topcoat/livetest/results")).expanduser()
CTX = ssl.create_default_context()
CTX.check_hostname, CTX.verify_mode = False, ssl.CERT_NONE
OFFERS = re.compile(r"soll ich|möchtest du,? dass ich|willst du,? dass ich|shall i|should i|do you want me to", re.I)
NOT_KNOW = re.compile(r"wei(ß|ss) (ich )?(es )?nicht|nicht m[öo]glich|kein(en)? zugriff|cannot access|don't know", re.I)


def _token() -> str:
    for line in ENV.read_text().splitlines():
        if line.startswith("TOPCOAT_DEV_ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("TOPCOAT_DEV_ADMIN_TOKEN not found in the env file")


def api(method: str, path: str, body: object | None = None, timeout: int = 1800) -> bytes:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:  # noqa: S310
        return r.read()


def psql(sql: str) -> list[list[str]]:
    out = subprocess.run(
        ["docker", "exec", "-i", "topcoat-db-1", "psql", "-U", "topcoat", "topcoat", "-tA", "-F", "\t", "-c", sql],
        capture_output=True, text=True, check=True,
    ).stdout
    return [line.split("\t") for line in out.splitlines() if line.strip()]


# --- judges: (answer text, metrics, state) -> list of problems ---------------------------------------------------------

def _has(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.I) is not None


def theme_json(text: str) -> dict | None:
    for block in re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.S):
        try:
            doc = json.loads(block)
        except ValueError:
            continue
        if isinstance(doc, dict) and ("css" in doc or "vars" in doc or "base" in doc):
            return doc
    return None


def j_f1(t, m, s):
    p = [] if _has(r"BUILTIN_THEMES|theme\.ts|ThemeStore|themes/check|THEMES\.md", t) else ["names neither theme.ts nor the store"]
    return p + (["says it does not know"] if NOT_KNOW.search(t) else [])


def j_f2(t, m, s):
    p = []
    if not _has(r"\blayout\b", t):
        p.append("no layout")
    if not _has(r"\bbase\b|\bbasis\b", t):
        p.append("no base")
    if not _has(r"\bcss\b", t):
        p.append("no css")
    return p + (["says it does not know"] if NOT_KNOW.search(t) else [])


def _check_theme(doc: dict) -> list[str]:
    try:
        res = json.loads(api("POST", "/api/ui/themes/check", doc, timeout=60))
    except Exception as exc:  # noqa: BLE001
        return [f"theme check call failed: {exc}"]
    return [str(x) for x in (res.get("problems") or [])]


def j_f3(t, m, s):
    doc = theme_json(t)
    if doc is None:
        return ["no theme JSON block"]
    s["theme"] = doc
    p = [f"check: {x}" for x in _check_theme(doc)][:3]
    if doc.get("base") == "schreibtisch":
        p.append("paper base for a dark theme")
    return p


def j_f4(t, m, s):
    doc = theme_json(t)
    if doc is None:
        return ["no theme JSON block"]
    p = [f"check: {x}" for x in _check_theme(doc)][:3]
    before = s.get("theme") or {}
    old = set((before.get("css") or "").splitlines()) | {f"{k}:{v}" for k, v in (before.get("vars") or {}).items()}
    new = set((doc.get("css") or "").splitlines()) | {f"{k}:{v}" for k, v in (doc.get("vars") or {}).items()}
    if len(new ^ old) < 5:
        p.append(f"barely changed ({len(new ^ old)} lines differ)")
    return p


def j_f5(t, m, s):
    return [] if _has(r"Topcoat", t) else ["does not name the project Topcoat"]


def j_f6(t, m, s):
    p = [] if _has(r"1\.2|Gewähltes Theme schreiben", t) else ["does not find task 1.2"]
    if not _has(r"todo|offen|nicht (begonnen|gestartet)|zu erledigen|ausstehend", t):
        p.append("no status")
    return p + (["says it does not know"] if NOT_KNOW.search(t) else [])


def j_f7(t, m, s):
    pid = s.get("project")
    if not pid:
        return ["no TEST project given (--project)"]
    rows = psql(
        "select count(*) from project_change_sets where status='pending' and project_id='%s' and created_at >= '%s'"
        % (pid, s["since"])
    ) + psql("select count(*) from project_changes where status='pending' and created_at >= '%s'" % s["since"])
    made = sum(int(r[0]) for r in rows)
    return [] if made else ["no change prepared"]


def j_f8(t, m, s):
    p = [] if _has(re.escape(NODE), t) and _has(r"\d\s?%|\d\s?(GB|GiB)", t) else [f"no state of {NODE} with numbers"]
    if (m.get("tool_calls") or 0) > 6:
        p.append(f"{m['tool_calls']} tool calls for a status question")
    return p


def j_f9(t, m, s):
    names = re.findall(r"`[\w.-]{3,}`", t)
    p = [] if len(names) >= 2 or _has(r"\d+\s+Container", t) else ["lists no containers"]
    return p + (["says it cannot look"] if NOT_KNOW.search(t) else [])


def j_f10(t, m, s):
    p = []
    if (m.get("fetch_ok") or 0) < 2:
        p.append(f"only {m.get('fetch_ok') or 0} pages read")
    if (m.get("fetch_failed") or 0) >= 3:
        p.append(f"{m['fetch_failed']} failed fetches")
    if not _has(r"262|256|128|32\s?k|32[\s.]?768|131[\s.]?072|262[\s.]?144", t):
        p.append("names no context length")
    return p


def j_f11(t, m, s):
    p = [] if _has(r"\b20\b|3[\s.]?500|8[\s.]?000|\b16\b", t) else ["names none of the limits"]
    return p + (["says it does not know"] if NOT_KNOW.search(t) else [])


def j_none(t, m, s):
    return ["says it does not know"] if NOT_KNOW.search(t) else []


# (case, application, turns: [(text, judge or None)])
CASES = [
    ("F1-2", "dev", [
        ("Bitte schaue dir im Repo sloppy-topcoat-local an und erkläre mir, wie dort gerade die Themes verwaltet werden.", j_f1),
        ("In welchem Format müsste ein solches Theme ausgearbeitet werden?", j_f2),
    ]),
    ("F3-4", "dev", [
        ("Entwirf mir ein dunkles Cyberpunk-Theme für den Desktop: Neon-Pink und Cyan auf fast Schwarz, Taskleiste oben.", j_f3),
        ("Das ist mir noch zu hell. Mach es deutlich dunkler und blauer.", j_f4),
    ]),
    ("F5", "project", [("Welche aktiven Projekte haben wir gerade?", j_f5)]),
    ("F6", "project", [("Wie ist der Stand von Aufgabe 1.2 im Projekt Topcoat und was fehlt dafür noch?", j_f6)]),
    ("F7", "project", [("Im Projekt 'TEST Messlatte': Lege ein Thema 'Rollout' an und darunter eine Aufgabe 'Staging prüfen'.", j_f7)]),
    ("F8", "admin", [(f"Wie ist der Stand von {NODE} gerade?", j_f8)]),
    ("F9", "admin", [("Welche Container laufen auf tools-vm-b und ist einer davon ungesund?", j_f9)]),
    ("F10", "research", [("Welche Kontextlänge unterstützt Qwen3-30B-A3B-Instruct-2507, und wie viel Speicher braucht der KV-Cache dafür ungefähr?", j_f10)]),
    ("F11", "dev", [
        ("Was macht hub/src/topcoat_hub/agent_loop.py im Repo sloppy-topcoat-local? Kurz.", j_none),
        ("Welche Limits hat ein Agentenlauf dort, mit Standardwerten?", j_none),
        ("Was passiert, wenn das Modell denselben Werkzeugaufruf wiederholt?", j_none),
        ("Und was, wenn eine Werkzeugausgabe zu lang ist?", j_none),
        ("Fasse zusammen: welche zwei Limits aus deiner zweiten Antwort sind am wichtigsten, mit ihren Werten?", j_f11),
    ]),
]


def metrics(conversation: str, after_id: int) -> dict:
    """Numbers of the agent tasks behind the answers after message ``after_id`` (children included)."""
    tasks = [r[0] for r in psql(
        "select distinct agent_task_id from messages where conversation_id='%s' and id > %d and agent_task_id is not null"
        % (conversation, after_id)
    )]
    if not tasks:
        return {"runs": 0}
    ids = ",".join(f"'{t}'" for t in tasks)
    scope = f"(task_id in ({ids}) or task_id in (select id from agent_tasks where parent_id in ({ids})))"
    row = psql(
        "select count(*), coalesce(round(avg((data->>'tokens')::int)),0), coalesce(max((data->>'tokens')::int),0), "
        f"coalesce(sum((data->>'tokens')::int),0) from agent_steps where kind='assistant' and {scope}"
    )[0]
    obs = psql(
        "select count(*), count(*) filter (where (data->'tool'->>'ok')='false'), "
        "count(*) filter (where data->'tool'->>'capability'='web.fetch' and coalesce(data->'tool'->>'ok','true')<>'false'), "
        "count(*) filter (where data->'tool'->>'capability'='web.fetch' and (data->'tool'->>'ok')='false') "
        f"from agent_steps where kind='observation' and jsonb_typeof(data->'tool')='object' and {scope}"
    )[0]
    stops = psql(
        f"select coalesce(stop_reason, status) from agent_tasks where id in ({ids}) and (stop_reason is not null or status<>'done')"
    )
    return {
        "runs": len(tasks), "calls": int(row[0]), "prompt_mean": int(float(row[1])), "prompt_max": int(row[2]),
        "prompt_total": int(row[3]), "tool_calls": int(obs[0]), "tool_failed": int(obs[1]),
        "fetch_ok": int(obs[2]), "fetch_failed": int(obs[3]), "stops": [s[0] for s in stops],
    }


def last_id(conversation: str) -> int:
    rows = psql("select coalesce(max(id),0) from messages where conversation_id='%s'" % conversation)
    return int(rows[0][0])


def node_record(name: str) -> dict:
    nodes = json.loads(api("GET", "/api/nodes"))
    nodes = nodes if isinstance(nodes, list) else nodes.get("nodes", [])
    return next(n for n in nodes if n["name"] == name)


def foreign(node: str, model: str) -> list[str]:
    """Models other than ``model`` loaded on ``node`` right now."""
    return [m["name"] for m in node_record(node).get("loaded", []) if m["name"] != model]


def clear(node: str, model: str) -> list[str]:
    """Unload every other model of the node: a measurement is of one model alone on its card."""
    others = foreign(node, model)
    if others:
        nid = node_record(node)["id"]
        for other in others:
            api("POST", f"/api/nodes/{nid}/actions", {"name": "models.unload", "args": {"model": other}})
    return others


def isolate(lanes: list[str]) -> dict:
    """Each lane's node gets a pool of its lane's model only (nothing else is loaded there while the
    yardstick runs); the other nodes keep theirs. Returns the pools to put back (PUT replaces all)."""
    pools = json.loads(api("GET", "/api/scheduling/pools"))["nodes"]
    original = {p["node"]: {"available": [m["name"] for m in p["models"] if m["available"]]} for p in pools if p["restricted"]}
    test = {k: {"available": list(v["available"])} for k, v in original.items()}
    for lane in lanes:
        model, node = lane.split("@", 1)
        test[node] = {"available": [model]}
    api("PUT", "/api/scheduling/pools", {"pools": test})
    return original


def run_case(case, lane: str, project: str | None, rnd: str) -> list[dict]:
    name, app, turns = case
    model, node = lane.split("@", 1)
    conv = json.loads(api("POST", "/api/conversations", {
        "title": f"TEST yard {rnd} {name} {node}", "application": app, "model": lane,
    }))
    cid = conv["id"]
    state: dict = {"project": project, "since": psql("select now()")[0][0]}
    out = []
    for i, (text, judge) in enumerate(turns, 1):
        unloaded = clear(node, model)
        before = last_id(cid)
        t0 = time.time()
        err = None
        try:
            api("POST", f"/api/conversations/{cid}/messages", {"content": text, "tools": "auto"})
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
        secs = round(time.time() - t0, 1)
        detail = json.loads(api("GET", f"/api/conversations/{cid}"))
        answer = next((m for m in reversed(detail.get("messages", [])) if m.get("role") == "assistant" and m.get("kind", "say") == "say"), {})
        body = answer.get("content") or ""
        m = metrics(cid, before)
        problems = ([f"request failed: {err}"] if err else []) + ([] if answer.get("status") == "complete" else [f"answer {answer.get('status')}"])
        problems += [f"run ended {s}" for s in m.get("stops", [])]
        if judge is not None:
            problems += judge(body, m, state)
        if OFFERS.search(body):
            problems.append("offers to look instead of looking")
        during = foreign(node, model)
        out.append({
            "case": name if len(turns) == 1 else f"{name}.{i}", "lane": lane, "seconds": secs, "pass": not problems,
            "problems": problems, "metrics": m, "answer_node": answer.get("node"), "conversation": cid,
            "answer": body[:4000],
            **({"others_unloaded": unloaded} if unloaded else {}),
            **({"others_during": during} if during else {}),
        })
    if name == "F7" and project:
        for (sid,) in psql("select id from project_change_sets where status='pending' and project_id='%s' and created_at >= '%s'" % (project, state["since"])):
            api("POST", f"/api/project/changesets/{sid}/refuse", {})
        for (chid,) in psql("select id from project_changes where status='pending' and created_at >= '%s'" % state["since"]):
            api("POST", f"/api/project/changes/{chid}/refuse", {})
    return out


def run_lane(lane: str, cases, project, rnd, results: dict) -> None:
    rows: list[dict] = []
    for case in cases:
        try:
            rows += run_case(case, lane, project, rnd)
        except Exception as exc:  # noqa: BLE001
            rows.append({"case": case[0], "lane": lane, "pass": False, "problems": [f"crashed: {exc}"], "metrics": {}})
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{rnd}_{lane.replace('@', '_')}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
        print(f"[{lane}] {case[0]} done", flush=True)
    results[lane] = rows


def table(results: dict) -> str:
    lines = ["| lane | case | pass | s | runs | calls | prompt mean/max | tools (failed) | stops | problems |", "|---|---|---|---|---|---|---|---|---|---|"]
    for lane, rows in results.items():
        for r in rows:
            m = r.get("metrics") or {}
            lines.append(
                f"| {lane} | {r['case']} | {'ok' if r['pass'] else 'FAIL'} | {r.get('seconds', '')} | {m.get('runs', '')} | {m.get('calls', '')} | "
                f"{m.get('prompt_mean', '')}/{m.get('prompt_max', '')} | {m.get('tool_calls', '')} ({m.get('tool_failed', '')}) | "
                f"{','.join(m.get('stops') or [])} | {'; '.join(r['problems'])[:160]} |"
            )
        ok = sum(1 for r in rows if r["pass"])
        lines.append(f"| {lane} | **total** | **{ok}/{len(rows)}** | | | | | | | |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("round")
    ap.add_argument("lanes")
    ap.add_argument("--only", default="")
    ap.add_argument("--project", default=None)
    args = ap.parse_args()
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    cases = [c for c in CASES if not only or c[0] in only or any(o in c[0] for o in only)]
    results: dict = {}
    lanes = args.lanes.split(",")
    original = isolate(lanes)
    threads = [threading.Thread(target=run_lane, args=(lane, cases, args.project, args.round, results)) for lane in args.lanes.split(",")]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    finally:
        api("PUT", "/api/scheduling/pools", {"pools": original})
    text = table(results)
    (OUT / f"{args.round}_table.md").write_text(text + "\n")
    print(text)
    return 0


def summary(rows: list[dict]) -> dict:
    """The numbers of one lane of one round: passes, abnormal runs, prompt sizes, failed tools, time."""
    ms = [r.get("metrics") or {} for r in rows]
    calls = [m for m in ms if m.get("calls")]
    return {
        "pass": f"{sum(1 for r in rows if r['pass'])}/{len(rows)}",
        "runs": sum(m.get("runs") or 0 for m in ms),
        "abnormal": sum(len(m.get("stops") or []) for m in ms),
        "calls": sum(m.get("calls") or 0 for m in ms),
        "prompt_mean": round(sum(m["prompt_mean"] * m["calls"] for m in calls) / max(sum(m["calls"] for m in calls), 1)),
        "prompt_max": max((m.get("prompt_max") or 0 for m in ms), default=0),
        "prompt_total": sum(m.get("prompt_total") or 0 for m in ms),
        "tools": sum(m.get("tool_calls") or 0 for m in ms),
        "tools_failed": sum(m.get("tool_failed") or 0 for m in ms),
        "seconds": round(sum(r.get("seconds") or 0 for r in rows)),
    }


def compare(rounds: list[str]) -> str:
    """One line per round and lane, from the stored results."""
    head = "| round | lane | pass | runs | abnormal | calls | prompt mean | prompt max | prompt total | tools (failed) | s |"
    lines = [head, "|" + "---|" * 11]
    for rnd in rounds:
        for f in sorted(OUT.glob(f"{rnd}_*.json")):
            s = summary(json.loads(f.read_text()))
            lane = f.stem[len(rnd) + 1 :]
            lines.append(
                f"| {rnd} | {lane} | {s['pass']} | {s['runs']} | {s['abnormal']} | {s['calls']} | {s['prompt_mean']} | "
                f"{s['prompt_max']} | {s['prompt_total']} | {s['tools']} ({s['tools_failed']}) | {s['seconds']} |"
            )
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "compare":
        print(compare(sys.argv[2:]))
        raise SystemExit(0)
    raise SystemExit(main())
