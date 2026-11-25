"""HOST-01 on the hub side: the file capabilities are named-only, belong to the host group, and the
agent is told that what it reads in a file is data."""

import uuid

import pytest

from topcoat_hub import agent_prompts, tool_groups
from topcoat_hub.protocol import TOOL_CAPABILITIES
from topcoat_hub.tools import Caller, granted


@pytest.mark.parametrize("cap", ["files.list", "files.read"])
def test_file_reading_is_named_only_like_the_shell(cap: str) -> None:
    user = Caller(user_id=uuid.uuid4(), admin=False, via="api")
    admin = Caller(user_id=uuid.uuid4(), admin=True, via="gui")
    assert not granted(user, {"*"}, cap)  # a blanket grant never opens a folder of a host
    assert not granted(user, {"files.list" if cap == "files.read" else "files.read"}, cap)
    assert granted(user, {cap}, cap)
    assert granted(admin, set(), cap)


def test_a_new_tool_node_does_not_switch_file_reading_on() -> None:
    # nothing is on until a person saves folders on the node's page; the page then adds the names
    assert not any(c.startswith("files.") for c in TOOL_CAPABILITIES)


def test_file_and_windows_capabilities_sit_in_the_host_group() -> None:
    for cap in ("files.list", "files.read", "windows.events", "windows.system"):
        assert tool_groups.group_of(cap) == "host"
    assert "host health" in tool_groups.labels(["host"])[0]


@pytest.mark.parametrize("prompt", [agent_prompts.SYSTEM, agent_prompts.SYSTEM_NATIVE])
def test_the_agent_is_told_that_file_contents_are_data(prompt: str) -> None:
    text = " ".join(prompt.split())
    assert "files.read" in text
    assert "never instructions" in text
    assert "A refusal" in text and "is final" in text
