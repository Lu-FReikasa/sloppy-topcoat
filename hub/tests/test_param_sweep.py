"""The parameter sweep runner (scripts/dev/param_sweep.py): scoring, a whole run against a scripted
gateway, compare, and that the shipped example plan and cases are valid."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "param_sweep.py"
spec = importlib.util.spec_from_file_location("param_sweep", SCRIPT)
assert spec and spec.loader
ps = importlib.util.module_from_spec(spec)
sys.modules["param_sweep"] = ps
spec.loader.exec_module(ps)

EXAMPLE = SCRIPT.parent / "eval-example"


def test_the_selftest_passes() -> None:
    assert ps.selftest() == 0


def test_every_check_kind_scores_by_rule() -> None:
    assert ps.score("Ja, klar", {"must": [["yes", "ja"]]})["passed"]
    assert ps.score("nein", {"must": [["yes", "ja"]]})["failed"] == ["must:yes"]
    assert ps.score("das ist rot", {"must_not": ["rot"]})["failed"] == ["must_not:rot"]
    assert ps.score("a b c d", {"max_words": 3})["failed"] == ["max_words:4>3"]
    assert ps.score("a", {"min_words": 3})["failed"] == ["min_words:1<3"]
    assert ps.score("x 42 y", {"regex": r"\b42\b"})["passed"]
    assert ps.score('here: {"a": 1, "b": 2}', {"json": {"keys": ["a", "b"]}})["passed"]
    assert ps.score("no json", {"json": {}})["failed"] == ["json:not an object"]
    assert ps.score("   ", {"min_words": 0})["failed"] == ["empty"]


def test_the_summary_counts_errors_cut_offs_and_tags() -> None:
    rows = [
        {
            "model": "m",
            "variant": "v",
            "case": "a",
            "rep": 0,
            "tags": ["t"],
            "passed": True,
            "seconds": 2.0,
            "tps": 10.0,
            "completion_tokens": 20,
            "finish": "stop",
        },
        {
            "model": "m",
            "variant": "v",
            "case": "b",
            "rep": 0,
            "tags": ["t"],
            "passed": False,
            "seconds": 4.0,
            "tps": 20.0,
            "completion_tokens": 80,
            "finish": "length",
        },
        {
            "model": "m",
            "variant": "v",
            "case": "c",
            "rep": 0,
            "tags": [],
            "passed": False,
            "seconds": 1.0,
            "error": "boom",
        },
    ]
    s = ps.summarize(rows)["m | v"]
    assert s["runs"] == 3 and s["errors"] == 1 and s["cut_off"] == 1
    assert s["pass_rate"] == pytest.approx(0.333, abs=0.001)
    assert s["median_seconds"] == 3.0 and s["mean_tps"] == 15.0 and s["by_tag"] == {"t": 0.5}


def test_compare_names_the_change_and_the_regressions() -> None:
    old = {
        "label": "o",
        "timestamp": "t",
        "summary": {"m | v": {"pass_rate": 1.0}},
        "rows": [{"model": "m", "variant": "v", "case": "a", "rep": 0, "passed": True}],
    }
    new = {
        "label": "n",
        "timestamp": "t",
        "summary": {"m | v": {"pass_rate": 0.5}, "m | w": {}},
        "rows": [{"model": "m", "variant": "v", "case": "a", "rep": 0, "passed": False}],
    }
    text = "\n".join(ps.compare(old, new))
    assert "pass_rate 1.0 -> 0.5" in text and "m | w: only in new" in text
    assert "regression: m | v | a#0" in text


def test_the_shipped_example_plan_and_cases_are_valid() -> None:
    plan = ps.load_plan(EXAMPLE / "sweep-example.json")
    assert [v["name"] for v in plan["variants"]] == ["base", "nothink"]
    ids = [c["id"] for c in plan["_cases"]]
    assert len(ids) == len(set(ids)) >= 10
    for case in plan["_cases"]:  # every check is something score() understands
        assert ps.score("", case["checks"])["failed"]  # an empty answer never passes
    json.dumps(plan["_cases"])


def test_a_plan_with_equal_variant_names_is_refused(tmp_path: Path) -> None:
    (tmp_path / "c.json").write_text(
        '{"cases":[{"id":"a","question":"q","checks":{"must":["x"]}}]}'
    )
    (tmp_path / "p.json").write_text(
        '{"cases":"c.json","models":["m"],"variants":[{"name":"a"},{"name":"a"}]}'
    )
    with pytest.raises(SystemExit):
        ps.load_plan(tmp_path / "p.json")
