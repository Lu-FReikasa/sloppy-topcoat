"""A check in steps (Phase 1, agent_stages.py): the plan, the step texts, and a whole run in which
every step starts with a fresh transcript and the answer cites the marks of all steps."""

import json

from fastapi.testclient import TestClient

from tests.test_agent import finish, start, task, tool, use, vm, wait_for  # noqa: F401
from topcoat_hub import agent_stages as st

TOOLS = [
    {"capability": "docker.logs", "description": "the log of a container", "node": "vm"},
    {"capability": "system.info", "description": "host facts", "node": "vm"},
]


def test_a_plan_is_checked_against_the_tools_there_are() -> None:
    text = "Plan:\n" + json.dumps(
        {
            "steps": [
                {"goal": "Read the log", "tools": ["docker.logs", "made.up"], "result": "lines"},
                {"goal": "Host facts", "tools": ["nothing.known"]},
                {"goal": ""},
                {"goal": "4th", "tools": []},
                {"goal": "5th"},
            ]
        }
    )
    steps = st.parse_plan(text, TOOLS)
    assert [s.goal for s in steps] == ["Read the log", "Host facts"]
    assert steps[0].tools == ["docker.logs"] and steps[1].tools == []
    assert st.parse_plan("no json at all", TOOLS) == []
    assert st.parse_plan('{"steps": "x"}', TOOLS) == []


def test_a_step_sees_its_goal_its_tools_and_the_earlier_results() -> None:
    stages = st.Stages(st.parse_plan(json.dumps({"steps": [
        {"goal": "Read the log", "tools": ["docker.logs"], "result": "the OOM line"},
        {"goal": "Host facts", "tools": []},
    ]}), TOOLS))  # fmt: skip
    assert [t["capability"] for t in st.step_tools(stages, TOOLS)] == ["docker.logs"]
    first = st.step_prompt("Why does web restart?", stages)
    assert "step 1 of 3: Read the log" in first and "the OOM line" in first
    stages.results.append(
        st.result_text(stages, "OOM.", [{"claim": "killed by OOM", "evidence": ["E1"]}])
    )
    stages.index = 1
    assert st.step_tools(stages, TOOLS) == TOOLS  # no tool named: all of them
    stages.index = 2
    last = st.step_prompt("Why does web restart?", stages)
    assert "last step (3 of 3)" in last and "- killed by OOM [E1]" in last


def test_a_run_in_steps_starts_each_step_fresh_and_answers_with_all_marks(
    client: TestClient,
    vm,  # noqa: F811
) -> None:
    loop = client.app.state.agents.loop
    loop.settings.agent_staged = True
    try:
        plan = json.dumps({"steps": [
            {"goal": "Read the log of web", "tools": ["docker.logs"], "result": "the reason"},
            {"goal": "Check the host", "tools": ["system.info"], "result": "uptime"},
        ]})  # fmt: skip
        model = use(
            client,
            plan,
            tool("vm-docker", "docker.logs", container="web"),
            finish(
                "The log shows an OOM kill.",
                [{"claim": "OOM kill", "evidence": ["E1"], "tag": "live"}],
            ),
            tool("vm-docker", "system.info"),
            finish("The host is up.", [{"claim": "up 1000 s", "evidence": ["E2"], "tag": "live"}]),
            finish(
                "web was killed for memory.",
                [
                    {"claim": "OOM kill", "evidence": ["E1"], "tag": "live"},
                    {"claim": "host up", "evidence": ["E2"], "tag": "live"},
                ],
            ),
        )
        t = wait_for(client, start(client).json()["id"], "done")
        assert t["stop_reason"] is None
        assert t["diagnosis"]["summary"] == "web was killed for memory."
        # call 0 is the plan; 1-2 step 1; 3-4 step 2; 5 the answer
        step2 = model.calls[3]
        assert len(step2) == 2  # system and task: none of step 1's outputs
        assert "Step 1 (Read the log of web): The log shows an OOM kill." in step2[-1]["content"]
        answer = model.calls[5][-1]["content"]
        assert "last step (3 of 3)" in answer and "[E2]" in answer
        notes = [s["data"].get("text", "") for s in t.get("steps", []) if s["kind"] == "note"]
        if notes:  # the task view lists its steps
            assert any(n.startswith("Plan in 3 steps") for n in notes)
    finally:
        loop.settings.agent_staged = False


def test_a_later_step_sees_what_the_earlier_one_looked_at_and_may_ask_it_again(
    client: TestClient,
    vm,  # noqa: F811
) -> None:
    """Live R4 (2026-11-11): a step saw only the summary of the step before and its repeat of the
    same call was refused; now the result lists the calls and a repeat shows the output again."""
    loop = client.app.state.agents.loop
    loop.settings.agent_staged = True
    try:
        plan = json.dumps({"steps": [
            {"goal": "Read the log of web", "tools": ["docker.logs"]},
            {"goal": "Explain it", "tools": ["docker.logs"]},
        ]})  # fmt: skip
        model = use(
            client,
            plan,
            tool("vm-docker", "docker.logs", container="web"),
            finish("Read it.", []),
            tool("vm-docker", "docker.logs", container="web"),  # the same call again
            finish("OOM.", [{"claim": "OOM kill", "evidence": ["E1"], "tag": "live"}]),
            finish(
                "web was killed for memory.", [{"claim": "OOM", "evidence": ["E1"], "tag": "live"}]
            ),
        )
        t = wait_for(client, start(client).json()["id"], "done")
        assert t["stop_reason"] is None
        step2 = model.calls[3][-1]["content"]
        assert "It looked at:" in step2 and "[E1] docker.logs" in step2
        again = model.calls[4][-1]["content"]
        assert "from an earlier step" in again and "You already made this call" not in again
    finally:
        loop.settings.agent_staged = False


def test_the_answer_step_ends_after_its_calls_without_a_stop(
    client: TestClient,
    vm,  # noqa: F811
) -> None:
    loop = client.app.state.agents.loop
    loop.settings.agent_staged = True
    try:
        plan = json.dumps({"steps": [{"goal": "a", "tools": ["docker.logs"]}, {"goal": "b"}]})
        use(
            client,
            plan,
            finish("a done.", []),
            finish("b done.", []),
            tool("vm-docker", "docker.logs", container="web", lines=1),
            tool("vm-docker", "docker.logs", container="web", lines=2),
            tool("vm-docker", "system.info"),  # one too many for the answer step
            finish("the answer.", []),
        )
        t = wait_for(client, start(client).json()["id"], "done")
        assert t["stop_reason"] is None and t["tool_calls"] == 2
    finally:
        loop.settings.agent_staged = False
