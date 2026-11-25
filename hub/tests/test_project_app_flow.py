"""The project application's own tools, checked without a model (PPH-05, the wiki check).

The project application plans; it may read the wiki (knowledge, read only) and it has no
repository, no machine and no web. These tests run the real tool adapter and the real
knowledge.read envelope, with the wiki stubbed to one document. What the project agent sees of a
long page must be the whole page in pieces, with the continuation shown first, never cut.
"""

import uuid
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from topcoat_hub import agent_prompts
from topcoat_hub.agent_service import ToolsAdapter
from topcoat_hub.knowledge import (
    KNOWLEDGE_NODE_ID,
    READ_CAPABILITY,
    READ_CHARS,
    KnowledgeService,
    read_tool,
    search_tool,
)
from topcoat_hub.knowledge_store import KnowledgeStore
from topcoat_hub.project_tools import ProjectTools
from topcoat_hub.tools import Caller

# what the agent sees of one tool output on the live hub (agent_observation_chars, measured on
# the 2026-11-07 run: about 3000 characters of the output, then "[… cut]")
OBSERVATION_CHARS = 3000
PATH = "Oberfläche und Looks~1.md"
# three sections of about 1100 characters: a whole page of about 3300 characters, in pieces
SECTIONS = [f"Abschnitt {i}. " + "Der Schreibtisch hat Programme. " * 35 for i in range(3)]
PAGE = "\n\n".join(SECTIONS)


class _Session:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _NoMachines:
    async def usable_tools(self, caller: Caller) -> list[dict[str, Any]]:
        return []


def _wiki_knowledge(monkeypatch: Any) -> KnowledgeService:
    """The real KnowledgeService.read, with the store answering one document of the wiki."""
    chunks = [(SimpleNamespace(text=section, source_id=1), "TopCoat") for section in SECTIONS]

    async def chunks_of_path(self: object, path: str) -> list[object]:
        return chunks if path == PATH else []

    monkeypatch.setattr(KnowledgeStore, "chunks_of_path", chunks_of_path)
    service = KnowledgeService.__new__(KnowledgeService)
    service._sm = _Session  # type: ignore[attr-defined]

    class _Offer:
        async def offer(self, user_id: uuid.UUID) -> list[dict[str, Any]]:
            return [search_tool(), read_tool()]

    service.offer = _Offer().offer  # type: ignore[method-assign]
    return service


async def test_the_project_run_sees_its_tools_and_the_wiki_and_nothing_of_repo_or_machines(
    client: TestClient, monkeypatch: Any
) -> None:
    adapter = ToolsAdapter(
        _NoMachines(),  # type: ignore[arg-type]
        knowledge=_wiki_knowledge(monkeypatch),
        project=ProjectTools(client.app.state.sessionmaker),
    )
    planner = Caller(uuid.uuid4(), False, "agent", application="project")
    offered = {t["capability"] for t in await adapter.available(planner)}

    assert "project.list" in offered
    assert READ_CAPABILITY in offered
    assert not [c for c in offered if c.startswith(("repo.", "github.", "docker.", "web."))]


async def test_a_long_wiki_page_reads_to_its_end_and_no_piece_is_cut_for_the_agent(
    client: TestClient, monkeypatch: Any
) -> None:
    assert len(PAGE) > READ_CHARS  # the page needs more than one piece
    adapter = ToolsAdapter(
        _NoMachines(),  # type: ignore[arg-type]
        knowledge=_wiki_knowledge(monkeypatch),
        project=ProjectTools(client.app.state.sessionmaker),
    )
    planner = Caller(uuid.uuid4(), False, "agent", application="project")

    seen: list[str] = []
    offset: int | None = 0
    while offset is not None:
        answer = await adapter.invoke(
            planner, KNOWLEDGE_NODE_ID, READ_CAPABILITY, {"path": PATH, "offset": offset}
        )
        output = answer["output"]
        shown = agent_prompts.output_text(output)
        shown_text, was_cut = agent_prompts.cut(shown, OBSERVATION_CHARS, newest=False)
        assert was_cut is False, shown_text[-80:]
        # the header the agent reads first names the continuation it must ask for next
        header = shown.splitlines()[0]
        assert f"offset={offset}" in header
        seen.append(output["content"])
        offset = output["next_offset"]
        if offset is not None:
            assert f"next_offset={offset}" in header

    assert "".join(seen) == PAGE
