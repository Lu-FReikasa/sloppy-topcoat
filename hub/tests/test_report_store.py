"""The reports of the Ablage: versions that never change, their diff, comments, the person's status
clicks, and that nobody else can see them (D20, D102)."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, TEST_DB_URL
from tests.users import make_user
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.report_store import ReportError, ReportStore


def make(client: TestClient, user_id: uuid.UUID = DEV_ADMIN_ID, body: str = "# A\n\none\n") -> str:
    async def go() -> str:
        async with client.app.state.sessionmaker() as db:
            report = await ReportStore(db, user_id).create(
                title="Systemd",
                question="What is systemd?",
                language="en",
                depth="thorough",
                body=body,
                meta={"sources": 2},
            )
            await db.commit()
            return str(report.id)

    return client.portal.call(go)


def run(client: TestClient, work: Any, user_id: uuid.UUID = DEV_ADMIN_ID) -> Any:
    async def go() -> Any:
        async with client.app.state.sessionmaker() as db:
            out = await work(ReportStore(db, user_id))
            await db.commit()
            return out

    return client.portal.call(go)


def test_a_new_report_has_its_first_version(client: TestClient) -> None:
    rid = uuid.UUID(make(client))

    async def look(store: ReportStore) -> tuple[Any, Any]:
        return await store.get(rid), await store.version(rid, 1)

    report, version = run(client, look)
    assert (report.status, report.current_version, report.released_version) == ("draft", 1, None)
    assert (
        version.parent_version is None
        and version.kind == "agent"
        and len(version.content_hash) == 64
    )
    assert version.meta == {"sources": 2} and version.body.startswith("# A")


def test_a_revision_is_a_new_version_and_the_old_one_never_changes(client: TestClient) -> None:
    rid = uuid.UUID(make(client))

    async def revise(store: ReportStore) -> None:
        await store.add_version(rid, body="# A\n\ntwo\n", meta={}, kind="revision", note="shorter")
        await store.add_version(rid, body="# A\n\nthree\n", meta={}, kind="person", parent=1)

    run(client, revise)

    async def look(store: ReportStore) -> tuple[Any, Any]:
        return await store.get(rid), list(await store.versions(rid))

    report, versions = run(client, look)
    assert report.current_version == 3 and report.status == "draft"
    assert [(v.version, v.parent_version, v.kind) for v in versions] == [
        (1, None, "agent"),
        (2, 1, "revision"),
        (3, 1, "person"),  # a version can name the one it revised, as a branch point does
    ]
    assert versions[0].body.endswith("one\n") and versions[1].note == "shorter"
    assert len({v.content_hash for v in versions}) == 3


def test_a_version_that_changes_nothing_is_refused(client: TestClient) -> None:
    rid = uuid.UUID(make(client))

    async def same(store: ReportStore) -> None:
        await store.add_version(rid, body="# A\n\none\n", meta={}, kind="revision")

    with pytest.raises(ReportError, match="did not change"):
        run(client, same)


def test_the_diff_counts_what_changed(client: TestClient) -> None:
    rid = uuid.UUID(make(client, body="# A\n\none\ntwo\nthree\n"))

    async def work(store: ReportStore) -> dict[str, Any]:
        await store.add_version(rid, body="# A\n\none\n2\nthree\nfour\n", meta={}, kind="revision")
        return await store.diff(rid, 1, 2)

    diff = run(client, work)
    assert (diff["added"], diff["removed"]) == (2, 1)
    assert "-two" in diff["diff"] and "+2" in diff["diff"] and "+four" in diff["diff"]
    assert diff["diff"].startswith("--- v1\n+++ v2")


def test_the_person_releases_and_a_newer_draft_does_not_take_the_release_away(
    client: TestClient,
) -> None:
    rid = uuid.UUID(make(client))

    async def work(store: ReportStore) -> Any:
        with pytest.raises(ReportError):  # no release without a review
            await store.set_status(rid, "released")
        await store.set_status(rid, "reviewed")
        report = await store.set_status(rid, "released")
        assert (report.status, report.released_version) == ("released", 1)
        # a revision is a draft again, but what was released stays version 1
        await store.add_version(rid, body="# A\n\nmore\n", meta={}, kind="revision")
        report = await store.get(rid)
        assert (report.status, report.current_version, report.released_version) == ("draft", 2, 1)
        await store.set_status(rid, "reviewed")
        await store.set_status(rid, "draft")  # a review taken back keeps the release
        assert (await store.get(rid)).released_version == 1
        await store.set_status(rid, "reviewed")
        await store.set_status(rid, "released")
        assert (await store.get(rid)).released_version == 2
        report = await store.set_status(rid, "draft")  # withdrawn: nothing is released any more
        return report

    report = run(client, work)
    assert (report.status, report.released_version, report.released_at) == ("draft", None, None)


def test_comments_hang_on_a_version_and_a_chapter_and_can_be_resolved(client: TestClient) -> None:
    rid = uuid.UUID(make(client))

    async def work(store: ReportStore) -> Any:
        first = await store.add_comment(rid, text="Quelle fehlt", anchor="1-geschichte")
        await store.add_version(rid, body="# A\n\nnew\n", meta={}, kind="revision")
        await store.add_comment(rid, text="besser", anchor="")
        with pytest.raises(ReportError):
            await store.add_comment(rid, text="x", version=9)
        with pytest.raises(ReportError):
            await store.add_comment(rid, text="   ")
        await store.resolve_comment(first.id, True)
        return (
            [c.text for c in await store.comments(rid)],
            [c.text for c in await store.comments(rid, version=2)],
            [c.text for c in await store.comments(rid, open_only=True)],
        )

    every, second, still_open = run(client, work)
    assert every == ["Quelle fehlt", "besser"] and second == ["besser"] and still_open == ["besser"]


def test_nobody_else_sees_a_report(client: TestClient) -> None:
    rid = uuid.UUID(make(client))
    other, _ = make_user(TEST_DB_URL, "carol")

    async def look(store: ReportStore) -> None:
        assert list(await store.list()) == []
        for call in (
            store.get(rid),
            store.versions(rid),
            store.add_comment(rid, text="hi"),
            store.set_status(rid, "reviewed"),
            store.delete(rid),
        ):
            with pytest.raises(ReportError, match="no such report"):
                await call

    run(client, look, user_id=other)
    assert run(client, ReportStore.count) == 1


def test_one_report_per_task_a_continued_task_makes_a_new_version(client: TestClient) -> None:
    from sqlalchemy import create_engine, text

    task_id = uuid.uuid4()
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO agent_tasks (id, user_id, prompt, title) VALUES (:i, :u, 'p', 't')"),
            {"i": str(task_id), "u": str(DEV_ADMIN_ID)},
        )

    async def work(store: ReportStore) -> Any:
        a = await store.create(
            title="T",
            question="q",
            language="en",
            depth="thorough",
            body="x\n",
            meta={},
            task_id=task_id,
        )
        b = await store.create(
            title="T",
            question="q",
            language="en",
            depth="thorough",
            body="y\n",
            meta={},
            task_id=task_id,
        )
        assert a.id == b.id
        return (await store.get(a.id)).current_version, await store.by_task(task_id)

    version, found = run(client, work)
    assert version == 2 and found is not None


# --- the API ---------------------------------------------------------------------------------


def test_the_api_lists_reads_versions_comments_and_clicks(client: TestClient) -> None:
    rid = make(client, body="# A\n\none\n")
    run(
        client,
        lambda s: s.add_version(
            uuid.UUID(rid), body="# A\n\ntwo\n", meta={"sources": 3}, kind="revision", note="n"
        ),
    )
    listed = client.get("/api/reports", headers=AUTH).json()["reports"]
    assert [r["id"] for r in listed] == [rid] and listed[0]["unread"] is True
    one = client.get(f"/api/reports/{rid}", headers=AUTH).json()
    assert (
        one["current_version"] == 2
        and one["shown"]["body"].endswith("two\n")
        and one["shown"]["meta"] == {"sources": 3}
    )
    old = client.get(f"/api/reports/{rid}?version=1", headers=AUTH).json()
    assert old["shown"]["body"].endswith("one\n")
    assert client.get(f"/api/reports/{rid}?version=7", headers=AUTH).status_code == 404

    versions = client.get(f"/api/reports/{rid}/versions", headers=AUTH).json()["versions"]
    assert (
        [v["version"] for v in versions] == [1, 2]
        and versions[1]["note"] == "n"
        and "body" not in versions[0]
    )
    diff = client.get(f"/api/reports/{rid}/diff?from=1&to=2", headers=AUTH).json()
    assert (diff["added"], diff["removed"]) == (1, 1)

    posted = client.post(
        f"/api/reports/{rid}/comments", headers=AUTH, json={"text": "Beleg?", "anchor": "x"}
    )
    assert posted.status_code == 201 and posted.json()["version"] == 2
    cid = posted.json()["id"]
    assert client.get(f"/api/reports/{rid}", headers=AUTH).json()["comments"][0]["text"] == "Beleg?"
    done = client.patch(f"/api/reports/{rid}/comments/{cid}", headers=AUTH, json={"resolved": True})
    assert done.json()["resolved"] is True
    assert (
        client.get(f"/api/reports/{rid}/comments?open_only=true", headers=AUTH).json()["comments"]
        == []
    )
    assert (
        client.post(f"/api/reports/{rid}/comments", headers=AUTH, json={"text": ""}).status_code
        == 422
    )

    bad = client.post(f"/api/reports/{rid}/status", headers=AUTH, json={"status": "released"})
    assert bad.status_code == 409 and bad.json()["detail"]["code"] == "conflict"
    assert (
        client.post(f"/api/reports/{rid}/status", headers=AUTH, json={"status": "reviewed"}).json()[
            "status"
        ]
        == "reviewed"
    )
    assert (
        client.post(f"/api/reports/{rid}/status", headers=AUTH, json={"status": "released"}).json()[
            "released_version"
        ]
        == 2
    )

    assert client.post(f"/api/reports/{rid}/read", headers=AUTH).status_code == 204
    assert client.get("/api/reports", headers=AUTH).json()["reports"][0]["unread"] is False
    assert client.delete(f"/api/reports/{rid}", headers=AUTH).status_code == 204
    assert client.get(f"/api/reports/{rid}", headers=AUTH).status_code == 404


def test_the_api_is_the_callers_own_and_needs_a_login(client: TestClient) -> None:
    rid = make(client)
    _, carol = make_user(TEST_DB_URL, "carol2")
    assert client.get("/api/reports").status_code == 401
    assert client.get("/api/reports", headers=carol).json()["reports"] == []
    for call in (
        client.get(f"/api/reports/{rid}", headers=carol),
        client.get(f"/api/reports/{rid}/versions", headers=carol),
        client.post(f"/api/reports/{rid}/status", headers=carol, json={"status": "reviewed"}),
        client.delete(f"/api/reports/{rid}", headers=carol),
    ):
        assert call.status_code == 404


def test_the_release_of_an_older_version_can_be_withdrawn_while_a_newer_draft_exists(
    client: TestClient,
) -> None:
    rid = uuid.UUID(make(client))

    async def work(store: ReportStore) -> Any:
        await store.set_status(rid, "reviewed")
        await store.set_status(rid, "released")
        await store.add_version(rid, body="# A\n\nnewer\n", meta={}, kind="revision")
        before = await store.get(rid)
        assert (before.status, before.released_version) == ("draft", 1)
        after = await store.set_status(rid, "draft")  # takes the release of version 1 back
        return after

    report = run(client, work)
    assert (report.status, report.released_version, report.current_version) == ("draft", None, 2)

    async def nothing_to_withdraw(store: ReportStore) -> None:
        await store.set_status(rid, "draft")

    with pytest.raises(ReportError):
        run(client, nothing_to_withdraw)  # a draft that was never released cannot be "withdrawn"
