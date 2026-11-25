"""Export and templates of a project (PPH-04, 04j).

What is tested: the JSON export holds everything of the project, the Akte with the result of the
chain check (also when a line was tampered with); trashed nodes only when asked; the Markdown zip
has one file per node and a timeline and no path can leave the folder; a template keeps structure
only (no reports, status, answers or history); a project made from it matches the original on every
kept field, with dates shifted to the given start, and its Akte names the template; a template that
cannot be applied leaves no project behind; another person's project and template are not reachable.
"""

import io
import json
import zipfile
from datetime import datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user

START = "2026-11-02T09:00:00+01:00"


def _post(client: TestClient, url: str, body: dict[str, Any], who=AUTH, status: int = 201) -> Any:  # type: ignore[no-untyped-def]
    r = client.post(f"/api/project{url}", json=body, headers=who)
    assert r.status_code == status, r.text
    return r.json()


def _get(client: TestClient, url: str, who=AUTH, status: int = 200) -> Any:  # type: ignore[no-untyped-def]
    r = client.get(f"/api/project{url}", headers=who)
    assert r.status_code == status, r.text
    return r


def build(client: TestClient, title: str = "Launch") -> dict[str, Any]:
    """A project with every kind of thing the export has to carry; returns ids by short name."""
    project = _post(
        client, "/projects", {"title": title, "description": "Ship it", "color": "teal"}
    )
    pid = project["id"]
    nodes = f"/projects/{pid}/nodes"
    phase = _post(client, nodes, {"kind": "topic", "title": "Phase 1", "color": "blue"})
    plan = _post(
        client,
        nodes,
        {
            "kind": "task",
            "title": "Write the plan",
            "body": "Draft it\n- [ ] outline\n- [ ] numbers",
            "parent_id": phase["id"],
            "start_at": START,
            "due_at": "2026-11-04T17:00:00+01:00",
            "priority": 3,
            "estimate_minutes": 120,
            "is_milestone": False,
        },
    )
    review = _post(
        client,
        nodes,
        {
            "kind": "task",
            "title": "Review the plan",
            "parent_id": phase["id"],
            "due_at": "2026-11-06T12:00:00+01:00",
            "depends_on": [plan["id"]],
        },
    )
    weekly = _post(
        client,
        nodes,
        {
            "kind": "task",
            "title": "Weekly sync",
            "due_at": "2026-11-09T10:00:00+01:00",
            "repeat_rule": {"every": 1, "unit": "week"},
        },
    )
    note = _post(
        client,
        nodes,
        {"kind": "report", "report_kind": "fact", "title": "Budget is 10 k", "body": "secret fact"},
    )
    gone = _post(client, nodes, {"kind": "task", "title": "Dropped idea"})
    label = _post(client, "/labels", {"title": "plan", "color": "#336699"})
    r = client.put(
        f"/api/project/nodes/{plan['id']}/labels", json={"add": [label["id"]]}, headers=AUTH
    )
    assert r.status_code == 200, r.text
    _post(client, f"/nodes/{plan['id']}/reminders", {"relative_to": "due", "offset_seconds": -3600})
    _post(client, f"/nodes/{plan['id']}/reminders", {"at": "2026-11-03T08:00:00+01:00"})
    _post(client, f"/nodes/{weekly['id']}/relations", {"other_id": plan["id"], "kind": "related"})
    r = client.patch(
        f"/api/project/nodes/{review['id']}",
        json={"revision": review["revision"], "status": "done"},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    _post(client, f"/nodes/{gone['id']}/trash", {}, status=200)
    return {
        "project": project,
        "phase": phase,
        "plan": plan,
        "review": review,
        "weekly": weekly,
        "note": note,
        "gone": gone,
    }


# --- the JSON export ----------------------------------------------------------------------------


def test_the_json_export_holds_the_whole_project(client: TestClient) -> None:
    made = build(client)
    doc = _get(client, f"/projects/{made['project']['id']}/export").json()
    assert doc["format"] == "topcoat-project-export" and doc["version"] == 1
    assert doc["project"]["title"] == "Launch" and doc["project"]["description"] == "Ship it"
    by_title = {n["title"]: n for n in doc["nodes"]}
    assert set(by_title) == {
        "Phase 1",
        "Write the plan",
        "Review the plan",
        "Weekly sync",
        "Budget is 10 k",
    }  # the trashed node is not in it
    plan = by_title["Write the plan"]
    assert plan["number"] == "1.1" and plan["priority"] == 3 and plan["estimate_minutes"] == 120
    assert [lb["title"] for lb in plan["labels"]] == ["plan"]
    assert sorted(
        (r["relative_to"], r["offset_seconds"]) for r in plan["reminders"] if r["relative_to"]
    ) == [("due", -3600)]
    assert any(r["at"] for r in plan["reminders"])
    assert by_title["Weekly sync"]["repeat_rule"]["unit"] == "week"
    assert by_title["Review the plan"]["status"] == "done"
    assert by_title["Review the plan"]["depends_on"] == [plan["id"]]
    assert doc["labels"] == [
        {"id": doc["labels"][0]["id"], "title": "plan", "color": "#336699", "description": ""}
    ]
    kinds = sorted((r["kind"], r["node"]["ref"], r["other"]["ref"]) for r in doc["relations"])
    assert ("blocked_by", "Launch 1.2", "Launch 1.1") in kinds
    assert any(k[0] == "related" for k in kinds)
    assert doc["akte"]["chain"] == {
        "ok": True,
        "events": len(doc["akte"]["events"]),
        "problems": [],
    }
    assert doc["akte"]["events"][0]["kind"] == "project.created"
    assert (
        "Content-Disposition" in _get(client, f"/projects/{made['project']['id']}/export").headers
    )


def test_trashed_nodes_come_with_only_when_asked(client: TestClient) -> None:
    made = build(client)
    pid = made["project"]["id"]
    doc = _get(client, f"/projects/{pid}/export?trashed=true").json()
    gone = next(n for n in doc["nodes"] if n["title"] == "Dropped idea")
    assert gone["trashed_at"] is not None and doc["include_trashed"] is True


def test_the_export_reports_a_broken_chain(client: TestClient) -> None:
    made = build(client)
    pid = made["project"]["id"]
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE project_events SET node_title = 'forged' WHERE project_id = :p AND seq = 3"
            ),
            {"p": pid},
        )
    engine.dispose()
    doc = _get(client, f"/projects/{pid}/export").json()
    assert doc["akte"]["chain"]["ok"] is False
    assert any("seq 3" in p for p in doc["akte"]["chain"]["problems"])
    md = zipfile.ZipFile(
        io.BytesIO(_get(client, f"/projects/{pid}/export?format=markdown").content)
    )
    assert "BROKEN" in md.read("timeline.md").decode()


def test_the_markdown_zip_has_a_file_per_node_and_a_timeline(client: TestClient) -> None:
    made = build(client)
    pid = made["project"]["id"]
    r = _get(client, f"/projects/{pid}/export?format=markdown")
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"] == 'attachment; filename="project-launch.zip"'
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert {"README.md", "timeline.md", "project.json"} <= set(names)
    assert sorted(n for n in names if n.startswith("nodes/")) == [
        "nodes/1-phase-1.md",
        "nodes/1.1-write-the-plan.md",
        "nodes/1.2-review-the-plan.md",
        "nodes/2-weekly-sync.md",
        "nodes/3-budget-is-10-k.md",
    ]
    plan = zf.read("nodes/1.1-write-the-plan.md").decode()
    assert "# Launch 1.1 Write the plan" in plan and "- [ ] outline" in plan
    assert "**Labels:** plan" in plan and "related" in plan
    assert "Hash chain: intact" in zf.read("timeline.md").decode()
    assert "Created" in zf.read("timeline.md").decode()
    assert json.loads(zf.read("project.json"))["project"]["title"] == "Launch"


def test_a_hostile_title_cannot_leave_the_folder(client: TestClient) -> None:
    project = _post(client, "/projects", {"title": "../../etc"})
    _post(
        client, f"/projects/{project['id']}/nodes", {"kind": "topic", "title": "../../../evil\\x"}
    )
    r = _get(client, f"/projects/{project['id']}/export?format=markdown")
    assert r.headers["content-disposition"] == 'attachment; filename="project-etc.zip"'
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert all(".." not in n.split("/") and not n.startswith("/") and "\\" not in n for n in names)
    assert "nodes/1-evil-x.md" in names


# --- templates ----------------------------------------------------------------------------------


def _iso(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def shape(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """What a template keeps, read from an export (instants compared as instants)."""
    number = {n["id"]: n["number"] for n in doc["nodes"]}
    rows = []
    for n in doc["nodes"]:
        if n["kind"] == "report":
            continue
        rows.append(
            {
                "number": n["number"],
                "parent": number.get(n["parent_id"]),
                "kind": n["kind"],
                "title": n["title"],
                "priority": n["priority"],
                "estimate": n["estimate_minutes"],
                "color": n["color"],
                "milestone": n["is_milestone"],
                "repeat": n["repeat_rule"],
                "dates": [_iso(n[k]) for k in ("start_at", "due_at", "end_at")],
                "labels": sorted(lb["title"] for lb in n["labels"]),
                "reminders": sorted(
                    (r["relative_to"] or "", r["offset_seconds"] or 0, str(_iso(r["at"])))
                    for r in n["reminders"]
                ),
                "blocked_by": sorted(number.get(d, "?") for d in n["depends_on"]),
            }
        )
    # a symmetric relation is stored once, in the order of the ids (random): compare the pair sorted
    rels = sorted(
        (r["kind"], *sorted((r["node"]["ref"].split()[-1], r["other"]["ref"].split()[-1])))
        for r in doc["relations"]
        if r["valid_to"] is None and r["kind"] != "blocked_by"
    )
    return [*rows, {"relations": rels}]


def test_a_template_keeps_structure_and_nothing_personal(client: TestClient) -> None:
    made = build(client)
    saved = _post(client, "/templates", {"project_id": made["project"]["id"], "name": "Launch kit"})
    content = saved["content"]
    assert saved["nodes"] == 4 and saved["has_dates"] is True
    titles = [n["title"] for n in content["nodes"]]
    assert titles == ["Phase 1", "Write the plan", "Review the plan", "Weekly sync"]
    flat = json.dumps(content)
    for personal in ("secret fact", "Budget is 10 k", "Dropped idea", "done", "status", START):
        assert personal not in flat
    plan = content["nodes"][1]
    assert plan["offsets"] == {"start_at": 0, "due_at": 2 * 86400 + 8 * 3600}
    assert "[x]" not in plan["body"] and plan["labels"] == ["plan"]
    assert {"at_offset": 23 * 3600} in plan["reminders"]
    # a symmetric relation is stored once, in the order of the ids: compare the pair as a set
    assert sorted(
        (r["kind"], frozenset((r["node"], r["other"]))) for r in content["relations"]
    ) == [
        ("blocked_by", frozenset(("n3", "n2"))),
        ("related", frozenset(("n4", "n2"))),
    ]
    listed = _get(client, "/templates").json()["templates"]
    assert [t["name"] for t in listed] == ["Launch kit"] and "content" not in listed[0]
    again = client.post(
        "/api/project/templates",
        json={"project_id": made["project"]["id"], "name": "Launch kit"},
        headers=AUTH,
    )
    assert again.status_code == 422


def test_round_trip_export_template_new_project_compare(client: TestClient) -> None:
    made = build(client)
    pid = made["project"]["id"]
    original = _get(client, f"/projects/{pid}/export").json()
    tpl = _post(client, "/templates", {"project_id": pid, "name": "Launch kit"})
    new = _post(
        client,
        f"/templates/{tpl['id']}/projects",
        {"title": "Launch 2", "start_at": START},
    )
    assert new["description"] == "Ship it" and new["color"] == "teal"
    copy = _get(client, f"/projects/{new['id']}/export").json()
    assert shape(copy) == shape(original)
    states = {n["title"]: n["status"] for n in copy["nodes"] if n["kind"] == "task"}
    assert set(states.values()) == {"todo"}  # nothing is done in a fresh project
    assert all(n["kind"] != "report" for n in copy["nodes"])
    events = copy["akte"]["events"]
    assert copy["akte"]["chain"]["ok"] is True
    named = [e for e in events if e["kind"] == "project.from_template"]
    assert len(named) == 1 and named[0]["payload"]["template"] == "Launch kit"
    assert named[0]["payload"]["template_id"] == tpl["id"]
    assert named[0]["actor"]["kind"] == "person"
    assert {e["kind"] for e in events} >= {"project.created", "node.created", "relation.added"}
    assert all(e["project_id"] == new["id"] for e in events)  # no history is carried over
    assert _get(client, f"/projects/{pid}/export").json()["akte"]["chain"]["ok"] is True


def test_dates_move_with_the_start(client: TestClient) -> None:
    made = build(client)
    tpl = _post(client, "/templates", {"project_id": made["project"]["id"], "name": "Kit"})
    later = "2026-12-01T09:00:00+01:00"
    new = _post(client, f"/templates/{tpl['id']}/projects", {"title": "Later", "start_at": later})
    nodes = {n["title"]: n for n in _get(client, f"/projects/{new['id']}/export").json()["nodes"]}
    plan = nodes["Write the plan"]
    assert _iso(plan["start_at"]) == _iso(later)
    assert _iso(plan["due_at"]) == _iso(later) + timedelta(days=2, hours=8)
    reminders = {r["relative_to"] or "at": r for r in plan["reminders"]}
    assert _iso(reminders["due"]["fire_at"]) == _iso(plan["due_at"]) - timedelta(hours=1)
    assert _iso(reminders["at"]["at"]) == _iso(later) + timedelta(hours=23)
    undated = _post(client, f"/templates/{tpl['id']}/projects", {"title": "No dates"})
    plain = _get(client, f"/projects/{undated['id']}/export").json()["nodes"]
    assert all(n["due_at"] is None and n["start_at"] is None for n in plain)
    naive = client.post(
        f"/api/project/templates/{tpl['id']}/projects",
        json={"title": "Naive", "start_at": "2026-12-01T09:00:00"},
        headers=AUTH,
    )
    assert naive.status_code == 422


def test_a_failed_creation_leaves_no_project(client: TestClient) -> None:
    made = build(client)
    tpl = _post(client, "/templates", {"project_id": made["project"]["id"], "name": "Kit"})
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:  # a template whose repeat rule no longer passes
        conn.execute(
            text(
                "UPDATE project_templates SET content = jsonb_set(content,"
                ' \'{nodes,3,fields,repeat_rule}\', \'{"every": 0, "unit": "week"}\')'
            )
        )
    engine.dispose()
    bad = client.post(
        f"/api/project/templates/{tpl['id']}/projects", json={"title": "Broken"}, headers=AUTH
    )
    assert bad.status_code == 422
    titles = [p["title"] for p in _get(client, "/projects").json()["projects"]]
    assert titles == ["Launch"]
    dup = client.post(
        f"/api/project/templates/{tpl['id']}/projects", json={"title": "Launch"}, headers=AUTH
    )
    assert dup.status_code == 422


def test_templates_can_be_deleted(client: TestClient) -> None:
    made = build(client)
    tpl = _post(client, "/templates", {"project_id": made["project"]["id"], "name": "Kit"})
    assert client.delete(f"/api/project/templates/{tpl['id']}", headers=AUTH).status_code == 204
    assert _get(client, "/templates").json()["templates"] == []
    _get(client, f"/templates/{tpl['id']}", status=404)


# --- isolation ----------------------------------------------------------------------------------


def test_another_persons_project_and_template_are_not_reachable(client: TestClient) -> None:
    made = build(client)
    pid = made["project"]["id"]
    tpl = _post(client, "/templates", {"project_id": pid, "name": "Mine"})
    _, who = make_user(TEST_DB_URL, "stranger export")
    for fmt in ("json", "markdown"):
        _get(client, f"/projects/{pid}/export?format={fmt}&trashed=true", who, status=404)
    save = client.post(
        "/api/project/templates", json={"project_id": pid, "name": "Stolen"}, headers=who
    )
    assert save.status_code == 404
    assert _get(client, "/templates", who).json()["templates"] == []
    _get(client, f"/templates/{tpl['id']}", who, status=404)
    assert client.delete(f"/api/project/templates/{tpl['id']}", headers=who).status_code == 404
    use = client.post(
        f"/api/project/templates/{tpl['id']}/projects", json={"title": "Taken"}, headers=who
    )
    assert use.status_code == 404
    assert _get(client, "/projects", who).json()["projects"] == []
    assert [t["name"] for t in _get(client, "/templates").json()["templates"]] == ["Mine"]
    _get(client, f"/projects/{pid}/export")  # the owner still can
    anonymous = client.get(f"/api/project/projects/{pid}/export")
    assert anonymous.status_code in (401, 403)
