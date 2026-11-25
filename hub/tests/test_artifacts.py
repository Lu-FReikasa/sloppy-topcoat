"""The library (reports apart from chats): a finished task files its report, the person reads,
marks and deletes it, and nobody else can (D20)."""

import uuid
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import eventually
from tests.test_agent import finish, start, tool, use, vm, wait_for  # noqa: F401
from tests.test_core_background import A_ID
from tests.users import make_user
from topcoat_hub.artifact_store import ArtifactStore


def _library(client: TestClient, who=AUTH) -> dict[str, Any]:
    r = client.get("/api/artifacts", headers=who)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_finished_task_files_its_report_in_the_library_not_in_a_chat(
    client: TestClient,
    vm,  # noqa: F811
) -> None:
    use(client, tool("vm-docker", "docker.ps"), finish("The web container ran out of memory."))
    t = wait_for(client, start(client).json()["id"], "done")
    lib = eventually(lambda: _nonempty(_library(client)))
    [item] = lib["artifacts"]
    assert item["task_id"] == t["id"] and item["unread"] is True and lib["unread"] == 1
    assert item["conversation_id"] is None and item["title"] == t["title"]
    full = client.get(f"/api/artifacts/{item['id']}", headers=AUTH).json()
    assert "ran out of memory" in full["body"]
    # read, then gone from the unread count
    assert client.post(f"/api/artifacts/{item['id']}/read", headers=AUTH).status_code == 204
    assert _library(client)["unread"] == 0
    assert client.delete(f"/api/artifacts/{item['id']}", headers=AUTH).status_code == 204
    assert _library(client)["artifacts"] == []
    assert client.get(f"/api/artifacts/{item['id']}", headers=AUTH).status_code == 404


def _nonempty(lib: dict[str, Any]) -> dict[str, Any]:
    assert lib["artifacts"], "no report yet"
    return lib


def test_another_user_cannot_see_open_mark_or_delete_a_report(
    client: TestClient,
    vm,  # noqa: F811
    clean_db: str,
) -> None:
    use(client, finish("Geheimer Befund von A"))
    wait_for(client, start(client).json()["id"], "done")
    [item] = eventually(lambda: _nonempty(_library(client)))["artifacts"]
    _, b = make_user(clean_db, "user-b")
    assert _library(client, b) == {"artifacts": [], "unread": 0}
    path = f"/api/artifacts/{item['id']}"
    assert client.get(path, headers=b).status_code == 404
    assert client.post(f"{path}/read", headers=b).status_code == 404
    assert client.delete(path, headers=b).status_code == 404
    assert _library(client)["artifacts"] != []  # untouched


def test_filing_a_report_twice_rewrites_it_and_it_is_unread_again(
    client: TestClient,
    vm,  # noqa: F811
) -> None:
    use(client, finish("first answer"))
    t = wait_for(client, start(client).json()["id"], "done")
    [item] = eventually(lambda: _nonempty(_library(client)))["artifacts"]
    client.post(f"/api/artifacts/{item['id']}/read", headers=AUTH)
    sessionmaker = client.app.state.sessionmaker

    async def again() -> None:
        async with sessionmaker() as db:
            await ArtifactStore(db, A_ID).put_report(uuid.UUID(t["id"]), "Neu", "second answer")
            await db.commit()

    client.portal.call(again)
    lib = _library(client)
    assert len(lib["artifacts"]) == 1 and lib["unread"] == 1
    full = client.get(f"/api/artifacts/{item['id']}", headers=AUTH).json()
    assert full["title"] == "Neu" and full["body"] == "second answer"


def test_the_lists_say_the_application_and_whether_the_hubs_own_work_made_an_entry(
    client: TestClient,
) -> None:
    """The Ablage filters by application and hides what a project run or a block program made
    unless asked (the lists were 400 entries of the documentation pipeline, 2026-11-13)."""
    from topcoat_hub.models import AgentTask
    from topcoat_hub.report_store import ReportStore

    sessionmaker = client.app.state.sessionmaker
    ids = {name: uuid.uuid4() for name in ("person", "block", "batch")}

    async def fill() -> None:
        async with sessionmaker() as db:
            for name, origin, app in (
                ("person", "tasks", "dev"),
                ("block", "block", "dev"),
                ("batch", "batch", "research"),
            ):
                db.add(
                    AgentTask(
                        id=ids[name],
                        user_id=A_ID,
                        prompt=name,
                        title=name,
                        origin=origin,
                        application=app,
                        status="done",
                    )
                )
            await db.flush()
            store = ArtifactStore(db, A_ID)
            await store.put_report(ids["person"], "person", "text")
            await store.put_report(ids["block"], "block", "text")
            reports = ReportStore(db, A_ID)
            common = {
                "question": "q",
                "language": "de",
                "depth": "thorough",
                "body": "b",
                "meta": {},
            }
            await reports.create(title="by batch", task_id=ids["batch"], **common)
            await reports.create(title="by a program", **common)
            await db.commit()

    client.portal.call(fill)
    seen = {a["title"]: (a["application"], a["auto"]) for a in _library(client)["artifacts"]}
    assert seen == {"person": ("dev", False), "block": ("dev", True)}
    listed = client.get("/api/reports", headers=AUTH).json()["reports"]
    assert {r["title"]: r["auto"] for r in listed} == {"by batch": True, "by a program": True}
