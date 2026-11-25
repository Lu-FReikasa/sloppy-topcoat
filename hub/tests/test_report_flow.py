"""A report is revised, released into the knowledge base, and the order of a revision is built from
the version and the open comments (D102, RES-01 phases 4 and 5)."""

import uuid
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fakenode import eventually
from tests.test_report_store import make, run
from topcoat_hub.models import DEV_ADMIN_ID
from topcoat_hub.report_knowledge import report_file, slug, wanted_files
from topcoat_hub.report_service import revision_in_prompt, revision_prompt

RID = uuid.UUID("12345678-1234-4234-8234-123456789012")


# --- the order of a revision ---------------------------------------------------------------


def comment(text: str, anchor: str = "") -> Any:
    return SimpleNamespace(text=text, anchor=anchor)


def test_the_order_names_the_report_its_version_and_the_open_comments() -> None:
    prompt = revision_prompt(
        report_id=RID,
        version=3,
        title="Systemd",
        language="de",
        body="# Systemd\n\nText.",
        comments=[comment("Quelle fehlt", "vorwort"), comment("zu kurz")],
        note="bitte straffen",
    )
    head = prompt.splitlines()[:2]
    assert head == ["depth: thorough", f"revise: {RID} 3"]
    assert "Quelle fehlt" in prompt and "[vorwort]" in prompt and "[whole report]" in prompt
    assert "<<<REPORT\n# Systemd" in prompt and "REPORT>>>" in prompt
    assert "in German" in prompt and "bitte straffen" in prompt
    assert revision_in_prompt(prompt) == (RID, 3)


def test_a_long_report_is_cut_in_the_order_and_a_revision_without_comments_still_has_a_task() -> (
    None
):
    prompt = revision_prompt(
        report_id=RID, version=1, title="T", language="en", body="x" * 50_000, comments=[]
    )
    assert "the rest of the report is cut" in prompt and "no open comment" in prompt
    assert "in English" in prompt


def test_only_the_header_of_a_prompt_names_a_revision() -> None:
    assert revision_in_prompt("depth: thorough\nwhat is x?") is None
    late = "\n".join(["line"] * 9) + f"\nrevise: {RID} 2"
    assert revision_in_prompt(late) is None
    assert revision_in_prompt("depth: thorough\nrevise: not-a-uuid 2") is None


# --- released reports in the knowledge base ----------------------------------------------------


def pair(version: int, released: int | None, title: str = "Systemd und mehr") -> Any:
    report = SimpleNamespace(id=RID, title=title, released_version=released)
    return report, SimpleNamespace(version=version, body=f"---\ntitle: {title}\n---\n\n# {title}\n")


def test_only_the_released_version_is_a_file_of_the_knowledge_base() -> None:
    files = wanted_files([pair(2, 2)])
    assert list(files) == ["report-12345678-systemd-und-mehr.md"]
    assert files["report-12345678-systemd-und-mehr.md"].startswith("---\ntitle: Systemd und mehr")
    assert wanted_files([pair(3, 2)]) == {}  # a newer draft is not what was released
    assert wanted_files([pair(1, None)]) == {}  # nothing released
    name, _ = report_file(pair(1, 1)[0], pair(1, 1)[1])
    assert name == "report-12345678-systemd-und-mehr.md"
    assert slug("Äußerer Rahmen: Größe!") == "aeusserer-rahmen-groesse"


def test_a_release_puts_the_report_into_the_private_source_and_a_withdrawal_takes_it_out(
    client: TestClient,
) -> None:
    from sqlalchemy import select

    from topcoat_hub.models import KnowledgeFile, KnowledgeSource

    rid = make(client, body="---\ntitle: Systemd\n---\n\n# Systemd\n\nsystemd ist PID 1.\n")

    async def files(db: Any) -> list[str]:
        rows = await db.execute(
            select(KnowledgeFile.path)
            .join(KnowledgeSource, KnowledgeSource.id == KnowledgeFile.source_id)
            .where(KnowledgeSource.name == "Berichte", KnowledgeSource.user_id == DEV_ADMIN_ID)
        )
        return sorted(r[0] for r in rows)

    async def have() -> list[str]:
        async with client.app.state.sessionmaker() as db:
            return await files(db)

    assert client.portal.call(have) == []
    for status in ("reviewed", "released"):
        assert (
            client.post(
                f"/api/reports/{rid}/status", headers=AUTH, json={"status": status}
            ).status_code
            == 200
        )
    got = eventually(
        lambda: client.portal.call(have) or (_ for _ in ()).throw(AssertionError("not yet"))
    )
    assert got == [f"report-{rid[:8]}-systemd.md"]
    # a draft of a newer version does not change what was released
    run(
        client,
        lambda s: s.add_version(
            uuid.UUID(rid), body="# Systemd\n\nneu\n", meta={}, kind="revision"
        ),
    )
    assert client.portal.call(have) == got
    # withdrawn: the file goes
    assert (
        client.post(
            f"/api/reports/{rid}/status", headers=AUTH, json={"status": "draft"}
        ).status_code
        == 200
    )
    assert client.portal.call(have) == []


def test_a_version_that_found_nothing_is_researched_again_not_revised() -> None:
    rid = uuid.uuid4()
    prompt = revision_prompt(
        report_id=rid, version=1, title="ls", language="de", body="# empty", comments=[],
        thin=True, question="How does ls work?",
    )  # fmt: skip
    assert revision_in_prompt(prompt) == (rid, 1)  # still filed as the next version
    assert "nothing to revise" in prompt and "<<<REPORT" not in prompt
    assert "Question: How does ls work?" in prompt


def test_the_title_of_a_task_skips_the_header_lines() -> None:
    from topcoat_hub.agent_store import title_of

    assert title_of("depth: thorough\nrevise: x 1\n\nWhat is ls?") == "What is ls?"
    assert title_of("depth: thorough") == "depth: thorough"


def test_the_envelope_of_a_tool_answer_is_not_shown_as_quote() -> None:
    from topcoat_hub.report_doc import _plain

    hits = '{"count":1,"results":[{"title":"ls","snippet":"lists files","url":"u"}]}'
    assert _plain(hits) == "ls: lists files"
    page = '{"chars":9,"status":200,"text":"Zeile eins"}'
    assert _plain(page) == "Zeile eins"
    cut = '{"chars":8000,"status":200,"text":"Zum Hauptinhalt\nweiter'
    assert " ".join(_plain(cut).split()) == "Zum Hauptinhalt weiter"
    assert _plain("plain text") == "plain text"
