"""The fix of an issue (issue_fixes.py, D103, D104): a draft is checked, the person's one click
applies it in the workshop and the tests run, a red test starts the next draft, a passed fix goes
to the host's spool, and the host's outcome is read back."""

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from topcoat_hub import issue_fixes as fx
from topcoat_hub.models import DEV_ADMIN_ID as USER
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.tools import Caller

GOOD = {
    "summary": "Fenster folgen beim Ziehen",
    "edits": [{"path": "hub/src/topcoat_hub/x.py", "old": "a = 1\n", "new": "a = 2\n"}],
    "files": [{"path": "hub/tests/test_x.py", "content": "def test_x():\n    assert True\n"}],
}

DIFF = (
    "diff --git a/hub/src/topcoat_hub/x.py b/hub/src/topcoat_hub/x.py\n"
    "--- a/hub/src/topcoat_hub/x.py\n+++ b/hub/src/topcoat_hub/x.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
    "diff --git a/hub/tests/test_x.py b/hub/tests/test_x.py\nnew file mode 100644\n"
    "--- /dev/null\n+++ b/hub/tests/test_x.py\n@@ -0,0 +1 @@\n+x\n"
)


# --- pure -----------------------------------------------------------------------------------------


def test_a_good_draft_is_read_and_shown_as_a_diff() -> None:
    summary, edits, files, problems = fx.parse_fix("```json\n" + json.dumps(GOOD) + "\n```")
    assert problems == [] and summary == GOOD["summary"] and edits == GOOD["edits"]
    text = fx.render(summary, edits, files)
    assert "-a = 1" in text and "+a = 2" in text and "neu: `hub/tests/test_x.py`" in text


def test_a_draft_outside_the_allowed_parts_or_without_change_is_no_fix() -> None:
    bad = {
        "edits": [
            {"path": "web/package.json", "old": "x", "new": "y"},
            {"path": "hub/../etc/passwd", "old": "x", "new": "y"},
            {"path": "hub/a.py", "old": "same", "new": "same"},
        ]
    }
    _, edits, _, problems = fx.parse_fix(json.dumps(bad))
    assert edits == [] and len(problems) == 4  # three edits and "changes nothing"
    assert fx.parse_fix("no json")[3] == ["the answer holds no JSON object"]


def test_the_diff_is_mapped_onto_the_overlay_tests_left_out_web_to_its_source() -> None:
    patch, left_out, refused = fx.overlay_patch(DIFF)
    assert "a/topcoat_hub/x.py" in patch and "b/topcoat_hub/x.py" in patch
    assert "hub/src" not in patch and left_out == ["hub/tests/test_x.py"] and refused == []
    web = (
        DIFF + "diff --git a/web/src/a.ts b/web/src/a.ts\n--- a/web/src/a.ts\n+++ b/web/src/a.ts\n"
    )
    # since 2026-11-20 the GUI's source goes along; the host builds it (hub-fix-overlay.sh)
    patch, _left, refused = fx.overlay_patch(web)
    assert refused == [] and "a/web-src/src/a.ts" in patch and "b/web-src/src/a.ts" in patch
    package = DIFF + "diff --git a/web/package.json b/web/package.json\n"
    assert fx.overlay_patch(package)[2] == ["web/package.json"]
    _s, _e, _f, problems = fx.parse_fix(
        '{"summary": "x", "edits": [{"path": "web/package.json", "old": "a", "new": "b"}]}'
    )
    assert problems and "web/src/" in problems[0]


# --- the service with a workshop that only answers ------------------------------------------------


class FakeTools:
    def __init__(
        self, test_state: str = "passed", edit_fails: bool = False, log: str = "1 passed"
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.test_state = test_state
        self.log = log
        self.edit_fails = edit_fails
        self.node = uuid.uuid4()

    async def proposable_tools(self, caller: Caller) -> list[dict[str, Any]]:
        return [{"capability": "repo.edit", "node_id": str(self.node), "node": "dev-node"}]

    async def invoke(self, caller: Caller, node: uuid.UUID, cap: str, data: dict[str, Any]) -> Any:
        self.calls.append(("invoke", cap, data))
        out: dict[str, Any] = {}
        if cap == "repo.status":
            out = {"clean": True, "head": "643dde6abcde", "branch": "topcoat/live"}
        elif cap == "repo.diff":
            out = {"diff": DIFF, "truncated": False, "masked": 0}
        elif cap == "repo.test_start":
            out = {"job_id": "a" * 24, "state": "running"}
        elif cap == "repo.test_status":
            out = {"running": False, "state": self.test_state, "log": self.log}
        return {"ok": True, "output": out}

    async def execute_confirmed(
        self,
        caller: Caller,
        node: uuid.UUID,
        cap: str,
        data: dict[str, Any],
        pid: uuid.UUID,
        digest: str,
    ) -> Any:
        self.calls.append(("confirmed", cap, data))
        if self.edit_fails and cap == "repo.edit":
            return {"ok": False, "error": {"message": "the old text does not occur"}}
        return {"ok": True, "output": {}}


class FakeRuns:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []

    async def start_node(self, user_id: uuid.UUID, node_id: uuid.UUID, **kw: Any) -> Any:
        self.started.append({"node": node_id, **kw})
        return SimpleNamespace(id=uuid.uuid4())


async def _issue(client: TestClient) -> Any:
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(USER, title=f"Fix {uuid.uuid4().hex[:6]}")
    topic = await store.create_node(USER, project.id, kind="topic", title="Issues")
    return await store.create_node(
        USER, project.id, kind="task", title="Fenster springt", parent_id=topic.id,
        body="Zielanwendung: dev\nProgramm: issue_check\nErgebnisform: Diagnose\nArt: issue\n",
    )  # fmt: skip


def _service(client: TestClient, tools: FakeTools, spool: Path | None = None) -> fx.FixService:
    settings = SimpleNamespace(fix_spool_dir=str(spool) if spool else "")
    service = fx.FixService(client.app.state.sessionmaker, tools, settings)
    service.runs = FakeRuns()
    return service


ADMIN = Caller(USER, True, "gui")


@pytest.mark.asyncio
async def test_apply_runs_the_workshop_steps_and_goes_back_to_live(client: TestClient) -> None:
    issue = await _issue(client)
    tools = FakeTools()
    service = _service(client, tools)
    fix = await service.create(USER, issue.id, None, GOOD["summary"], GOOD["edits"], GOOD["files"])
    with pytest.raises(fx.FixError):  # an API key, or no admin: refused
        await service.apply(Caller(USER, True, "api"), fix.id)
    await service.apply(ADMIN, fix.id)
    await asyncio.gather(*service._tasks)
    done = await service.get(USER, fix.id)
    assert done.state == "passed" and done.base_commit == "643dde6abcde"
    want = f"topcoat/issue-{issue.number.replace('.', '-')}-{fix.id.hex[:6]}"
    assert done.branch == want and "a = 2" in done.diff
    order = [c[1] for c in tools.calls]
    assert order == [
        "repo.status", "repo.start", "repo.edit", "repo.write", "repo.diff", "repo.commit",
        "repo.status", "repo.test_start", "repo.start", "repo.test_status",
    ]  # fmt: skip
    assert [c[0] for c in tools.calls if c[1] in ("repo.edit", "repo.write")] == ["confirmed"] * 2
    assert tools.calls[8][2]["topic"] == "live"  # back to the live code before the tests end


@pytest.mark.asyncio
async def test_a_red_test_starts_the_next_draft_with_the_output(client: TestClient) -> None:
    issue = await _issue(client)
    service = _service(client, FakeTools(test_state="failed"))
    fix = await service.create(USER, issue.id, None, "x", GOOD["edits"], [])
    await service.apply(ADMIN, fix.id)
    await asyncio.gather(*service._tasks)
    assert (await service.get(USER, fix.id)).state == "failed"
    [started] = service.runs.started
    assert started["program"] == "issue_fix" and started["iterate"] is True
    node = await ProjectStore(client.app.state.sessionmaker).get_node(USER, issue.id)
    assert (
        "1 passed" in node.next_iteration["feedback"]
        and "failed" in node.next_iteration["feedback"]
    )


@pytest.mark.asyncio
async def test_a_red_fix_in_steps_is_planned_again_in_steps(client: TestClient) -> None:
    """A fix from issue_steps gets issue_steps again, with the failed draft beside the output."""
    from topcoat_hub.models import AgentTask

    issue = await _issue(client)
    run = uuid.uuid4()
    async with client.app.state.sessionmaker() as db:
        db.add(AgentTask(id=run, user_id=USER, prompt="p", title="t", status="done",
                         diagnosis={"program": "issue_steps"}))  # fmt: skip
        await db.commit()
    service = _service(client, FakeTools(test_state="failed"))
    fix = await service.create(USER, issue.id, run, "x", GOOD["edits"], [])
    await service.apply(ADMIN, fix.id)
    await asyncio.gather(*service._tasks)
    [started] = service.runs.started
    assert started["program"] == "issue_steps"
    node = await ProjectStore(client.app.state.sessionmaker).get_node(USER, issue.id)
    assert "gescheiterte Entwurf" in node.next_iteration["feedback"]
    assert "+a = 2" in node.next_iteration["feedback"]


@pytest.mark.asyncio
async def test_an_edit_that_does_not_fit_leaves_the_workshop_on_live(client: TestClient) -> None:
    issue = await _issue(client)
    tools = FakeTools(edit_fails=True)
    service = _service(client, tools)
    fix = await service.create(USER, issue.id, None, "x", GOOD["edits"], [])
    await service.apply(ADMIN, fix.id)
    await asyncio.gather(*service._tasks)
    assert (await service.get(USER, fix.id)).state == "failed"
    last = tools.calls[-1]
    assert last[1] == "repo.start" and last[2]["topic"] == "live" and last[2]["discard"] is True


@pytest.mark.asyncio
async def test_a_passed_fix_goes_to_the_spool_and_the_outcome_comes_back(
    client: TestClient, tmp_path: Path
) -> None:
    issue = await _issue(client)
    (tmp_path / "request").mkdir()
    service = _service(client, FakeTools(), spool=tmp_path)
    fix = await service.create(USER, issue.id, None, "x", GOOD["edits"], GOOD["files"])
    with pytest.raises(fx.FixError, match="only a fix whose tests passed"):
        await service.rollout(ADMIN, fix.id)
    await service.apply(ADMIN, fix.id)
    await asyncio.gather(*service._tasks)
    with pytest.raises(fx.FixError):
        await service.rollout(Caller(USER, False, "gui"), fix.id)
    queued = await service.rollout(ADMIN, fix.id)
    assert queued.state == "queued"
    req = tmp_path / "request" / str(fix.id)
    manifest = json.loads((req / "manifest.json").read_text())
    assert manifest["label"] == f"fix-{issue.number}" and manifest["base_commit"] == "643dde6abcde"
    assert (req / "READY").exists() and "b/topcoat_hub/x.py" in (req / "fix.patch").read_text()
    assert manifest["left_out"] == ["hub/tests/test_x.py"]
    (tmp_path / "done").mkdir()
    (tmp_path / "done" / f"{fix.id}.json").write_text(
        json.dumps({"state": "live", "build": "643dde6-overlay+fix-1", "message": "ok"})
    )
    assert await service.sync_rollouts() == 1
    live = await service.get(USER, fix.id)
    assert live.state == "live" and live.rollout["build"] == "643dde6-overlay+fix-1"


def test_a_draft_is_held_against_the_live_code_before_any_click() -> None:
    """Live 2026-11-18: drafts quoted an invented body of ``_children`` and a placeholder ("# ...
    other code ..."); that showed only on the click. The real lines come back with the refusal."""
    code = "def a():\n    return 1\n\n\ndef b():\n    return 2\n    return 2\n"
    files: dict[str, str | None] = {"hub/x.py": code, "hub/gone.py": None}
    ok = {"path": "hub/x.py", "old": "def a():\n    return 1", "new": "x"}
    assert fx.edit_problems([ok], files) == []
    [missing] = fx.edit_problems(
        [{"path": "hub/x.py", "old": "def b():\n    return 3", "new": "x"}], files
    )
    assert "edit 1 (hub/x.py): the old text is not in the file" in missing
    assert "def b():\n    return 2" in missing and "lines 1-" in missing  # the real lines
    [twice] = fx.edit_problems([{"path": "hub/x.py", "old": "    return 2", "new": "x"}], files)
    assert "occurs 2 times" in twice
    [gone] = fx.edit_problems([{"path": "hub/gone.py", "old": "a", "new": "b"}], files)
    assert "does not exist" in gone
    # a file that was not read (the workshop was away) is left to the click
    assert fx.edit_problems([{"path": "hub/other.py", "old": "a", "new": "b"}], files) == []


def test_an_edit_may_change_what_an_earlier_edit_wrote() -> None:
    """Fixes in steps (2026-11-19): the second step quotes the function the first step added; held
    against the live file it was refused. The edits are checked in order, each on what the ones
    before left, and the state after them comes back."""
    code = "def a():\n    return 1\n"
    files: dict[str, str | None] = {"hub/x.py": code}
    first = {"path": "hub/x.py", "old": "def a():\n    return 1\n", "new": "def b():\n    pass\n"}
    second = {"path": "hub/x.py", "old": "def b():\n    pass", "new": "def b():\n    return 2"}
    edits, problems, state = fx.check_edits([first, second], files)
    assert problems == [] and edits == [first, second]
    assert state["hub/x.py"] == "def b():\n    return 2\n" and files["hub/x.py"] == code
    # the text the first edit replaced is gone for the second one
    again = {"path": "hub/x.py", "old": "    return 1", "new": "x"}
    [gone] = fx.check_edits([first, again], files)[1]
    assert gone.startswith("edit 2 (hub/x.py): the old text is not in the file")
    # a refused edit leaves the state as it was; numbers can start later (a step of a plan)
    _, [late], state = fx.check_edits([again], {"hub/x.py": "y\n"}, first=4)
    assert late.startswith("edit 4 ") and state["hub/x.py"] == "y\n"


async def test_verify_reads_the_whole_file_in_parts(client: TestClient) -> None:
    lines = [f"line {i}" for i in range(1, 801)]

    class Reading(FakeTools):
        async def invoke(self, caller: Caller, node: uuid.UUID, cap: str, data: Any) -> Any:
            if cap != "repo.read":
                return await super().invoke(caller, node, cap, data)
            start = data.get("start_line", 1)
            end = min(start + 399, len(lines))
            out = {"text": "\n".join(lines[start - 1 : end]), "end_line": end}
            return {"ok": True, "output": {**out, "more_after": end < len(lines)}}

    service = _service(client, Reading())
    good = [{"path": "hub/x.py", "old": "line 799\nline 800", "new": "x"}]
    assert (await service.verify(USER, good))[1] == []
    _, [bad] = await service.verify(USER, [{"path": "hub/x.py", "old": "line 9000", "new": "x"}])
    assert "not in the file" in bad


async def test_a_new_file_of_a_draft_must_not_exist_yet(client: TestClient) -> None:
    """Live 2026-11-18: a draft "created" hub/tests/test_project_runs.py, an existing file: the
    write would have dropped every test in it and the suite would still have passed."""

    class Reading(FakeTools):
        async def invoke(self, caller: Caller, node: uuid.UUID, cap: str, data: Any) -> Any:
            if cap != "repo.read":
                return await super().invoke(caller, node, cap, data)
            if data["path"] in ("hub/tests/test_old.py", "hub/src/topcoat_hub/x.py"):
                return {"ok": True, "output": {"text": "def test_x(): pass", "end_line": 1}}
            return {"ok": False, "error": {"message": "not a file of this repository"}}

    service = _service(client, Reading())
    # a test file that exists gets the next free name (1.15): no refusal, the draft is renamed
    old = [{"path": "hub/tests/test_old.py", "content": "x"}]
    assert (await service.verify(USER, [], old))[1] == []
    assert old[0]["path"] == "hub/tests/test_old_2.py"
    # any other file that exists is still refused: the write would replace it
    _, [problem] = await service.verify(
        USER, [], [{"path": "hub/src/topcoat_hub/x.py", "content": "x"}]
    )
    assert "exists already" in problem
    new = [{"path": "hub/tests/test_new.py", "content": "x"}]
    assert (await service.verify(USER, [], new))[1] == []


def test_an_old_text_that_misses_only_blank_lines_is_taken_from_the_code() -> None:
    """Live 2026-11-18: the models wrote one blank line where the code has two (and dropped
    spaces at line ends); every draft was refused for that alone."""
    code = "A = 1\n\n\nB = 2  \nC = 3\n\n\ndef f():\n    return A\n"
    quoted = "A = 1\n\nB = 2\nC = 3\n"
    assert fx.verbatim(code, quoted) == "A = 1\n\n\nB = 2  \nC = 3\n"
    [fitted] = fx.fit_edits([{"path": "p", "old": quoted, "new": "x"}], {"p": code})
    assert code.count(fitted["old"]) == 1
    # another word, or a quote that fits twice, stays as it was (and is refused)
    assert fx.verbatim(code, "A = 1\nB = 3") is None
    assert fx.verbatim("x = 1\n\nx = 1\n", "x = 1") is None
    # a part of a line is no whole line
    assert fx.verbatim(code, "return A") is None


async def test_a_draft_out_of_form_gets_its_next_draft_after_the_run(client: TestClient) -> None:
    """Live 2026-11-18: "edit 2: the new text is missing" ended the issue's drafts; now the draft
    is kept as failed and the next one starts like after a red test."""
    from topcoat_hub.project_events import PERSON
    from topcoat_hub.project_runs import RunService

    issue = await _issue(client)
    sm = client.app.state.sessionmaker
    runs = RunService(sm, SimpleNamespace(), PERSON)
    service = _service(client, FakeTools())
    runs.fixes = service
    task_id = uuid.uuid4()
    from topcoat_hub.models import AgentTask

    async with sm() as db:
        db.add(AgentTask(id=task_id, user_id=USER, prompt="fix", title="fix", status="done"))
        await db.commit()
    await runs._store.set_run(USER, issue.id, status="in_progress", task_id=task_id, reason="x")
    bad = json.dumps({"summary": "x", "edits": [{"path": "hub/a.py", "old": "a"}], "files": []})
    text, problems = await runs._fix_draft(USER, task_id, bad)
    assert problems and "unbrauchbar" in text
    [fix] = await service.of_issue(USER, issue.id)
    assert fix.state == "failed" and "Form" in (fix.error or "")
    assert task_id in runs._refused_fixes  # its next draft waits for the end of the run


def test_a_red_web_part_of_the_workshop_image_does_not_fail_a_hub_fix() -> None:
    """Live 2026-11-18: 3545 hub tests passed, the run was red on the image's old web packages."""
    web = "=== web: lint, typecheck, tests\nsrc/chat/Markdown.tsx(97,35): error TS2307: mermaid\n"
    green = "....\n3545 passed, 6 skipped, 3 warnings in 508.72s (0:08:28)\n"
    assert "TS2307" in (fx.web_only_failure(green + web) or "")
    red = "....\n1 failed, 3544 passed in 500.1s\n"
    assert fx.web_only_failure(red + web) is None
    assert fx.web_only_failure(green) is None  # no web part: nothing to excuse
    assert fx.web_only_failure("cut output\n" + web) is None  # the hub's summary is not there


WEB_RED = (
    "....\n3545 passed, 6 skipped in 508.72s\n"
    "=== web: lint, typecheck, tests\nsrc/project/ListView.tsx(3,1): error TS2307\n"
)


@pytest.mark.asyncio
async def test_a_red_web_part_fails_a_fix_of_the_gui_but_not_one_of_the_hub(
    client: TestClient,
) -> None:
    """The web part is excused only for a fix that does not touch web/ (the image's packages may be
    older); a fix of the GUI is judged by it."""
    issue = await _issue(client)
    service = _service(client, FakeTools(test_state="failed", log=WEB_RED))
    hub_fix = await service.create(USER, issue.id, None, "x", GOOD["edits"], [])
    await service.apply(ADMIN, hub_fix.id)
    await asyncio.gather(*service._tasks)
    assert (await service.get(USER, hub_fix.id)).state == "passed"
    gui = [{"path": "web/src/project/ListView.tsx", "old": "a", "new": "b"}]
    web_fix = await service.create(USER, issue.id, None, "y", gui, [])
    await service.apply(ADMIN, web_fix.id)
    await asyncio.gather(*service._tasks)
    assert (await service.get(USER, web_fix.id)).state == "failed"


def test_a_plan_may_change_the_gui_with_a_vitest_file() -> None:
    from topcoat_hub.fix_steps import parse_plan

    doc = {
        "summary": "x",
        "steps": [{"path": "web/src/project/ListView.tsx", "anchor": "a", "do": "b"}],
        "test": {"path": "web/src/project/collapse.test.tsx", "do": "collapsed from 3"},
    }
    plan, problems = parse_plan(json.dumps(doc))
    assert problems == [] and plan is not None
    doc["test"]["path"] = "web/src/project/collapse.tsx"
    assert parse_plan(json.dumps(doc))[1]


CODE_2_10 = (
    "  }, [api, fail, isProject, selected, filter, sort, tick]);\n"
    "\n"
    "  const select = (id: string) => {\n"
    "    setSelected(id);\n"
    '    setTab("tree");\n'
    "  };\n"
)


def test_lines_added_at_one_line_with_guessed_context_are_added_there() -> None:
    """Live 2026-11-21 (issue 2.10): "insert two functions before `const select`" came with a
    guessed `setTab("tree");` above the anchor, twice; the lines to add were right."""
    old = 'setTab("tree");\nconst select = (id: string) => {'
    new = (
        'setTab("tree");\nconst openNode = (id: string) => {\n  setOpened(id);\n};\n'
        "const select = (id: string) => {"
    )
    path = "web/src/project/ProjectPage.tsx"
    _edits, problems, after = fx.check_edits(
        [{"path": path, "old": old, "new": new}], {path: CODE_2_10}
    )
    assert problems == []
    text = after[path] or ""
    assert "  const openNode = (id: string) => {\n    setOpened(id);\n  };\n  const select" in text
    assert text.count('setTab("tree");') == 1  # the guessed context is not written twice
    # lines added after the line work the same way
    old = "const select = (id: string) => {\nsetWrong();"
    new = "const select = (id: string) => {\n  forget();\nsetWrong();"
    _e, problems, after = fx.check_edits(
        [{"path": path, "old": old, "new": new}], {path: CODE_2_10}
    )
    assert problems == [] and "  const select = (id: string) => {\n    forget();\n" in after[path]


def test_an_edit_that_changes_more_than_it_adds_is_still_refused() -> None:
    path = "web/src/project/ProjectPage.tsx"
    old = 'setTab("tree");\nconst select = (id: string) => {'
    new = 'setTab("list");\nconst select = (id: string) => {'
    _e, problems, _a = fx.check_edits([{"path": path, "old": old, "new": new}], {path: CODE_2_10})
    assert problems and "the old text is not in the file" in problems[0]


def test_a_block_quoted_with_the_wrong_indentation_takes_the_files() -> None:
    """Live 2026-11-21 (issue 2.10): the toggle button quoted with 16 spaces where the file has 14,
    in both tries."""
    code = (
        "            {hasChildren ? (\n"
        "              <button\n"
        '                type="button"\n'
        "              >\n"
        '                {collapsed.has(n.id) ? "▸" : "▾"}\n'
        "              </button>\n"
    )
    old = (
        "                <button\n"
        '                  type="button"\n'
        "                >\n"
        '                  {collapsed.has(n.id) ? "▸" : "▾"}\n'
        "                </button>"
    )
    new = old.replace('"▸"', "`▸ ${childCount}`")
    path = "web/src/project/ListView.tsx"
    _e, problems, after = fx.check_edits([{"path": path, "old": old, "new": new}], {path: code})
    assert problems == []
    assert '\n                {collapsed.has(n.id) ? `▸ ${childCount}` : "▾"}\n' in after[path]
    assert "              </button>" in after[path]
    # mixed indentation errors are not guessed
    mixed = old.replace("                </button>", "              </button>")
    _e, problems, _a = fx.check_edits([{"path": path, "old": mixed, "new": new}], {path: code})
    assert problems and "the old text is not in the file" in problems[0]


def test_a_kept_line_that_lost_its_first_characters_is_restored() -> None:
    """Live 2026-11-21 (issue 2.10): deleting `toggle` kept the comment below it as `** The nodes`
    instead of `/** The nodes`, and the GUI did not parse."""
    old = "function toggle() {\n  return 1;\n}\n\n/** The nodes in their order. */"
    new = "** The nodes in their order. */"
    assert fx.mended(old, new) == "/** The nodes in their order. */"
    # a line changed on purpose stays as written
    assert fx.mended("const a = 1;", "const a = 2;") == "const a = 2;"
    assert fx.mended("  value = compute(x)", "  value = compute(x, y)") == "  value = compute(x, y)"
    # a line whose original is still there is not touched
    assert fx.mended("// a\n// ab", "// a\n/ ab") == "// a\n// ab"
    code = "function toggle() {\n  return 1;\n}\n\n/** The nodes in their order. */\nexport f();\n"
    path = "web/src/project/ProjectPage.tsx"
    _e, problems, after = fx.check_edits([{"path": path, "old": old, "new": new}], {path: code})
    assert problems == [] and after[path] == "/** The nodes in their order. */\nexport f();\n"


# --- the same failure twice: stop (plan step 1.13, failure_print.py) ------------------------------

RED_LOG = (
    "E       AssertionError: assert 3 == 2\n"
    "FAILED hub/tests/test_x.py::test_x - AssertionError: assert 3 == 2\n1 failed, 9 passed\n"
)


async def _blocked_items(client: TestClient) -> list[Any]:
    from sqlalchemy import select

    from topcoat_hub.models import InboxItem

    async with client.app.state.sessionmaker() as db:
        mine = (InboxItem.user_id == USER, InboxItem.kind == fx.KIND_BLOCKED)
        return list(await db.scalars(select(InboxItem).where(*mine)))


@pytest.mark.asyncio
async def test_the_same_failure_twice_stops_the_drafts_and_tells_the_inbox(
    client: TestClient,
) -> None:
    issue = await _issue(client)
    service = _service(client, FakeTools(test_state="failed", log=RED_LOG))
    before = len(await _blocked_items(client))
    first = await service.create(USER, issue.id, None, "x", GOOD["edits"], GOOD["files"])
    await service.apply(ADMIN, first.id)
    await asyncio.gather(*service._tasks)
    assert len(service.runs.started) == 1  # a first failure gets its next draft
    node = await ProjectStore(client.app.state.sessionmaker).get_node(USER, issue.id)
    # the failure names the test: its last version is the place a new draft must change
    assert "Fehlerstelle (hub/tests/test_x.py)" in node.next_iteration["feedback"]
    assert "letzte Fassung an der Fehlerstelle" in node.next_iteration["feedback"]
    second = await service.create(USER, issue.id, None, "y", GOOD["edits"], [])
    await service.apply(ADMIN, second.id)
    await asyncio.gather(*service._tasks)
    assert len(service.runs.started) == 1  # the same failure again: no further draft
    items = await _blocked_items(client)
    assert len(items) == before + 1
    item = items[-1]
    assert issue.number in item.title and "same failure 2 times" in item.summary
    assert item.ref["node_id"] == str(issue.id)
    view = fx.fix_view(await service.get(USER, second.id))
    assert view["failure_print"] == fx.fix_view(await service.get(USER, first.id))["failure_print"]


@pytest.mark.asyncio
async def test_a_blocked_sub_task_of_a_large_task_still_tells_the_inbox(
    client: TestClient,
) -> None:
    """D109 lets the coordinator replan first, but nothing ticks it in the background yet: until
    then the person hears of the stop as for any issue (review of #103)."""
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(USER, title=f"Fix {uuid.uuid4().hex[:6]}")
    issue = await store.create_node(
        USER, project.id, kind="task", title="Teilaufgabe",
        body="Zielanwendung: dev\nProgramm: issue_investigate\nErgebnisform: Diagnose\n"
             "Art: issue\nVorhaben: 1\n",
    )  # fmt: skip
    service = _service(client, FakeTools(test_state="failed", log=RED_LOG))
    before = len(await _blocked_items(client))
    for summary in ("x", "y"):
        fix = await service.create(USER, issue.id, None, summary, GOOD["edits"], [])
        await service.apply(ADMIN, fix.id)
        await asyncio.gather(*service._tasks)
    assert len(service.runs.started) == 1  # the drafts stopped as for any issue
    assert len(await _blocked_items(client)) == before + 1


@pytest.mark.asyncio
async def test_another_failure_gets_its_next_draft(client: TestClient) -> None:
    issue = await _issue(client)
    tools = FakeTools(test_state="failed", log=RED_LOG)
    service = _service(client, tools)
    first = await service.create(USER, issue.id, None, "x", GOOD["edits"], [])
    await service.apply(ADMIN, first.id)
    await asyncio.gather(*service._tasks)
    tools.log = "E   KeyError: 'family'\nFAILED hub/tests/test_x.py::test_x - KeyError: 'family'\n"
    second = await service.create(USER, issue.id, None, "y", GOOD["edits"], [])
    await service.apply(ADMIN, second.id)
    await asyncio.gather(*service._tasks)
    assert len(service.runs.started) == 2


@pytest.mark.asyncio
async def test_a_draft_that_stays_the_same_where_it_failed_is_refused(client: TestClient) -> None:
    issue = await _issue(client)
    service = _service(client, FakeTools(test_state="failed", log=RED_LOG))
    assert await service.unchanged_at_failure(USER, issue.id, GOOD["edits"], GOOD["files"]) is None
    first = await service.create(USER, issue.id, None, "x", GOOD["edits"], GOOD["files"])
    await service.apply(ADMIN, first.id)
    await asyncio.gather(*service._tasks)
    # the failure names test_x.py: the same test again is refused, whatever else changes
    other_edit = [{"path": "hub/src/topcoat_hub/x.py", "old": "a = 1\n", "new": "a = 3\n"}]
    why = await service.unchanged_at_failure(USER, issue.id, other_edit, GOOD["files"])
    assert why and "hub/tests/test_x.py" in why
    changed = [{"path": "hub/tests/test_x.py", "content": "def test_x():\n    assert 2 == 2\n"}]
    assert await service.unchanged_at_failure(USER, issue.id, GOOD["edits"], changed) is None
