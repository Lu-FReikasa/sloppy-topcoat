"""The plan tool (project_plan.py): fixed fields, every problem at once, the body from the hub.

A plan is a topic and its tasks; a task has an application, result columns and checklist items; a
blocker must be an earlier task of the same plan. A plan that does not fit is refused with all its
problems in one answer, and a plan that fits becomes the operations of an ordinary change set."""

import uuid

import pytest

from topcoat_hub.project_changesets import check_operations
from topcoat_hub.project_plan import compile_plan, schema_problems

PROJECT = str(uuid.uuid4())


def plan(**over):
    base = {
        "project_id": PROJECT,
        "title": "Wiki-Abgleich",
        "topic": {"title": "Abgleich Wiki und Repo"},
        "tasks": [
            {
                "title": "Wiki-Seite Looks prüfen",
                "application": "research",
                "result": ["Aussage", "Beleg"],
                "checklist": ["Registrierung in look.ts prüfen"],
            },
            {
                "title": "Repo-Stand prüfen",
                "application": "dev",
                "result": ["Befund", "Beleg"],
                "checklist": ["web/src/look.ts lesen"],
                "blocked_by": ["Wiki-Seite Looks prüfen"],
            },
        ],
    }
    base.update(over)
    return base


def test_a_plan_that_fits_becomes_a_checked_change_set() -> None:
    ops, problems = compile_plan(plan())
    assert problems == []
    assert schema_problems(plan()) == []
    check_operations(ops)  # the same check as any change set
    assert ops[0]["kind"] == "topic"
    assert ops[1]["parent"] == ops[0]["local"] == "plan"
    assert ops[2]["depends_on"] == [ops[1]["local"]]


def test_the_hub_writes_the_body_from_the_fields() -> None:
    ops, _ = compile_plan(plan())
    assert ops[1]["body"] == (
        "Zielanwendung: research\nModus: lesen\nErgebnisform: Aussage | Beleg\n"
        "- [ ] Registrierung in look.ts prüfen"
    )


def test_a_field_cannot_break_the_body_layout() -> None:
    bad = plan()
    bad["tasks"][0]["result"] = ["Aussage|Beleg"]
    bad["tasks"][0]["checklist"] = ["zwei\nZeilen"]
    ops, _ = compile_plan(bad)
    assert ops[1]["body"] == (
        "Zielanwendung: research\nModus: lesen\nErgebnisform: Aussage/Beleg\n- [ ] zwei Zeilen"
    )


def test_every_schema_problem_is_reported_at_once() -> None:
    broken = {
        "project_id": PROJECT,
        "tasks": [{"title": "", "application": "node-amd", "result": []}],
    }
    problems = schema_problems(broken)
    assert len(problems) >= 4  # missing topic and title, the application, the empty title, ...
    assert any("node-amd" in p for p in problems)


def test_a_blocker_must_be_an_earlier_task() -> None:
    late = plan()
    late["tasks"] = list(reversed(late["tasks"]))  # the blocked task now comes first
    _, problems = compile_plan(late)
    assert len(problems) == 1 and "no earlier task" in problems[0]


def test_a_repeated_title_is_reported() -> None:
    twice = plan()
    twice["tasks"].append(dict(twice["tasks"][0]))
    _, problems = compile_plan(twice)
    assert any("named twice" in p for p in problems)


@pytest.mark.parametrize("application", ["research", "dev", "project"])
def test_the_three_applications_are_accepted(application: str) -> None:
    p = plan()
    p["tasks"][0]["application"] = application
    assert schema_problems(p) == []


def test_a_task_says_its_mode_and_only_dev_or_admin_may_write() -> None:
    def plan(application: str, mode: str | None) -> dict:
        task = {
            "title": "Prüfen",
            "application": application,
            "result": ["Befund"],
            "checklist": ["eins"],
        }
        if mode is not None:
            task["mode"] = mode
        return {"topic": {"title": "Thema"}, "tasks": [task]}

    ops, problems = compile_plan(plan("research", None))
    assert problems == []
    assert "Modus: lesen" in ops[1]["body"]  # reading is the default and is written down
    _, problems = compile_plan(plan("research", "schreiben"))
    assert problems and "only in dev or admin" in problems[0]
    ops, problems = compile_plan(plan("admin", "schreiben"))
    assert problems == [] and "Modus: schreiben" in ops[1]["body"]
