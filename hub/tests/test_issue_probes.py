"""Tries of an issue's investigation (issue_probes.py): only under the person's probe mandate (D83),
one new test file on a throw-away branch, only that file tested, back to the live code; the checks
run on every try and a revoked mandate stops the next one."""

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub import issue_investigation as ii
from topcoat_hub import issue_probes as ip
from topcoat_hub.issue_fixes import LIVE_REPO, WEB_MARK
from topcoat_hub.models import DEV_ADMIN_ID as USER
from topcoat_hub.project_events import PERSON
from topcoat_hub.project_store import ProjectStore


class Workshop:
    """The dev node's workshop as far as a try uses it."""

    def __init__(self) -> None:
        self.node = uuid.uuid4()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.confirmed: list[tuple[uuid.UUID, str]] = []

    async def proposable_tools(self, caller: Any) -> list[dict[str, Any]]:
        return [{"capability": "repo.edit", "node_id": str(self.node)}]

    async def invoke(self, caller: Any, node: Any, cap: str, data: dict[str, Any]) -> Any:
        self.calls.append((cap, data))
        out: dict[str, Any] = {}
        if cap == "repo.status":
            out = {"clean": True, "head": "abc"}
        elif cap == "repo.test_start":
            out = {"job_id": "j1"}
        elif cap == "repo.test_status":
            log = (
                "F\nE  AssertionError: got \"'22c5'\" as a word\n1 failed\n"
                + WEB_MARK
                + "\nweb noise"
            )
            out = {"running": False, "state": "failed", "log": log}
        return {"ok": True, "output": out}

    async def execute_confirmed(
        self, caller: Any, node: Any, cap: str, data: dict[str, Any], pid: Any, digest: str
    ) -> Any:
        self.calls.append((cap, data))
        self.confirmed.append((pid, data["path"]))
        return {"ok": True, "output": {}}


class Fixes:
    def __init__(self, tools: Workshop) -> None:
        import asyncio

        self.tools = tools
        self._lock = asyncio.Lock()

    async def _dev_node(self, caller: Any) -> uuid.UUID:
        return self.tools.node

    async def _read(self, caller: Any, node: Any, cap: str, data: dict[str, Any]) -> Any:
        return (await self.tools.invoke(caller, node, cap, data))["output"]


async def _issue(client: TestClient) -> tuple[ProjectStore, Any]:
    store = ProjectStore(client.app.state.sessionmaker, PERSON)
    project = await store.create_project(USER, title=f"Probe {uuid.uuid4().hex[:6]}")
    issue = await store.create_node(
        USER, project.id, kind="task", title="Filter", body="Art: issue"
    )
    return store, issue


@pytest.mark.asyncio
async def test_no_try_without_the_persons_mandate_then_one_file_one_test(
    client: TestClient,
) -> None:
    store, issue = await _issue(client)
    shop = Workshop()
    runner = ip.ProbeRunner(Fixes(shop), store)
    test = "def test_x():\n    assert 1 == 2, 'got 1'\n"
    out = await runner.run(USER, issue.project_id, issue.number, 1, test)
    assert "kein aktives Versuchs-Mandat" in out and shop.calls == []

    mandate = await store.create_mandate(
        USER, issue.project_id, repo=LIVE_REPO, branch=ip.probe_branch(issue.number),
        actions=[ip.ACTION], push=False, text=ip.probe_scope(issue.number), days=ip.DAYS,
    )  # fmt: skip
    out = await runner.run(USER, issue.project_id, issue.number, 1, test)
    assert out.startswith("Testlauf: failed") and "got \"'22c5'\"" in out
    assert "web noise" not in out  # the image's web part is not the try's
    order = [c[0] for c in shop.calls]
    assert order == ["repo.status", "repo.start", "repo.write", "repo.test_start", "repo.start",
                     "repo.test_status"]  # fmt: skip
    assert shop.calls[1][1]["topic"] == ip.probe_branch(issue.number).removeprefix("topcoat/")
    assert shop.calls[3][1]["path"] == "tests/_probe_1.py"  # only this file, relative to hub/
    assert shop.calls[4][1] == {"repo": LIVE_REPO, "topic": "live", "discard": True}
    assert shop.confirmed == [(mandate.id, "hub/tests/_probe_1.py")]  # the hub names the file
    assert (await store.get_mandate(USER, mandate.id)).uses == 1

    # the checks run on every try: a secret, a file too large; a revoked mandate stops the next
    shop.calls.clear()
    secret = "TOKEN = 'ghp_" + "a" * 36 + "'\ndef test_x():\n    pass\n"
    assert "Geheimnis" in await runner.run(USER, issue.project_id, issue.number, 2, secret)
    big = "x = 1\n" * 5000
    assert "größer" in await runner.run(USER, issue.project_id, issue.number, 3, big)
    await store.revoke_mandate(USER, mandate.id)
    assert "kein aktives" in await runner.run(USER, issue.project_id, issue.number, 4, test)
    assert shop.calls == []


def test_a_probe_mandate_covers_no_order_and_no_push() -> None:
    assert ip.ACTION not in ("fix", "create_prototype")
    scope = ip.probe_scope("2.8")
    assert "topcoat/probe-issue-2-8" in scope and "kein Push" in scope and "_probe_" in scope


@pytest.mark.asyncio
async def test_the_card_makes_the_mandate_with_the_hubs_scope_only_for_the_person(
    client: TestClient,
) -> None:
    _, issue = await _issue(client)
    _, admin_key = make_user(TEST_DB_URL, "probe key", role="admin")
    refused = client.post(f"/api/project/nodes/{issue.id}/probe-mandate", headers=admin_key)
    assert refused.status_code in (401, 403)
    made = client.post(f"/api/project/nodes/{issue.id}/probe-mandate", headers=AUTH)
    assert made.status_code == 200, made.text
    m = made.json()["mandate"]
    assert m["actions"] == ["probe"] and m["push"] is False and m["repo"] == LIVE_REPO
    assert m["branch"] == ip.probe_branch(issue.number) and m["state"] == "active"
    # the issue's page shows it
    shown = client.get(f"/api/project/nodes/{issue.id}/fixes", headers=AUTH).json()
    assert shown["probe_mandate"]["id"] == m["id"]


@pytest.mark.asyncio
async def test_a_try_question_runs_a_written_test_only_with_tries_allowed() -> None:
    async def read(paths: list[str]) -> dict[str, str | None]:
        return {}

    asked: list[str] = []

    async def ask(name: str, inputs: dict[str, Any]) -> tuple[str, str]:
        asked.append(name)
        if name == "investigate_round":
            if asked.count("investigate_round") == 1:
                return json.dumps({"questions": [{"to": "try", "question": "quotes?"}]}), "m"
            return "kein json", "m"
        if name == "probe_read":
            return "hub/src/topcoat_hub/x.py: def parse(text)", "m"
        if name == "probe_test":
            return "<<<<<<< FILE\ndef test_q():\n    assert False, 'got word'\n>>>>>>> END", "m"
        return "dev answer", "m"

    ran: list[tuple[int, str]] = []

    async def try_run(n: int, content: str) -> str:
        ran.append((n, content))
        return "Testlauf: failed\nAssertionError: got word"

    inv = await ii.investigate(ask, read, "Frage", "", try_run)
    # the hub's own first try reproduces the problem, then the lead's try of its guess
    content = "def test_q():\n    assert False, 'got word'\n"
    assert ran == [(1, content), (2, content)]
    assert asked.index("probe_test") < asked.index("investigate_round")
    assert "das Problem nachgestellt" in "\n".join(inv.log)
    assert "probe_test" in asked and "Versuch" in "\n".join(inv.log)
    assert "got word" in ii.with_log(inv)
    # without tries allowed, the same question goes to Development
    asked.clear()
    inv = await ii.investigate(ask, read, "Frage", "", None)
    assert "probe_test" not in asked and "probe_dev" in asked


@pytest.mark.asyncio
async def test_a_test_that_did_not_run_is_written_once_more_and_the_cause_is_tried() -> None:
    """Live 2026-11-19: a try's test imported a function that does not exist (nothing observed),
    and the lead never tried its own cause. A test that did not run is written once more with its
    error; the lead's first report is held back until the hub tried its cause."""

    async def read(paths: list[str]) -> dict[str, str | None]:
        return {"hub/src/topcoat_hub/x.py": "def parse(text):\n    return text\n"}

    report = {
        "problem": "p", "cause": "the quotes stay part of the word", "evidence": [], "fix": "f",
        "summary": "s", "steps": [{"path": "hub/src/topcoat_hub/x.py", "anchor": "def parse(text):",
                                   "do": "x"}],
        "test": {"path": "hub/tests/test_new_quotes.py", "do": "d"},
    }  # fmt: skip
    hints: list[str] = []
    rounds: list[str] = []

    async def ask(name: str, inputs: dict[str, Any]) -> tuple[str, str]:
        if name in ("probe_read", "issue_area"):  # round 0 without an answer (1.15)
            return "", "m"
        if name == "probe_test":
            hints.append(inputs["hint"])
            return "<<<<<<< FILE\n```python\ndef test_q():\n    pass\n```\n>>>>>>> END", "m"
        rounds.append(inputs["hint"])
        return json.dumps({"report": report}), "m"

    outputs = iter(
        [
            "Testlauf: failed\nE   ImportError: cannot import name 'project_node_search'",
            "Testlauf: failed\nAssertionError: got \"'22c5'\"",
            "Testlauf: passed\n1 passed",
        ]
    )
    contents: list[str] = []

    async def try_run(n: int, content: str) -> str:
        contents.append(content)
        return next(outputs)

    inv = await ii.investigate(ask, read, "Frage", "", try_run)
    # the reproduction ran twice (the first test did not run), the second time told why
    assert hints[0] == "" and "DID NOT RUN" in hints[1] and "project_node_search" in hints[1]
    assert contents[0] == "def test_q():\n    pass\n"  # the code fence is no part of the file
    # the first report was held back and its cause tried; the second report is taken
    assert len(rounds) == 2 and "WAS TRIED" in rounds[1]
    assert inv.plan is not None and "die vermutete Ursache geprüft" in "\n".join(inv.log)
    assert "erster Test lief nicht" in ii.with_log(inv)


@pytest.mark.asyncio
async def test_the_hub_reads_the_named_modules_and_completes_their_imports() -> None:
    """Live 2026-11-19: the reader named "project_filter", the writer imported it without
    topcoat_hub and the try ended in an ImportError."""
    source = (
        "import re\n\n\ndef compile_filter(text, now=None):\n    pass\n\n\n"
        "class Parser:\n    x = 1\n"
    )

    async def read(paths: list[str]) -> dict[str, str | None]:
        return {p: (source if p.endswith("/project_filter.py") else None) for p in paths}

    names, outline = await ii.module_outline(
        read,
        "The logic is in project_filter (hub/src/topcoat_hub/project_filter.py), see read_tools",
    )
    assert names == ["project_filter"]
    assert outline.startswith("# topcoat_hub.project_filter")
    assert "def compile_filter(text, now=None):" in outline and "class Parser:" in outline
    test = "import pytest\nfrom project_filter import compile_filter\nimport project_filter\n"
    fixed = ii.fix_imports(test, names)
    assert "from topcoat_hub.project_filter import compile_filter" in fixed
    assert "from topcoat_hub import project_filter" in fixed and "import pytest" in fixed


def test_the_akte_keeps_the_line_that_decides_a_try() -> None:
    out = (
        "Testlauf: failed\nbringing up nodes...\nERROR collecting tests/_probe_1.py\n"
        "E   ImportError: x\n"
    )
    assert ip.outcome_line(out) == "Testlauf: failed | E   ImportError: x"
    assert (
        ip.outcome_line("Testlauf: passed\n..\n1 passed in 2s\n")
        == "Testlauf: passed | 1 passed in 2s"
    )
    assert ip.outcome_line("(kein Versuch: leer)") == "(kein Versuch: leer)"


def test_the_outline_keeps_whole_signatures_and_a_wrong_call_is_no_finding() -> None:
    """Live 2026-11-19: "def compile_filter(" alone reached the writer, which passed three
    arguments."""
    source = (
        "def compile_filter(\n    source: str, *, now=None, zone: str = 'x'\n"
        ") -> tuple[int, bool]:\n    pass\n\n\nclass Parser:\n    pass\n"
    )
    assert ii.signatures(source) == [
        "def compile_filter( source: str, *, now=None, zone: str = 'x' ) -> tuple[int, bool]:",
        "class Parser:",
    ]
    wrong = "E   TypeError: compile_filter() takes 1 positional argument but 3 were given"
    assert ii._BROKEN.search(wrong) is not None
    assert ii._BROKEN.search("E   AssertionError: got 'x'") is None


def test_a_test_without_the_markers_is_taken_from_its_code_block() -> None:
    """Live 2026-11-19: the writer answered with a plain code block; the try was dropped."""
    block = "Here is the test:\n```python\nfrom x import y\n\ndef test_q():\n    assert y\n```\n"
    assert ii.bare_test(block) == "from x import y\n\ndef test_q():\n    assert y\n"
    assert (
        ii.bare_test("import x\n\ndef test_q():\n    pass")
        == "import x\n\ndef test_q():\n    pass\n"
    )
    assert ii.bare_test("I could not write a test.") == ""


def test_the_outline_shows_the_public_methods_of_a_class() -> None:
    """Live 2026-11-19: ProjectStore.search_nodes was missing; the writer imported it from the
    module."""
    source = (
        "class ProjectStore(Base):\n    def __init__(self, sm):\n        pass\n\n"
        "    async def search_nodes(\n        self, user_id, filter_text\n    ) -> list:\n"
        "        pass\n\n    def _private(self):\n        pass\n\n\ndef helper(x):\n    pass\n"
    )
    assert ii.signatures(source) == [
        "class ProjectStore(Base):",
        "    def __init__(self, sm):",
        "    async def search_nodes( self, user_id, filter_text ) -> list:",
        "def helper(x):",
    ]
