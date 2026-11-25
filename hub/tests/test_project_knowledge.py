"""The reports of the projects in the knowledge base (PPH-04, 04d; project_knowledge.py).

Scope: the current documents, decisions and facts and the descriptions of the projects are written
into the person's own source "Projekte"; a superseded, trashed or other kind of report is not; an
unchanged file is not written again; what disappeared is taken out; a second person sees nothing of
it.
"""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import TEST_DB_URL
from tests.users import make_user
from topcoat_hub.models import DEV_ADMIN_ID, KnowledgeFile, KnowledgeSource
from topcoat_hub.project_knowledge import SOURCE, ProjectReportIndex, slug
from topcoat_hub.project_store import ProjectStore


def test_names_are_plain_ascii() -> None:
    assert slug("Welche Datenbank? Über Größe") == "welche-datenbank-ueber-groesse"
    assert slug("???") == "untitled"


async def files_of(client: TestClient, user_id: uuid.UUID) -> dict[str, str]:
    async with client.app.state.sessionmaker() as db:
        source = await db.scalar(
            select(KnowledgeSource).where(
                KnowledgeSource.user_id == user_id, KnowledgeSource.name == SOURCE
            )
        )
        if source is None:
            return {}
        rows = await db.execute(
            select(KnowledgeFile.path, KnowledgeFile.content_hash).where(
                KnowledgeFile.source_id == source.id
            )
        )
        return {path: digest for path, digest in rows}


async def test_current_reports_are_indexed_and_the_rest_is_left_out(client: TestClient) -> None:
    sessionmaker = client.app.state.sessionmaker
    store = ProjectStore(sessionmaker)
    index = ProjectReportIndex(sessionmaker, client.app.state.knowledge)

    project = await store.create_project(
        DEV_ADMIN_ID, title="Database", description="Which store holds the Akte?"
    )
    decision = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="report", title="Use Postgres", report_kind="decision",
        body="We decided on PostgreSQL because of pgvector.",
    )  # fmt: skip
    await store.create_node(
        DEV_ADMIN_ID, project.id, kind="report", title="A thought", report_kind="note"
    )
    await store.create_node(DEV_ADMIN_ID, project.id, kind="task", title="Install it")

    assert await index.sync_user(DEV_ADMIN_ID) == 2  # the description and the decision
    files = await files_of(client, DEV_ADMIN_ID)
    assert sorted(f.split("-", 2)[2] for f in files) == [
        "1-use-postgres.md",
        "database.md",
    ]
    assert not any("thought" in f or "install" in f for f in files)

    assert await index.sync_user(DEV_ADMIN_ID) == 0  # nothing changed, nothing is written again

    newer = await store.create_node(
        DEV_ADMIN_ID,
        project.id,
        kind="report",
        title="Use SQLite",
        report_kind="decision",
        body="Changed our mind.",
    )
    await store.supersede_report(DEV_ADMIN_ID, decision.id, by_id=newer.id)
    assert await index.sync_user(DEV_ADMIN_ID) == 2  # the new one in, the superseded one out
    files = await files_of(client, DEV_ADMIN_ID)
    assert any("use-sqlite" in f for f in files)
    assert not any("use-postgres" in f for f in files)


async def test_the_document_of_a_doc_program_stays_out_and_an_indexed_one_leaves(
    client: TestClient,
) -> None:
    sessionmaker = client.app.state.sessionmaker
    store = ProjectStore(sessionmaker)
    index = ProjectReportIndex(sessionmaker, client.app.state.knowledge)
    project = await store.create_project(DEV_ADMIN_ID, title="Docs", description="")
    task = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="task", title="B12 Knowledge",
        body="Ziel\nProgramm: doc_section",
    )  # fmt: skip
    other = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="task", title="Plain", body="no program"
    )
    plain = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="report", title="Plain doc", report_kind="document",
        body="kept", parent_id=other.id,
    )  # fmt: skip
    assert await index.sync_user(DEV_ADMIN_ID) == 2  # the description and the plain document
    # a document that came in before the rule (here: under a task without a program, then the
    # program is named) leaves on the next sync
    doc = await store.create_node(
        DEV_ADMIN_ID, project.id, kind="report", title="Dokument: B12 Knowledge",
        report_kind="document", body="draft of a wiki page", parent_id=task.id,
    )  # fmt: skip
    assert await index.sync_user(DEV_ADMIN_ID) == 0
    files = await files_of(client, DEV_ADMIN_ID)
    assert not any("dokument" in f for f in files)
    assert any("plain-doc" in f for f in files)
    await store.update_node(
        DEV_ADMIN_ID, other.id, revision=other.revision, body="Programm: doc_section"
    )
    assert await index.sync_user(DEV_ADMIN_ID) == 1  # the plain document is taken out
    assert not any("plain-doc" in f for f in await files_of(client, DEV_ADMIN_ID))
    assert plain.id != doc.id


async def test_a_trashed_project_leaves_the_index_and_nobody_else_sees_it(
    client: TestClient,
) -> None:
    sessionmaker = client.app.state.sessionmaker
    store = ProjectStore(sessionmaker)
    index = ProjectReportIndex(sessionmaker, client.app.state.knowledge)
    other, _ = make_user(TEST_DB_URL, "Other")

    mine = await store.create_project(DEV_ADMIN_ID, title="Mine", description="private")
    theirs = await store.create_project(other, title="Theirs", description="also private")
    assert await index.sync_all() == 2
    assert len(await files_of(client, DEV_ADMIN_ID)) == 1
    assert len(await files_of(client, other)) == 1
    # each person has one private source of their own, and the files are their own
    async with sessionmaker() as db:
        sources = list(
            await db.scalars(select(KnowledgeSource).where(KnowledgeSource.name == SOURCE))
        )
    assert {(s.user_id, s.visibility) for s in sources} == {
        (DEV_ADMIN_ID, "private"),
        (other, "private"),
    }
    assert any("mine" in f for f in await files_of(client, DEV_ADMIN_ID))
    assert not any("mine" in f for f in await files_of(client, other))
    assert theirs.id != mine.id

    await store.trash_project(DEV_ADMIN_ID, mine.id)
    assert await index.sync_all() == 1
    assert await files_of(client, DEV_ADMIN_ID) == {}
    assert len(await files_of(client, other)) == 1
