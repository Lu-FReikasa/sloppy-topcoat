"""The hooks of an agent run, switched by a block program (PRG-00 R5)."""

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_agent import finish, tool, use, vm, wait_for  # noqa: F401
from tests.test_blocks import FakePorts
from topcoat_hub import agent_hooks
from topcoat_hub.agent_loop import Limits
from topcoat_hub.auth import CurrentUser
from topcoat_hub.blocks import program
from topcoat_hub.blocks.engine import BlockEngine
from topcoat_hub.models import DEV_ADMIN_ID

ADMIN = CurrentUser(id=str(DEV_ADMIN_ID), name="admin", role="admin")

# --- what a program may write -----------------------------------------------------------------


@pytest.mark.parametrize(
    "hooks",
    [
        {"step_check": "yes"},
        {"claims": 1},
        {"repeat_guard": 0},
        {"repeat_guard": 21},
        {"repeat_guard": True},
        {"stall_guard": -1},
        {"stall_guard": 2.5},
        {"after_tool": []},
        ["step_check"],
    ],
)
def test_hooks_that_make_no_sense_are_refused(hooks: Any) -> None:
    with pytest.raises(agent_hooks.HookError):
        agent_hooks.check(hooks, "x")


def test_no_hooks_is_allowed_and_means_the_frame_decides() -> None:
    assert agent_hooks.check(None, "x") == {}
    limits = Limits()
    assert agent_hooks.apply(limits, None) is limits
    assert agent_hooks.apply(limits, {}) is limits


def test_hooks_are_put_on_top_of_the_limits_of_the_frame() -> None:
    base = Limits(max_repeats=3, max_stalled_turns=12, step_check_model="small")
    got = agent_hooks.apply(
        base,
        {"step_check": False, "claims": False, "second_reader": False, "repeat_guard": 2},
        "checker",
    )
    assert (got.step_check_model, got.check_claims, got.second_reader) == ("", False, False)
    assert (got.max_repeats, got.max_stalled_turns) == (2, 12)  # what is not named stays
    on = agent_hooks.apply(Limits(), {"step_check": True, "stall_guard": 0}, "checker")
    assert on.step_check_model == "checker" and on.max_stalled_turns == 0


def agent_block(hooks: Any) -> dict[str, Any]:
    return {
        "name": "p",
        "inputs": {"question": "text"},
        "stack": [{"b": "agent", "prompt": "{{question}}", "out": "d", "hooks": hooks}],
    }


def test_a_program_validates_its_hooks_and_names_where_they_are_wrong() -> None:
    ok = program.validate(agent_block({"step_check": True, "repeat_guard": 4}))
    assert ok["stack"][0]["hooks"] == {"step_check": True, "repeat_guard": 4}
    with pytest.raises(program.ProgramError, match=r"stack.*hooks\.repeat_guard"):
        program.validate(agent_block({"repeat_guard": 99}))
    assert "second_reader" in program.catalogue()["hooks"]


def test_the_engine_hands_the_hooks_to_the_agent() -> None:
    ports = FakePorts(lambda m, msgs: "x")
    spec = program.validate(agent_block({"claims": False}))
    asyncio.run(BlockEngine(ports, seed=1).run(spec, {"question": "why?"}))
    assert ports.agent_hooks == [{"claims": False}]


# --- in a run ---------------------------------------------------------------------------------


def run_with(client: TestClient, hooks: dict[str, Any] | None) -> dict[str, Any]:
    agents = client.app.state.agents

    async def go() -> str:
        started = await agents.start(ADMIN, "why does web restart?", None, hooks=hooks)
        return str(started.id)

    return wait_for(client, client.portal.call(go), "done", "failed")


def test_a_repeat_guard_of_one_ends_a_run_at_the_first_repeated_call(
    client: TestClient,
    vm,  # noqa: F811
) -> None:
    _, node = vm
    same = tool("vm-docker", "docker.ps")
    model = use(client, same, same, same, same, finish("Enough."))
    done = run_with(client, {"repeat_guard": 1})
    assert done["stop_reason"] == "repeated_calls"
    assert len(node.capability_calls) == 1
    assert len(model.calls) == 3  # call, the repeat, the question to wrap up


def test_a_run_without_hooks_keeps_the_default_of_three_repeats(
    client: TestClient,
    vm,  # noqa: F811
) -> None:
    same = tool("vm-docker", "docker.ps")
    model = use(client, same, same, same, same, finish("Enough."))
    done = run_with(client, None)
    assert done["stop_reason"] == "repeated_calls"
    assert len(model.calls) == 5  # the call, three repeats, the question to wrap up
