"""A project run may read the notes (the wiki) next to its project tools (agent_service.py).

Before, a project run was offered its project tools and nothing else, so the project agent could
not compare the wiki with the repository. The notes are offered to it now; machines, web and the
other tools stay out of a project run."""

import asyncio
import uuid
from types import SimpleNamespace

from topcoat_hub.agent_service import ToolsAdapter
from topcoat_hub.project_store import APPLICATION
from topcoat_hub.tools import Caller


class Offer:
    def __init__(self, tools):
        self.tools = tools
        self.calls = []

    async def offer(self, *args, **kwargs):
        self.calls.append(args)
        return list(self.tools)


PROJECT_TOOL = {"capability": "project.list", "name": "project.list"}
NOTE_TOOL = {"capability": "knowledge.search", "name": "knowledge.search"}
MACHINE_TOOL = {"capability": "shell.read", "name": "shell.read"}


def service(knowledge=None, usable=(MACHINE_TOOL,)):
    async def usable_tools(caller):
        return list(usable)

    tools = SimpleNamespace(usable_tools=usable_tools)
    return ToolsAdapter(
        tools=tools,
        knowledge=knowledge,
        project=Offer([PROJECT_TOOL]),
        messages=Offer([]),
    )


def run(coro):
    return asyncio.run(coro)


def test_a_project_run_is_offered_the_notes_next_to_its_project_tools() -> None:
    knowledge = Offer([NOTE_TOOL])
    svc = service(knowledge=knowledge)
    caller = Caller(uuid.uuid4(), True, "agent", application=APPLICATION)
    names = [t["capability"] for t in run(svc.available(caller))]
    assert names == ["project.list", "knowledge.search"]
    assert knowledge.calls  # asked for the notes of the caller's user


def test_a_project_run_gets_no_machine_and_no_web_even_when_they_exist() -> None:
    svc = service(knowledge=Offer([NOTE_TOOL]), usable=(MACHINE_TOOL,))
    caller = Caller(uuid.uuid4(), True, "agent", application=APPLICATION)
    names = [t["capability"] for t in run(svc.available(caller))]
    assert "shell.read" not in names
    assert "web.search" not in names


def test_without_a_knowledge_service_a_project_run_keeps_its_project_tools() -> None:
    svc = service(knowledge=None)
    caller = Caller(uuid.uuid4(), True, "agent", application=APPLICATION)
    names = [t["capability"] for t in run(svc.available(caller))]
    assert names == ["project.list"]
