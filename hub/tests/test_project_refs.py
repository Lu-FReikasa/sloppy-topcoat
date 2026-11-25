"""The project tools take the words a person uses (2026-11-10, C5): a task's number, a project's
title, the beginning of an id. Plans with the usual shape slips are put right before the check."""

from types import SimpleNamespace

import pytest

from tests.test_project_search import caller, store, tools, world  # noqa: F401
from topcoat_hub import project_refs
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.project_plan import repair_plan, schema_problems
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.project_tools import ProjectTools


def _p(id_: str, title: str) -> SimpleNamespace:
    return SimpleNamespace(id=id_, title=title)


def _n(id_: str, number: str, title: str) -> SimpleNamespace:
    return SimpleNamespace(id=id_, number=number, title=title)


def test_a_project_is_found_by_title_part_or_id_beginning() -> None:
    projects = [
        _p("5a2cc486-089e-4990-99f9-ea24997a404c", "Topcoat-Hub Wiki-Abgleich (Test)"),
        _p("cd118f1c-65ed-4a9b-be6b-2e3ec2d757f2", "Topcoat Weiterentwicklung"),
    ]
    assert project_refs.pick_project("topcoat-hub wiki-abgleich (test)", projects).startswith("5a2")
    assert project_refs.pick_project("Weiterentwicklung", projects).startswith("cd1")
    assert project_refs.pick_project("cd118f1c", projects).startswith("cd1")
    assert project_refs.pick_project("Topcoat", projects) is None  # two fit: nothing is guessed
    assert project_refs.pick_project("ef1f70b3", projects) is None


def test_a_node_is_found_by_its_number_its_id_or_its_title() -> None:
    nodes = [
        _n("249e18db-a312-48c4-a65d-09cd4b7c5d47", "2.1", "Theme-Format analysieren"),
        _n("875cb36d-86c7-4a16-91b7-f423860df2cf", "2.2", "Deployment klären"),
    ]
    assert project_refs.pick_node("2.1", nodes).startswith("249e")
    assert project_refs.pick_node("875cb36d", nodes).startswith("875c")
    assert project_refs.pick_node("deployment klären", nodes).startswith("875c")
    assert project_refs.pick_node("3.1", nodes) is None


@pytest.mark.asyncio
async def test_the_tool_takes_a_number_and_a_title(
    store: ProjectStore,  # noqa: F811
    tools: ProjectTools,  # noqa: F811
) -> None:
    web, _, t = await world(store)
    number = t["css"].number
    out = (
        await tools.run_tool(caller(), "project.node.get", {"node_id": number, "project_id": "Web"})
    )["output"]
    assert out["node"]["id"] == str(t["css"].id)
    # a title across all projects, when only one node has it
    out = (await tools.run_tool(caller(), "project.node.get", {"node_id": "Backup job"}))["output"]
    assert out["node"]["id"] == str(t["backup"].id)
    # an id's beginning for the project
    got = (await tools.run_tool(caller(), "project.get", {"project_id": str(web.id)[:8]}))["output"]
    assert got["project"]["id"] == str(web.id)
    assert DEV_ADMIN_ID


def test_a_plan_with_the_usual_slips_is_put_right() -> None:
    # live 2026-11-10: "title is a required property" and tasks inside the topic
    plan = {
        "project_id": "5a2cc486-089e-4990-99f9-ea24997a404c",
        "topic": {
            "title": "Neue Themes",
            "tasks": [
                {
                    "name": "Theme-Format im Code klären",
                    "application": "dev",
                    "result": ["Aussage", "Beleg"],
                    "description": "docs/THEMES.md und web/src/desktop/theme.ts lesen",
                }
            ],
        },
    }
    fixed = repair_plan(plan)
    assert fixed["title"] == "Neue Themes" and fixed["topic"] == {"title": "Neue Themes"}
    task = fixed["tasks"][0]
    assert task["title"] == "Theme-Format im Code klären"
    assert task["checklist"] == ["docs/THEMES.md und web/src/desktop/theme.ts lesen"]
    assert "description" not in task and "name" not in task
    assert schema_problems(fixed) == []
    # a plan without a topic stays as it was: the check names what is missing
    assert repair_plan({"project_id": "x"}) == {"project_id": "x"}


def test_a_batch_names_its_tasks_by_number() -> None:
    assert project_refs.needs_lookup({"project_id": "Web", "node_ids": ["2.1"]})
    assert not project_refs.needs_lookup(
        {"node_ids": ["249e18db-a312-48c4-a65d-09cd4b7c5d47"], "max_runs": 1}
    )
