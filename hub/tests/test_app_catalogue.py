"""What each application a task can run in may use, from the ceilings of its runs (2026-11-18)."""

import pytest
from fastapi.testclient import TestClient

from topcoat_hub import agent_prompts, app_catalogue


def test_a_line_says_what_an_application_can_and_cannot_use() -> None:
    line = app_catalogue.describe(
        "research", "Research", {"groups": ["notes", "wiki"], "internet": True}
    )
    assert line.startswith("- research (Research) can use: notes (")
    assert "the internet" in line and "wiki (" in line
    # live 2026-11-18: Research was asked which models the nodes run; it has no topcoat tools
    assert "can NOT use: topcoat, workshop, project" in line
    dev = app_catalogue.describe("dev", "Entwicklung", {"groups": ["workshop"], "internet": False})
    assert "can NOT use: topcoat, wiki, notes, project, internet" in dev
    every = app_catalogue.describe("admin", "Verwaltung", None)
    assert "every tool group" in every


@pytest.mark.asyncio
async def test_the_catalogue_is_read_from_the_programs_that_bind_the_runs(
    client: TestClient,
) -> None:
    async with client.app.state.sessionmaker() as db:
        text = await app_catalogue.catalogue(db)
    lines = {line.split(" ")[1]: line for line in text.splitlines()}
    assert set(lines) == {"research", "dev", "admin", "project"}
    assert "topcoat (" in lines["admin"] and "models" in lines["admin"]
    assert "NOT use: topcoat" in lines["research"] and "wiki (" in lines["research"]
    part = app_catalogue.section(text)
    assert "Zielanwendung" in part and "depends_on" in part and "Chat is no application" in part


def test_the_project_role_and_its_catalogue_go_together() -> None:
    # the catalogue is added by the loop for Projekte runs; the role names the propose tool
    assert "project.changeset.propose" in agent_prompts.system_prompt([], application="project")
