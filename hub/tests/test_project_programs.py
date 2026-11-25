"""A project task that names a block program (W3): reading the task, the rows, the sources."""

import asyncio
import json
import re
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_project_runs import FakeLook
from topcoat_hub.agent_store import AgentStore
from topcoat_hub.blocks.engine import RunResult
from topcoat_hub.models import DEV_ADMIN_ID as USER
from topcoat_hub.models import AgentEvidence, AgentTask, ProjectNode
from topcoat_hub.project_check import SecondLookResult
from topcoat_hub.project_programs import (
    UNKNOWN,
    Source,
    area_of,
    expand_rows,
    files_of,
    gathered_rows,
    models_of,
    number_sources,
    page_of,
    parse_rows,
    program_of,
    render_document,
)
from topcoat_hub.project_revise import UNCONFIRMED, parse_revision, unknown_refs
from topcoat_hub.project_store import ProjectStore


@pytest.fixture(autouse=True)
def places(client: TestClient) -> None:
    """No node is connected in the test hub: the batch gets two places."""

    async def two(targets: set[str]) -> int:
        return 2

    client.app.state.project_runs.places = two


BODY = (
    "Zielanwendung: dev\nProgramm: wiki_check\nSeite: Hub Aufbau~abc.md\n"
    "Ergebnisform: Aussage | Befund | Beleg\n"
)


def test_a_task_names_its_program_and_its_page() -> None:
    assert program_of(BODY) == "wiki_check" and page_of(BODY) == "Hub Aufbau~abc.md"
    assert program_of("Zielanwendung: dev\n") is None and page_of("") is None
    assert program_of("Programm: Wiki Check\n") is None  # not a program name


def test_rows_are_read_as_objects_and_a_round_without_one_is_none() -> None:
    rows = parse_rows(['{"Aussage": "a"}', "", "no object", 'Here: {"Aussage": "b",}'])
    assert rows[0] == {"Aussage": "a"} and rows[1] is None and rows[2] is None
    assert rows[3] == {"Aussage": "b"}  # a trailing comma is repaired


def test_sources_get_one_number_per_run_and_a_beleg_follows_them() -> None:
    tools = [{"ref": "F1", "text": "guide", "node": "hub"}, {"ref": "F2", "text": "list"}]
    rows = [
        {"Aussage": "a", "Befund": "veraltet", "Beleg": "F2, E1 Zeile 3"},
        None,  # a round that wrote nothing
        {"Aussage": "c", "Befund": "aktuell", "Beleg": "E2 and E9"},
    ]
    reads = [
        [{"ref": "E1", "excerpt": "first read", "tag": "live"}],
        [],
        [
            {"ref": "E1", "excerpt": "", "tag": "live"},
            {"ref": "E2", "excerpt": "kept", "tag": "doc"},
        ],
    ]
    table, sources = number_sources(rows, tools, reads)
    assert [s.ref for s in sources] == ["E1", "E2", "E3", "E4"]
    assert [s.excerpt for s in sources] == ["guide", "list", "first read", "kept"]
    assert table[0]["Beleg"] == "E2, E3 Zeile 3"  # F2 is the second tool read, E1 the row's read
    # the row's own E2 is E4; E9 it never read names no source, not another one's number
    assert table[1]["Beleg"] == f"E4 and {UNKNOWN}"
    assert len(table) == 2 and table[0]["Aussage"] == "a"
    # a row that names no source the run has is left out; the others stay
    lone, _ = number_sources(
        [{"Aussage": "x", "Beleg": "-"}, {"Aussage": "y", "Beleg": "F1"}], tools, []
    )
    assert [r["Aussage"] for r in lone] == ["y"]


def test_every_row_list_counts_in_the_order_written_with_its_own_tasks() -> None:
    texts, tasks = gathered_rows(
        {"page": "p", "rows_facts": ['{"a": 1}', '{"a": 2}'], "open": ["o"], "rows": ['{"a": 3}']},
        {"rows": [["t1", "t2"]]},
    )
    assert texts == ['{"a": 1}', '{"a": 2}', '{"a": 3}']
    assert tasks == [[], [], ["t1", "t2"]]  # rows from the facts have no agent task


# --- through the project: a batch runs the program, the project judges its rows ------------------


class FakeRun:
    def __init__(self, result: RunResult) -> None:
        self.result = result
        self.task: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        self.task.set_result(None)


class FakeBlocks:
    def __init__(self, result: RunResult) -> None:
        self.result = result
        self.started: list[dict[str, Any]] = []

    def programs(self) -> dict[str, dict[str, Any]]:
        return {"wiki_check": {"name": "wiki_check", "inputs": {"page": "text", "task": "text",
                                                                "form": "text"}}}  # fmt: skip

    def start(self, user: Any, program: dict[str, Any], inputs: dict[str, Any]) -> FakeRun:
        self.started.append(inputs)
        return FakeRun(self.result)


async def _child_with_read(client: TestClient, excerpt: str) -> str:
    """An agent task of a row (an agent block's run) that kept one read, E1."""
    tid = uuid.uuid4()
    async with client.app.state.sessionmaker() as db:
        db.add(AgentTask(id=tid, user_id=USER, prompt="claim", title="claim", status="done"))
        await db.flush()
        db.add(AgentEvidence(task_id=tid, user_id=USER, ref="E1", tag="live", source={},
                             excerpt=excerpt, bytes=len(excerpt)))  # fmt: skip
        await db.commit()
    return str(tid)


async def _settle(runs: Any) -> None:
    for _ in range(50):
        if not runs._watchers:
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_program_task_runs_the_program_and_its_rows_are_judged_like_any_result(
    client: TestClient,
) -> None:
    runs = client.app.state.project_runs
    look = FakeLook()
    runs.second_look = look
    runs.reviser = None  # the plain path: the project's own second look judges the rows
    child = await _child_with_read(client, "config.py:106 review_tick_seconds = 0.0")
    result = RunResult(
        status="ok",
        vars={"rows": ['{"Aussage": "Review 900 s", "Befund": "unzutreffend", "Beleg": "E1"}',
                       '{"Aussage": "Head 0038", "Befund": "veraltet", "Beleg": "F1"}']},
        sources=[{"ref": "F1", "text": "na01_node_aliases.py", "node": "dev", "call": {}}],
        collected_tasks={"rows": [[child], []]},
    )  # fmt: skip
    runs.blocks = FakeBlocks(result)

    async def page(user_id: uuid.UUID, path: str) -> str:
        return f"the text of {path}"

    runs._page_text = page
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(USER, title=f"Wiki {uuid.uuid4().hex[:8]}")
    node = await store.create_node(USER, project.id, kind="task", title="Hub Aufbau", body=BODY)
    await runs.create_batch(USER, project.id, [node.id], max_runs=1)
    await _settle(runs)

    inputs = runs.blocks.started[0]
    assert inputs["page"] == "the text of Hub Aufbau~abc.md"
    assert inputs["form"] == "Aussage | Befund | Beleg" and "Programm: wiki_check" in inputs["task"]
    async with client.app.state.sessionmaker() as db:
        done = await db.scalar(select(ProjectNode).where(ProjectNode.id == node.id))
        task = await db.get(AgentTask, done.task_id)
        kept = await AgentStore(db, USER).evidence(task.id)
    assert task.kind == "program" and task.status == "done"
    # the tool read is E1, the row's own read E2: each Beleg points at its own source
    assert [r["Beleg"] for r in task.diagnosis["table"]] == ["E2", "E1"]
    assert [k.excerpt for k in kept] == [
        "na01_node_aliases.py",
        "config.py:106 review_tick_seconds = 0.0",
    ]
    assert look.calls == 1 and done.status == "in_review"


@pytest.mark.asyncio
async def test_a_program_cut_off_by_a_restart_goes_back_into_the_queue(client: TestClient) -> None:
    runs = client.app.state.project_runs
    runs.second_look = FakeLook()
    never = SimpleNamespace(result=RunResult(), task=asyncio.get_event_loop().create_future())
    blocks = FakeBlocks(RunResult())
    blocks.start = lambda user, program, inputs: never  # type: ignore[method-assign]
    runs.blocks = blocks

    async def page(user_id: uuid.UUID, path: str) -> str:
        return "text"

    runs._page_text = page
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(USER, title=f"Wiki {uuid.uuid4().hex[:8]}")
    node = await store.create_node(USER, project.id, kind="task", title="Seite", body=BODY)
    await runs.create_batch(USER, project.id, [node.id], max_runs=3)
    # a new process: it knows nothing of the run that was going
    runs._programs.clear()
    for w in list(runs._watchers):
        w.cancel()
    await runs.kick_all()
    async with client.app.state.sessionmaker() as db:
        first = await db.scalars(
            select(AgentTask).where(AgentTask.kind == "program", AgentTask.user_id == USER)
        )
        ended = [t for t in first.all() if t.stop_reason == "hub_restart"]
    assert ended and ended[0].status == "failed"
    async with client.app.state.sessionmaker() as db:
        again = await db.scalar(select(ProjectNode).where(ProjectNode.id == node.id))
    # back in the queue, and started again on the same kick (the batch had runs left)
    assert again.status == "in_progress" and again.task_id != ended[0].id
    never.task.cancel()


@pytest.mark.asyncio
async def test_a_program_the_hub_does_not_have_stops_the_batch_with_the_reason(
    client: TestClient,
) -> None:
    runs = client.app.state.project_runs
    runs.blocks = FakeBlocks(RunResult())
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(USER, title=f"Wiki {uuid.uuid4().hex[:8]}")
    body = BODY.replace("wiki_check", "no_such_program")
    node = await store.create_node(USER, project.id, kind="task", title="Seite", body=body)
    await runs.create_batch(USER, project.id, [node.id], max_runs=1)
    batch = (await runs.batches(USER, project.id))[0]
    assert batch.status == "stopped" and "no_such_program" in (batch.stop_reason or "")


# --- the author revises what the second look does not confirm (W3b) -------------------------------


def test_a_task_names_a_model_per_role() -> None:
    body = (
        BODY
        + "Modelle: judge=Qwen3.5-4B-Q4_K_M@node-amd, author=gemma-4-26B, reviewer=x@node-nvidia\n"
    )
    assert models_of(body) == {
        "judge": "Qwen3.5-4B-Q4_K_M@node-amd",
        "author": "gemma-4-26B",
        "reviewer": "x@node-nvidia",
    }
    assert models_of(BODY) == {}


def test_a_revision_keeps_whole_rows_and_its_reasons_and_may_not_invent_a_source() -> None:
    answer = (
        '{"rows": [{"Aussage": "a", "Befund": "veraltet", "Beleg": "E2"}, {"Aussage": "b"}],'
        ' "removed": [{"Aussage": "c", "Grund": "kein Beleg"}]}'
    )
    rows, removed = parse_revision(answer, ["Aussage", "Befund", "Beleg"])
    assert rows == [{"Aussage": "a", "Befund": "veraltet", "Beleg": "E2"}]  # b lacks columns
    assert removed == [{"Aussage": "c", "Grund": "kein Beleg"}]
    assert parse_revision("no object", ["Aussage"]) is None
    table = [{"Aussage": "a", "Beleg": "E2"}, {"Aussage": "b", "Beleg": "E9 and F3"}]
    assert unknown_refs(table, {"E1", "E2"}) == [2]


class ScriptedLook:
    """A reviewer that objects to rows whose Befund is still 'aktuell' for statement 'b'."""

    def __init__(self) -> None:
        self.tables: list[list[dict[str, Any]]] = []

    async def check(self, question, table, excerpts, author, reviewer=None):  # type: ignore[no-untyped-def]
        self.tables.append([dict(r) for r in table])
        bad = {n: "E1 says otherwise" for n, r in enumerate(table, 1)
               if r["Aussage"] in ("b", "stubborn") and r["Befund"] == "aktuell"}  # fmt: skip
        if bad:
            return SecondLookResult(False, "objected", "rev@node", bad)
        return SecondLookResult(True, "ok", "rev@node")


class ScriptedAuthor:
    def __init__(self, fixes: bool) -> None:
        self.fixes = fixes
        self.calls = 0

    async def author(self, model: Any) -> str:
        return model or "author-model"

    async def revise(self, model, task, table, objections, excerpts):  # type: ignore[no-untyped-def]
        self.calls += 1
        rows = [dict(r) for r in table]
        for n in objections:
            if self.fixes and rows[n - 1]["Aussage"] == "b":
                rows[n - 1]["Befund"] = "veraltet"
        return rows, []


@pytest.mark.asyncio
async def test_the_author_fixes_an_objection_and_the_fixed_table_is_confirmed(
    client: TestClient,
) -> None:
    runs = client.app.state.project_runs
    runs.second_look, runs.reviser = ScriptedLook(), ScriptedAuthor(fixes=True)
    table = [{"Aussage": "a", "Befund": "aktuell", "Beleg": "E1"},
             {"Aussage": "b", "Befund": "aktuell", "Beleg": "E1"}]  # fmt: skip
    kept, removed, looked = await runs._review("page", table, {"E1": "x"}, {"author": "big"})
    assert [r["Befund"] for r in kept] == ["aktuell", "veraltet"] and removed == []
    assert looked["ok"] and looked["rounds"] == 1 and looked["author"] == "big"
    assert len(runs.second_look.tables) == 2  # looked at again after the revision


@pytest.mark.asyncio
async def test_what_stays_unconfirmed_is_removed_with_the_reviewers_reason(
    client: TestClient,
) -> None:
    runs = client.app.state.project_runs
    runs.second_look, runs.reviser = ScriptedLook(), ScriptedAuthor(fixes=False)
    table = [{"Aussage": "a", "Befund": "aktuell", "Beleg": "E1"},
             {"Aussage": "stubborn", "Befund": "aktuell", "Beleg": "E1"}]  # fmt: skip
    kept, removed, looked = await runs._review("page", table, {"E1": "x"}, {})
    assert [r["Aussage"] for r in kept] == ["a"]
    assert removed == [{"Aussage": "stubborn", "Grund": f"{UNCONFIRMED}: E1 says otherwise"}]
    # the author changed nothing: the progress guard ended it after one round
    assert looked["ok"] and looked["rounds"] == 1 and runs.reviser.calls == 1


# --- a part of the code documented (doc_section) --------------------------------------------------


def test_a_task_names_the_part_of_the_repository_it_documents() -> None:
    assert area_of("Bereich: hub/src/topcoat_hub/blocks/\n") == "hub/src/topcoat_hub/blocks"
    assert area_of("Bereich: ../etc\n") is None and area_of("") is None


def test_rows_written_in_one_go_are_backed_by_the_runs_agent_and_share_its_numbers() -> None:
    _texts, tasks = gathered_rows({"rows": ['{"a": 1}', '{"a": 2}']}, {}, ["t1"])
    assert tasks == [["t1"], ["t1"]]
    reads = [{"task": "t1", "ref": "E1", "excerpt": "engine.py 1-40"},
             {"task": "t1", "ref": "E2", "excerpt": "program.py 1-30"}]  # fmt: skip
    rows = [{"Text": "x", "Beleg": "E1"}, {"Text": "y", "Beleg": "E1, E2"}]
    table, sources = number_sources(rows, [], [reads, reads])
    assert [s.ref for s in sources] == ["E1", "E2"]  # one number per read, however many rows
    assert [r["Beleg"] for r in table] == ["E1", "E1, E2"]


def test_the_confirmed_paragraphs_become_a_page_with_sources_and_what_was_removed() -> None:
    table = [
        {"Abschnitt": "Ablauf", "Text": "Die Engine geht den Stapel durch.", "Beleg": "E2"},
        {"Abschnitt": "Zweck", "Text": "Programme sind Daten.", "Beleg": "E1"},
    ]
    sources = [
        Source("E1", "live", "x", {"capability": "repo.read", "input": {"path": "builtin.py"}}),
        Source(
            "E2",
            "live",
            "y",
            {
                "capability": "repo.read",
                "input": {"path": "engine.py", "start_line": 1, "end_line": 40},
            },
        ),
    ]
    gone = [{"Aussage": "", "Text": "z", "Grund": "nicht bestätigt: x"}]
    page = render_document("Block-Programme", table, sources, gone, "abc", "2026-11-11")
    assert page.index("## Zweck") < page.index(
        "## Ablauf"
    )  # the order of the sections, not of the rows
    assert "Programme sind Daten. [E1]" in page
    assert "- **E2** repo.read engine.py, Zeilen 1-40" in page
    assert "## Nicht belegt, entfernt" in page and "nicht bestätigt: x" in page


def test_a_round_that_wrote_a_list_of_paragraphs_gives_each_its_own_row() -> None:
    texts = [
        '[{"Text": "a", "Beleg": "E1"}, {"Text": "b", "Beleg": "E2"}]',
        "",
        '{"Text": "c"}',
        "[]",
    ]
    rows, tasks = expand_rows(texts, [["t1"], ["t2"], ["t3"], ["t4"]])
    assert [r and r["Text"] for r in rows] == ["a", "b", None, "c"]
    assert tasks == [["t1"], ["t1"], ["t2"], ["t3"]]  # each paragraph keeps its file's reading
    # the same E1 in two files' paragraphs points at two different reads
    reads = {"t1": [{"task": "t1", "ref": "E1", "excerpt": "engine"}],
             "t2": [{"task": "t2", "ref": "E1", "excerpt": "program"}]}  # fmt: skip
    table, sources = number_sources(
        [{"Text": "x", "Beleg": "E1"}, {"Text": "y", "Beleg": "E1"}], [], [reads["t1"], reads["t2"]]
    )
    assert [r["Beleg"] for r in table] == ["E1", "E2"]
    assert [s.excerpt for s in sources] == ["engine", "program"]


def test_a_task_names_its_files_relative_to_its_part_and_cannot_leave_the_repository() -> None:
    body = (
        "Bereich: hub/src/topcoat_hub\n"
        "Dateien: main.py, `config.py`, bench/, ../../docs/DEVELOPMENT.md\n"
    )
    assert files_of(body, area_of(body)) == [
        "hub/src/topcoat_hub/main.py",
        "hub/src/topcoat_hub/config.py",
        "hub/src/topcoat_hub/bench/",
        "hub/docs/DEVELOPMENT.md",
    ]
    assert files_of("Bereich: deploy\nDateien: ../../etc/passwd\n", "deploy") == []
    assert files_of("Bereich: x\n", "x") == []


@pytest.mark.asyncio
async def test_a_program_without_a_free_place_waits_and_the_batch_goes_on(
    client: TestClient,
) -> None:
    from topcoat_hub.blocks.service import BlockError

    runs = client.app.state.project_runs
    blocks = FakeBlocks(RunResult())

    def full(user: Any, program: Any, inputs: Any) -> Any:
        raise BlockError(429, "too_many_runs", "too many runs are going; wait or stop one")

    blocks.start = full  # type: ignore[method-assign]
    runs.blocks = blocks

    async def page(user_id: uuid.UUID, path: str) -> str:
        return "text"

    runs._page_text = page
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(USER, title=f"Wiki {uuid.uuid4().hex[:8]}")
    node = await store.create_node(USER, project.id, kind="task", title="Seite", body=BODY)
    batch = await runs.create_batch(USER, project.id, [node.id], max_runs=2)
    assert batch.status == "active" and batch.runs == 0  # waiting, not stopped
    blocks.has_room = lambda user_id: False  # type: ignore[attr-defined]
    await runs.dispatch(USER, batch.id)
    async with client.app.state.sessionmaker() as db:
        rows = (
            await db.scalars(select(AgentTask).where(AgentTask.title == "[project task: Seite]"))
        ).all()
    assert len(rows) == 1  # asked first: a busy start leaves no further task behind
    async with client.app.state.sessionmaker() as db:
        waiting = await db.scalar(select(ProjectNode).where(ProjectNode.id == node.id))
    assert waiting.status == "todo"


@pytest.mark.asyncio
async def test_a_program_the_hub_stops_on_shutdown_is_not_judged_as_failed(
    client: TestClient,
) -> None:
    runs = client.app.state.project_runs
    runs.second_look = FakeLook()
    runs.blocks = FakeBlocks(RunResult(status="stopped"))  # what a shutdown leaves on the result

    async def page(user_id: uuid.UUID, path: str) -> str:
        return "text"

    runs._page_text = page
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(USER, title=f"Wiki {uuid.uuid4().hex[:8]}")
    node = await store.create_node(USER, project.id, kind="task", title="Seite", body=BODY)
    await runs.create_batch(USER, project.id, [node.id], max_runs=2)
    await _settle(runs)
    async with client.app.state.sessionmaker() as db:
        now = await db.scalar(select(ProjectNode).where(ProjectNode.id == node.id))
        task = await db.get(AgentTask, now.task_id)
    assert now.status == "in_progress" and task.status == "running"  # left for the requeue


def test_the_second_look_reads_a_long_page_in_portions_that_fit() -> None:
    from topcoat_hub.project_runs import LOOK_ROWS, look_portions

    excerpts = {f"E{i}": "x" * 9000 for i in range(1, 30)}
    table = [{"Text": f"p{n}", "Beleg": f"E{n % 3 + 1}"} for n in range(30)]  # three sources
    parts = look_portions(table, excerpts)
    assert [s for s, _ in parts] == [0, 12, 24] and all(len(r) <= LOOK_ROWS for _, r in parts)
    # each paragraph citing a new big source: the characters cut the portions sooner
    wide = [{"Text": f"p{n}", "Beleg": f"E{n + 1}"} for n in range(12)]
    assert [len(r) for _, r in look_portions(wide, excerpts)] == [5, 5, 2]
    assert look_portions([], excerpts) == [(0, [])]


def test_doc_rows_get_bare_section_names_and_lose_sentences_about_the_reading() -> None:
    from topcoat_hub.project_programs import META_REASON, section_name, tidy_doc_rows

    assert section_name("Abschnitt: Zweck") == "Zweck" and section_name("## ablauf") == "Ablauf"
    assert section_name("Sonstiges") == "Sonstiges"
    table = [
        {"Abschnitt": "Abschnitt: Grenzen", "Beleg": "E1",
         "Text": "`MAX_ROUNDS` ist 6. Die exakten Werte sind nicht ersichtlich."},
        {"Abschnitt": "Grenzen", "Beleg": "E2",
         "Text": "Die vollständige Implementierung ist aufgrund der Notizen nicht einsehbar."},
        {"Abschnitt": "Ablauf", "Beleg": "E3", "Text": "Ist der Knoten nicht bekannt, wartet er."},
    ]  # fmt: skip
    kept, removed = tidy_doc_rows(table)
    assert [(r["Abschnitt"], r["Text"]) for r in kept] == [
        ("Grenzen", "`MAX_ROUNDS` ist 6."),
        ("Ablauf", "Ist der Knoten nicht bekannt, wartet er."),  # about the code: it stays
    ]
    assert len(removed) == 2 and all(r["Grund"] == META_REASON for r in removed)


def test_an_edit_names_the_paragraphs_it_joins_and_takes_their_sources() -> None:
    from topcoat_hub.project_revise import parse_edit

    rows = [{"Abschnitt": "Zweck", "Text": "a" * 100, "Beleg": "E1"},
            {"Abschnitt": "Zweck", "Text": "b" * 100, "Beleg": "E2, E3"},
            {"Abschnitt": "Zweck", "Text": "d" * 100, "Beleg": "E4"}]  # fmt: skip
    answer = (
        '{"paragraphs": [{"Text": "' + "c" * 150 + '", "Aus": [1, 2, 9]},'
        ' {"Text": "again", "Aus": [2]}, {"Text": "e", "Aus": [3]}]}'
    )
    out = parse_edit(answer, rows, "Zweck")
    # the Beleg is the one of the paragraphs joined; a paragraph is joined once; a paragraph much
    # shorter than what it joins is left out, so paragraph 3 stays as it was
    assert out == [({"Abschnitt": "Zweck", "Text": "c" * 150, "Beleg": "E1, E2, E3"}, [0, 1])]
    assert parse_edit("no json", rows, "Zweck") is None


class EditingAuthor(ScriptedAuthor):
    """Joins the paragraphs of a section in pairs: "Zusammen: a b", then "Zusammen: c"."""

    def __init__(self) -> None:
        super().__init__(fixes=False)
        self.edited: list[str] = []

    async def edit(self, model, title, section, rows):  # type: ignore[no-untyped-def]
        self.edited.append(section)
        out = []
        for i in range(0, len(rows), 2):
            idx = list(range(i, min(i + 2, len(rows))))
            joined = " ".join(rows[j]["Text"] for j in idx)
            refs = ", ".join(rows[j]["Beleg"] for j in idx)
            out.append(({"Abschnitt": section, "Text": f"Zusammen: {joined}", "Beleg": refs}, idx))
        return out


@pytest.mark.asyncio
async def test_a_doc_task_passes_its_files_and_the_page_is_tidied_edited_and_filed(
    client: TestClient,
) -> None:
    runs = client.app.state.project_runs
    runs.second_look, runs.reviser = FakeLook(), EditingAuthor()
    para = [{"Abschnitt": "Abschnitt: Zweck", "Text": "Die Engine führt Programme aus.",
             "Beleg": "F1"},
            {"Abschnitt": "Zweck", "Text": "Programme sind Daten. Mehr ist nicht ersichtlich.",
             "Beleg": "F1"},
            {"Abschnitt": "Ablauf", "Text": "Ein Block nach dem anderen.",
             "Beleg": "F1"}]  # fmt: skip
    result = RunResult(
        status="ok",
        vars={"rows": [json.dumps(para, ensure_ascii=False)]},
        sources=[{"ref": "F1", "text": "engine.py", "node": "dev", "call": {}}],
        collected_tasks={"rows": [[]]},
    )
    blocks = FakeBlocks(result)
    blocks.programs = lambda: {  # type: ignore[method-assign]
        "doc_section": {"name": "doc_section",
                        "inputs": {"area": "text", "files": "texts", "form": "text"}}
    }  # fmt: skip
    # the run takes a moment, as a real one does: the task is the node's before it ends
    blocks.start = lambda user, program, inputs: (  # type: ignore[method-assign]
        blocks.started.append(inputs)
        or SimpleNamespace(result=result, task=asyncio.ensure_future(asyncio.sleep(0.2)))
    )
    runs.blocks = blocks
    body = (
        "Zielanwendung: dev\nProgramm: doc_section\nBereich: hub/src/topcoat_hub/blocks\n"
        "Dateien: engine.py, program.py\nErgebnisform: Abschnitt | Text | Beleg\n"
    )
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(USER, title=f"Doku {uuid.uuid4().hex[:8]}")
    node = await store.create_node(USER, project.id, kind="task", title="Blöcke", body=body)
    await runs.create_batch(USER, project.id, [node.id], max_runs=1)
    await _settle(runs)

    assert blocks.started[0]["files"] == [
        "hub/src/topcoat_hub/blocks/engine.py",
        "hub/src/topcoat_hub/blocks/program.py",
    ]
    assert runs.reviser.edited == ["Zweck"]  # a section of one paragraph is left as it is
    async with client.app.state.sessionmaker() as db:
        doc = await db.scalar(select(ProjectNode).where(ProjectNode.parent_id == node.id))
    assert doc is not None and "## Zweck" in doc.body and "Abschnitt:" not in doc.body
    assert "Zusammen: Die Engine führt Programme aus. Programme sind Daten. [E1]" in doc.body
    assert "Mehr ist nicht ersichtlich." in doc.body.split("## Nicht belegt, entfernt")[1]


class RefusesOne:
    """A reviewer that refuses the edited paragraph that holds "c"."""

    async def check(self, question, table, excerpts, author, reviewer=None):  # type: ignore[no-untyped-def]
        bad = {n: "joined too much" for n, r in enumerate(table, 1)
               if str(r.get("Text", "")).startswith("Zusammen") and "c" in r["Text"]}  # fmt: skip
        if bad:
            return SecondLookResult(False, "objected", "rev@node", bad)
        return SecondLookResult(True, "ok", "rev@node")


@pytest.mark.asyncio
async def test_each_edited_paragraph_stands_only_when_confirmed_else_its_paragraphs_stay(
    client: TestClient,
) -> None:
    runs = client.app.state.project_runs
    runs.second_look, runs.reviser = RefusesOne(), EditingAuthor()
    table = [{"Abschnitt": "Zweck", "Text": "a", "Beleg": "E1"},
             {"Abschnitt": "Zweck", "Text": "b", "Beleg": "E2"},
             {"Abschnitt": "Zweck", "Text": "c", "Beleg": "E3"},
             {"Abschnitt": "Ablauf", "Text": "d", "Beleg": "E1"}]  # fmt: skip
    out, edited = await runs._edit("Seite", table, {"E1": "x", "E2": "y", "E3": "z"}, {})
    assert [r["Text"] for r in out] == ["Zusammen: a b", "c", "d"]
    assert out[0]["Beleg"] == "E1, E2" and edited == ["Zweck"]


class PortionAuthor(ScriptedAuthor):
    def __init__(self) -> None:
        super().__init__(fixes=False)
        self.seen: list[tuple[int, dict[int, str]]] = []

    async def revise(self, model, task, table, objections, excerpts):  # type: ignore[no-untyped-def]
        self.seen.append((len(table), dict(objections)))
        return [dict(r, Text="fixed") if n in objections else dict(r)
                for n, r in enumerate(table, 1)], []  # fmt: skip


@pytest.mark.asyncio
async def test_the_revision_works_in_the_portions_of_the_second_look(client: TestClient) -> None:
    runs = client.app.state.project_runs
    runs.reviser = PortionAuthor()
    excerpts = {f"E{i}": "x" * 9000 for i in range(1, 4)}
    table = [{"Text": f"p{n}", "Beleg": f"E{n % 3 + 1}"} for n in range(30)]  # 12 + 12 + 6
    found = await runs._revise("m", "q", table, {2: "no", 26: "no"}, excerpts)
    assert found is not None
    new_table, _gone = found
    # only the first and the last portion were revised, each with its own row numbers
    assert runs.reviser.seen == [(12, {2: "no"}), (6, {2: "no"})]
    assert [n for n, r in enumerate(new_table, 1) if r["Text"] == "fixed"] == [2, 26]
    assert len(new_table) == 30


def test_a_removed_row_handed_back_as_listed_reads_as_its_statement() -> None:
    from topcoat_hub.project_programs import statement

    dumped = "1. Abschnitt: Ablauf; Text: `dispatch` verteilt Aufgaben. Beleg: ['E57']"
    assert statement(dumped) == "`dispatch` verteilt Aufgaben."
    assert statement("Die Datei nutzt ein `Gateway`.") == "Die Datei nutzt ein `Gateway`."


def test_the_editors_note_of_what_it_joined_is_not_left_in_the_text() -> None:
    from topcoat_hub.project_revise import parse_edit

    rows = [{"Abschnitt": "Zweck", "Text": "a" * 40, "Beleg": "E1"}]
    answer = '{"paragraphs": [{"Text": "' + "c" * 40 + ' (Aus: [1])", "Aus": [1]}]}'
    out = parse_edit(answer, rows, "Zweck")
    assert out is not None and out[0][0]["Text"] == "c" * 40


def test_a_sentence_naming_what_the_cited_code_lacks_leaves_the_page() -> None:
    from topcoat_hub.project_programs import NAME_REASON, check_names

    code = "async def _start_program(self, user, node):\n    run = self.blocks.start(user)\n"
    table = [
        {"Abschnitt": "Ablauf", "Beleg": "E1", "Text": "`_start_program` startet den Lauf. "
         "Danach ruft `_start_project_run` den `self.blocks.start` auf."},
        {"Abschnitt": "Zweck", "Beleg": "E1", "Text": "Die Datei `hub/x/project_runs.py` startet."},
        {"Abschnitt": "Zweck", "Beleg": "E9", "Text": "`whatever` without a kept read."},
    ]  # fmt: skip
    kept, removed = check_names(table, {"E1": code})
    assert [r["Text"] for r in kept] == [
        "`_start_program` startet den Lauf.",
        "Die Datei `hub/x/project_runs.py` startet.",  # a path is not checked
        "`whatever` without a kept read.",  # nothing to compare: the second look judges it
    ]
    assert removed == [
        {"Aussage": "Danach ruft `_start_project_run` den `self.blocks.start` auf.",
         "Grund": f"{NAME_REASON}: _start_project_run"}
    ]  # fmt: skip


def test_the_editors_numbers_of_the_paragraphs_it_joined_leave_the_text() -> None:
    from topcoat_hub.project_revise import parse_edit

    rows = [{"Abschnitt": "Zweck", "Text": "a" * 40, "Beleg": "E1"}]
    answer = '{"paragraphs": [{"Text": "' + "c" * 40 + ' (3). Dann (10, 11) d.", "Aus": [1]}]}'
    out = parse_edit(answer, rows, "Zweck")
    assert out is not None and out[0][0]["Text"] == "c" * 40 + ". Dann d."


class FakeReportBlocks(FakeBlocks):
    def programs(self) -> dict[str, dict[str, Any]]:
        return {"research_report": {"name": "research_report", "inputs": {"question": "text"}}}


@pytest.mark.asyncio
async def test_a_research_task_is_a_research_report_and_the_report_is_its_result(
    client: TestClient,
) -> None:
    """Live 2026-11-15: task 1.6 ("a report on the Windows XP design") ran as a three-row checklist
    table ("Referenzbericht erstellt") with no design in it, and was blocked. A Recherche task
    delivers what Recherche delivers: a checked and revised report."""
    runs = client.app.state.project_runs
    report = "Windows XP nutzt das Luna-Design.\n\n## Taskleiste\nBlau mit Verlauf (#245EDC)."
    result = RunResult(
        status="ok", vars={"draft": report, "verdict": {"ok": True}}, filed=["rep-1"]
    )
    runs.blocks = FakeReportBlocks(result)
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(USER, title=f"XP {uuid.uuid4().hex[:8]}")
    body = (
        "Zielanwendung: research\nErgebnisform: Ergebnis | Beleg\n"
        "- [ ] Farben der Taskleiste\n- [ ] Startmenü"
    )
    node = await store.create_node(USER, project.id, kind="task", title="XP-Design", body=body)
    await runs.create_batch(USER, project.id, [node.id], max_runs=1)
    await _settle(runs)

    question = runs.blocks.started[0]["question"]
    assert question.startswith("XP-Design") and "- Farben der Taskleiste" in question
    assert "Zielanwendung" not in question and "Ergebnisform" not in question
    async with client.app.state.sessionmaker() as db:
        done = await db.scalar(select(ProjectNode).where(ProjectNode.id == node.id))
        filed = list(
            await db.scalars(
                select(ProjectNode).where(
                    ProjectNode.parent_id == node.id, ProjectNode.kind == "report"
                )
            )
        )
    assert done.status == "in_review"
    assert len(filed) == 1 and "#245EDC" in filed[0].body and "geprüft" not in filed[0].title


class FakeThemeBlocks(FakeBlocks):
    def programs(self) -> dict[str, dict[str, Any]]:
        return {"theme_design": {"name": "theme_design", "inputs": {"question": "text"}}}


THEME = {
    "key": "xp-luna",
    "name": "XP Luna",
    "base": "schreibtisch",
    "layout": "cascade",  # the shape small models write: read as {"kind": "cascade"}
    "bar": {"position": "bottom", "items": ["start", "programs", "clock"]},
    "chrome": {"buttons": ["minimise", "maximise", "close"], "titlebar": "top"},
    "motion": {"style": "fade", "ms": 150},
    "css": "--accent: #245EDC;\n.dt-taskbar { background: linear-gradient(#245EDC, #1941A5); }",
}


def test_a_theme_task_is_a_theme_program_and_its_result_is_checked_by_the_hub() -> None:
    import json as _json

    from topcoat_hub.project_programs import default_program
    from topcoat_hub.project_runs import theme_result

    assert default_program("dev", "Entwicklung: Windows XP Theme", "") == "theme_design"
    assert default_program("dev", "Fehler beheben", "") is None
    assert default_program("research", "XP-Design", "") == "research_report"
    text, problems = theme_result("Hier ist das Theme:\n" + _json.dumps(THEME))
    assert problems == [] and text.startswith("```json") and '"kind": "cascade"' in text
    _, problems = theme_result(_json.dumps({**THEME, "css": "@import url(x.css);"}))
    assert problems and "import" in " ".join(problems).lower()
    assert theme_result("kein JSON")[1] == ["the answer holds no JSON document"]


def test_the_theme_guide_shipped_with_the_protocol_is_the_one_in_docs() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert (root / "proto/ui/theme-guide.md").read_text(encoding="utf-8") == (
        root / "docs/THEMES.md"
    ).read_text(encoding="utf-8"), "copy docs/THEMES.md to proto/ui/theme-guide.md"


@pytest.mark.asyncio
async def test_a_theme_is_written_from_the_earlier_results_and_lands_below_its_task(
    client: TestClient,
) -> None:
    import json as _json

    runs = client.app.state.project_runs
    result = RunResult(
        status="ok", vars={"draft": _json.dumps(THEME), "verdict": {"ok": True, "issues": []}}
    )
    runs.blocks = FakeThemeBlocks(result)
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(USER, title=f"Themes {uuid.uuid4().hex[:8]}")
    topic = await store.create_node(USER, project.id, kind="topic", title="Themes")
    form = "Ergebnisform: Ergebnis | Beleg\n- [ ] erledigen"
    design = await store.create_node(
        USER, project.id, kind="task", title="XP-Design",
        body=f"Zielanwendung: research\n{form}", parent_id=topic.id,
    )  # fmt: skip
    await store.create_node(
        USER, project.id, kind="report", title="Ergebnis: XP-Design",
        body="Taskleiste blau (#245EDC), Start-Knopf grün.", parent_id=design.id,
        report_kind="document",
    )  # fmt: skip
    await store.update_node(USER, design.id, revision=design.revision, status="in_review")
    build = await store.create_node(
        USER, project.id, kind="task", title="Entwicklung: XP Theme",
        body=f"Zielanwendung: dev\n{form}", parent_id=topic.id,
    )  # fmt: skip
    await runs.create_batch(USER, project.id, [build.id], max_runs=1)
    await _settle(runs)

    question = runs.blocks.started[0]["question"]
    assert "Start-Knopf grün" in question  # the design the theme rests on
    assert "JSON Schema" in question and ".dt-taskbar" in question  # the format and its classes
    async with client.app.state.sessionmaker() as db:
        done = await db.scalar(select(ProjectNode).where(ProjectNode.id == build.id))
        filed = await db.scalar(
            select(ProjectNode).where(
                ProjectNode.parent_id == build.id, ProjectNode.kind == "report"
            )
        )
    assert done.status == "in_review"
    assert filed is not None and "```json" in filed.body and "xp-luna" in filed.body


class FakeBrowserTools:
    """The dev node's browser as the hub's tool service shows it: open, viewport, shot, close."""

    JPEG = b"\xff\xd8\xff\xe0fake-jpeg"

    def __init__(self, open_error: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.open_error = open_error
        self.node = str(uuid.uuid4())

    async def usable_tools(self, caller: Any) -> list[dict[str, Any]]:
        names = ("browser.open", "browser.viewport", "browser.shot", "browser.close")
        return [{"node_id": self.node, "node": "dev", "capability": n} for n in names]

    async def invoke(
        self, caller: Any, node_id: uuid.UUID, capability: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        import base64 as _b64

        self.calls.append((capability, tool_input))
        if capability == "browser.open" and self.open_error:
            return {"ok": False, "error": {"message": self.open_error}}
        if capability == "browser.shot":
            return {
                "ok": True,
                "output": {"format": "jpeg", "base64": _b64.b64encode(self.JPEG).decode()},
            }
        return {"ok": True, "output": {}}


async def _theme_task(client: TestClient, tools: FakeBrowserTools, monkeypatch: Any) -> ProjectNode:
    import json as _json

    from topcoat_hub import theme_preview

    async def repo(*_: Any) -> str:
        return "demo"

    monkeypatch.setattr(theme_preview, "last_repo", repo)
    runs = client.app.state.project_runs
    # as on the hub: the agents hold the loop's adapter, the ToolService is its .tools
    monkeypatch.setattr(runs._agents, "tools", SimpleNamespace(tools=tools))
    runs.blocks = FakeThemeBlocks(
        RunResult(status="ok", vars={"draft": _json.dumps(THEME), "verdict": {"ok": True}})
    )
    store = ProjectStore(client.app.state.sessionmaker)
    project = await store.create_project(USER, title=f"Themes {uuid.uuid4().hex[:8]}")
    task = await store.create_node(
        USER, project.id, kind="task", title="Entwicklung: XP Theme",
        body="Zielanwendung: dev\nErgebnisform: Ergebnis | Beleg\n- [ ] erledigen",
    )  # fmt: skip
    await runs.create_batch(USER, project.id, [task.id], max_runs=1)
    await _settle(runs)
    async with client.app.state.sessionmaker() as db:
        filed = await db.scalar(
            select(ProjectNode).where(
                ProjectNode.parent_id == task.id, ProjectNode.kind == "report"
            )
        )
    assert filed is not None
    return filed


@pytest.mark.asyncio
async def test_a_checked_theme_is_drawn_by_the_workshop_browser_and_its_pictures_land_in_the_result(
    client: TestClient, monkeypatch: Any
) -> None:
    from topcoat_hub.models import ReportImage

    tools = FakeBrowserTools()
    filed = await _theme_task(client, tools, monkeypatch)

    names = [c for c, _ in tools.calls]
    assert names == [
        "browser.open",
        "browser.shot",
        "browser.viewport",
        "browser.shot",
        "browser.close",
    ]
    opened = tools.calls[0][1]
    assert opened["repo"] == "demo" and opened["theme"]["key"] == "xp-luna"
    assert opened["theme"]["layout"] == {"kind": "cascade"}  # the normalised theme is drawn
    assert "## Vorschau" in filed.body and "```json" in filed.body
    ids = re.findall(r"\(/api/reports/images/([0-9a-f-]{36})\)", filed.body)
    assert len(ids) == 2
    async with client.app.state.sessionmaker() as db:
        images = list(
            await db.scalars(
                select(ReportImage).where(ReportImage.id.in_([uuid.UUID(i) for i in ids]))
            )
        )
    assert {i.user_id for i in images} == {USER} and all(i.data == tools.JPEG for i in images)


@pytest.mark.asyncio
async def test_a_theme_without_a_browser_stays_a_theme_without_pictures(
    client: TestClient, monkeypatch: Any
) -> None:
    tools = FakeBrowserTools(
        open_error="a browser session is open (repository demo): close it first"
    )
    filed = await _theme_task(client, tools, monkeypatch)

    assert [c for c, _ in tools.calls] == ["browser.open"]  # nothing to close: it never opened
    assert "Vorschau" not in filed.body and "xp-luna" in filed.body
