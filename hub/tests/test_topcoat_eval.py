"""The evaluation suite for a running hub (scripts/dev/topcoat_eval.py): scoring, review-case
scoring, compare, the safety rules, and the whole run against a scripted hub (no network)."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "topcoat_eval.py"
spec = importlib.util.spec_from_file_location("topcoat_eval", SCRIPT)
assert spec and spec.loader
ev = importlib.util.module_from_spec(spec)
sys.modules["topcoat_eval"] = ev
spec.loader.exec_module(ev)

EXAMPLE = SCRIPT.parent / "eval-example"
KIT = ev.load_review_kit()
FALLBACK = ev.load_review_kit(prefer_hub=False)


def load(name: str, key: str, validate: Any) -> Any:
    return ev.load_section(str(EXAMPLE / name), key, name, validate)


# --- scoring -------------------------------------------------------------------------------------


def test_score_answer_items_alternatives_and_forbidden_strings() -> None:
    assert ev.score_answer("It has 24 GiB of VRAM", ["24", ["GB", "GiB"]])["passed"]
    assert ev.score_answer("it HAS 24   gb", ["24", ["GB", "GiB"]])["passed"]
    r = ev.score_answer("It has 24 of them", ["24", ["GB", "GiB"]])
    assert not r["passed"] and r["missing"] == ["GB | GiB"]
    bad = "0.0." + "0.0"  # an address string to look for, not a bind
    r = ev.score_answer(f"The port is 8443, not {bad}", ["8443"], [bad])
    assert not r["passed"] and r["forbidden"] == [bad]


def test_an_empty_answer_never_passes() -> None:
    assert not ev.score_answer("", [])["passed"]
    assert not ev.score_answer("  ", ["x"], [])["passed"]


def test_input_validation_names_the_problem() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        ev.validate_facts({"questions": [{"id": "a", "question": "q", "must": ["x"]}] * 2})
    with pytest.raises(ValueError, match="must"):
        ev.validate_facts({"questions": [{"id": "a", "question": "q", "must": [3]}]})
    with pytest.raises(ValueError, match="contradicting note"):
        ev.validate_review_cases([{"id": "c", "answer": "x", "notes": ["n"], "expected": "error"}])
    with pytest.raises(ValueError, match="needs"):
        ev.validate_scenarios([{"id": "s", "steps": [{"expect": {}}]}])


def test_the_shipped_example_data_is_valid_and_has_synthetic_facts_only() -> None:
    load("facts.json", "questions", ev.validate_facts)
    cases = load("review_cases.json", "review_cases", ev.validate_review_cases)
    load("memory_scenarios.json", "memory_scenarios", ev.validate_scenarios)
    assert {"incomplete_notes", "contradicted", "matching"} <= {c["kind"] for c in cases}
    text = "".join(p.read_text() for p in EXAMPLE.glob("*.json"))
    import re

    assert not re.search(
        r"\b(?!10\.0\.0\.)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
        text.replace("127.0.0.1", "").replace("0.0." + "0.0", ""),
    )


# --- review cases --------------------------------------------------------------------------------


def _reply(*findings: dict[str, Any]) -> str:
    return json.dumps({"findings": list(findings)})


def _case(cid: str) -> dict[str, Any]:
    cases = load("review_cases.json", "review_cases", ev.validate_review_cases)
    return next(c for c in cases if c["id"] == cid)


def test_correct_answer_with_incomplete_notes_must_be_judged_ok() -> None:
    case = _case("incomplete-notes-bind")
    # the production failure: "not mentioned in the notes" taken as a contradiction
    wrong = _reply(
        {
            "claim": "binds to 127.0.0.1",
            "reason": "not mentioned",
            "note": "N1",
            "severity": "major",
        }
    )
    details, m = ev.score_review(case, wrong, KIT)
    assert details["passed"] is False and m["false_alarm"] and m["verdict"] == "error"
    assert m["major_with_note"] == 1 and m["major_unsupported"] == 1
    details, m = ev.score_review(case, _reply(), KIT)
    assert details["passed"] and not m["false_alarm"]


def test_wrong_answer_with_a_contradicting_note_must_be_an_error_quoting_the_claim() -> None:
    case = _case("contradicted-vram")
    good = _reply(
        {
            "claim": "node-amd has 8 GB of VRAM",
            "reason": "N2 says 16",
            "note": "N2",
            "severity": "major",
        }
    )
    details, m = ev.score_review(case, good, KIT)
    assert details["passed"] and m["hit"] and m["major_unsupported"] == 0
    # right verdict, but the evidence is the wrong note: counted as an unsupported major finding
    other = _reply({"claim": "8 GB", "reason": "x", "note": "N1", "severity": "major"})
    details, m = ev.score_review(case, other, KIT)
    assert details["passed"] and m["major_unsupported"] == 1
    # no evidence at all is only a hint
    details, m = ev.score_review(case, _reply({"claim": "8 GB", "reason": "x", "note": None}), KIT)
    assert not details["passed"] and m["verdict"] == "hint" and not m["hit"]
    # the verdict is right but the finding is about something else
    details, _ = ev.score_review(
        case, _reply({"claim": "swap", "reason": "x", "note": "N2", "severity": "major"}), KIT
    )
    assert not details["passed"] and not details["claim_found"]


def test_correct_answer_with_a_matching_note_is_ok_and_garbage_is_unparseable() -> None:
    case = _case("matching-vram")
    assert ev.score_review(case, _reply(), KIT)[0]["passed"]
    details, m = ev.score_review(case, "I think the answer is fine.", KIT)
    assert not details["passed"] and m["unparseable"]


def test_a_finding_keeps_its_note_only_if_the_second_question_confirms_it() -> None:
    case = _case("incomplete-notes-bind")
    wrong = _reply(
        {
            "claim": "binds to 127.0.0.1",
            "reason": "not mentioned",
            "note": "N1",
            "severity": "major",
        }
    )
    asked: list[tuple[str, str]] = []

    def unrelated(claim: str, note: str) -> bool:
        asked.append((claim, note))
        return False

    # "not in the notes" is no contradiction: the evidence goes, the finding stays as a hint
    details, m = ev.score_review(case, wrong, KIT, unrelated)
    assert asked == [("binds to 127.0.0.1", "node-amd has an AMD GPU with 16 GB VRAM.")]
    assert m["verdict"] == "hint" and not m["false_error"] and m["false_alarm"] and m["hint"]
    # a hint cannot make the agent speak up: a correct answer still passes, the hint is counted
    assert m["major_with_note"] == 0 and details["passed"]
    # a note that really contradicts keeps its evidence
    real = _case("contradicted-vram")
    good = _reply({"claim": "8 GB", "reason": "x", "note": "N2", "severity": "major"})
    details, m = ev.score_review(real, good, KIT, lambda c, n: True)
    assert details["passed"] and m["hit"]
    # without the second question the first look stands alone
    _, m = ev.score_review(case, wrong, KIT)
    assert m["verdict"] == "error" and m["false_error"]


def test_part_b_asks_the_second_question_and_needs_a_named_model(fake: Any, tmp_path: Path) -> None:
    hub = fake()
    out = tmp_path / "r.json"
    assert ev.cmd_run_quiet(_args(hub, out, parts="B", review_model=None)) == 1
    assert "pass --review-model" in json.loads(out.read_text())["aborted"]
    assert ev.cmd_run_quiet(_args(hub, out, parts="B", review_model="fake-model@node-x")) == 0
    res = json.loads(out.read_text())
    assert res["summary"]["B"]["false_error_rate"] == 0.0
    assert res["summary"]["B"]["pass_rate"] == 1.0


def test_the_fallback_copy_agrees_with_the_hubs_review_functions() -> None:
    if KIT.source != "hub":
        pytest.skip("hub package not importable")
    assert FALLBACK.system == KIT.system
    assert FALLBACK.confirm_system == KIT.confirm_system
    notes = [(__import__("uuid").uuid4(), "node-amd has 16 GB of VRAM.")]
    samples = [
        _reply({"claim": "8 GB", "reason": "r", "note": "N1", "severity": "major"}),
        _reply({"claim": " spaced   out ", "reason": "r", "note": "N9", "severity": "bogus"}),
        "Here you go: " + _reply({"claim": "c", "reason": "r", "note": None}) + " done",
        "no json at all",
        '{"findings": "nope"}',
    ]
    for s in samples:
        a, b = KIT.parse_findings(s, notes), FALLBACK.parse_findings(s, notes)
        assert a == b
        assert KIT.verdict_of(a) == FALLBACK.verdict_of(b)


def test_summary_rates_for_the_review_part() -> None:
    def case(
        cid: str, exp: str, verdict: str, kind: str, unsup: int = 0, with_note: int = 0
    ) -> dict:
        return {
            "id": cid,
            "part": "B",
            "passed": exp == verdict,
            "latency_ms": 10,
            "metrics": {
                "kind": kind,
                "expected": exp,
                "verdict": verdict,
                "false_alarm": exp == "ok" and verdict != "ok",
                "hit": exp != "ok" and exp == verdict,
                "major_with_note": with_note,
                "major_unsupported": unsup,
                "unparseable": False,
            },
        }

    s = ev.summarize(
        [
            case("a", "ok", "error", "incomplete_notes", 1, 1),
            case("b", "ok", "ok", "matching"),
            case("c", "error", "error", "contradicted", 0, 1),
            case("d", "error", "hint", "contradicted"),
        ]
    )["B"]
    assert s["false_alarm_rate"] == 0.5 and s["hit_rate"] == 0.5
    assert s["major_unsupported_share"] == 0.5
    assert s["by_kind"]["matching"] == 1.0 and s["by_kind"]["incomplete_notes"] == 0.0


# --- memory checks -------------------------------------------------------------------------------


def _note(nid: str, text: str, status: str = "active", evidence: str = "doc") -> dict[str, Any]:
    return {"id": nid, "text": text, "status": status, "evidence": evidence}


def test_contradiction_check_tells_a_question_from_a_silent_overwrite() -> None:
    expect = {"contradiction": {"old": ["7421"], "new": ["7422"], "outcome": "clarify"}}
    silent = [_note("1", "port 7421", "superseded"), _note("2", "port 7422")]
    [c] = ev.evaluate_step(expect, silent, [], ev.Turn(), set(), [])
    assert not c["passed"] and "silent overwrite" in c["detail"]
    disputed = [_note("1", "port 7421", "disputed"), _note("2", "port 7422", "disputed")]
    assert ev.evaluate_step(expect, disputed, [], ev.Turn(), set(), [])[0]["passed"]
    q = [{"id": "q", "candidates": [{"text": "port 7421"}, {"text": "port 7422"}]}]
    assert ev.evaluate_step(expect, silent, q, ev.Turn(), set(), [])[0]["passed"]
    asked = ev.Turn(memory={"questions": 1})
    assert ev.evaluate_step(expect, silent, [], asked, set(), [])[0]["passed"]


def test_note_evidence_forgotten_and_deleted_note_not_used() -> None:
    notes = [_note("1", "room B-17", "deleted"), _note("2", "other", evidence="bogus")]
    out = ev.evaluate_step({"note": {"contains": ["other"]}}, notes, [], None, set(), [])
    assert [c["check"] for c in out] == ["note_stored", "evidence_value"]
    assert out[0]["passed"] and not out[1]["passed"]
    gone: set[str] = set()
    before = [_note("1", "room B-17")]
    expect = {"forgotten": ["B-17"], "answer": {"must_not": ["B-17"]}}
    turn = ev.Turn(message={"content": "no idea", "memory_refs": [{"id": "1"}]})
    out = ev.evaluate_step(expect, notes[:1], [], turn, gone, before)
    names = {c["check"]: c["passed"] for c in out}
    assert names == {"forgotten": True, "answer_text": True, "deleted_note_not_used": False}
    assert gone == {"1"}


# --- compare -------------------------------------------------------------------------------------


def _results(label: str, passed: dict[str, bool]) -> dict[str, Any]:
    cases = [
        {"id": k, "part": "A", "passed": v, "latency_ms": 100, "summary": "s"}
        for k, v in passed.items()
    ]
    return {
        "schema_version": 1,
        "label": label,
        "timestamp": "t",
        "cases": cases,
        "summary": ev.summarize(cases),
        "models_seen": ["m"],
        "nodes_seen": ["n"],
    }


def test_compare_lists_regressions_first_and_computes_deltas() -> None:
    old = _results("old", {"a": True, "b": False, "c": True, "gone": True})
    new = _results("new", {"a": False, "b": True, "c": True, "fresh": True})
    c = ev.compare(old, new)
    assert c["regressions"] == ["a|A"] and c["improvements"] == ["b|A"]
    assert c["added"] == ["fresh|A"] and c["removed"] == ["gone|A"]
    assert c["parts"]["A"]["pass_rate"]["delta"] == pytest.approx(0.0, abs=0.3)
    text = ev.render_compare(c)
    assert text.index("REGRESSIONS (1)") < text.index("improvements (1)")


def test_compare_command_and_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "o.json").write_text(json.dumps(_results("old", {"a": True})))
    (tmp_path / "n.json").write_text(json.dumps(_results("new", {"a": False})))
    argv = ["compare", str(tmp_path / "o.json"), str(tmp_path / "n.json")]
    assert ev.main(argv) == 0
    assert ev.main([*argv, "--fail-on-regression"]) == 1
    assert "REGRESSIONS (1)" in capsys.readouterr().out


# --- the whole thing against a scripted hub ------------------------------------------------------


def test_selftest_passes_offline() -> None:
    assert ev.selftest() == 0


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch):
    made = []

    def make(**kw: Any) -> Any:
        f = ev.FakeHub(
            load("facts.json", "questions", ev.validate_facts),
            load("review_cases.json", "review_cases", ev.validate_review_cases),
            load("memory_scenarios.json", "memory_scenarios", ev.validate_scenarios),
            **kw,
        )
        made.append(f)
        monkeypatch.setenv("EVAL_TEST_KEY", f.key)
        return f

    yield make
    for f in made:
        f.close()


def _args(fake_hub: Any, out: Path, **over: Any) -> Any:
    opts: dict[str, Any] = {
        "hub": fake_hub.url,
        "key_env": "EVAL_TEST_KEY",
        "facts": str(EXAMPLE / "facts.json"),
        "out": str(out),
        "allow_http": True,
        "test_user_confirmed": True,
        "parts": "A,B,C",
    }
    return ev.default_args(**{**opts, **over})


def test_a_full_run_writes_the_results_schema_and_never_the_key(fake: Any, tmp_path: Path) -> None:
    hub = fake()
    out = tmp_path / "r.json"
    assert ev.cmd_run_quiet(_args(hub, out, label="sample")) == 0
    text = out.read_text()
    res = json.loads(text)
    assert hub.key not in text and "EVAL_TEST_KEY" not in text
    assert res["schema_version"] == 1 and res["label"] == "sample" and res["timestamp"]
    assert res["hub"]["image_tag"] == "selftest" and res["hub"]["version"] == "0.0-selftest"
    assert res["models_seen"] == ["fake-model"] and res["nodes_seen"] == ["node-fake"]
    assert set(res["summary"]) == {"A", "B", "C"}
    for c in res["cases"]:
        assert {"id", "part", "passed", "details", "latency_ms"} <= set(c)
    assert res["summary"]["B"]["false_alarm_rate"] == 0.0 and res["summary"]["B"]["hit_rate"] == 1.0
    assert res["cleanup"]["errors"] == 0 and not hub.convs


def test_cleanup_removes_only_what_the_run_created(fake: Any, tmp_path: Path) -> None:
    hub = fake()
    keep = hub.add_note("old-conv", "an older note of the test user", "doc")
    hub.convs["old-conv"] = {"id": "old-conv", "title": "my own chat"}
    assert ev.cmd_run_quiet(_args(hub, tmp_path / "r.json", parts="C")) == 0
    assert hub.notes[keep["id"]]["status"] == "active" and "old-conv" in hub.convs
    assert all(n["status"] == "deleted" for k, n in hub.notes.items() if k != keep["id"])


def test_refuses_an_admin_key_unless_allowed(fake: Any, tmp_path: Path) -> None:
    hub = fake(role="admin")
    assert ev.cmd_run_quiet(_args(hub, tmp_path / "r.json", parts="B")) == 2
    assert not (tmp_path / "r.json").exists()
    assert not any("/v1/chat" in line or "/conversations" in line for line in hub.log)
    assert ev.cmd_run_quiet(_args(hub, tmp_path / "r.json", parts="B", allow_admin=True)) == 0


def test_parts_a_and_c_need_the_test_user_confirmation(fake: Any, tmp_path: Path) -> None:
    hub = fake()
    out = tmp_path / "r.json"
    assert ev.cmd_run_quiet(_args(hub, out, test_user_confirmed=False)) == 2
    assert hub.log == [] and not out.exists()
    assert ev.cmd_run_quiet(_args(hub, out, parts="B", test_user_confirmed=False)) == 0


def test_plain_http_and_missing_key_are_refused(
    fake: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = fake()
    assert ev.cmd_run_quiet(_args(hub, tmp_path / "r.json", allow_http=False)) == 2
    monkeypatch.delenv("EVAL_TEST_KEY")
    assert ev.cmd_run_quiet(_args(hub, tmp_path / "r.json")) == 2
    assert hub.log == []


def test_max_cases_is_a_hard_limit_and_the_pause_is_kept(fake: Any, tmp_path: Path) -> None:
    hub = fake()
    out = tmp_path / "r.json"
    started = time.monotonic()
    assert ev.cmd_run_quiet(_args(hub, out, parts="A,B", max_cases=3, pause=0.1)) == 0
    res = json.loads(out.read_text())
    assert len(res["cases"]) == 3 and res["skipped_by_max_cases"] >= 1
    assert time.monotonic() - started >= 0.2


def test_ctrl_c_still_writes_partial_results_and_cleans_up(
    fake: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = fake()
    real = ev.part_c

    def interrupted(run: Any, scenarios: list[dict[str, Any]]) -> None:
        real(run, scenarios[:1])
        raise KeyboardInterrupt

    monkeypatch.setattr(ev, "part_c", interrupted)
    out = tmp_path / "r.json"
    assert ev.cmd_run_quiet(_args(hub, out, parts="B,C")) == 130
    res = json.loads(out.read_text())
    assert "Ctrl-C" in res["aborted"] and {c["part"] for c in res["cases"]} == {"B", "C"}
    assert not hub.convs


def test_every_request_has_a_timeout() -> None:
    class Slow(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            time.sleep(1.0)

        def log_message(self, *a: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        hub = ev.Hub(f"http://127.0.0.1:{server.server_port}", "k", timeout=0.2)
        with pytest.raises(ev.HubError):
            hub.get("/api/health")
        with pytest.raises(ValueError, match="relative"):
            hub.get("http://elsewhere.invalid/x")
    finally:
        server.shutdown()


def test_a_dead_hub_stops_a_part_after_three_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EVAL_TEST_KEY", "k")
    run_args = ev.default_args(
        hub="http://127.0.0.1:9",
        key_env="EVAL_TEST_KEY",
        out=str(tmp_path / "r.json"),
        allow_http=True,
        parts="B",
        model="m",
    )
    # preflight already fails against a closed port: results say so
    assert ev.cmd_run_quiet(run_args) == 1
    assert "preflight" in json.loads((tmp_path / "r.json").read_text())["aborted"]


# --- part D --------------------------------------------------------------------------------------


def test_agent_tasks_run_through_the_agent_bench_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": "c",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }

    steps: Iterator[dict[str, Any]] = iter(
        [
            {
                "tool_calls": [
                    call(
                        "replace_in_file",
                        {
                            "path": "pager/paginate.py",
                            "old": "page * size",
                            "new": "(page - 1) * size",
                        },
                    )
                ]
            },
            {"content": "Fixed."},
        ]
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._send({"status": "ok", "version": "x", "role": "user"})

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self._send({"choices": [{"message": {"role": "assistant", **next(steps)}}]})

        def _send(self, obj: Any) -> None:
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("EVAL_TEST_KEY", "k")
    try:
        args = ev.default_args(
            hub=f"http://127.0.0.1:{server.server_port}",
            key_env="EVAL_TEST_KEY",
            out=str(tmp_path / "r.json"),
            allow_http=True,
            parts="D",
            model="fake",
            agent_tasks="off-by-one",
            max_turns=6,
        )
        assert ev.cmd_run_quiet(args) == 0
    finally:
        server.shutdown()
    res = json.loads((tmp_path / "r.json").read_text())
    [case] = res["cases"]
    assert case["id"] == "agent-off-by-one" and case["part"] == "D" and case["passed"]
    assert res["summary"]["D"]["pass_rate"] == 1.0
