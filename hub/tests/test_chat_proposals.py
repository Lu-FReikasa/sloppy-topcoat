"""File changes of the workshop are proposed in the chat (D99): the check of a chat may propose
them, nothing else; the report card gets the proposals of the check."""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

from topcoat_hub.agent_loop import AgentLoop
from topcoat_hub.chat_engine.answer import _change_facts, _task_proposals

ALL = [
    {"capability": "docker.restart", "node": "vm"},
    {"capability": "repo.edit", "node": "dev"},
    {"capability": "repo.write", "node": "dev"},
    {"capability": "repo.apply_patch", "node": "dev"},
    {"capability": "repo.push", "node": "dev"},
]


def loop(enabled: bool = True) -> AgentLoop:
    out = object.__new__(AgentLoop)

    async def proposable(_caller: Any) -> list[dict[str, Any]]:
        return list(ALL)

    out.tools = SimpleNamespace(proposable=proposable)
    out.proposals = SimpleNamespace(enabled=enabled)
    return out


def names(found: list[dict[str, Any]]) -> list[str]:
    return [t["capability"] for t in found]


def test_a_task_from_the_tasks_page_may_propose_every_action() -> None:
    assert names(asyncio.run(loop()._proposable(object(), "tasks"))) == names(ALL)


def test_a_chat_may_propose_the_file_changes_and_nothing_else() -> None:
    got = asyncio.run(loop()._proposable(object(), "chat"))
    assert names(got) == ["repo.edit", "repo.write", "repo.apply_patch"]  # no restart, no push


def test_other_origins_and_a_hub_without_proposals_propose_nothing() -> None:
    assert asyncio.run(loop()._proposable(object(), "care")) == []
    assert asyncio.run(loop(enabled=False)._proposable(object(), "chat")) == []


def test_the_proposals_of_a_check_are_read_for_the_report_card() -> None:
    task = uuid.uuid4()

    async def for_task(user: uuid.UUID, task_id: uuid.UUID) -> list[dict[str, Any]]:
        return [{"id": "p1", "status": "pending"}]

    state = SimpleNamespace(proposals=SimpleNamespace(for_task=for_task))
    assert asyncio.run(_task_proposals(state, uuid.uuid4(), task)) == [
        {"id": "p1", "status": "pending"}
    ]

    async def broken(user: uuid.UUID, task_id: uuid.UUID) -> list[dict[str, Any]]:
        raise RuntimeError("database gone")

    bad = SimpleNamespace(proposals=SimpleNamespace(for_task=broken))
    assert asyncio.run(_task_proposals(bad, uuid.uuid4(), task)) == []  # the answer goes on
    assert asyncio.run(_task_proposals(SimpleNamespace(), uuid.uuid4(), task)) == []


def test_a_pending_proposal_counts_as_a_change_that_waits_for_the_click() -> None:
    task = SimpleNamespace(diagnosis={"failed_calls": []})
    waiting, failed, texts = _change_facts([{"status": "pending"}], task)
    assert waiting == 1 and failed == [] and texts is None  # no change set: nothing to say on texts


def test_a_proposal_written_with_an_underscore_names_the_listed_action_and_no_other() -> None:
    # live (2026-11-06): "project.changeset_propose" was refused as not an action, although the
    # hub lists "project.changeset.propose"; the spelling is forgiven, the action is not changed
    from topcoat_hub.proposals import _same_action

    assert _same_action("project.changeset.propose", "project.changeset_propose")
    assert _same_action("repo.apply_patch", "repo.apply_patch")
    assert not _same_action("project.changeset.propose", "project.changeset.delete")
    assert not _same_action("repo.push", "repo_pushx")
