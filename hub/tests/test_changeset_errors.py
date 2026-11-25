"""AGT-02: a wrong call of a writing tool teaches the right one."""

import json

import pytest

from topcoat_hub import project_changesets as cs
from topcoat_hub import project_tools


@pytest.mark.parametrize("name", sorted(cs.SCHEMAS))
def test_every_operation_has_an_example_that_validates(name: str) -> None:
    # (the local names and ids are placeholders: the schema only wants strings of a length)
    example = dict(cs.EXAMPLES[name])
    cs.jsonschema.validate(example, cs.SCHEMAS[name])


def test_the_tool_offers_the_keys_of_every_operation_to_the_model() -> None:
    spec = project_tools.SPECS["project.changeset.propose"]
    item = spec["schema"]["properties"]["operations"]["items"]
    # an entry without op reaches the hub (guess_op, or the check's refusal with an example)
    assert "required" not in item
    assert "enum" not in item["properties"]["op"]  # an unknown op gets the message of the check
    assert all(name in spec["description"] for name in cs.SCHEMAS)  # the names are listed there
    every_key = {k for s in cs.SCHEMAS.values() for k in s["properties"]} - {"op"}
    assert every_key <= set(item["properties"])
    assert "node" in item["properties"] and "node_id" not in item["properties"]


@pytest.mark.parametrize("name", sorted(cs.SCHEMAS))
def test_an_example_passes_the_schema_of_the_tool_itself(name: str) -> None:
    schema = project_tools.SPECS["project.changeset.propose"]["schema"]
    call = {"project_id": "610e361a-62ff-42cf-b5d3-2268b31e3d27", "operations": [cs.EXAMPLES[name]]}
    cs.jsonschema.validate(call, schema)


def test_a_wrong_key_passes_the_tool_schema_and_is_taught_by_the_check() -> None:
    # the schema of the tool must not answer first with a general message: the check teaches
    schema = project_tools.SPECS["project.changeset.propose"]["schema"]
    wrong = {"op": "update_node", "node_id": "n1", "status": "dropped"}
    call = {"project_id": "610e361a-62ff-42cf-b5d3-2268b31e3d27", "operations": [wrong]}
    cs.jsonschema.validate(call, schema)
    with pytest.raises(cs.SetError) as caught:
        cs.check_operations([wrong])
    assert "`node_id` is called `node`" in caught.value.reason


def test_the_description_steers_one_change_to_the_single_tool_and_shows_examples() -> None:
    text = project_tools.SPECS["project.changeset.propose"]["description"]
    assert "ONE change" in text and "project.node.update" in text
    assert "`node`" in text and "not `node_id`" in text
    for name in ("set_status", "update_node", "create_node"):
        assert json.dumps(cs.EXAMPLES[name]) in text


def test_an_unknown_key_names_the_operation_it_belongs_to_and_the_allowed_keys() -> None:
    # what a 12B model wrote on 2026-10-30: node_id, reason and status inside update_node
    wrong = {
        "op": "update_node",
        "node": "n1",
        "node_id": "n1",
        "reason": "duplicate",
        "status": "dropped",
    }
    with pytest.raises(cs.SetError) as caught:
        cs.check_operations([wrong])
    text = caught.value.reason
    assert "'node_id'" in text and "unexpected" in text  # the schema's own words stay
    assert "`node_id` is called `node`" in text
    assert "`status` belongs to the operation `set_status`" in text
    assert "`reason` is not a field of any operation" in text
    assert "update_node takes:" in text and "revision" in text and "(required: node)" in text
    assert json.dumps(cs.EXAMPLES["update_node"]) in text


def test_a_missing_key_shows_the_example_too() -> None:
    with pytest.raises(cs.SetError) as caught:
        cs.check_operations([{"op": "set_status", "node": "n1"}])
    assert "'status' is a required property" in caught.value.reason
    assert json.dumps(cs.EXAMPLES["set_status"]) in caught.value.reason


NODE = "75c1fa00-7086-4e90-af46-a5a903dbc081"


def test_an_application_given_as_a_field_becomes_the_line_of_the_body() -> None:
    # live 2026-11-18: "application": "research" and "labels" on update_node were refused, and the
    # run gave up; the meaning is plain, so it is kept
    ops = cs.normalize_operations(
        [
            {
                "op": "update_node",
                "node": NODE,
                "revision": 1,
                "body": "Zielanwendung: dev\nErgebnisform: Ergebnis | Beleg\n- [ ] a",
                "application": "research",
                "labels": ["research"],
            },
            {"op": "create_node", "kind": "task", "title": "t", "application": "dev"},
        ]
    )
    assert ops[0] == {
        "op": "update_node",
        "node": NODE,
        "revision": 1,
        "body": "Zielanwendung: research\nErgebnisform: Ergebnis | Beleg\n- [ ] a",
    }
    assert ops[1] == {"op": "label", "node": NODE, "labels": ["research"]}
    assert ops[2]["body"] == "Zielanwendung: dev" and "application" not in ops[2]
    cs.check_operations(ops)  # every one of them passes the schema now
    # without a body to write the field stays, and the check names it
    alone = cs.normalize_operations([{"op": "update_node", "node": "n1", "application": "dev"}])
    assert alone[0]["application"] == "dev"


def test_a_status_on_an_update_becomes_its_own_operation() -> None:
    # live 2026-11-18: {"op": "update_node", "status": "dropped"} was refused, the run gave up
    ops = cs.normalize_operations([{"op": "update_node", "node": NODE, "status": "dropped"}])
    assert ops == [{"op": "set_status", "node": NODE, "status": "dropped"}]
    both = cs.normalize_operations(
        [{"op": "update_node", "node": NODE, "revision": 2, "title": "t", "status": "done"}]
    )
    assert both == [
        {"op": "set_status", "node": NODE, "status": "done"},
        {"op": "update_node", "node": NODE, "revision": 2, "title": "t"},
    ]
    cs.check_operations(both)


def test_a_body_escaped_twice_gets_its_line_breaks_back() -> None:
    # live 2026-11-18: the body arrived as one line with the two characters backslash and n, and
    # "Zielanwendung: research\nErgebnisform: ..." was read as the name of the application
    escaped = "Zielanwendung: research" + "\\n" + "Ergebnisform: Frage | Quelle" + "\\n" + "- [ ] a"
    (op,) = cs.normalize_operations([{"op": "create_node", "kind": "task", "title": "t",
                                      "body": escaped}])  # fmt: skip
    assert op["body"] == "Zielanwendung: research\nErgebnisform: Frage | Quelle\n- [ ] a"
    cs.check_task_form([op])
    # a text that has real line breaks keeps a backslash-n it means (code, a path)
    kept = "Zeile 1\nprint('a\\nb')"
    (same,) = cs.normalize_operations([{"op": "create_node", "title": "t", "body": kept}])
    assert same["body"] == kept


def test_admin_is_an_application_a_task_may_name() -> None:
    body = "Zielanwendung: admin\nErgebnisform: Befund | Beleg\n- [ ] Modelle auf node-amd"
    cs.check_task_form([{"op": "create_node", "kind": "task", "title": "t", "body": body}])
    wrong = "Zielanwendung: node-nvidia\nErgebnisform: Befund | Beleg\n- [ ] a"
    with pytest.raises(cs.SetError, match="research, dev, admin or project"):
        cs.check_task_form([{"op": "create_node", "kind": "task", "title": "t", "body": wrong}])
