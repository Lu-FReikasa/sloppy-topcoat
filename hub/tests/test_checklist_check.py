"""A Development task's checklist worked through item by item (checklist_check, 2026-11-18)."""

import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_blocks import FakePorts
from tests.test_project_programs import (  # noqa: F401  (places is an autouse fixture)
    FakeRun,
    ScriptedLook,
    _child_with_read,
    _settle,
    places,
)
from topcoat_hub import agent_hooks
from topcoat_hub.agent_loop import Limits
from topcoat_hub.blocks import builtin
from topcoat_hub.blocks import program as prog
from topcoat_hub.blocks.engine import AgentOutcome, BlockEngine, RunResult
from topcoat_hub.models import DEV_ADMIN_ID as USER
from topcoat_hub.models import AgentTask, ProjectNode
from topcoat_hub.project_programs import (
    CHECKLIST_PROGRAM,
    checklist_items,
    default_program,
    without_checklist,
)
from topcoat_hub.project_revise import UNCONFIRMED
from topcoat_hub.project_store import ProjectStore

BODY = (
    "Zielanwendung: dev\nErgebnisform: Aussage | Befund | Beleg\n\nWo steckt Bild-Code?\n\n"
    "- [ ] report_images lesen\n- [x] message_tools.py prüfen\n"
)


def test_a_dev_task_with_a_checklist_runs_item_by_item() -> None:
    assert default_program("dev", "Bilder", BODY) == CHECKLIST_PROGRAM
    assert default_program("dev", "Bilder", "Zielanwendung: dev\n") is None  # no checklist
    assert default_program("research", "Bilder", BODY) == "research_report"
    assert default_program("dev", "Theme dunkel", BODY) == "theme_design"  # a theme stays one
    # the program only reads: a task that writes code stays a free run
    assert default_program("dev", "Bilder", "Modus: schreiben\n" + BODY) is None
    assert checklist_items(BODY) == ["report_images lesen", "message_tools.py prüfen"]
    rest = without_checklist(BODY)
    assert "Wo steckt Bild-Code?" in rest and "report_images" not in rest


def test_the_program_ships_valid_and_runs_each_item_in_its_own_agent_block() -> None:
    checked = prog.validate(builtin.PROGRAMS[CHECKLIST_PROGRAM])
    loop = checked["stack"][-1]
    assert loop["b"] == "for_each" and loop["in"] == "items"
    assert [b["b"] for b in loop["stack"]] == ["agent", "ask"]
    agent = loop["stack"][0]
    assert "{{hint}}" in agent["prompt"] and "ONE item" in agent["prompt"]
    # the orientation is read once, before the items: the wiki pages too (live 2026-11-18: every
    # item read up to ten wiki pages itself and ran into the time limit)
    assert "{{page1}}" in agent["prompt"] and "do not read" in agent["prompt"]
    assert agent["hooks"]["max_calls"] == builtin.ITEM_CALLS and loop["max_parallel"] == 2
    assert "language of the task" in agent["prompt"]


def test_a_call_cap_never_lifts_the_frame_of_the_application() -> None:
    assert agent_hooks.apply(Limits(max_tool_calls=60), {"max_calls": 10}).max_tool_calls == 10
    assert agent_hooks.apply(Limits(max_tool_calls=8), {"max_calls": 10}).max_tool_calls == 8


LOOP = {
    "name": "loop",
    "title": "loop",
    "inputs": {"items": "texts"},
    "stack": [
        {
            "b": "for_each",
            "in": "items",
            "as": "item",
            "collect": "row",
            "into": "rows",
            "max_parallel": 2,
            "stack": [
                {"b": "agent", "app": "dev", "prompt": "{{item}}", "out": "found"},
                {"b": "ask", "prompt": "row of {{found}}", "out": "row"},
            ],
        }
    ],
}


class SlowAgents(FakePorts):
    """An agent per item that takes its time: the first item the longest."""

    async def agent(self, prompt, application, depth, picked, hooks=None, progress=None):  # type: ignore[no-untyped-def]
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(0.05 if prompt == "a" else 0.01)
        finally:
            self.running -= 1
        return AgentOutcome(True, f"found {prompt}", model="m", task_id=f"t-{prompt}")


def test_items_run_side_by_side_each_with_its_own_item_and_come_back_in_order() -> None:
    ports = SlowAgents(lambda model, messages: messages[-1]["content"])
    result = asyncio.run(BlockEngine(ports).run(LOOP, {"items": ["a", "b", "c"]}))
    assert result.status == "ok" and ports.peak == 2
    # "a" ended last, its row still comes first; no round saw another round's item
    assert result.vars["rows"] == ["row of found a", "row of found b", "row of found c"]
    assert result.collected_tasks["rows"] == [["t-a"], ["t-b"], ["t-c"]]
    assert "item" not in result.vars and "found" not in result.vars


def test_a_helping_call_that_fails_leaves_an_empty_answer_and_the_run_goes_on() -> None:
    program = {
        "name": "pages",
        "title": "pages",
        "inputs": {"items": "texts"},
        "stack": [
            {"b": "ask", "as": "texts", "prompt": "pages?", "on_fail": "empty", "out": "pages"},
            {"b": "ask", "prompt": "after {{pages}}", "out": "row"},
        ],
    }
    ports = FakePorts(lambda model, messages: "no list at all")
    result = asyncio.run(BlockEngine(ports).run(program, {"items": []}))
    assert result.status == "ok" and result.vars["pages"] == []
    assert any("no JSON list" in n for n in result.notes)


class ChecklistBlocks:
    """The program's runs in order: the whole checklist first, then one item at a time."""

    def __init__(self, results: list[RunResult]) -> None:
        self.results = results
        self.started: list[dict[str, Any]] = []
        self.programs_started: list[dict[str, Any]] = []

    def programs(self) -> dict[str, dict[str, Any]]:
        return {CHECKLIST_PROGRAM: builtin.PROGRAMS[CHECKLIST_PROGRAM]}

    def start(self, user: Any, program: dict[str, Any], inputs: dict[str, Any]) -> FakeRun:
        self.started.append(inputs)
        self.programs_started.append(program)
        result = self.results[len(self.started) - 1]
        result.vars = {**inputs, **result.vars}  # the engine starts from the inputs
        return FakeRun(result)


def _rows(*rows: str, tasks: list[list[str]]) -> RunResult:
    return RunResult(status="ok", vars={"rows": list(rows)}, collected_tasks={"rows": tasks})


async def _run(
    client: TestClient, blocks: ChecklistBlocks, look: Any = None
) -> tuple[ProjectNode, AgentTask]:
    runs = client.app.state.project_runs
    runs.second_look, runs.reviser, runs.blocks = look or ScriptedLook(), None, blocks
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(USER, title=f"Dev {uuid.uuid4().hex[:8]}")
    node = await store.create_node(USER, project.id, kind="task", title="Bild-Code", body=BODY)
    await runs.create_batch(USER, project.id, [node.id], max_runs=1)
    await _settle(runs)
    async with client.app.state.sessionmaker() as db:
        done = await db.scalar(select(ProjectNode).where(ProjectNode.id == node.id))
        task = await db.get(AgentTask, done.task_id)
    return done, task


@pytest.mark.asyncio
async def test_a_refused_item_runs_once_more_with_the_reason_and_its_new_row_stands(
    client: TestClient,
) -> None:
    good = await _child_with_read(client, "report_images.py:12 class ReportImage")
    wrong = await _child_with_read(client, "message_tools.py:189 image_url")
    better = await _child_with_read(client, "message_tools.py:189 image_url")
    blocks = ChecklistBlocks(
        [
            _rows(
                '{"Aussage": "a", "Befund": "aktuell", "Beleg": "E1"}',
                '{"Aussage": "b", "Befund": "aktuell", "Beleg": "E1"}',  # the reviewer objects
                tasks=[[good], [wrong]],
            ),
            _rows('{"Aussage": "b", "Befund": "veraltet", "Beleg": "E1"}', tasks=[[better]]),
        ]
    )
    done, task = await _run(client, blocks)
    first, again = blocks.started
    assert first["items"] == ["report_images lesen", "message_tools.py prüfen"]
    assert first["hint"] == "" and "report_images lesen" not in first["task"]
    # only the refused item runs again, told why, never shown its refused row
    assert again["items"] == ["message_tools.py prüfen"]
    assert "E1 says otherwise" in again["hint"] and "aktuell" not in again["hint"]
    assert [r["Befund"] for r in task.diagnosis["table"]] == ["aktuell", "veraltet"]
    assert task.diagnosis["second_look"]["rounds"] == 1 and task.diagnosis["removed"] == []
    assert done.status == "in_review"


@pytest.mark.asyncio
async def test_an_item_refused_twice_is_removed_with_the_reason_and_the_task_blocked(
    client: TestClient,
) -> None:
    good = await _child_with_read(client, "report_images.py:12 class ReportImage")
    wrong = await _child_with_read(client, "nothing about it")
    blocks = ChecklistBlocks(
        [
            _rows(
                '{"Aussage": "a", "Befund": "aktuell", "Beleg": "E1"}',
                '{"Aussage": "stubborn", "Befund": "aktuell", "Beleg": "E1"}',
                tasks=[[good], [wrong]],
            ),
            _rows('{"Aussage": "stubborn", "Befund": "aktuell", "Beleg": "E1"}', tasks=[[wrong]]),
        ]
    )
    done, task = await _run(client, blocks)
    assert len(blocks.started) == 2  # one more run per refused item, not a loop
    assert [r["Aussage"] for r in task.diagnosis["table"]] == ["a"]
    (gone,) = task.diagnosis["removed"]
    assert gone["Aussage"].startswith("message_tools.py prüfen")
    assert gone["Grund"] == f"{UNCONFIRMED}: E1 says otherwise"
    assert done.status == "blocked"  # one item of two is not answered


@pytest.mark.asyncio
async def test_an_item_whose_run_found_nothing_runs_again(client: TestClient) -> None:
    good = await _child_with_read(client, "report_images.py:12 class ReportImage")
    late = await _child_with_read(client, "message_tools.py:189 image_url")
    blocks = ChecklistBlocks(
        [
            _rows('{"Aussage": "a", "Befund": "aktuell", "Beleg": "E1"}', "", tasks=[[good], []]),
            _rows('{"Aussage": "c", "Befund": "aktuell", "Beleg": "E1"}', tasks=[[late]]),
        ]
    )
    done, task = await _run(client, blocks)
    assert "found nothing" in blocks.started[1]["hint"]
    assert len(task.diagnosis["table"]) == 2 and done.status == "in_review"


class LookWithAnotherModel(ScriptedLook):
    async def other_model(self, author: str | None, application: str) -> str | None:
        return "coder@node-amd"


@pytest.mark.asyncio
async def test_a_row_citing_everything_runs_again_on_another_model(client: TestClient) -> None:
    good = await _child_with_read(client, "report_images.py:12 class ReportImage")
    broad = await _child_with_read(client, "many reads")
    fine = await _child_with_read(client, "message_tools.py:189 image_url")
    blocks = ChecklistBlocks(
        [
            _rows(
                '{"Aussage": "a", "Befund": "aktuell", "Beleg": "E1"}',
                '{"Aussage": "c", "Befund": "aktuell", "Beleg": "E1, E2, E3, E4, E5"}',
                tasks=[[good], [broad]],
            ),
            _rows('{"Aussage": "c", "Befund": "aktuell", "Beleg": "E1"}', tasks=[[fine]]),
        ]
    )
    runs = client.app.state.project_runs
    await _run(client, blocks)
    assert "cites 5 reads" in blocks.started[1]["hint"]
    # the new run of the item goes to another model than the one that wrote the row
    runs.second_look = LookWithAnotherModel()
    blocks = ChecklistBlocks(
        [
            _rows(
                '{"Aussage": "a", "Befund": "aktuell", "Beleg": "E1"}',
                '{"Aussage": "b", "Befund": "aktuell", "Beleg": "E1"}',
                tasks=[[good], [broad]],
            ),
            _rows('{"Aussage": "b", "Befund": "veraltet", "Beleg": "E1"}', tasks=[[fine]]),
        ]
    )
    runs.blocks = blocks
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(USER, title=f"Dev {uuid.uuid4().hex[:8]}")
    node = await store.create_node(USER, project.id, kind="task", title="Bild-Code", body=BODY)
    await runs.create_batch(USER, project.id, [node.id], max_runs=1)
    await _settle(runs)
    readers = [
        b.get("pick")
        for b in blocks.programs_started[1]["stack"][-1]["stack"]
        if b.get("role") == "reader"
    ]
    assert readers == [{"fixed": "coder@node-amd"}]
    assert blocks.programs_started[0]["stack"][-1]["stack"][0].get("pick") is None


def test_the_filed_result_says_where_each_cited_read_was_made() -> None:
    # live 2026-11-18: the result of 3.1.2 said only "E14, E9" and nobody could look them up
    from topcoat_hub.project_runs import result_document

    rows = [{"Ergebnis": "keine Bildtools", "Beleg": "E14, E9"}, {"Ergebnis": "x", "Beleg": "E2"}]
    reads = [
        ("E2", {"capability": "wiki.read", "input": {"path": "B Hub/B4 Chat"}}, "the page"),
        ("E3", {"capability": "repo.read", "input": {"path": "a.py"}}, "a.py: lines 1-9 of 9"),
        (
            "E9",
            {"capability": "repo.read", "input": {"path": "hub/chat_tools.py", "start_line": 1}},
            "hub/chat_tools.py: lines 1-200 of 834\nimport json",
        ),
        (
            "E14",
            {"capability": "repo.read", "input": {"path": "hub/tools.py", "start_line": 160}},
            "hub/tools.py: lines 160-361 of 762 (1 secrets masked)\n  for n in config",
        ),
    ]
    text = result_document({"table": rows}, "in_review", "ok", "t", ["Ergebnis", "Beleg"], reads)
    sources = text.split("**Quellen:**\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert sources == [
        "- **E2** — `wiki.read B Hub/B4 Chat`",
        "- **E9** — `hub/chat_tools.py: lines 1-200 of 834` (repo.read)",
        "- **E14** — `hub/tools.py: lines 160-361 of 762 (1 secrets masked)` (repo.read)",
    ]  # E3 is not cited: not listed
    assert text.rstrip().endswith("_Lauf: t_")


class NoLook:
    """A reviewer that cannot run (live 2026-11-22: its one slot was taken by three programs)."""

    async def check(self, question, table, excerpts, author, reviewer=None):  # type: ignore[no-untyped-def]
        from topcoat_hub.project_check import SecondLookResult

        return SecondLookResult(False, "the second look could not run in time", None)


@pytest.mark.asyncio
async def test_rows_whose_look_could_not_run_stay_unchecked(client: TestClient) -> None:
    """Issue 2.19: a second look that did not run is no negative verdict. Task 3.3.3 lost three
    right rows that way and ended with 0 rows."""
    a = await _child_with_read(client, "report_images.py:12 class ReportImage")
    b = await _child_with_read(client, "message_tools.py:189 image_url")
    blocks = ChecklistBlocks(
        [
            _rows(
                '{"Aussage": "a", "Befund": "aktuell", "Beleg": "E1"}',
                '{"Aussage": "c", "Befund": "aktuell", "Beleg": "E1"}',
                tasks=[[a], [b]],
            )
        ]
    )
    _, task = await _run(client, blocks, NoLook())
    assert len(blocks.started) == 1  # no verdict: nothing runs again
    assert [r["Aussage"] for r in task.diagnosis["table"]] == ["a", "c"]
    assert task.diagnosis["removed"] == []
    look = task.diagnosis["second_look"]
    assert look["ok"] is False and len(look["unchecked"]) == 2
    assert "2 unchecked" in look["reason"]


def test_an_unchecked_document_says_so() -> None:
    from topcoat_hub.project_programs import render_document

    row = {"Abschnitt": "Zweck", "Text": "Ein Satz.", "Beleg": "E1"}
    checked = render_document("T", [row], [], [], "b1", "2026-11-24")
    unchecked = render_document("T", [row], [], [], "b1", "2026-11-24", unchecked=True)
    assert "geprüft" in checked and "ungeprüft" not in checked
    assert "**ungeprüft**" in unchecked and "Ein Satz." in unchecked
