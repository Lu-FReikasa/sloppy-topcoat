"""Harmless variants of an operation's names are mapped to the schema's (the model wrote parent_id
and repeated the project; the set was refused on the form, which the model then repeated)."""

import uuid

from topcoat_hub.project_changesets import check_operations, normalize_operations

PROJECT = uuid.uuid4()


def test_parent_id_is_the_parent_and_the_project_is_the_calls() -> None:
    ops = [
        {
            "op": "create_node",
            "kind": "topic",
            "title": "Abgleich",
            "project_id": str(PROJECT),
            "parent_id": None,
        }
    ]
    fixed = normalize_operations(ops, PROJECT)
    assert fixed == [{"op": "create_node", "kind": "topic", "title": "Abgleich"}]
    check_operations(fixed)  # the schema accepts it


def test_a_project_named_as_parent_is_the_top_level() -> None:
    ops = [{"op": "create_node", "kind": "task", "title": "x", "parent": str(PROJECT)}]
    assert "parent" not in normalize_operations(ops, PROJECT)[0]
    none = [{"op": "create_node", "kind": "task", "title": "x", "parent": None}]
    assert "parent" not in normalize_operations(none, PROJECT)[0]


def test_both_spellings_keep_the_schemas() -> None:
    ops = [{"op": "create_node", "kind": "task", "title": "x", "parent": "n1", "parent_id": "n2"}]
    assert normalize_operations(ops, PROJECT)[0]["parent"] == "n1"


def test_unknown_names_are_still_refused() -> None:
    ops = [{"op": "create_node", "kind": "task", "title": "x", "colour_of_the_sky": "blue"}]
    try:
        check_operations(normalize_operations(ops, PROJECT))
    except Exception as exc:  # the schema's refusal, as before
        assert "colour_of_the_sky" in str(exc) or "Additional" in str(exc)
    else:
        raise AssertionError("an unknown field must still be refused")


def test_a_plans_task_names_its_place_and_its_result_form() -> None:
    from topcoat_hub.project_changesets import SetError, check_task_form

    bare = [{"op": "create_node", "kind": "task", "title": "Repo prüfen"}]
    no_target = [{"op": "create_node", "kind": "task", "title": "x", "body": "Ergebnisform: A | B"}]
    no_form = [{"op": "create_node", "kind": "task", "title": "x", "body": "Zielanwendung: dev"}]
    good = [
        {
            "op": "create_node",
            "kind": "task",
            "title": "x",
            "body": "Zielanwendung: dev\nErgebnisform: Artikel | Befund\n- [ ] a",
        },
        {"op": "create_node", "kind": "topic", "title": "Thema"},
    ]
    for bad in (bare, no_target, no_form):
        try:
            check_task_form(bad)
        except SetError:
            pass
        else:
            raise AssertionError("a plan's task without its place or its form must be refused")
    check_task_form(good)  # a topic needs none


def test_a_parent_the_set_created_earlier_is_found_however_it_is_named() -> None:
    from topcoat_hub.project_changesets import normalize_operations

    for parent in ("local:1", "<topic_id_placeholder>", "local:n1", "Launch", "local:Launch"):
        ops = [
            {"op": "create_node", "kind": "topic", "title": "Launch plan"},
            {"op": "create_node", "kind": "task", "title": "Write", "parent": parent},
        ]
        fixed = normalize_operations(ops, PROJECT)
        assert fixed[1]["parent"] == fixed[0]["local"], parent


def test_an_unclear_parent_is_still_refused() -> None:
    from topcoat_hub.project_changesets import check_operations, normalize_operations

    ops = [
        {"op": "create_node", "kind": "topic", "title": "A"},
        {"op": "create_node", "kind": "topic", "title": "B"},
        {"op": "create_node", "kind": "task", "title": "x", "parent": "nowhere"},
    ]
    with_parent = normalize_operations(ops, PROJECT)
    assert with_parent[2]["parent"] == "nowhere"
    try:
        check_operations(with_parent)
    except Exception:
        return
    raise AssertionError("an unknown parent must still be refused")


def test_a_name_the_set_gave_is_written_without_the_prefix() -> None:
    from topcoat_hub.project_changesets import check_operations, normalize_operations

    ops = [
        {"op": "create_node", "kind": "topic", "title": "Plan", "local": "n1"},
        {
            "op": "create_node",
            "kind": "task",
            "title": "x",
            "parent": "local:n1",
            "body": "Zielanwendung: dev\nErgebnisform: a | b",
        },
    ]
    fixed = normalize_operations(ops, PROJECT)
    assert fixed[1]["parent"] == "n1"
    check_operations(fixed)


def test_a_parent_id_that_names_a_created_node_is_resolved() -> None:
    from topcoat_hub.project_changesets import check_operations, normalize_operations

    ops = [
        {"op": "create_node", "kind": "topic", "parent_id": str(PROJECT), "title": "Plan"},
        {
            "op": "create_node",
            "kind": "task",
            "parent_id": "n1",
            "title": "x",
            "body": "Zielanwendung: dev\nErgebnisform: a | b",
        },
    ]
    fixed = normalize_operations(ops, PROJECT)
    assert "parent" not in fixed[0] and fixed[1]["parent"] == fixed[0]["local"]
    check_operations(fixed)


def test_a_made_up_name_for_the_one_topic_is_found_with_several_tasks() -> None:
    from topcoat_hub.project_changesets import check_operations, normalize_operations

    body = "Zielanwendung: research\nErgebnisform: a | b"
    ops = [
        {"op": "create_node", "kind": "topic", "parent_id": str(PROJECT), "title": "Plan"},
        {
            "op": "create_node",
            "kind": "task",
            "parent_id": "<topic_id_placeholder>",
            "title": "A",
            "body": body,
        },
        {"op": "create_node", "kind": "task", "parent_id": "local:1", "title": "B", "body": body},
    ]
    fixed = normalize_operations(ops, PROJECT)
    topic = fixed[0]["local"]
    assert fixed[1]["parent"] == topic and fixed[2]["parent"] == topic
    check_operations(fixed)


def test_a_plans_task_needs_its_checklist_too() -> None:
    from topcoat_hub.project_changesets import SetError, check_task_form

    no_list = [
        {
            "op": "create_node",
            "kind": "task",
            "title": "x",
            "body": "Zielanwendung: dev\nErgebnisform: a | b",
        }
    ]
    try:
        check_task_form(no_list)
    except SetError:
        pass
    else:
        raise AssertionError("a plan's task without a checklist must be refused")
    ok = [
        {
            "op": "create_node",
            "kind": "task",
            "title": "x",
            "body": "Zielanwendung: dev\nErgebnisform: a | b\n- [ ] Punkt",
        }
    ]
    check_task_form(ok)


def test_a_node_name_is_refused_as_an_application_with_the_reason() -> None:
    from topcoat_hub.project_changesets import SetError, check_task_form

    op = [
        {
            "op": "create_node",
            "kind": "task",
            "title": "x",
            "body": "Zielanwendung: dev-node\nErgebnisform: a | b\n- [ ] Punkt",
        }
    ]
    try:
        check_task_form(op)
    except SetError as exc:
        assert "not an application" in str(exc)
    else:
        raise AssertionError("a node name is no application")


def test_a_plans_task_gets_the_missing_lines_instead_of_a_refusal() -> None:
    from topcoat_hub.project_changesets import check_task_form, complete_task_form

    ops = [
        {"op": "create_node", "kind": "task", "title": "[Entwicklung] Code für Themes"},
        {
            "op": "create_node",
            "kind": "task",
            "title": "Wiki prüfen",
            "body": "Ergebnisform: A | B",
        },
        {"op": "create_node", "kind": "topic", "title": "Thema"},
    ]
    done = complete_task_form(ops)
    assert done[0]["body"].splitlines() == [
        "Zielanwendung: dev",
        "Ergebnisform: Ergebnis | Beleg",
        "- [ ] [Entwicklung] Code für Themes",
    ]
    assert done[1]["body"].startswith(
        "Zielanwendung: research\n- [ ] Wiki prüfen\nErgebnisform: A | B"
    )
    assert done[2] == ops[2]
    check_task_form(done)


def test_a_named_but_unknown_application_is_not_replaced() -> None:
    from topcoat_hub.project_changesets import SetError, check_task_form, complete_task_form

    op = [{"op": "create_node", "kind": "task", "title": "x", "body": "Zielanwendung: dev-node"}]
    done = complete_task_form(op)
    assert "Zielanwendung: dev-node" in done[0]["body"]
    try:
        check_task_form(done)
    except SetError:
        pass
    else:
        raise AssertionError("an unknown application must still be refused")


THEMES = str(uuid.uuid4())


def test_the_numbers_in_the_titles_say_the_tree_and_leave_the_titles() -> None:
    # live 2026-11-15: "1.8 Wireframe, 1.8.1 Recherche, 1.8.2 Dev" became 1.8, 1.9 and 1.10
    ops = [
        {"op": "create_node", "kind": "task", "title": "1.8 Wireframe", "parent": THEMES},
        {"op": "create_node", "kind": "task", "title": "1.8.1 Recherche", "parent": THEMES},
        {"op": "create_node", "kind": "task", "title": "1.8.2 Dev", "parent": THEMES},
    ]
    fixed = normalize_operations(ops, PROJECT)
    assert [op["title"] for op in fixed] == ["Wireframe", "Recherche", "Dev"]
    assert fixed[0]["parent"] == THEMES
    assert fixed[1]["parent"] == fixed[2]["parent"] == fixed[0]["local"]
    check_operations(fixed)
    # one numbered title alone is a title, not a scheme
    alone = [{"op": "create_node", "kind": "task", "title": "3.5 mm Klinke", "parent": THEMES}]
    assert normalize_operations(alone, PROJECT)[0]["title"] == "3.5 mm Klinke"


def test_the_same_placeholder_twice_is_the_same_parent() -> None:
    for placeholder in ("<new_node_id_1.8>", "n1"):
        ops = [
            {"op": "create_node", "kind": "task", "title": "Wireframe", "parent": THEMES},
            {"op": "create_node", "kind": "task", "title": "Recherche", "parent": placeholder},
            {"op": "create_node", "kind": "task", "title": "Dev", "parent": placeholder},
        ]
        fixed = normalize_operations(ops, PROJECT)
        assert fixed[1]["parent"] == fixed[2]["parent"] == fixed[0]["local"], placeholder
        check_operations(fixed)


def test_a_task_for_the_administration_is_guessed_as_admin() -> None:
    """Live 2026-11-18: "Punkt 2 an Verwaltung delegieren" without a line became research."""
    from topcoat_hub.project_changesets import complete_task_form

    ops = [
        {
            "op": "create_node",
            "kind": "task",
            "title": "Modelle der Nodes",
            "body": "Punkt 2 an Verwaltung delegieren.",
        }
    ]
    assert complete_task_form(ops)[0]["body"].startswith("Zielanwendung: admin\n")


def test_an_application_named_by_another_word_is_its_key() -> None:
    """Live 2026-11-18: "Zielanwendung: development" was refused and cost the run a turn."""
    from topcoat_hub.project_changesets import check_task_form, normalize_operations

    body = "Zielanwendung: {}\nErgebnisform: Frage | Antwort\n- [ ] x"
    for word, key in (("development", "dev"), ("Verwaltung", "admin"), ("Recherche", "research")):
        op = {"op": "create_node", "kind": "task", "title": "x", "body": body.format(word)}
        done = normalize_operations([op])
        assert done[0]["body"].startswith(f"Zielanwendung: {key}\n")
        check_task_form(done)
    # a name nobody uses for an application stays and is refused later
    odd = {"op": "create_node", "kind": "task", "title": "x", "body": body.format("vm-a")}
    assert "Zielanwendung: vm-a" in normalize_operations([odd])[0]["body"]


def test_a_point_about_a_machine_belongs_to_the_administration() -> None:
    """Issue 2.4 (live 2026-11-18): three plans in a row put "Welche Modelle auf node-amd und
    node-nvidia …" into a dev or research task, which can never see the machines."""
    import pytest

    from topcoat_hub.project_changesets import SetError, check_machine_points

    def task(app: str, point: str) -> dict:
        body = f"Zielanwendung: {app}\nErgebnisform: Frage | Antwort\n- [ ] {point}"
        return {"op": "create_node", "kind": "task", "title": "x", "body": body}

    machines = ["node-amd", "node-nvidia"]
    with pytest.raises(SetError, match="Zielanwendung: admin"):
        check_machine_points([task("research", "Welche Modelle laufen auf node-amd?")], machines)
    with pytest.raises(SetError, match="node-nvidia"):
        check_machine_points([task("dev", "VRAM von node-nvidia?")], machines)
    check_machine_points([task("admin", "Welche Modelle laufen auf node-amd?")], machines)
    check_machine_points([task("research", "Wie arbeitet node-amdx?")], machines)  # no whole word
    check_machine_points([task("research", "Wie nimmt llama-server Bilder an?")], machines)
