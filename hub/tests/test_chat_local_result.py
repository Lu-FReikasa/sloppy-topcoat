"""The chat's own reads (node state, monitors) reach the tools (live 2026-11-11: every
"Wie ist der Stand von node-nvidia?" ended without an answer: AgentService has no ``invoke``)."""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

from topcoat_hub import local_tools
from topcoat_hub.chat_engine.answer import _local_result
from topcoat_hub.topcoat_self import TOPCOAT_NODE_ID


class _Tools:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def invoke(self, caller, node_id, capability, tool_input, task_id=None):
        self.calls.append((caller.user_id, node_id, capability, tool_input))
        return {"ok": True, "output": {"nodes": [{"name": "node-nvidia", "state": "available"}]}}


def test_the_node_state_read_goes_through_the_service_tools() -> None:
    tools = _Tools()
    state = SimpleNamespace(agents=SimpleNamespace(tools=tools))
    user = SimpleNamespace(uuid=uuid.uuid4(), is_admin=True, via_key=False)
    turn = SimpleNamespace(application="admin", scope=None, decide_available=[])
    text = asyncio.run(
        _local_result(state, user, turn, local_tools.NODE_STATE, {"node": "node-nvidia"})
    )
    assert tools.calls and tools.calls[0][1] == TOPCOAT_NODE_ID
    assert tools.calls[0][2] == local_tools.NODE_STATE_CAPABILITY
    assert isinstance(text, str) and text
