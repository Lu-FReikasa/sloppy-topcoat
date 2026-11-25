"""Research is run by its frame (quick, default, thorough), not by the limits of a server check."""

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.test_agent import finish, start, task, use, vm, wait_for  # noqa: F401
from topcoat_hub import agent_prompts, research_profile, web_access
from topcoat_hub.agent_loop import Limits
from topcoat_hub.config import Settings
from topcoat_hub.live_settings import BY_KEY
from topcoat_hub.message_service import request_prompt

# --- the frame of a request -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "frame"),
    [
        ("quick", "quick"),
        ("standard", "default"),
        ("default", "default"),
        ("thorough", "thorough"),
        ("Thorough, with sources from the vendor", "thorough"),
        ("a detailed guide to framework XY", "default"),
        ("", "default"),
        (None, "default"),
        (3, "default"),
    ],
)
def test_a_depth_is_one_of_three_frames(said: Any, frame: str) -> None:
    assert research_profile.depth_of(said) == frame


def test_the_frame_travels_in_the_header_of_the_request_prompt() -> None:
    node = SimpleNamespace(
        message_type="project.request",
        source_app="project",
        target_app="research",
        id="n1",
        chain_id="c1",
        body="How does the framework paginate?\n\nWanted:\n- depth: quick",
        payload={"depth": "thorough"},
    )
    prompt = request_prompt("Shop", node)  # type: ignore[arg-type]
    assert "\ndepth: thorough\n" in prompt
    # a restarted task reads the same frame from its prompt
    assert research_profile.depth_in_prompt(prompt) == "thorough"


def test_only_the_header_names_the_frame_not_a_line_of_the_body() -> None:
    body = "\n".join(["line"] * 12 + ["depth: quick"])
    assert research_profile.depth_in_prompt(f"[request]\nreply_to: x\n\n{body}") == "default"


def test_the_other_programs_get_no_depth_line() -> None:
    node = SimpleNamespace(
        message_type="project.request",
        source_app="project",
        target_app="dev",
        id="n1",
        chain_id="c1",
        body="x",
        payload={"depth": "thorough"},
    )
    assert "depth:" not in request_prompt("Shop", node)  # type: ignore[arg-type]


# --- no fixed limits ------------------------------------------------------------------------


def test_thorough_and_default_have_no_count_and_no_clock_by_default() -> None:
    settings = Settings()
    for frame in ("default", "thorough"):
        lim = research_profile.limits_for(settings, frame, Limits())
        assert lim.max_turns == research_profile.UNLIMITED_COUNT
        assert lim.max_tool_calls == research_profile.UNLIMITED_COUNT
        assert lim.time_budget_seconds == research_profile.UNLIMITED_SECONDS
        assert lim.max_web_searches == lim.max_web_fetches == research_profile.UNLIMITED_COUNT
        assert lim.stop_web_at_usable is False
        assert lim.max_stalled_turns == settings.research_stall_turns > 0
        assert lim.max_output_tokens > Limits().max_output_tokens
        assert lim.observation_chars > Limits().observation_chars


def test_quick_is_a_soft_look_in_minutes() -> None:
    lim = research_profile.limits_for(Settings(), "quick", Limits())
    assert lim.time_budget_seconds == Settings().research_quick_seconds
    assert lim.max_web_fetches < research_profile.UNLIMITED_COUNT


def test_a_limit_an_admin_sets_is_used() -> None:
    settings = Settings(
        research_max_tool_calls=40, research_time_budget_seconds=7200, research_stall_turns=0
    )
    lim = research_profile.limits_for(settings, "thorough", Limits())
    assert lim.max_tool_calls == 40 and lim.time_budget_seconds == 7200
    assert lim.max_stalled_turns == 0  # 0 = the guard is off


def test_every_research_setting_can_be_changed_in_the_gui() -> None:
    keys = [k for k in Settings.model_fields if k.startswith("research_")]
    assert keys
    for key in keys:
        assert key in BY_KEY and BY_KEY[key].group == "research"


# --- the web budget -------------------------------------------------------------------------


def test_a_research_run_reads_on_after_a_usable_page() -> None:
    page = {"text": "documentation " * 400}
    check = web_access.WebBudget()
    check.note_call(web_access.FETCH)
    check.note_result(web_access.FETCH, page, "E1")
    assert check.refusal(web_access.SEARCH) is not None  # a server check answers from it

    research = web_access.WebBudget(1000, 1000, stop_at_usable=False, max_attempts=10**6)
    for _ in range(20):
        research.note_call(web_access.FETCH)
        research.note_result(web_access.FETCH, page, "E1")
    assert research.refusal(web_access.FETCH) is None
    assert research.refusal(web_access.SEARCH) is None


# --- the prompt -----------------------------------------------------------------------------


@pytest.mark.parametrize("build", [agent_prompts.system_prompt, agent_prompts.system_prompt_native])
@pytest.mark.parametrize("frame", research_profile.DEPTHS)
def test_the_research_prompt_is_not_a_diagnosis_prompt(build: Any, frame: str) -> None:
    plain = build([])
    assert "diagnosis agent" in plain and "at most 3 searches" in plain  # the anchors exist
    text = research_profile.personalise(plain, frame)
    assert "diagnosis agent" not in text
    assert "at most 3 searches" not in text
    assert "at most 4 findings" not in text
    assert "research agent" in text
    assert research_profile.persona(frame).web in text


def test_thorough_asks_for_a_paper_and_quick_for_an_overview() -> None:
    assert "scientific paper" in research_profile.persona("thorough").style
    assert "overview" in research_profile.persona("quick").style.lower()


# --- the progress guard in a real run -------------------------------------------------------


def test_a_run_that_brings_nothing_new_ends_by_itself(client: TestClient, vm) -> None:  # noqa: F811
    use(
        client,
        "not an action",
        "still not an action",
        "and again",
        finish("Nothing more to find."),
        max_stalled_turns=2,
        max_format_errors=10,
    )
    done = wait_for(client, start(client).json()["id"], "done", "failed")
    assert task(client, done["id"])["stop_reason"] == "no_progress"


def test_results_without_content_are_no_progress(client: TestClient, vm, monkeypatch) -> None:  # noqa: F811
    # a tool that only points somewhere (repo.show) gives an evidence tag per call but no text:
    # a model guessing one path after the other must end as stalled, not run to the ceiling
    from tests.test_agent import tool
    from topcoat_hub import agent_loop

    monkeypatch.setattr(agent_loop, "NO_CONTENT_CAPABILITIES", ("docker.logs",))
    use(
        client,
        *[tool("vm-docker", "docker.logs", container=f"c{i}", lines=10) for i in range(8)],
        finish("never reached"),
        max_stalled_turns=3,
        max_tool_calls=20,
        max_turns=20,
    )
    done = wait_for(client, start(client).json()["id"], "done", "failed")
    assert task(client, done["id"])["stop_reason"] == "no_progress"
    assert task(client, done["id"])["tool_calls"] < 8


# --- a long report is written in sections ---------------------------------------------------


def test_sections_come_behind_the_abstract_in_the_order_they_were_first_written() -> None:
    from topcoat_hub.message_tools import _with_sections

    sections = [("Scope", "What was asked."), ("Findings", "A [E1] and B [E2].")]
    text = _with_sections("Abstract.", sections)
    assert text == "Abstract.\n\n## Scope\n\nWhat was asked.\n\n## Findings\n\nA [E1] and B [E2]."
    assert _with_sections("Abstract.", []) == "Abstract."


def test_the_asking_agents_are_told_how_to_choose_the_frame() -> None:
    from topcoat_hub.agent_prompts import description_limit
    from topcoat_hub.message_tools import EXTRA_DESCRIPTION
    from topcoat_hub.project_tools import SPECS

    assert research_profile.DEPTH_GUIDE in SPECS["project.request.send"]["description"]
    assert research_profile.DEPTH_GUIDE in EXTRA_DESCRIPTION
    # the answer tools are long, and must reach the model whole (the cut is at 400 characters)
    assert description_limit("dev.message.send") > 400
    assert description_limit("research.report.section") > 400
    assert description_limit("docker.logs") == agent_prompts.DESCRIPTION_CHARS


def test_a_thorough_request_is_written_in_sections_and_arrives_as_one_document(
    client: TestClient,
    vm,  # noqa: F811
    open_ceiling: None,
) -> None:
    import re
    import uuid

    from tests.fakenode import eventually
    from tests.test_agent import tool, use
    from tests.test_project_requests_e2e import _reply_to, _verdict
    from topcoat_hub.models import DEV_ADMIN_ID
    from topcoat_hub.project_store import ProjectStore
    from topcoat_hub.project_tools import ProjectTools
    from topcoat_hub.tools import Caller

    seen: dict[str, Any] = {"first": None, "tools": None, "state": 0}

    def reply(messages: list[dict[str, str]]) -> str:
        first, last = messages[1]["content"], messages[-1]["content"]
        if "[review of a message" in first:
            return finish("Reviewed.") if "recorded" in last else _verdict(messages)
        if "[planner run" in first:
            return finish("Nothing to change.")
        seen["first"] = seen["first"] or first
        seen["system"] = messages[0]["content"]
        if last.startswith("[E1]") and seen["state"] == 0:
            seen["state"] = 1
            return tool(
                "messages", "research.report.section", title="Scope", markdown="What was asked."
            )
        if seen["state"] == 1:
            seen["state"] = 2
            return tool(
                "messages", "research.report.section", title="Findings", markdown="Killed [E1]."
            )
        if seen["state"] == 2:
            seen["state"] = 3
            return tool(
                "messages",
                "research.message.send",
                reply_to=_reply_to(messages),
                kind="result",
                summary="Abstract: the web container was killed for memory.",
                claims=[{"claim": "web was killed for memory", "evidence": ["E1"]}],
            )
        return (
            finish()
            if len(messages) > 3
            else tool("vm-docker", "docker.logs", container="web", lines=50)
        )

    use(client, *[reply] * 40)
    store = ProjectStore(client.app.state.sessionmaker)

    async def ask() -> uuid.UUID:
        project = await store.create_project(DEV_ADMIN_ID, title="Memory")
        await store.set_scope(
            DEV_ADMIN_ID, project.id, revision=1, scope={"research": {"request_report": "free"}}
        )
        tools = ProjectTools(client.app.state.sessionmaker)
        tools.agents = client.app.state.agents
        out = await tools.run_tool(
            Caller(DEV_ADMIN_ID, True, "agent", application="project"),
            "project.request.send",
            {
                "project_id": str(project.id),
                "target": "research",
                "topic": "why does web restart?",
                "depth": "thorough",
            },
        )
        assert out["output"]["queue_note"] == "", out["output"]
        return project.id

    pid = client.portal.call(ask)

    def delivered() -> Any:
        rows = client.portal.call(store.messages_of, DEV_ADMIN_ID, pid)
        results = [r for r in rows if r.message_type == "research.result"]
        assert results
        return results[0]

    result = eventually(delivered, timeout=20)
    assert "\ndepth: thorough\n" in seen["first"]
    assert "research agent" in seen["system"] and "diagnosis agent" not in seen["system"]
    summary = result.payload["summary"]
    assert summary.startswith("Abstract: the web container")
    assert "## Scope\n\nWhat was asked." in summary
    assert summary.index("## Scope") < summary.index("## Findings")
    assert re.search(r"## Findings\n\nKilled \[E1\]\.", summary)


# --- a thorough research ends as a report (D102) --------------------------------------------


def _ask_research(client: TestClient, depth: str) -> Any:
    """A request to Research with a depth, as the project agent sends it; returns the project id."""
    from topcoat_hub.models import DEV_ADMIN_ID
    from topcoat_hub.project_store import ProjectStore
    from topcoat_hub.project_tools import ProjectTools
    from topcoat_hub.tools import Caller

    store = ProjectStore(client.app.state.sessionmaker)

    async def ask() -> Any:
        project = await store.create_project(DEV_ADMIN_ID, title="Memory")
        await store.set_scope(
            DEV_ADMIN_ID, project.id, revision=1, scope={"research": {"request_report": "free"}}
        )
        tools = ProjectTools(client.app.state.sessionmaker)
        tools.agents = client.app.state.agents
        out = await tools.run_tool(
            Caller(DEV_ADMIN_ID, True, "agent", application="project"),
            "project.request.send",
            {
                "project_id": str(project.id),
                "target": "research",
                "topic": "why does web restart?",
                "depth": depth,
            },
        )
        assert out["output"]["queue_note"] == "", out["output"]
        return out["output"]["message"]["task_id"]

    return client.portal.call(ask)


def _research_script(client: TestClient) -> None:
    """Research looks at the web container, writes a foreword and a chapter, and finishes."""
    from tests.test_agent import tool, use

    state = {"n": 0}

    def reply(messages: list[dict[str, str]]) -> str:
        first = messages[1]["content"]
        if "[review of a message" in first or "[planner run" in first:
            return finish("Nothing to do.")
        if "depth: " not in first:
            return finish("Nothing to do.")
        state["n"] += 1
        if state["n"] == 1:
            return tool("vm-docker", "docker.logs", container="web", lines=50)
        if state["n"] == 2:
            return tool(
                "messages",
                "research.report.section",
                title="Vorwort",
                markdown="Der Bericht klärt, warum web neu startet. Augenmerk: der Speicher [E1].",
            )
        if state["n"] == 3:
            return tool(
                "messages",
                "research.report.section",
                title="Befund",
                markdown="Der Container wurde wegen Speichermangel beendet [E1].",
            )
        return finish(
            "web ran out of memory.",
            [{"claim": "web was killed for memory", "tag": "live", "evidence": ["E1"]}],
            "",
        )

    use(client, *[reply] * 40)


def test_a_thorough_research_ends_as_a_report_in_the_ablage(
    client: TestClient,
    vm,  # noqa: F811
    open_ceiling: None,
) -> None:
    _research_script(client)
    task_id = _ask_research(client, "thorough")

    def filed() -> Any:
        reports = client.get("/api/reports", headers=AUTH).json()["reports"]
        assert reports, "no report yet"
        return reports[0]

    from tests.fakenode import eventually

    report = eventually(filed, timeout=30)
    assert report["task_id"] == task_id and report["status"] == "draft"
    assert report["depth"] == "thorough" and report["current_version"] == 1
    body = client.get(f"/api/reports/{report['id']}", headers=AUTH).json()["shown"]["body"]
    for part in (
        "## Foreword",
        "## 1. Befund",
        "## Sources",
        "## How it was made",
        "### Limits of this research",
    ):
        assert part in body, part
    assert "[1]" in body and "[E1]" not in body  # the marks became citations
    assert "docker.logs" in body  # the source of the citation is listed
    assert "out of memory" in body  # the literal passage of the evidence
    meta = client.get(f"/api/reports/{report['id']}", headers=AUTH).json()["shown"]["meta"]
    assert meta["tool_calls"] >= 1 and meta["claims_backed"] == 1 and meta["build"]


def test_a_quick_research_makes_no_report(
    client: TestClient,
    vm,  # noqa: F811
) -> None:
    from tests.test_agent import wait_for

    _research_script(client)
    task_id = _ask_research(client, "quick")
    wait_for(client, task_id, "done")
    assert client.get("/api/reports", headers=AUTH).json()["reports"] == []


def test_a_default_research_files_its_answer_as_a_report(
    client: TestClient,
    vm,  # noqa: F811
) -> None:
    # live (2026-11-06): a default answer was a card of findings and sources with no answers;
    # the answer is the document now (the claims with their marks, no sections needed)
    from tests.test_agent import wait_for

    _research_script(client)
    task_id = _ask_research(client, "default")
    wait_for(client, task_id, "done")
    # the report is filed right after the task says done: wait for it, do not race it
    from tests.fakenode import eventually

    def filed() -> None:
        assert len(client.get("/api/reports", headers=AUTH).json()["reports"]) == 1

    eventually(filed, timeout=10)


def _stopped_script(client: TestClient) -> None:
    """Research asks the same thing again and again; the hub stops it; the model answers the stop
    in plain text instead of with the action that was asked for."""
    from tests.test_agent import tool, use

    write_up = (
        "Bericht\n\n" + "web wurde wegen Speichermangel beendet; der Container startet neu. " * 12
    )

    def reply(messages: list[dict[str, str]]) -> str:
        first, last = messages[1]["content"], messages[-1]["content"]
        if "[review of a message" in first or "[planner run" in first or "depth: " not in first:
            return finish("Nothing to do.")
        if "LIMIT reached" in last:
            return write_up
        return tool("vm-docker", "docker.logs", container="web", lines=50)

    use(client, *[reply] * 60)


def test_a_research_that_was_stopped_keeps_the_write_up_and_says_why(
    client: TestClient,
    vm,  # noqa: F811
    open_ceiling: None,
) -> None:
    from tests.fakenode import eventually

    _stopped_script(client)
    _ask_research(client, "thorough")

    def filed() -> Any:
        reports = client.get("/api/reports", headers=AUTH).json()["reports"]
        assert reports, "no report yet"
        return reports[0]

    report = eventually(filed, timeout=40)
    shown = client.get(f"/api/reports/{report['id']}", headers=AUTH).json()["shown"]
    body, meta = shown["body"], shown["meta"]
    assert "**This research is incomplete.**" in body
    assert "the same request was made several times in a row" in body
    assert "web wurde wegen Speichermangel beendet" in body  # the model's write-up was kept
    assert "The run stopped (" not in body  # and not the placeholder of the loop
    assert "## 1. Results (the model's write-up)" in body
    assert "docker.logs" in body.split("**Search log**")[1]  # the log of the calls
    assert meta["complete"] is False and meta["end_reason"] == "repeated_calls"


class _Store:
    def __init__(self, evidence: list[Any], steps: list[Any]) -> None:
        self._evidence, self._steps = evidence, steps

    async def evidence(self, task_id: Any) -> list[Any]:
        return self._evidence

    async def steps(self, task_id: Any) -> list[Any]:
        return self._steps


async def test_a_chapter_needs_something_read_and_a_mark_for_it() -> None:
    from types import SimpleNamespace

    from topcoat_hub.message_tools import SECTION_SENT_BACK, _lacks_sources

    text = "word " * 50
    web = [SimpleNamespace(ref="E1", source={"node": "tools", "capability": "web.fetch"})]
    own = [SimpleNamespace(ref="E2", source={"node": "messages"})]

    async def lacks(evidence: list, steps: list, title: str, body: str) -> str | None:
        return await _lacks_sources(_Store(evidence, steps), "t", title, body)  # type: ignore[arg-type]

    # read something but cited nothing: back once
    assert await lacks(web, [], "Options", text) == "no_marks"
    sent_back = SimpleNamespace(kind="note", data={SECTION_SENT_BACK: "Options"})
    assert await lacks(web, [sent_back], "Options", text) is None  # second try is taken
    assert await lacks(web, [sent_back], "Other", text) == "no_marks"
    assert await lacks(web, [], "Options", text + "[E1]") is None
    # nothing read (the answers of the report tools are no reading): refused every time
    assert await lacks([], [], "Options", text + "[E1]") == "nothing_read"
    assert await lacks(own, [sent_back], "Options", text) == "nothing_read"
    # the foreword and a short chapter are exempt
    assert await lacks([], [], "Vorwort", text) is None
    assert await lacks([], [], "Options", "short text") is None


def test_a_research_chat_run_is_not_offered_the_send_tool_it_cannot_use() -> None:
    # live (2026-11-06): the chat's research run called research.message.send five times and was
    # refused each time (no_request); the run answers the chat, not a request, so it has no send
    import asyncio
    from uuid import uuid4

    from topcoat_hub.message_tools import MESSAGES_NODE, MessageTools, capability_of
    from topcoat_hub.tools import Caller

    async def _no_request(self: Any, caller: Any) -> bool:
        return False

    tools = object.__new__(MessageTools)
    tools._answers_a_request = _no_request.__get__(tools)  # type: ignore[method-assign]
    caller = Caller(user_id=uuid4(), admin=True, via="gui", application="research", task_id=uuid4())
    names = [t["capability"] for t in asyncio.run(tools.offer(caller))]
    assert capability_of("research") not in names
    assert "research.report.section" in names  # the sections of a document stay offered
    assert MESSAGES_NODE == "messages"
