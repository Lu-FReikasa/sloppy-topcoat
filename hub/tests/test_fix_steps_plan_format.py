"""The plan format after measuring run 1.19 (2026-11-27): ``needs`` for the steps a step builds on
(#237), ``where: "new"`` with ``near`` for code that does not exist yet (#238), and a dotted name
that is the end of exactly one symbol's name (#239)."""

import json
from typing import Any

from topcoat_hub import fix_steps as fs

PATH = "hub/src/topcoat_hub/x.py"
CODE = (
    "import re\n\n\n"
    "def parse(text):\n    return text.strip()\n\n\n"
    "class BlockService:\n    def ask(self, text):\n        return parse(text)\n\n\n"
    "class HubBlockPorts:\n    def run(self):\n        return 1\n"
)


def plan(*steps: dict[str, Any]) -> str:
    return json.dumps(
        {
            "summary": "s",
            "steps": [{"path": PATH, "do": "change it", **s} for s in steps],
            "test": {"path": "hub/tests/test_x_new.py", "do": "it fails today"},
        }
    )


def settled(text: str) -> tuple[fs.Plan, list[str]]:
    p, problems = fs.parse_plan(text)
    assert p is not None, problems
    return p, fs.plan_problems(p, {PATH: CODE, "hub/tests/test_x_new.py": None})


def test_needs_names_earlier_steps_and_after_still_works() -> None:
    for key in ("needs", "after"):
        p, problems = settled(plan({"symbol": "parse"}, {"symbol": "BlockService.ask", key: [1]}))
        assert problems == [] and p.steps[1].after == [1], key


def test_a_line_number_in_needs_is_dropped_but_a_later_step_is_still_refused() -> None:
    # live 1.19 (2.16, 2.23): line numbers 482-490 and 46/397 refused the whole plan
    p, problems = settled(plan({"symbol": "parse", "needs": [482, 490]}, {"symbol": "parse"}))
    assert problems == [] and p.steps[0].after == []
    p2, problems2 = fs.parse_plan(plan({"symbol": "parse", "needs": [2]}, {"symbol": "parse"}))
    assert p2 is None and any("comes later" in x for x in problems2)


def test_new_code_goes_after_near_or_at_the_end() -> None:
    # live 1.19 (3.3.6): the new function itself was the anchor
    p, problems = settled(
        plan(
            {"symbol": "_obsolete_pages", "where": "new", "near": "parse"},
            {"symbol": "_other_new", "where": "new"},
            {"symbol": "parse", "where": "new"},
        )
    )
    assert problems == []
    first, second, third = p.steps
    assert (first.symbol, first.where, first.anchor) == ("parse", "after", "def parse(text):")
    assert (second.symbol, second.where) == ("", "end")
    assert (third.symbol, third.where) == ("parse", "after")
    assert fs.where_text(first).startswith("new code right after")


def test_new_code_near_a_symbol_that_is_not_there_is_refused_with_names() -> None:
    _, problems = settled(plan({"symbol": "x_new", "where": "new", "near": "nope"}))
    assert problems and "no symbol 'nope'" in problems[0]


def test_a_dotted_name_that_ends_exactly_one_symbol_is_that_symbol() -> None:
    # live 1.19 (2.18): `Service.ask` for `BlockService.ask`
    p, problems = settled(plan({"symbol": "Service.ask"}))
    assert problems == [] and p.steps[0].symbol == "BlockService.ask"
    # another class with the same method name is no match: `Ports.ask` stays refused
    _, problems = settled(plan({"symbol": "Ports.ask"}))
    assert problems and "nearest names" in problems[0]
