"""A fix in steps (fix_steps.py): the plan's anchors are held against the live code, each step is a
fresh run on the code as the steps before left it, a refused step runs once more with the reason,
and all edits become one draft."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from topcoat_hub import fix_steps as fs
from topcoat_hub import issue_fixes as fx
from topcoat_hub.blocks.engine import RunResult
from topcoat_hub.models import DEV_ADMIN_ID as USER
from topcoat_hub.project_store import ProjectStore

CODE = (
    "import re\n\n\n"
    "def parse(text):\n    return text.strip()\n\n\n"
    "class Service:\n    def run(self, text):\n        value = parse(text)\n        return value\n"
)
LIVE: dict[str, str | None] = {"hub/src/topcoat_hub/x.py": CODE}
PATH = "hub/src/topcoat_hub/x.py"


def plan(**over: Any) -> str:
    doc = {
        "summary": "Leere Texte werden abgelehnt",
        "steps": [
            {"path": PATH, "anchor": "def parse(text):", "do": "add a check", "after": []},
            {"path": PATH, "anchor": "value = parse(text)", "do": "use it", "after": [1]},
        ],
        "test": {"path": "hub/tests/test_x_empty.py", "do": "an empty text is refused"},
        **over,
    }
    return json.dumps(doc)


def edit(old: str, new: str) -> str:
    return json.dumps({"edits": [{"path": PATH, "old": old, "new": new}], "files": []})


async def read(paths: list[str]) -> dict[str, str | None]:
    return {p: LIVE.get(p) for p in paths}


class Asker:
    """Answers in order; keeps what each run was given."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, inputs: dict[str, Any]) -> tuple[str, str]:
        self.calls.append((name, inputs))
        return self.answers.pop(0), f"model-{len(self.calls)}"


TEST_FILE = json.dumps(
    {"edits": [], "files": [{"path": "hub/tests/test_x_empty.py", "content": "def test(): pass\n"}]}
)


def test_a_plan_is_read_and_its_anchors_held_against_the_code() -> None:
    p, problems = fs.parse_plan(plan())
    assert problems == [] and p is not None and [s.n for s in p.steps] == [1, 2]
    assert fs.plan_problems(p, dict(LIVE)) == []
    # an anchor that is not there, one that stands twice, a test file that exists
    bad, _ = fs.parse_plan(
        plan(
            steps=[
                {"path": PATH, "anchor": "def nothing():", "do": "x"},
                {"path": PATH, "anchor": "return", "do": "x"},
            ],
            test={"path": "hub/tests/test_x_empty.py", "do": "x"},
        )
    )
    assert bad is not None
    files = {**LIVE, "hub/tests/test_x_empty.py": "old"}
    problems = fs.plan_problems(bad, files)
    assert any("'def nothing():' is not in the file" in m for m in problems)
    # gpt-oss, live 2026-11-19: a call over two lines written as one, and a step that names itself
    joined, refused = fs.parse_plan(
        plan(steps=[{"path": PATH, "anchor": "def run(self, text): value = parse(text)",
                     "do": "x", "after": [1]}])
    )  # fmt: skip
    assert refused == [] and joined is not None and joined.steps[0].after == []
    assert fs.plan_problems(joined, dict(LIVE)) == []
    assert joined.steps[0].anchor == "def run(self, text):"
    # a line that stands twice, told apart by the line after it (live 2026-11-19: three calls)
    twice = "def f():\n    return 2\n\n\ndef g():\n    return 2\n    pass\n"
    two, _ = fs.parse_plan(plan(steps=[{"path": PATH, "anchor": "return 2\npass", "do": "x"}]))
    assert two is not None and fs.plan_problems(two, {PATH: twice}) == []
    assert two.steps[0].anchor == "pass"
    one, _ = fs.parse_plan(plan(steps=[{"path": PATH, "anchor": "return 2", "do": "x"}]))
    assert one is not None and "consecutive lines" in fs.plan_problems(one, {PATH: twice})[0]
    # a near miss names the line the file has (live 2026-11-19: an invented call)
    near, _ = fs.parse_plan(
        plan(steps=[{"path": PATH, "anchor": "value = parse(raw_input)", "do": "x"}])
    )
    assert near is not None
    [miss] = fs.plan_problems(near, dict(LIVE))
    assert "the file has: 'value = parse(text)'" in miss
    assert any("stands 2 times" in m for m in problems)
    assert any("exists already" in m for m in problems)
    # a line number copied along is no part of the anchor
    numbered, _ = fs.parse_plan(
        plan(steps=[{"path": PATH, "anchor": "4: def parse(text):", "do": "x"}])
    )
    assert numbered is not None and numbered.steps[0].anchor == "def parse(text):"
    # a call over several lines: the first of them that stands once is the anchor (live 2026-11-19)
    multi, _ = fs.parse_plan(
        plan(
            steps=[
                {
                    "path": PATH,
                    "anchor": "    def run(self, text):\n        return value",
                    "do": "x",
                }
            ]
        )
    )
    assert multi is not None and fs.plan_problems(multi, dict(LIVE)) == []
    assert multi.steps[0].anchor == "def run(self, text):"
    # form: a step that builds on a later one, no test, a path outside
    _, problems = fs.parse_plan(
        plan(steps=[{"path": "web/a.ts", "anchor": "a", "do": "x", "after": [2]}], test={})
    )
    assert len(problems) == 2 and "web/a.ts" in problems[0]


@pytest.mark.asyncio
async def test_each_step_sees_the_code_as_the_steps_before_left_it() -> None:
    """Step 2 quotes the line step 1 wrote: held against the live file it would be refused."""
    asker = Asker(
        [
            edit(
                "def parse(text):\n    return text.strip()",
                "def parse(text):\n    return check(text.strip())",
            ),
            # the first answer of step 2 misquotes; the second gets the reason and the real lines
            edit("    value = parse(txt)", "x"),
            edit("        value = parse(text)\n", "        value = parse(text) or None\n"),
            TEST_FILE,
        ]
    )
    out = await fs.run_steps(asker, read, "Leere Texte", plan(), "planner-model")
    assert out.failed == "" and len(out.edits) == 2 and out.files[0]["path"].endswith("x_empty.py")
    # step 2 was shown the code with step 1 applied, and the diff of step 1
    step2 = asker.calls[1][1]
    assert "return check(text.strip())" in step2["code"] and "+    return check" in step2["done"]
    retry = asker.calls[2][1]
    assert "REFUSED" in retry["hint"] and "value = parse(text)" in retry["hint"]
    # the edits in order apply to the live code
    _, problems, after = fx.check_edits(out.edits, dict(LIVE))
    assert problems == [] and "parse(text) or None" in after[PATH] and "check(" in after[PATH]
    assert out.log[0] == "Plan (planner-model): ok"
    assert out.log[2].endswith("ok im 2. Versuch") and out.log[-1].startswith("Test (")


def test_a_step_answers_in_marker_blocks_code_with_quotes_survives() -> None:
    """Live 2026-11-19: a step's JSON broke on the escaping of a docstring and kept no edit."""
    answer = (
        "<<<<<<< OLD\n    def run(self, text):\n        value = parse(text)\n=======\n"
        '    def run(self, text):\n        """A "quoted" docstring."""\n'
        "        value = parse(text)\n>>>>>>> NEW\n"
    )
    edits, files, problems = fs.read_answer(answer, PATH)
    assert problems == [] and files == [] and edits[0]["path"] == PATH
    assert '"""A "quoted" docstring."""' in edits[0]["new"]
    _, misfit, after = fx.check_edits(edits, dict(LIVE))
    assert misfit == [] and after[PATH].count('"""') == 2
    test = fs.read_answer("<<<<<<< FILE\ndef test_x():\n    pass\n>>>>>>> END", "hub/tests/t.py")
    assert test == ([], [{"path": "hub/tests/t.py", "content": "def test_x():\n    pass\n"}], [])
    # the path and a marker written into the content are dropped (gpt-oss, live)
    junk = "<<<<<<< FILE\nhub/tests/t.py\n=======\nimport uuid\n>>>>>>> END"
    assert fs.read_answer(junk, "hub/tests/t.py")[1][0]["content"] == "import uuid\n"
    # a block left open, and an answer in the old JSON form
    assert "no '>>>>>>> NEW'" in fs.read_answer("<<<<<<< OLD\nx\n=======\ny", PATH)[2][0]
    assert fs.read_answer(edit("a", "b"), PATH)[0] == [{"path": PATH, "old": "a", "new": "b"}]


@pytest.mark.asyncio
async def test_a_plan_that_misses_the_code_is_planned_once_more_with_the_reason() -> None:
    wrong = plan(steps=[{"path": PATH, "anchor": "def parse(raw_input):", "do": "x"}])
    good = plan(steps=[{"path": PATH, "anchor": "def parse(text):", "do": "x"}])
    asker = Asker([good, edit("    return text.strip()", "    return text"), TEST_FILE])
    out = await fs.run_steps(asker, read, "Frage", wrong, "p")
    assert out.failed == "" and asker.calls[0][0] == "issue_steps"
    assert (
        "ABGELEHNT" in asker.calls[0][1]["question"]
        and "def parse(raw_input):" in asker.calls[0][1]["question"]
    )
    # twice wrong: no steps run at all
    asker = Asker([wrong])
    out = await fs.run_steps(asker, read, "Frage", wrong, "p")
    assert out.failed.startswith("der Plan passt nicht") and len(asker.calls) == 1
    # a second answer that is no plan at all is shown
    asker = Asker(["Ich kann das nicht planen."])
    out = await fs.run_steps(asker, read, "Frage", wrong, "p")
    assert "Ich kann das nicht planen." in out.failed


@pytest.mark.asyncio
async def test_a_repair_builds_on_the_red_draft() -> None:
    """Live 2026-11-19: issue 2.7's first draft had the right code and a wrong test; the repair,
    planned anew, threw the code away. A repair keeps the draft's edits and may be only a test."""
    first = {"path": PATH, "old": "    return text.strip()\n", "new": "    return check(text)\n"}
    old_test = {"path": "hub/tests/test_x_empty.py", "content": "broken\n"}
    only_test = plan(steps=[])
    asker = Asker([TEST_FILE])
    out = await fs.run_steps(asker, read, "Frage", only_test, "p", base=([first], [old_test]))
    assert out.failed == "" and out.edits == [first]
    assert out.files == [{"path": "hub/tests/test_x_empty.py", "content": "def test(): pass\n"}]
    assert "return check(text)" in asker.calls[0][1]["done"]  # the test sees the draft's change
    # a repair step sees the code with the draft applied
    step = plan(steps=[{"path": PATH, "anchor": "return check(text)", "do": "strip it"}])
    asker = Asker([edit("    return check(text)", "    return check(text.strip())"), TEST_FILE])
    out = await fs.run_steps(asker, read, "Frage", step, "p", base=([first], [old_test]))
    assert out.failed == "" and len(out.edits) == 2
    _, problems, after = fx.check_edits(out.edits, dict(LIVE))
    assert problems == [] and "check(text.strip())" in after[PATH]
    # a step whose work the draft did already says NOTHING and is passed (live 2026-11-19)
    again = plan(steps=[{"path": PATH, "anchor": "return check(text)", "do": "call check"}])
    asker = Asker(["NOTHING", TEST_FILE])
    out = await fs.run_steps(asker, read, "Frage", again, "p", base=([first], [old_test]))
    assert out.failed == "" and out.edits == [first] and "nichts zu tun" in out.log[-2]
    # without a draft a plan with no steps is still refused
    assert fs.parse_plan(only_test)[1] == ["the plan has no steps"]


@pytest.mark.asyncio
async def test_a_step_that_misses_twice_ends_the_fix_with_its_reason() -> None:
    asker = Asker(
        [
            edit("nope", "x"),
            json.dumps({"edits": [{"path": "hub/other.py", "old": "a", "new": "b"}]}),
        ]
    )
    out = await fs.run_steps(asker, read, "Frage", plan(), "p")
    assert out.failed.startswith("Schritt 1") and "must change" in out.failed and out.edits == []
    assert "die letzte Antwort begann mit" in out.failed and "hub/other.py" in out.failed


# --- through the project: a run of issue_steps becomes one draft ----------------------------------


class StepBlocks:
    """issue_steps / fix_step runs that answer from a list."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.started: list[tuple[str, dict[str, Any]]] = []

    def programs(self) -> dict[str, dict[str, Any]]:
        from topcoat_hub.blocks.builtin import PROGRAMS

        return PROGRAMS

    def start(self, user: Any, program: dict[str, Any], inputs: dict[str, Any]) -> Any:
        self.started.append((program["name"], inputs))
        result = RunResult(status="ok", vars={"draft": self.answers.pop(0)}, authors={"draft": "m"})
        task: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        task.set_result(None)
        return SimpleNamespace(result=result, task=task)


class LiveTools:
    node = uuid.uuid4()

    async def proposable_tools(self, caller: Any) -> list[dict[str, Any]]:
        return [{"capability": "repo.edit", "node_id": str(self.node)}]

    async def invoke(self, caller: Any, node: Any, cap: str, data: dict[str, Any]) -> Any:
        text = LIVE.get(data.get("path", "")) if cap == "repo.read" else None
        if text is None:
            return {"ok": False, "error": {"message": "not a file of this repository"}}
        return {"ok": True, "output": {"text": text.rstrip("\n"), "end_line": 11}}


@pytest.mark.asyncio
async def test_a_run_of_issue_steps_becomes_one_draft(client: TestClient) -> None:
    from topcoat_hub.models import AgentTask
    from topcoat_hub.project_events import PERSON
    from topcoat_hub.project_runs import RunService

    sm = client.app.state.sessionmaker
    store = ProjectStore(sm)
    project = await store.create_project(USER, title=f"Steps {uuid.uuid4().hex[:6]}")
    issue = await store.create_node(
        USER, project.id, kind="task", title="Leere Texte",
        body="Zielanwendung: dev\nErgebnisform: Diagnose\nArt: issue\n",
    )  # fmt: skip
    runs = RunService(sm, SimpleNamespace(), PERSON)
    service = fx.FixService(sm, LiveTools(), SimpleNamespace(fix_spool_dir=""))
    runs.fixes = service
    runs.blocks = StepBlocks(
        [
            edit(
                "def parse(text):\n    return text.strip()",
                "def parse(text):\n    return check(text)",
            ),
            edit("    return check(text)", "    return check(text.strip())"),
            TEST_FILE,
        ]
    )
    task_id = uuid.uuid4()
    async with sm() as db:
        db.add(AgentTask(id=task_id, user_id=USER, prompt="fix", title="fix", status="done"))
        await db.commit()
    await runs._store.set_run(USER, issue.id, status="in_progress", task_id=task_id, reason="x")
    text, problems = await runs._fix_in_steps(USER, task_id, plan(), "planner")
    assert problems == [] and "**Schritte:**" in text and "wartet auf" in text
    assert [name for name, _ in runs.blocks.started] == ["fix_step"] * 3
    [fix] = await service.of_issue(USER, issue.id)
    assert (
        fix.state == "draft" and len(fix.edits) == 2 and fix.files[0]["path"].endswith("empty.py")
    )


def test_a_test_file_among_the_steps_becomes_the_plans_test() -> None:
    """Live 2026-11-21 (issue 2.10): gpt-oss put the new GUI test among the steps four times and
    named a Python test for a change of web/ only; the plan was refused each time."""
    doc = {
        "summary": "collapse",
        "steps": [
            {"path": "web/src/project/ProjectPage.tsx", "anchor": "a", "do": "collapse"},
            {"path": "web/src/project/ListView.tsx", "anchor": "b", "do": "count", "after": [1]},
            {"path": "web/src/project/collapse.test.tsx", "anchor": "N/A", "do": "renders 5"},
        ],
        "test": {"path": "hub/tests/test_collapse.py", "do": "python"},
    }
    plan, problems = fs.parse_plan(json.dumps(doc))
    assert problems == [] and plan is not None
    assert [s.path for s in plan.steps] == [
        "web/src/project/ProjectPage.tsx", "web/src/project/ListView.tsx",
    ]  # fmt: skip
    assert plan.test_path == "web/src/project/collapse.test.tsx" and plan.test_do == "renders 5"


def test_a_test_step_in_the_middle_keeps_the_after_of_the_others() -> None:
    doc = {
        "steps": [
            {"path": "hub/src/topcoat_hub/a.py", "anchor": "x", "do": "one"},
            {"path": "hub/tests/test_new.py", "anchor": "y", "do": "the test"},
            {"path": "hub/src/topcoat_hub/b.py", "anchor": "z", "do": "two", "after": [1]},
        ],
        "test": {"path": "hub/tests/test_given.py", "do": "given"},
    }
    plan, problems = fs.parse_plan(json.dumps(doc))
    assert problems == [] and plan is not None
    assert [(s.n, s.after) for s in plan.steps] == [(1, []), (2, [1])]
    assert plan.test_path == "hub/tests/test_given.py"  # a valid test of the plan stays


def test_an_anchor_that_is_clearly_one_line_of_the_file_is_taken() -> None:
    """Live 2026-11-21 (issue 2.10): `useState(new Set())` for `useState<Set<string>>(new Set())`;
    the hub named the line itself ("the file has: ...") and still refused the plan. A line that is
    only about as alike as another (the repair that anchored on a line its red draft had changed,
    0.76 against 0.73) is not guessed."""
    code = (
        "  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());\n"
        "  const [found, setFound] = useState<ProjectNode[] | null>(null);\n"
        "                  collapsed.has(n.id)\n"
        '                {collapsed.has(n.id) ? `▸ ${n.childCount}` : "▾"}\n'
    )
    path = "web/src/project/ProjectPage.tsx"
    plan = fs.Plan(
        "s",
        [fs.Step(1, path, "const [collapsed, setCollapsed] = useState(new Set());", "a")],
        "web/src/project/collapse.test.tsx",
        "t",
    )
    assert fs.plan_problems(plan, {path: code, plan.test_path: None}) == []
    assert plan.steps[0].anchor.startswith("const [collapsed, setCollapsed] = useState<Set")
    plan.steps = [fs.Step(1, path, '{collapsed.has(n.id) ? "▸" : "▾"}', "b")]
    [problem] = fs.plan_problems(plan, {path: code, plan.test_path: None})
    assert "is not in the file" in problem and "the file has" in problem


def test_an_anchor_with_two_alike_lines_is_still_refused() -> None:
    code = "  value = compute(alpha, beta)\n  value = compute(alpha, gamma)\n"
    path = "hub/src/topcoat_hub/x.py"
    plan = fs.Plan(
        "s", [fs.Step(1, path, "value = compute(alpha, delta)", "a")], "hub/tests/test_x.py", "t"
    )
    problems = fs.plan_problems(plan, {path: code, plan.test_path: None})
    assert problems and "is not in the file" in problems[0]


def test_a_plan_the_person_gave_may_have_finer_steps() -> None:
    """Live 2026-11-21: the plan of issue 2.10 needed ten small steps; six is the models' limit."""
    steps = [{"path": PATH, "symbol": f"f{n}", "do": "x"} for n in range(1, 11)]
    doc = {"summary": "s", "steps": steps, "test": {"path": "hub/tests/test_x.py", "do": "t"}}
    _plan, problems = fs.parse_plan(json.dumps(doc))
    assert problems == [f"at most {fs.MAX_FIX_STEPS} steps"]
    plan, problems = fs.parse_plan(json.dumps({**doc, "given": True}))
    assert problems == [] and plan is not None and len(plan.steps) == 10


# --- symbols instead of anchors (plan step 1.10, code_symbols.py) ---------------------------------

HUB_SRC = fs.__file__.rsplit("fix_steps.py", 1)[0]


def _live(*rels: str) -> dict[str, str | None]:
    """Real files of the hub as the live code (the refused plans of 2026-11-23 were about them)."""
    out: dict[str, str | None] = {}
    for rel in rels:
        with open(HUB_SRC + rel, encoding="utf-8") as f:
            out["hub/src/topcoat_hub/" + rel] = f.read()
    return out


def _symbol_plan(steps: list[dict[str, Any]]) -> fs.Plan:
    p, problems = fs.parse_plan(
        json.dumps({"summary": "s", "steps": steps, "test": {"path": "hub/tests/test_new_x.py",
                                                            "do": "it holds"}})
    )  # fmt: skip
    assert problems == [] and p is not None, problems
    return p


def test_the_refused_plans_of_2026_11_23_as_symbol_plans_are_taken() -> None:
    """2.18.3 named "147-148" as anchors, 2.16.1 a call that stands three times and an import
    line it remembered wrong, 2.19.3 line numbers again: by symbol they all fit."""
    svc = "hub/src/topcoat_hub/blocks/service.py"
    api = "hub/src/topcoat_hub/api/project.py"
    runs = "hub/src/topcoat_hub/project_runs.py"
    files = _live("blocks/service.py", "api/project.py", "project_runs.py")
    # 2.18.3 as it came: line numbers are refused with what to give instead
    _, problems = fs.parse_plan(
        json.dumps({"steps": [{"path": svc, "anchor": "147-148", "do": "retry"}],
                    "test": {"path": "hub/tests/test_new_x.py", "do": "x"}})
    )  # fmt: skip
    assert problems and "name the symbol" in problems[0]
    plan_218 = _symbol_plan([{"path": svc, "symbol": "HubBlockPorts.ask", "where": "in",
                              "do": "a cut answer is asked once more elsewhere"}])  # fmt: skip
    plan_216 = _symbol_plan([
        {"path": api, "symbol": "iterate_node", "do": "ProgramsBusy answers 429"},
        {"path": api, "symbol": "imports", "do": "import ProgramsBusy"},
    ])  # fmt: skip
    plan_219 = _symbol_plan([{"path": runs, "symbol": "_finish_checklist", "where": "in",
                              "do": "keep rows whose second look did not run"}])  # fmt: skip
    for p in (plan_218, plan_216, plan_219):
        assert fs.plan_problems(p, files) == []
    # the hub settles the names as the file has them, the anchor is the symbol's head line
    assert plan_219.steps[0].symbol == "RunService._finish_checklist"
    assert plan_219.steps[0].anchor.startswith("async def _finish_checklist(")
    assert plan_216.steps[1].anchor.startswith(("from ", "import "))
    # the step is shown the whole symbol, not 60 lines around a guess
    code = fs.code_at(files[svc] or "", plan_218.steps[0], [])
    assert "async def ask(" in code and "return text, model" in code
    assert "in `HubBlockPorts.ask`" not in fs.plan_text(plan_218)  # the words, not the code
    assert "inside `HubBlockPorts.ask`" in fs.plan_text(plan_218)


def test_a_symbol_that_is_not_there_names_the_nearest_ones_and_old_anchor_plans_still_go() -> None:
    p = _symbol_plan([{"path": PATH, "symbol": "Service.runn", "do": "x"}])
    [problem] = fs.plan_problems(p, dict(LIVE))
    assert "no symbol 'Service.runn'" in problem and "Service.run" in problem
    assert not any(ch.isdigit() for ch in problem.split("nearest names:")[1])  # names, no lines
    old, _ = fs.parse_plan(plan())
    assert old is not None and fs.plan_problems(old, dict(LIVE)) == []
    end = _symbol_plan([{"path": PATH, "where": "end", "do": "add a helper"}])
    assert fs.plan_problems(end, dict(LIVE)) == [] and end.steps[0].anchor == "return value"


def test_replace_symbol_writes_a_small_function_whole() -> None:
    answer = (
        "<<<<<<< REPLACE SYMBOL parse\n"
        "def parse(text):\n    if not text:\n        raise ValueError\n    return text.strip()\n"
        ">>>>>>> END\n"
    )
    edits, files, problems = fs.read_answer(answer, PATH)
    assert problems == [] and files == [] and edits[0]["symbol"] == "parse"
    plain, problems = fs.symbol_edits(edits, CODE, PATH)
    assert problems == [] and plain[0]["old"] == "def parse(text):\n    return text.strip()\n"
    _, misfit, after = fx.check_edits(plain, dict(LIVE))
    assert misfit == [] and "raise ValueError" in (after[PATH] or "")
    long = "def big():\n" + "    x = 1\n" * 100
    _, problems = fs.symbol_edits(
        [{"path": PATH, "symbol": "big", "new": "def big():\n    pass\n"}], long, PATH
    )
    assert problems and "OLD/NEW" in problems[0]


@pytest.mark.asyncio
async def test_a_symbol_step_runs_with_replace_symbol() -> None:
    sym_plan = json.dumps({
        "summary": "Leere Texte werden abgelehnt",
        "steps": [{"path": PATH, "symbol": "parse", "where": "in", "do": "refuse empty"}],
        "test": {"path": "hub/tests/test_x_empty.py", "do": "an empty text is refused"},
    })  # fmt: skip
    asker = Asker([
        "<<<<<<< REPLACE SYMBOL parse\ndef parse(text):\n    if not text:\n        raise "
        "ValueError\n    return text.strip()\n>>>>>>> END\n",
        TEST_FILE,
    ])  # fmt: skip
    out = await fs.run_steps(asker, read, "Leere Texte", sym_plan, "planner-model")
    assert out.failed == "" and len(out.edits) == 1
    step = asker.calls[0][1]
    assert "inside `parse`" in step["step"] and "def parse(text):" in step["code"]


# --- a test file that exists gets a free name (plan step 1.15) ------------------------------------


@pytest.mark.asyncio
async def test_a_test_file_that_exists_gets_the_next_free_name() -> None:
    live = {"hub/tests/test_x.py": "old", "hub/tests/test_x_2.py": "old too"}

    async def read_live(paths: list[str]) -> dict[str, str | None]:
        return {p: live.get(p) for p in paths}

    assert await fs.free_test_path("hub/tests/test_new.py", read_live) == (
        "hub/tests/test_new.py",
        "",
    )
    path, note = await fs.free_test_path("hub/tests/test_x.py", read_live)
    assert path == "hub/tests/test_x_3.py" and "test_x.py gibt es schon" in note
    web, _ = await fs.free_test_path("web/src/a/Panel.test.tsx", read_live)
    assert web == "web/src/a/Panel.test.tsx"
    assert fs._numbered("web/src/a/Panel.test.tsx", 2) == "web/src/a/Panel2.test.tsx"
    # in a plan: no refusal, and the JSON names the free file
    raw = json.loads(plan(test={"path": "hub/tests/test_x.py", "do": "t"}))
    p, _ = fs.parse_plan(json.dumps(raw))
    assert p is not None and await fs.settle_test(p, raw, read_live)
    assert p.test_path == raw["test"]["path"] == "hub/tests/test_x_3.py"
    assert fs.plan_problems(p, {**LIVE, **(await read_live([p.test_path]))}) == []
