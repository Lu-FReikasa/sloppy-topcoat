"""Block programs (PRG-00 R3): validator, pick evaluator and interpreter with fake ports."""

import asyncio
import json
from collections.abc import Callable
from typing import Any, ClassVar

import pytest

from topcoat_hub.blocks import builtin, pick, program
from topcoat_hub.blocks.engine import (
    TOOL_TEXT_LIMIT,
    AgentOutcome,
    BlockEngine,
    BlockFailure,
    ToolOutcome,
)
from topcoat_hub.blocks.program import ProgramError

# --- the pick evaluator -----------------------------------------------------------------------

POOL = [
    pick.Candidate("big", "node-a", size_mb=20000, tps=30, loaded=True),
    pick.Candidate("small", "node-a", size_mb=2000, tps=120),
    pick.Candidate("small", "node-b", size_mb=2000, tps=60, loaded=True),
    pick.Candidate("bge-m3", "node-b", kinds=("embedding",)),
]


def test_a_fixed_pick_is_taken_as_written() -> None:
    got = pick.evaluate({"fixed": "x@node-z"}, POOL)
    assert got and (got.model, got.node) == ("x", "node-z")


def test_the_pool_is_filtered_by_kind_and_node() -> None:
    assert pick.evaluate({"kind": "embedding"}, POOL).model == "bge-m3"  # type: ignore[union-attr]
    got = pick.evaluate({"node": "node-b"}, POOL)
    assert got and got.node == "node-b" and got.model == "small"
    assert pick.evaluate({"kind": "tts"}, POOL) is None


def test_ranks() -> None:
    assert pick.evaluate({"rank": "fastest"}, POOL).model == "small"  # type: ignore[union-attr]
    assert pick.evaluate({"rank": "largest"}, POOL).model == "big"  # type: ignore[union-attr]
    got = pick.evaluate({"rank": "loaded"}, POOL)
    assert got and got.model == "small"  # big and small@node-b are loaded; the faster one wins
    assert pick.evaluate({"rank": "fastest"}, POOL).node == "node-a"  # type: ignore[union-attr]


def test_min_tps_drops_slow_models() -> None:
    got = pick.evaluate({"rank": "largest", "min_tps": 50}, POOL)
    assert got and got.model == "small"


def test_other_than_avoids_the_author_and_says_so_when_there_is_no_other() -> None:
    got = pick.evaluate({"rank": "largest"}, POOL, avoid={"big"})
    assert got and got.model == "small" and not got.note
    only_big = [c for c in POOL if c.model == "big"]
    got = pick.evaluate({}, only_big, avoid={"big"})
    assert got and got.model == "big" and "not independent" in got.note


def test_same_as_names_the_author() -> None:
    got = pick.evaluate({}, POOL, same_as="small@node-a")
    assert got and got.model == "small" and got.node == "node-b"  # the loaded one


@pytest.mark.parametrize(
    "bad",
    [{"rank": "tastiest"}, {"colour": 1}, {"other_than": "x"}, {"min_tps": "fast"}, {"fixed": ""}],
)
def test_a_pick_that_makes_no_sense_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(pick.PickError):
        pick.check_spec(bad, "x")


# --- the validator ----------------------------------------------------------------------------


def prog(*stack: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"name": "t", "inputs": {"question": "text"}, "stack": list(stack), **extra}


ASK = {"b": "ask", "prompt": "{{question}}", "out": "a"}
CHECK = {"b": "check", "in": "a", "out": "v"}


def test_the_shipped_programs_are_valid() -> None:
    for p in builtin.PROGRAMS.values():
        assert program.validate(p)["name"] == p["name"]


def test_validate_fills_defaults_and_leaves_the_original_alone() -> None:
    original = prog(ASK, CHECK, {"b": "repeat_until", "cond": "v.ok", "stack": [ASK, CHECK]})
    checked = program.validate(original)
    assert checked["stack"][2]["guard"] == "progress" and checked["stack"][2]["max"] == 0
    assert "guard" not in original["stack"][2]


@pytest.mark.parametrize(
    ("stack", "message"),
    [
        ([{"b": "nope"}], "unknown block"),
        ([{"b": "ask", "prompt": "{{ghost}}", "out": "a"}], "ghost"),
        ([ASK, {"b": "check", "in": "missing", "out": "v"}], "not defined"),
        ([ASK, CHECK, {"b": "if", "cond": "a", "then": []}], "bool"),
        ([ASK, CHECK, {"b": "if", "cond": "v.agree", "then": []}], "v.ok"),
        ([ASK, {"b": "repeat_until", "cond": "a", "stack": [ASK]}], "bool"),
        (
            [ASK, CHECK, {"b": "repeat_until", "cond": "v.ok", "guard": "never", "stack": [ASK]}],
            "guard",
        ),
        ([ASK, CHECK, {"b": "repeat_until", "cond": "v.ok", "stack": [{"b": "stop"}]}], "write"),
        (
            [
                {
                    "b": "parallel",
                    "lanes": [
                        [{"b": "ask", "prompt": "x", "out": "same"}],
                        [{"b": "ask", "prompt": "y", "out": "same"}],
                    ],
                }
            ],
            "both write",
        ),
        ([{"b": "parallel", "lanes": [[ASK]]}], "two to eight"),
        (
            [ASK, {"b": "ask", "prompt": "x", "out": "b", "pick": {"other_than": ["ghost"]}}],
            "ghost",
        ),
        ([ASK, {"b": "compare", "in": ["a"], "out": "c"}], "two or three"),
        ([ASK, CHECK, {"b": "compare", "in": ["a", "v"], "out": "c"}], "texts with texts"),
        ([{"b": "ask", "prompt": "x", "out": "1bad"}], "variable name"),
        ([{"b": "ask", "prompt": "x", "out": "a", "colour": 1}], "no field"),
        ([{"b": "agent", "prompt": "x", "out": "a", "depth": "bottomless"}], "depth"),
    ],
)
def test_an_illegal_program_is_refused_with_a_reason(
    stack: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(ProgramError, match=message):
        program.validate(prog(*stack))


def test_a_variable_defined_in_one_branch_only_does_not_exist_afterwards() -> None:
    branch = {"b": "if", "cond": "v.ok", "then": [{"b": "set", "out": "x", "value": "1"}]}
    with pytest.raises(ProgramError, match="not defined"):
        program.validate(prog(ASK, CHECK, branch, {"b": "set", "out": "y", "value": "{{x}}"}))
    both = {**branch, "else": [{"b": "set", "out": "x", "value": "2"}]}
    program.validate(prog(ASK, CHECK, both, {"b": "set", "out": "y", "value": "{{x}}"}))


def test_a_program_needs_a_name_an_input_and_a_block() -> None:
    for bad in (
        {"inputs": {"q": "text"}, "stack": [ASK]},
        {"name": "x", "inputs": {}, "stack": [ASK]},
        {"name": "x", "inputs": {"q": "text"}, "stack": []},
        {"name": "x", "inputs": {"q": "number"}, "stack": [ASK]},
    ):
        with pytest.raises(ProgramError):
            program.validate(bad)


def test_the_depth_of_nesting_is_bounded() -> None:
    block: dict[str, Any] = {"b": "set", "out": "x", "value": "1"}
    for _ in range(program.MAX_DEPTH + 2):
        block = {"b": "if", "cond": "flag", "then": [block]}
    with pytest.raises(ProgramError, match="deeply"):
        program.validate({"name": "t", "inputs": {"flag": "bool"}, "stack": [block]})


# --- the interpreter, with fake ports -----------------------------------------------------------


class FakePorts:
    """Scripted models: ``reply(model, messages)`` decides what a model says."""

    def __init__(
        self,
        reply: Callable[[str, list[dict[str, str]]], str],
        pool: list[pick.Candidate] | None = None,
        agent_text: str = "AGENT TEXT",
    ) -> None:
        self.reply = reply
        self.pool = pool if pool is not None else POOL
        self.agent_text = agent_text
        self.agent_hooks: list[dict[str, Any] | None] = []
        self.asked: list[tuple[str, list[dict[str, str]]]] = []
        self.thinks: list[bool | None] = []  # the ``think`` each call was made with
        self.filed: list[tuple[str, str]] = []
        self.running = 0
        self.peak = 0
        self.delay = 0.0

    async def ask(self, messages, picked, max_tokens, temperature, think=None, schema=None):  # type: ignore[no-untyped-def]
        model = picked.model if picked else "default"
        self.asked.append((model, messages))
        self.thinks.append(think)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return self.reply(model, messages), model
        finally:
            self.running -= 1

    async def agent(self, prompt, application, depth, picked, hooks=None, progress=None):  # type: ignore[no-untyped-def]
        self.agent_hooks.append(hooks)
        model = picked.model if picked else "agent-model"
        if progress is not None:  # what the live view shows while the agent works
            step = {"kind": "call", "capability": "search", "node": "node-a"}
            progress({"what": "tool_step", "data": step})
        return AgentOutcome(True, f"{self.agent_text} for {prompt}", model=model)

    async def candidates(self) -> list[pick.Candidate]:
        return self.pool

    async def file_report(self, title: str, body: str) -> str | None:
        self.filed.append((title, body))
        return f"report-{len(self.filed)}"

    tools: list[tuple[str, dict[str, Any], str | None, str]]

    async def tool(self, capability, tool_input, node, application):  # type: ignore[no-untyped-def]
        if not hasattr(self, "tools"):
            self.tools = []
        self.tools.append((capability, tool_input, node, application))
        if capability == "x.refused":
            raise BlockFailure("x.refused is not offered here")
        text = json.dumps({"capability": capability, "input": tool_input})
        return ToolOutcome(text, node or "tools-a", {"capability": capability})


def verdict_json(ok: bool, *issues: str) -> str:
    return json.dumps({"ok": ok, "issues": list(issues)})


def run(ports: FakePorts, p: dict[str, Any], question: str = "why?") -> Any:
    return asyncio.run(BlockEngine(ports, seed=1).run(p, {"question": question}))


def test_ask_writes_its_variable_and_remembers_the_author() -> None:
    ports = FakePorts(lambda m, msgs: "an answer")
    result = run(ports, prog({**ASK, "pick": {"rank": "largest"}}))
    assert result.status == "ok" and result.vars["a"] == "an answer"
    assert result.authors["a"] == "big"
    assert ports.asked[0][1][-1]["content"] == "why?"  # the template was filled


def test_the_check_is_done_by_another_model_than_the_author() -> None:
    ports = FakePorts(
        lambda m, msgs: verdict_json(True) if "Question" in msgs[-1]["content"] else "text"
    )
    result = run(
        ports, prog({**ASK, "pick": {"rank": "largest"}}, {**CHECK, "pick": {"other_than": ["a"]}})
    )
    assert result.vars["v"]["ok"] is True
    assert [m for m, _ in ports.asked] == ["big", "small"]


def test_ok_with_issues_listed_is_not_ok() -> None:
    ports = FakePorts(
        lambda m, msgs: (
            verdict_json(True, "still wrong") if "Question" in msgs[-1]["content"] else "t"
        )
    )
    result = run(ports, prog(ASK, CHECK))
    assert result.vars["v"] == {
        "ok": False,
        "issues": ["still wrong"],
        "model": result.vars["v"]["model"],
    }


def test_an_unreadable_check_is_tried_twice_then_fails_the_run() -> None:
    ports = FakePorts(lambda m, msgs: "no json here" if "Question" in msgs[-1]["content"] else "t")
    result = run(ports, prog(ASK, CHECK))
    assert result.status == "failed" and "cannot be read" in result.error
    assert sum(1 for _, msgs in ports.asked if "Question" in msgs[-1]["content"]) == 2


LOOP = prog(
    {"b": "agent", "prompt": "{{question}}", "out": "draft"},
    {"b": "check", "in": "draft", "question": "question", "out": "v"},
    {
        "b": "repeat_until",
        "cond": "v.ok",
        "stack": [
            {"b": "revise", "draft": "draft", "issues": "v", "out": "draft"},
            {"b": "check", "in": "draft", "question": "question", "out": "v"},
        ],
    },
    {"b": "file_report", "in": "draft", "title": "{{question}}"},
)


def test_the_revision_loop_ends_when_the_check_is_satisfied() -> None:
    round_no = {"n": 0}

    def reply(model: str, msgs: list[dict[str, str]]) -> str:
        if msgs[0]["content"].startswith("You revise"):
            round_no["n"] += 1
            assert "- the date is wrong" in msgs[-1]["content"]  # the issues reach the reviser
            return f"revised {round_no['n']}"
        # first check fails, the check of the second revision passes
        return (
            verdict_json(True)
            if "revised 2" in msgs[-1]["content"]
            else verdict_json(False, "the date is wrong")
        )

    ports = FakePorts(reply)
    result = run(ports, LOOP)
    assert result.status == "ok" and result.vars["draft"] == "revised 2"
    assert round_no["n"] == 2
    assert ports.filed == [("why?", "revised 2")] and result.filed == ["report-1"]
    assert not result.notes


def test_a_loop_that_makes_no_progress_stops_by_its_guard() -> None:
    def reply(model: str, msgs: list[dict[str, str]]) -> str:
        if msgs[0]["content"].startswith("You revise"):
            return "the same text"
        return verdict_json(False, "never good enough")

    ports = FakePorts(reply)
    result = run(ports, LOOP)
    assert result.status == "ok"
    ends = [s for s in result.steps if s["event"] == "loop_end"]
    assert [e["reason"] for e in ends] == ["stalled"]
    assert ends[0]["rounds"] <= 2  # the second round changed nothing
    assert any("changed nothing" in n for n in result.notes)


def test_an_optional_count_ends_a_loop_that_keeps_changing() -> None:
    counter = {"n": 0}

    def reply(model: str, msgs: list[dict[str, str]]) -> str:
        if msgs[0]["content"].startswith("You revise"):
            counter["n"] += 1
            return f"version {counter['n']}"
        return verdict_json(False, "more")

    looped = json.loads(json.dumps(LOOP))
    looped["stack"][2]["max"] = 3
    result = run(FakePorts(reply), looped)
    assert counter["n"] == 3
    assert [s["reason"] for s in result.steps if s["event"] == "loop_end"] == ["max"]


def test_if_takes_one_branch() -> None:
    ports = FakePorts(
        lambda m, msgs: verdict_json(False, "x") if "Question" in msgs[-1]["content"] else "t"
    )
    branch = {
        "b": "if",
        "cond": "not v.ok",
        "then": [{"b": "set", "out": "r", "value": "failed"}],
        "else": [{"b": "set", "out": "r", "value": "passed"}],
    }
    assert run(ports, prog(ASK, CHECK, branch)).vars["r"] == "failed"


def test_for_each_and_stop() -> None:
    p = {
        "name": "t",
        "inputs": {"items": "texts"},
        "stack": [
            {
                "b": "for_each",
                "in": "items",
                "as": "item",
                "stack": [{"b": "set", "out": "last", "value": "got {{item}}"}],
            },
            {"b": "stop", "reason": "enough"},
            {"b": "set", "out": "never", "value": "x"},
        ],
    }
    result = asyncio.run(BlockEngine(FakePorts(lambda m, s: "x")).run(p, {"items": ["a", "b"]}))
    assert result.vars["last"] == "got b" and "never" not in result.vars
    assert result.status == "ok" and result.reason == "enough"


LANES = prog(
    {
        "b": "parallel",
        "max_parallel": 1,
        "lanes": [
            [{"b": "ask", "prompt": "one", "out": "a"}],
            [{"b": "ask", "prompt": "two", "out": "b"}],
        ],
    }
)


def test_lanes_run_one_after_another_unless_allowed_side_by_side() -> None:
    ports = FakePorts(lambda m, msgs: "x")
    ports.delay = 0.02
    run(ports, LANES)
    assert ports.peak == 1
    wide = json.loads(json.dumps(LANES))
    wide["stack"][0]["max_parallel"] = 2
    ports = FakePorts(lambda m, msgs: "x")
    ports.delay = 0.02
    result = run(ports, wide)
    assert ports.peak == 2 and result.vars["a"] == "x" and result.vars["b"] == "x"


def test_a_failing_lane_fails_the_run_and_stops_the_other() -> None:
    def reply(model: str, msgs: list[dict[str, str]]) -> str:
        if msgs[-1]["content"] == "two":
            raise RuntimeError("node gone")
        return "x"

    wide = json.loads(json.dumps(LANES))
    wide["stack"][0]["max_parallel"] = 2
    result = run(FakePorts(reply), wide)
    assert result.status == "failed" and "node gone" in result.error


def test_verdicts_are_compared_by_a_fixed_rule_without_a_model() -> None:
    def reply(model: str, msgs: list[dict[str, str]]) -> str:
        return (
            verdict_json(model == "big", "small disagrees")
            if "Question" in msgs[-1]["content"]
            else "t"
        )

    p = prog(
        ASK,
        {"b": "check", "in": "a", "pick": {"fixed": "big"}, "out": "v1"},
        {"b": "check", "in": "a", "pick": {"fixed": "small"}, "out": "v2"},
        {"b": "compare", "in": ["v1", "v2"], "rule": "all", "out": "both"},
        {"b": "compare", "in": ["v1", "v2"], "rule": "majority", "out": "most"},
    )
    ports = FakePorts(reply)
    result = run(ports, p)
    assert result.vars["both"]["ok"] is False and result.vars["both"]["issues"] == [
        "small disagrees"
    ]
    assert result.vars["most"]["ok"] is False  # one of two is not a majority
    assert len(ports.asked) == 3  # the ask and two checks: the comparison asked nobody


WORDS = {"big": "alpha", "small": "beta"}  # answers must not give their authors away


def judge_reply(winner_by_text: dict[str, str]) -> Callable[[str, list[dict[str, str]]], str]:
    def reply(model: str, msgs: list[dict[str, str]]) -> str:
        content = msgs[-1]["content"]
        if "Answer A" not in content:
            return f"{WORDS.get(model, model)} text"
        letters = {}
        for letter in ("A", "B"):
            block = content.split(f"Answer {letter}:")[1]
            letters[letter] = block.split("<<<")[1].split(">>>")[0].strip()
        wanted = next((k for k in letters if winner_by_text["win"] in letters[k]), None)
        return json.dumps(
            {
                "agree": False,
                "differences": ["they differ on the date"],
                "winner": wanted,
                "merged": "MERGED",
            }
        )

    return reply


COMPARE = prog(
    {"b": "ask", "prompt": "{{question}}", "pick": {"fixed": "big"}, "out": "x"},
    {"b": "ask", "prompt": "{{question}}", "pick": {"fixed": "small@node-a"}, "out": "y"},
    {"b": "compare", "in": ["x", "y"], "mode": "pick", "out": "c"},
)


def test_the_third_agent_is_not_an_author_and_sees_anonymous_answers() -> None:
    ports = FakePorts(judge_reply({"win": "alpha"}))
    pool = [*POOL, pick.Candidate("judge", "node-a", size_mb=500, tps=50)]
    ports.pool = pool
    result = run(ports, COMPARE)
    judged = [(m, msgs) for m, msgs in ports.asked if "Answer A" in msgs[-1]["content"]]
    assert len(judged) == 2  # pick runs twice, the order swapped
    assert {m for m, _ in judged} == {"judge"}  # neither big nor small wrote the answers
    for _, msgs in judged:
        assert "big" not in msgs[-1]["content"] and "small" not in msgs[-1]["content"]
    first, second = (msgs[-1]["content"].index("alpha text") for _, msgs in judged)
    assert first != second  # swapped order
    c = result.vars["c"]
    assert c["text"] == "alpha text" and c["judge"] == "judge" and c["verdict"] == "differ"


def test_a_pick_without_a_winner_in_both_rounds_keeps_the_first_text_and_says_so() -> None:
    flip = {"n": 0}

    def reply(model: str, msgs: list[dict[str, str]]) -> str:
        if "Answer A" not in msgs[-1]["content"]:
            return f"{WORDS.get(model, model)} text"
        flip["n"] += 1
        return json.dumps(
            {"agree": False, "differences": ["d"], "winner": "A", "merged": ""}
        )  # always A: position bias

    ports = FakePorts(reply)
    ports.pool = [*POOL, pick.Candidate("judge", "node-a", size_mb=500)]
    result = run(ports, COMPARE)
    c = result.vars["c"]
    assert c["text"] == "alpha text"
    assert any("no answer won both rounds" in d for d in c["differences"])


def test_merge_uses_the_text_the_judge_wrote_and_an_only_judge_is_flagged() -> None:
    merge = json.loads(json.dumps(COMPARE))
    merge["stack"][2]["mode"] = "merge"
    ports = FakePorts(judge_reply({"win": "alpha"}))
    ports.pool = [
        pick.Candidate("big", "node-a", size_mb=1),
        pick.Candidate("small", "node-a", size_mb=1),
    ]
    result = run(ports, merge)
    assert result.vars["c"]["text"] == "MERGED"
    assert any("not independent" in n for n in result.notes)  # only the authors were there to judge


def test_an_empty_pool_fails_the_run_with_a_reason() -> None:
    ports = FakePorts(lambda m, s: "x", pool=[])
    result = run(ports, prog(ASK, {**CHECK, "pick": {"rank": "best"}}))
    assert result.status == "failed" and "no model" in result.error


def test_an_agent_that_fails_fails_the_run() -> None:
    class Failing(FakePorts):
        async def agent(self, prompt, application, depth, picked, hooks=None, progress=None):  # type: ignore[no-untyped-def]
            return AgentOutcome(False, error="the task ended: stalled")

    result = run(
        Failing(lambda m, s: "x"), prog({"b": "agent", "prompt": "{{question}}", "out": "d"})
    )
    assert result.status == "failed" and "stalled" in result.error


def test_what_runs_is_validated_again_and_inputs_are_required() -> None:
    engine = BlockEngine(FakePorts(lambda m, s: "x"))
    with pytest.raises(ProgramError):
        asyncio.run(engine.run(prog({"b": "nope"}), {"question": "q"}))
    with pytest.raises(BlockFailure, match="missing"):
        asyncio.run(engine.run(prog(ASK), {}))


def test_the_run_is_an_event_stream() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    ports = FakePorts(lambda m, s: "x")
    asyncio.run(
        BlockEngine(ports).run(prog(ASK), {"question": "q"}, lambda e, d: events.append((e, d)))
    )
    names = [e for e, _ in events]
    assert names[0] == "step" and names[-1] == "run"
    assert [d["seq"] for _, d in events] == list(range(len(events)))
    done = next(d for e, d in events if e == "step" and d["state"] == "done")
    assert done["out"] == "a" and done["preview"] == "x"


def test_think_reaches_the_model_only_when_the_program_says_so() -> None:
    """``think`` is the program's word on reasoning: false goes to the model, absent is None."""
    ports = FakePorts(lambda m, s: verdict_json(True) if "Question" in s[-1]["content"] else "x")
    program = prog(
        {"b": "ask", "prompt": "{{question}}", "think": False, "out": "a"},
        {"b": "check", "in": "a", "question": "question", "think": False, "out": "v"},
        {"b": "revise", "draft": "a", "issues": "v", "out": "d"},
    )
    run(ports, program)
    assert ports.thinks[0] is False and ports.thinks[1] is False
    assert ports.thinks[2] is None  # revise: the hub's default


def test_think_must_be_true_or_false() -> None:
    with pytest.raises(ProgramError, match="think"):
        program.validate(prog({"b": "ask", "prompt": "{{question}}", "think": "no", "out": "a"}))


def test_events_say_which_block_picks_calls_and_what_the_agent_does() -> None:
    """The live view attaches a pick, a call and the agent's activity to the block that made it."""
    events: list[tuple[str, dict[str, Any]]] = []
    ports = FakePorts(lambda m, s: "x")
    program = prog(
        {"b": "agent", "prompt": "{{question}}", "out": "d"},
        {"b": "ask", "prompt": "{{question}}", "pick": {"rank": "largest"}, "out": "a"},
    )
    emit = lambda e, d: events.append((e, d))  # noqa: E731
    asyncio.run(BlockEngine(ports).run(program, {"question": "q"}, emit))
    by_name = lambda name: [d for e, d in events if e == name]  # noqa: E731
    activity = by_name("activity")
    assert activity and activity[0]["path"] == "stack[0]"
    assert activity[0]["data"]["node"] == "node-a"
    call = next(d for d in by_name("call") if d["b"] == "ask")
    assert call["path"] == "stack[1]" and call["model"] == "big"
    pick_event = next(d for d in by_name("pick") if d["path"] == "stack[1]")
    assert pick_event["model"] == "big"
    assert all(isinstance(d["at"], float) for _, d in events)  # the hub's clock on every event


def test_exclusive_is_carried_by_a_pick() -> None:
    got = pick.evaluate({"rank": "largest", "exclusive": True}, POOL)
    assert got and got.exclusive
    assert not pick.evaluate({"rank": "largest"}, POOL).exclusive  # type: ignore[union-attr]
    with pytest.raises(pick.PickError):
        pick.check_spec({"exclusive": "yes"}, "x")


def test_an_empty_answer_is_asked_again_with_more_room() -> None:
    from topcoat_hub.blocks.engine import EmptyAnswer

    class Thinker(FakePorts):
        def __init__(self) -> None:
            super().__init__(lambda m, s: "x")
            self.limits: list[int] = []

        async def ask(self, messages, picked, max_tokens, temperature, think=None, schema=None):  # type: ignore[no-untyped-def]
            self.limits.append(max_tokens)
            if len(self.limits) == 1:
                raise EmptyAnswer("spent every token thinking")
            return "an answer", "m"

    ports = Thinker()
    result = run(ports, prog({**ASK, "max_tokens": 1000}))
    assert result.status == "ok" and result.vars["a"] == "an answer"
    assert ports.limits == [1000, 3000]
    assert any("more room" in n for n in result.notes)


def test_an_answer_that_stays_empty_fails_the_run() -> None:
    from topcoat_hub.blocks.engine import EmptyAnswer

    class Mute(FakePorts):
        async def ask(self, messages, picked, max_tokens, temperature, think=None, schema=None):  # type: ignore[no-untyped-def]
            raise EmptyAnswer("nothing again")

    result = run(Mute(lambda m, s: "x"), prog(ASK))
    assert result.status == "failed" and "nothing" in result.error


# --- tool, ask as a list, gathering a loop (W3, 2026-11-11) ---------------------------------------


def test_a_tool_block_calls_the_capability_without_a_model_and_keeps_the_source() -> None:
    ports = FakePorts(lambda m, msgs: "unused")
    p = prog(
        {"b": "tool", "capability": "repo.list", "app": "dev",
         "input": {"repo": "r", "path": "{{question}}", "depth": 2}, "out": "listing"},
        {"b": "tool", "capability": "topcoat.guide", "app": "admin", "node": "hub", "out": "guide"},
    )  # fmt: skip
    result = run(ports, p, question="docs/decisions")
    assert result.status == "ok" and ports.asked == []  # no model was asked
    assert ports.tools[0] == ("repo.list", {"repo": "r", "path": "docs/decisions", "depth": 2},
                              None, "dev")  # fmt: skip
    assert ports.tools[1][2:] == ("hub", "admin")
    assert [s["ref"] for s in result.sources] == ["F1", "F2"]
    assert json.loads(result.vars["listing"])["input"]["path"] == "docs/decisions"


def test_a_long_tool_answer_is_cut_and_a_refused_tool_fails_the_run() -> None:
    class Long(FakePorts):
        async def tool(self, capability, tool_input, node, application):  # type: ignore[no-untyped-def]
            return ToolOutcome("x" * (TOOL_TEXT_LIMIT + 50))

    result = run(Long(lambda m, msgs: ""), prog({"b": "tool", "capability": "a.b", "out": "t"}))
    assert len(result.vars["t"]) < TOOL_TEXT_LIMIT + 20 and result.vars["t"].endswith("[… cut]")
    refused = run(
        FakePorts(lambda m, msgs: ""), prog({"b": "tool", "capability": "x.refused", "out": "t"})
    )
    assert refused.status == "failed" and "not offered" in refused.error


@pytest.mark.parametrize(
    "block",
    [
        {"b": "tool", "capability": "shell", "out": "t"},  # not a dotted capability name
        {"b": "tool", "capability": "a.b", "input": {"x": "{{nope}}"}, "out": "t"},
        {"b": "tool", "capability": "a.b", "input": {"x": None}, "out": "t"},
        {"b": "tool", "capability": "a.b", "app": "nowhere", "out": "t"},
        {"b": "ask", "prompt": "{{question}}", "as": "table", "out": "t"},
    ],
)
def test_a_tool_or_list_block_that_makes_no_sense_is_refused(block: dict[str, Any]) -> None:
    with pytest.raises(ProgramError):
        program.validate(prog(block))


def test_an_ask_as_a_list_writes_texts_and_asks_once_more_for_a_list() -> None:
    answers = iter(["no list here", 'Sure: ["a", {"k": 1}, ""]'])
    ports = FakePorts(lambda m, msgs: next(answers, "an item's answer"))
    p = prog({**ASK, "as": "texts", "out": "items"},
             {"b": "for_each", "in": "items", "as": "it", "stack": [ASK]})  # fmt: skip
    result = run(ports, p)
    assert result.status == "ok"
    assert result.vars["items"] == ["a", '{"k": 1}']  # an object stays JSON, an empty item goes
    assert "JSON list" in ports.asked[0][1][-1]["content"]
    assert any("no JSON list" in n for n in result.notes)
    never = run(FakePorts(lambda m, msgs: "never a list"), prog({**ASK, "as": "texts"}))
    assert never.status == "failed" and "no JSON list" in never.error
    assert any("never a list" in n for n in never.notes)  # what the model wrote, to see why


def test_a_list_cut_off_by_the_token_limit_keeps_its_whole_items() -> None:
    cut = '[{"a": 1}, {"a": 2}, {"a": 3, "b": "unfinish'
    result = run(FakePorts(lambda m, msgs: cut), prog({**ASK, "as": "texts"}))
    assert result.status == "ok" and result.vars["a"] == ['{"a": 1}', '{"a": 2}']


def test_for_each_gathers_a_text_of_every_round_with_the_agent_tasks_of_that_round() -> None:
    class Agents(FakePorts):
        n = 0

        async def agent(self, prompt, application, depth, picked, hooks=None, progress=None):  # type: ignore[no-untyped-def]
            self.n += 1
            if "b" in prompt:
                return AgentOutcome(False, error="no node", task_id=f"t{self.n}")
            return AgentOutcome(True, f"row for {prompt}", model="m", task_id=f"t{self.n}")

    ports = Agents(lambda m, msgs: '["a", "b", "c"]')
    p = prog(
        {**ASK, "as": "texts", "out": "claims"},
        {"b": "for_each", "in": "claims", "as": "claim", "collect": "row", "into": "rows",
         "stack": [{"b": "agent", "prompt": "{{claim}}", "on_fail": "empty", "out": "row"}]},
    )  # fmt: skip
    result = run(ports, p)
    assert result.status == "ok"
    assert result.vars["rows"] == ["row for a", "", "row for c"]  # a failed round gathers nothing
    assert result.collected_tasks["rows"] == [["t1"], ["t2"], ["t3"]]
    assert any("no node" in n for n in result.notes)
    # without on_fail an agent that fails still ends the run
    p["stack"][1]["stack"][0].pop("on_fail")
    assert run(Agents(lambda m, msgs: '["b"]'), p).status == "failed"


@pytest.mark.parametrize(
    "loop",
    [
        {"collect": "row"},  # collect without into
        {"collect": "nothing", "into": "rows"},  # the stack does not write it
    ],
)
def test_a_loop_that_gathers_wrongly_is_refused(loop: dict[str, Any]) -> None:
    p = prog(
        {**ASK, "as": "texts", "out": "claims"},
        {"b": "for_each", "in": "claims", "as": "claim",
         "stack": [{"b": "ask", "prompt": "{{claim}}", "out": "row"}], **loop},
    )  # fmt: skip
    with pytest.raises(ProgramError):
        program.validate(p)


# --- choose and roles (W3b, 2026-11-11) ---------------------------------------------------------


def test_choose_takes_the_variable_the_object_names_and_says_whether_it_found_one() -> None:
    ports = FakePorts(
        lambda m, msgs: '[{"aussage": "a", "quelle": "F2"}, {"aussage": "b", "quelle": "code"}]'
    )
    p = prog(
        {"b": "set", "out": "one", "value": "fact one"},
        {"b": "set", "out": "two", "value": "fact two"},
        {**ASK, "as": "texts", "out": "claims"},
        {
            "b": "for_each",
            "in": "claims",
            "as": "claim",
            "collect": "row",
            "into": "rows",
            "stack": [
                {
                    "b": "choose",
                    "in": "claim",
                    "field": "quelle",
                    "cases": {"F1": "one", "F2": "two"},
                    "out": "slice",
                    "found": "has",
                },
                {
                    "b": "if",
                    "cond": "has",
                    "then": [{"b": "set", "out": "row", "value": "fact: {{slice}}"}],
                    "else": [{"b": "set", "out": "row", "value": "no fact"}],
                },
            ],
        },
    )
    result = run(ports, p)
    assert result.status == "ok" and result.vars["rows"] == ["fact: fact two", "no fact"]


def test_choose_needs_defined_texts_for_its_cases() -> None:
    p = prog(
        {"b": "choose", "in": "question", "field": "q", "cases": {"F1": "nowhere"}, "out": "s"}
    )
    with pytest.raises(ProgramError):
        program.validate(p)


def test_a_role_gives_its_blocks_the_model_a_caller_names_and_others_keep_theirs() -> None:
    p = prog(
        {**ASK, "role": "judge", "pick": {"rank": "fastest"}},
        {"b": "ask", "prompt": "{{a}}", "role": "author", "out": "b"},
        {
            "b": "for_each",
            "in": "items",
            "as": "it",
            "stack": [{**ASK, "role": "judge", "out": "c"}],
        },
        inputs={"question": "text", "items": "texts"},
    )
    assert program.roles(p) == ["judge", "author"]
    swapped = program.with_roles(p, {"judge": "small@node-b"})
    assert swapped["stack"][0]["pick"] == {"fixed": "small@node-b"}
    assert swapped["stack"][2]["stack"][0]["pick"] == {"fixed": "small@node-b"}
    assert "pick" not in swapped["stack"][1]  # nobody named the author
    assert p["stack"][0]["pick"] == {"rank": "fastest"}  # the original stays as it was
    program.validate(swapped)


def test_an_agent_block_waits_while_the_persons_agent_runs_are_full(monkeypatch: Any) -> None:
    from types import SimpleNamespace

    from topcoat_hub.blocks import service

    starts: list[int] = []

    class FakeCheck:
        def __init__(self, *a: Any, **kw: Any) -> None:
            self.result = SimpleNamespace(task=None, error="", reason="")

        async def events(self) -> Any:
            starts.append(1)
            if len(starts) < 3:  # the first two starts find every agent run taken
                self.result.error = "too many tasks are running; wait or cancel one"
                self.result.reason = "too_many_tasks"
            else:
                self.result.task = SimpleNamespace(
                    id="t1", status="done", summary="read", diagnosis={}
                )
            return
            yield

    monkeypatch.setattr(service, "ToolCheck", FakeCheck)
    monkeypatch.setattr(service, "AGENT_WAIT_SECONDS", 0.0)
    ports = service.HubBlockPorts(None, None, None, None, SimpleNamespace(uuid="u"))  # type: ignore[arg-type]

    async def no_report(*a: Any) -> None:
        return None

    monkeypatch.setattr(ports, "agent", ports.agent)
    outcome = asyncio.run(_agent_without_reports(ports, monkeypatch))
    assert outcome.ok and outcome.task_id == "t1" and len(starts) == 3


async def _agent_without_reports(ports: Any, monkeypatch: Any) -> Any:
    class NoReports:
        def __init__(self, *a: Any) -> None:
            pass

        async def by_task(self, task_id: Any) -> None:
            return None

    class DB:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

    from topcoat_hub.blocks import service

    monkeypatch.setattr(service, "ReportStore", NoReports)
    ports.sessionmaker = lambda: DB()
    return await ports.agent("read x", "dev", "default", None)


def test_a_tool_block_waits_for_its_node_to_connect_again(monkeypatch: Any) -> None:
    from types import SimpleNamespace

    from topcoat_hub.blocks import service

    looks: list[int] = []

    class Tools:
        async def available(self, caller: Any) -> list[dict[str, Any]]:
            looks.append(1)
            if len(looks) < 3:  # the dev node is still connecting
                return []
            return [
                {
                    "capability": "repo.list",
                    "node": "dev",
                    "node_id": "00000000-0000-0000-0000-000000000001",
                }
            ]

        async def invoke(self, caller: Any, node_id: Any, capability: str, given: Any) -> Any:
            return {"ok": True, "output": {"files": ["a.py"]}}

    class Agents:
        tools = Tools()

        async def _within_application(self, user: Any, app: Any, scope: Any) -> None:
            return None

    monkeypatch.setattr(service, "TOOL_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(service.tool_groups, "apply_scope", lambda usable, scope: usable)
    settings = SimpleNamespace(agent_tools_wait_seconds=60.0)
    person = SimpleNamespace(uuid="u", is_admin=True, via_key=False)
    ports = service.HubBlockPorts(None, Agents(), None, settings, person)  # type: ignore[arg-type]
    found = asyncio.run(ports.tool("repo.list", {"repo": "r"}, None, "dev"))
    assert json.loads(found.text) == {"files": ["a.py"]} and len(looks) == 3


# --- reading a file in parts (doc_section, 2026-11-12) --------------------------------------------


class Lines(FakePorts):
    """repo.read answers with the file's total_lines; a.missing is not there."""

    async def tool(self, capability, tool_input, node, application):  # type: ignore[no-untyped-def]
        if tool_input.get("path") == "a.missing":
            raise BlockFailure("no such file")
        if capability == "repo.read":
            total = 800 if tool_input.get("path") == "big.py" else 120
            return ToolOutcome(json.dumps({"text": "x", "total_lines": total}))
        return await super().tool(capability, tool_input, node, application)


def test_ranges_share_a_file_in_even_parts_without_a_model() -> None:
    p = prog(
        {"b": "tool", "capability": "repo.read", "input": {"path": "{{question}}"}, "out": "head"},
        {"b": "ranges", "in": "head", "size": 350, "out": "parts"},
    )
    result = run(Lines(lambda m, msgs: "unused"), p, question="big.py")
    # 800 lines in three parts, the rest shared: no last part of 100 lines
    assert result.vars["parts"] == ["1-267", "268-534", "535-800"]
    assert run(Lines(lambda m, msgs: ""), p, question="small.py").vars["parts"] == ["1-120"]
    # a read that names no total gives no parts
    plain = prog({"b": "set", "out": "head", "value": "not json"}, p["stack"][1])
    assert run(Lines(lambda m, msgs: ""), plain).vars["parts"] == []


@pytest.mark.parametrize("size", [5, 6000, "350", True])
def test_a_ranges_block_with_a_senseless_size_is_refused(size: Any) -> None:
    with pytest.raises(ProgramError):
        program.validate(
            prog(
                {"b": "set", "out": "head", "value": "{{question}}"},
                {"b": "ranges", "in": "head", "size": size, "out": "parts"},
            )
        )


def test_a_tool_that_may_fail_writes_nothing_and_the_loop_goes_on() -> None:
    p = {
        "name": "t",
        "inputs": {"files": "texts"},
        "stack": [
            {"b": "for_each", "in": "files", "as": "file", "collect": "head", "into": "heads",
             "stack": [{"b": "tool", "capability": "repo.read", "on_fail": "empty",
                        "input": {"path": "{{file}}"}, "out": "head"}]},
        ],
    }  # fmt: skip
    engine = BlockEngine(Lines(lambda m, msgs: ""), seed=1)
    result = asyncio.run(engine.run(p, {"files": ["a.missing", "small.py"]}))
    assert result.status == "ok"
    assert result.vars["heads"][0] == "" and "120" in result.vars["heads"][1]
    assert any("no such file" in n for n in result.notes)
    p["stack"][0]["stack"][0]["on_fail"] = "maybe"
    with pytest.raises(ProgramError):
        program.validate(p)


def test_a_nested_loop_appends_to_what_the_rounds_before_gathered() -> None:
    p = {
        "name": "t",
        "inputs": {"files": "texts"},
        "stack": [
            {"b": "for_each", "in": "files", "as": "file", "stack": [
                {"b": "tool", "capability": "repo.read", "input": {"path": "{{file}}"},
                 "out": "head"},
                {"b": "ranges", "in": "head", "size": 350, "out": "parts"},
                {"b": "for_each", "in": "parts", "as": "lines", "collect": "part",
                 "into": "rows", "append": True,
                 "stack": [{"b": "agent", "prompt": "{{file}} {{lines}}", "out": "part"}]},
            ]},
        ],
    }  # fmt: skip
    result = asyncio.run(
        BlockEngine(Lines(lambda m, msgs: ""), seed=1).run(p, {"files": ["small.py", "big.py"]})
    )
    assert result.status == "ok"
    assert [r.split(" for ")[1] for r in result.vars["rows"]] == [
        "small.py 1-120", "big.py 1-267", "big.py 268-534", "big.py 535-800",
    ]  # fmt: skip
    assert len(result.collected_tasks["rows"]) == 4
    del p["stack"][0]["stack"][2]["into"], p["stack"][0]["stack"][2]["collect"]
    with pytest.raises(ProgramError):  # append without into
        program.validate(p)


def test_doc_section_reads_the_named_files_in_parts_and_asks_no_planner() -> None:
    ports = Lines(lambda m, msgs: '[{"Abschnitt": "Zweck", "Text": "t", "Beleg": "E1"}]')
    inputs = {"area": "hub", "task": "t", "form": "Abschnitt | Text | Beleg", "focus": "",
              "files": ["hub/small.py", "big.py"]}  # fmt: skip
    result = asyncio.run(BlockEngine(ports, seed=1).run(builtin.DOC_SECTION, inputs))
    assert result.status == "ok"
    parts = 1 + -(-800 // builtin.PART_LINES)  # one part of small, the parts of big
    assert len(result.vars["rows"]) == parts
    assert not any("choose which files" in m[0]["content"] for _, m in ports.asked)
    assert len(ports.asked) == parts  # the author once per part, nothing else


def test_a_tool_reads_the_lines_of_a_range_keeps_one_field_and_names_its_source() -> None:
    class Code(FakePorts):
        async def tool(self, capability, tool_input, node, application):  # type: ignore[no-untyped-def]
            self.got = tool_input
            body = {"text": "def f():\n    return 1", "total_lines": 2}
            return ToolOutcome(json.dumps(body), "dev-a", {"input": tool_input})

    ports = Code(lambda m, msgs: "unused")
    p = prog(
        {"b": "set", "out": "lines", "value": "120-269"},
        {"b": "tool", "capability": "repo.read", "input": {"path": "x.py"}, "lines": "{{lines}}",
         "field": "text", "ref": "src", "out": "code"},
        {"b": "set", "out": "cite", "value": "Beleg: {{src}}"},
    )  # fmt: skip
    result = run(ports, p)
    assert result.status == "ok"
    assert ports.got == {"path": "x.py", "start_line": 120, "end_line": 269}
    assert result.vars["code"] == "def f():\n    return 1"  # the code, not the JSON around it
    assert result.sources[0]["text"] == result.vars["code"] and result.vars["cite"] == "Beleg: F1"
    bad = prog({"b": "tool", "capability": "repo.read", "lines": "{{nope}}", "out": "code"})
    with pytest.raises(ProgramError):
        program.validate(bad)


def test_parts_begin_where_a_definition_begins() -> None:
    outline = (
        "big.py:1:import x\nbig.py:250:def a():\nbig.py:262:    def inner():\nbig.py:520:class B:\n"
    )
    p = prog(
        {"b": "tool", "capability": "repo.read", "input": {"path": "big.py"}, "out": "head"},
        {"b": "set", "out": "outline", "value": outline},
        {"b": "ranges", "in": "head", "at": "outline", "size": 300, "out": "parts"},
    )
    # 800 lines: even cuts at 268 and 535; the latest definition before each, past half a part
    assert run(Lines(lambda m, msgs: ""), p).vars["parts"] == ["1-261", "262-519", "520-800"]


THEME_DOC = {
    "key": "glas",
    "name": "Glas",
    "base": "terminal",
    "layout": {"kind": "cascade"},
    "bar": {"position": "bottom", "items": ["start", "programs", "clock"]},
    "chrome": {"buttons": ["minimise", "maximise", "close"], "titlebar": "top"},
    "motion": {"style": "fade", "ms": 200},
    "css": "--bar: #245edb;\n.dt-taskbar { background: var(--bar); }",
}


def test_a_theme_check_carries_the_hubs_findings_even_when_the_model_passes_it() -> None:
    # live 2026-11-15: the model passed a theme whose css drew almost nothing
    broken = {**THEME_DOC, "css": ".dt-window { color: red; }\n.dt-task { background: var(--x); }"}
    seen: list[str] = []

    def reply(model: str, msgs: list[dict[str, str]]) -> str:
        if msgs[0]["content"].startswith("You revise"):
            seen.append(msgs[-1]["content"])
            return json.dumps(THEME_DOC)
        if "Question" in msgs[-1]["content"]:
            return verdict_json(True)  # the model finds nothing
        return json.dumps(broken)  # the writer

    looped = prog(
        {"b": "ask", "prompt": "{{question}}", "out": "draft"},
        {"b": "check", "in": "draft", "lint": "theme", "out": "v"},
        {
            "b": "repeat_until",
            "cond": "v.ok",
            "max": 2,
            "stack": [
                {"b": "revise", "draft": "draft", "issues": "v", "out": "draft"},
                {"b": "check", "in": "draft", "lint": "theme", "out": "v"},
            ],
        },
    )
    result = run(FakePorts(reply), looped)
    assert result.status == "ok"
    assert len(seen) == 1 and ".dt-window" in seen[0] and "var(--x)" in seen[0]
    assert result.vars["v"]["ok"] is True  # the revised theme passes both
    assert any(s["event"] == "lint" for s in result.steps)


def test_a_check_names_only_the_lints_it_knows() -> None:
    with pytest.raises(ProgramError, match="lint"):
        program.validate(prog(ASK, {**CHECK, "lint": "spelling"}))
    assert builtin.THEME_DESIGN["stack"][2]["lint"] == "theme"


def test_a_check_may_think_with_room_for_its_verdict() -> None:
    class Limits(FakePorts):
        limits: ClassVar[list[int]] = []

        async def ask(self, messages, picked, max_tokens, temperature, think=None, schema=None):  # type: ignore[no-untyped-def]
            self.limits.append(max_tokens)
            return await super().ask(messages, picked, max_tokens, temperature, think)

    ports = Limits(lambda m, s: verdict_json(True) if "Question" in s[-1]["content"] else "x")
    run(ports, prog(ASK, {**CHECK, "think": True, "max_tokens": 12000}))
    assert ports.limits[1] == 12000 and ports.thinks[1] is True
    check = builtin.THEME_DESIGN["stack"][2]
    assert check["think"] is False  # live 2026-11-15: a thinking check wrote no verdict
    with pytest.raises(ProgramError, match="max_tokens"):
        program.validate(prog(ASK, {**CHECK, "max_tokens": "lots"}))


def test_when_the_lint_decides_the_models_findings_are_advice() -> None:
    # live 2026-11-15: the model named faults that were not there in every round
    rounds: list[str] = []

    def reply(model: str, msgs: list[dict[str, str]]) -> str:
        if msgs[0]["content"].startswith("You revise"):
            rounds.append(msgs[-1]["content"])
            return json.dumps(THEME_DOC)
        if "Question" in msgs[-1]["content"]:
            return verdict_json(False, "the name field is missing")  # it is not
        return json.dumps({**THEME_DOC, "css": ".dt-window { color: red; }"})

    check = {"b": "check", "in": "draft", "lint": "theme", "decides": "lint", "out": "v"}
    looped = prog(
        {"b": "ask", "prompt": "{{question}}", "out": "draft"},
        check,
        {"b": "repeat_until", "cond": "v.ok", "max": 3,
         "stack": [{"b": "revise", "draft": "draft", "issues": "v", "out": "draft"}, check]},
    )  # fmt: skip
    result = run(FakePorts(reply), looped)
    assert len(rounds) == 1  # one revision for the lint; the model's word does not hold it
    assert ".dt-window" in rounds[0] and "name field" in rounds[0]  # both reach the revision
    assert result.vars["v"]["ok"] is True
    assert result.vars["v"]["advice"] == ["the name field is missing"]
    with pytest.raises(ProgramError, match="decides"):
        program.validate(prog(ASK, {**CHECK, "decides": "lint"}))


def test_a_list_left_only_in_the_reasoning_is_taken() -> None:
    """Live 2026-11-18: gpt-oss twice answered a step that asks for one JSON list with nothing,
    the list only in its reasoning, and the whole issue_fix run failed."""
    from topcoat_hub.blocks.engine import EmptyAnswer, json_from_reasoning

    assert json_from_reasoning('We need terms. Maybe ["a"]. Final: ["expand_short_ids", "x"]') == (
        '["expand_short_ids", "x"]'
    )
    assert json_from_reasoning("no list here [not json") is None

    class Thinker(FakePorts):
        async def ask(self, messages, picked, max_tokens, temperature, think=None, schema=None):  # type: ignore[no-untyped-def]
            raise EmptyAnswer("nothing", 'I will answer ["a", "b"]')

    ask_list = {"b": "ask", "as": "texts", "prompt": "List?", "out": "items"}
    result = run(Thinker(lambda m, s: "x"), prog(ask_list))
    assert result.status == "ok" and result.vars["items"] == ["a", "b"]
    # a step that wants prose does not take the reasoning
    result = run(Thinker(lambda m, s: "x"), prog(ASK))
    assert result.status == "failed"
