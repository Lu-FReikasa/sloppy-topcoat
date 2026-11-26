"""A large task cut into sub-tasks (D109, issue #31): the program ``decompose`` is valid and names
its form, the hub's check refuses what the form cannot (files, symbols, layers, limits, a cycle)
with every problem at once, and a refused answer is asked again once with those problems."""

import json
from typing import Any

from jsonschema import Draft202012Validator

from topcoat_hub import decompose, output_schemas
from topcoat_hub.blocks import builtin
from topcoat_hub.blocks.program import validate

CODE = {
    "hub/src/topcoat_hub/project_store.py": (
        "class ProjectStore:\n    def add(self):\n        pass\n\n    def close(self):\n"
        "        pass\n"
    ),
    "hub/src/topcoat_hub/api/project.py": "def router():\n    pass\n",
    "web/src/projects/ProjectView.tsx": "export function ProjectView() {\n  return null;\n}\n",
}


async def read(paths: list[str]) -> dict[str, str | None]:
    return {p: CODE.get(p) for p in paths}


def sub(sid: str, **kw: Any) -> dict[str, Any]:
    base = {
        "id": sid,
        "title": f"Teil {sid}",
        "goal": "one sentence",
        "layer": "store",
        "files": ["hub/src/topcoat_hub/project_store.py"],
        "symbols": ["ProjectStore.add"],
        "new_files": [f"hub/tests/test_{sid}.py"],
        "accept": [f"test:hub/tests/test_{sid}.py::test_it"],
        "depends_on": [],
    }
    return {**base, **kw}


GOOD = {
    "summary": "Teilaufgaben werden gespeichert und gezeigt",
    "subtasks": [
        sub("s1"),
        sub("s2", layer="api", files=["hub/src/topcoat_hub/api/project.py"], symbols=["router"],
            accept=["endpoint:GET /api/projects/1/large"], depends_on=["s1"]),
        sub("s3", layer="gui", files=["web/src/projects/ProjectView.tsx"],
            symbols=["ProjectView"], new_files=["web/src/projects/Large.test.tsx"],
            accept=["screenshot:/projects", "person: the list reads well"], depends_on=["s2"]),
        sub("s4", layer="research", files=[], symbols=[], new_files=[],
            accept=["person: the plan is sound"]),
    ],
}  # fmt: skip


def test_the_program_is_valid_and_its_form_takes_a_good_answer() -> None:
    prog = validate(builtin.PROGRAMS["decompose"])
    assert prog["stack"][-1]["schema"] == "decompose" and prog["stack"][-1]["out"] == "draft"
    schema = output_schemas.get("decompose")
    assert schema is not None
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(GOOD)
    too_many = {"summary": "x", "subtasks": [sub(f"s{i}") for i in range(9)]}
    assert not Draft202012Validator(schema).is_valid(too_many)


async def test_a_good_answer_passes_in_an_order_that_respects_the_dependencies() -> None:
    dec, problems = await decompose.check(json.dumps(GOOD), read)
    assert problems == [] and dec is not None
    assert dec.order() == ["s1", "s2", "s3", "s4"]
    assert dec.subtasks[2].accept == ["screenshot:/projects", "person: the list reads well"]
    assert json.loads(decompose.as_text(dec))["subtasks"][1]["depends_on"] == ["s1"]


async def test_every_problem_comes_back_at_once() -> None:
    bad = {
        "summary": "x",
        "subtasks": [
            sub("s1", depends_on=["s3"]),
            sub("s2", files=["hub/src/topcoat_hub/nope.py"], depends_on=["s9"]),
            sub("s3", symbols=["ProjectStore.ad"], depends_on=["s1"]),
            sub("s4", layer="gui"),  # hub files in a gui sub-task
            sub("s5", files=["a/b.py", "a/c.py", "a/d.py", "a/e.py"]),
            sub("s6", new_files=["hub/src/topcoat_hub/project_store.py"], accept=["works"]),
            sub("s7", layer="admin"),
        ],
    }
    dec, problems = await decompose.check(json.dumps(bad), read)
    text = "\n".join(problems)
    assert dec is None
    assert "cycle (sub-tasks s1, s3)" in text
    assert "depends on 's9', which is not in the answer" in text
    assert "nope.py does not exist" in text
    assert "'ProjectStore.ad' is not in" in text and "nearest: ProjectStore.add" in text
    assert "not under web/src/ (layer gui)" in text
    assert "4 files, at most 3" in text
    assert "project_store.py exists already" in text
    assert "'works' is not one of test:" in text
    assert "a admin sub-task changes no file" in text


async def test_limits_ids_and_form_of_an_answer() -> None:
    nine = {"summary": "x", "subtasks": [sub(f"s{i}") for i in range(9)]}
    _, problems = await decompose.check(json.dumps(nine), read)
    assert any("at most 8 per large task" in p for p in problems)
    twice = {"summary": "x", "subtasks": [sub("s1"), sub("s1", layer="nowhere")]}
    _, problems = await decompose.check(json.dumps(twice), read)
    assert any("the id stands twice" in p for p in problems)
    assert any("not 'nowhere'" in p for p in problems)
    assert (await decompose.check("no json here", read))[1] == ["the answer holds no JSON object"]
    own = {"summary": "x", "subtasks": [sub("s1", depends_on=["s1"])]}
    _, problems = await decompose.check(json.dumps(own), read)
    assert problems == ["sub-task s1: it depends on itself"]


async def test_a_path_the_workshop_could_not_read_is_named_not_passed() -> None:
    async def nothing(paths: list[str]) -> dict[str, str | None]:
        return {}

    _, problems = await decompose.check(json.dumps({"summary": "x", "subtasks": [sub("s1")]}),
                                        nothing)  # fmt: skip
    assert problems == ["sub-task s1: hub/src/topcoat_hub/project_store.py could not be read"]


async def test_a_refused_answer_is_asked_again_once_with_all_its_problems() -> None:
    first = json.dumps({"summary": "x", "subtasks": [sub("s1", symbols=["Nope"])]})
    answers = [first, json.dumps(GOOD)]
    calls: list[tuple[str, dict[str, Any]]] = []

    async def ask(program: str, inputs: dict[str, Any]) -> tuple[str, str]:
        calls.append((program, inputs))
        return answers[len(calls) - 1], "coder@node-amd"

    out = await decompose.decompose(ask, read, "Große Aufgabe", "- map", "Development")
    assert out.decomposition is not None and out.problems == [] and out.model == "coder@node-amd"
    assert [c[0] for c in calls] == ["decompose", "decompose"]
    assert calls[0][1]["feedback"] == "" and calls[0][1]["map"] == "- map"
    assert "'Nope' is not in" in calls[1][1]["feedback"] and first in calls[1][1]["feedback"]


async def test_two_refusals_end_with_the_problems_of_the_last() -> None:
    async def ask(program: str, inputs: dict[str, Any]) -> tuple[str, str]:
        return "{}", ""

    out = await decompose.decompose(ask, read, "t", "", "c", hint="Replan s2")
    assert out.decomposition is None and out.problems == ["the answer has no sub-tasks"]
    assert len(out.answers) == decompose.TRIES
