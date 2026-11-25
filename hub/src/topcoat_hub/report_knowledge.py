"""Released research reports in the knowledge base (D102, RES-01 phase 5).

A report that the person released becomes part of the local, growing knowledge: its released
version is written as a Markdown file into the person's private upload source "Berichte" (the same
way the reports of the projects go into "Projekte", ``project_knowledge.py``), where the knowledge
base chunks, filters secrets from and embeds it like any note. A later question finds it with its
date, its question and its sources.

What is indexed: the version a person released last (``Report.released_version``), nothing else. A
draft, a version that was not released, a report whose release was withdrawn and a deleted report
are not in the source, and the sync removes their files. The sync compares content hashes, so a
file that did not change is not read again. It runs after every click that changes a release and,
with the projects' reports, as a step of the background task ``knowledge`` (D75).
"""

import hashlib
import logging
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.knowledge import KnowledgeError, KnowledgeService
from topcoat_hub.models import KnowledgeFile, KnowledgeSource, Report, ReportVersion
from topcoat_hub.report_store import WIKI

log = logging.getLogger(__name__)

SOURCE = "Berichte"
MAX_CHARS = 200_000  # a file is cut here (the knowledge base takes at most 1 MiB)

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def slug(text: str, limit: int = 40) -> str:
    """A file name part: lower case letters, digits and hyphens."""
    out = re.sub(r"[^a-z0-9]+", "-", text.lower().translate(_UMLAUTS))
    return out.strip("-")[:limit].strip("-") or "untitled"


def report_file(report: Report, version: ReportVersion) -> tuple[str, str]:
    """The file name and the text of a released version. The text is the document itself; its
    front matter carries the title, the question, the version and the date."""
    name = f"report-{str(report.id)[:8]}-{slug(report.title)}.md"
    return name, version.body[:MAX_CHARS]


def wanted_files(pairs: list[tuple[Report, ReportVersion]]) -> dict[str, str]:
    """Every file one person's released reports should have in the source. Pure: the pairs are
    (report, the version it has released)."""
    files: dict[str, str] = {}
    for report, version in pairs:
        if report.released_version is None or version.version != report.released_version:
            continue
        name, text = report_file(report, version)
        files[name] = text
    return files


class ReportIndex:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], knowledge: KnowledgeService
    ) -> None:
        self.sm = sessionmaker
        self.knowledge = knowledge

    async def sync_all(self) -> int:
        """Bring the source of every person with reports up to date; the number of files written
        or removed."""
        async with self.sm() as db:
            people = list(await db.scalars(select(Report.user_id).distinct()))
        changed = 0
        for user_id in people:
            try:
                changed += await self.sync_user(user_id)
            except Exception:
                log.exception("research reports: the sync of one person failed")
        return changed

    async def sync_user(self, user_id: uuid.UUID) -> int:
        async with self.sm() as db:
            rows: Any = await db.execute(
                select(Report, ReportVersion)
                .join(
                    ReportVersion,
                    (ReportVersion.report_id == Report.id)
                    & (ReportVersion.version == Report.released_version),
                )
                .where(
                    Report.user_id == user_id,
                    Report.released_version.is_not(None),
                    Report.application != WIKI,  # the wiki is read through its own tools only
                )
            )
            pairs = [(r, v) for r, v in rows]
            source, have = await self._have(db, user_id)
        wanted = wanted_files(pairs)
        source_id = source.id if source else None
        changed = 0
        for name, text in sorted(wanted.items()):
            digest = hashlib.sha256(text.encode()).hexdigest()
            if have.get(name) == digest:
                continue
            try:
                source_row, _ = await self.knowledge.put_upload(
                    user_id, source_id, SOURCE, "private", name, text
                )
            except KnowledgeError as exc:
                log.warning("research reports: %s was not indexed: %s", name, exc.message)
                continue
            source_id = source_row.id
            changed += 1
        if source_id is not None:
            for name in sorted(set(have) - set(wanted)):
                if await self.knowledge.drop_upload(user_id, source_id, name):
                    changed += 1
        return changed

    async def try_sync_user(self, user_id: uuid.UUID) -> None:
        """After a click: the sync, but a failure never fails the click."""
        try:
            await self.sync_user(user_id)
        except Exception:
            log.exception("research reports: the sync after a click failed")

    async def _have(
        self, db: AsyncSession, user_id: uuid.UUID
    ) -> tuple[KnowledgeSource | None, dict[str, str]]:
        source = await db.scalar(
            select(KnowledgeSource).where(
                KnowledgeSource.user_id == user_id,
                KnowledgeSource.name == SOURCE,
                KnowledgeSource.kind == "upload",
            )
        )
        if source is None:
            return None, {}
        rows: Any = await db.execute(
            select(KnowledgeFile.path, KnowledgeFile.content_hash).where(
                KnowledgeFile.source_id == source.id, KnowledgeFile.user_id == user_id
            )
        )
        return source, {path: digest for path, digest in rows}
