"""The reports of the projects in the knowledge base (PPH-04, 04d).

A later question ("what did we decide about the database?") should find a decision, a document or
a fact of a project without the agent searching every project. The current version of each such
report, and the description of each project, is written as a Markdown file into the person's
private upload source "Projekte" (the same way the finished hand-overs go into "Akten",
``handover.py``). The knowledge base chunks, filters secrets from and embeds them as it does for
any note.

What is indexed: reports of the kinds ``document``, ``decision`` and ``fact`` that are current (a
report that was superseded is not) and not in the trash, and the description of each project that
is not in the trash. What is not: task churn, notes and questions, superseded versions. Those stay
behind the history tools of the project agent. Nor are the documents a doc program
(``project_programs.DOC_PROGRAMS``) files below its task: they are drafts of wiki pages, and the
wiki (``wiki_store.py``) is where they are read once released.

The sync compares content hashes, so a file that did not change is not read again, and it removes
the files of reports that were superseded, trashed or purged. It runs as a step of the background
task ``knowledge`` (D75), once per round for everybody. Listing the people who have projects is the
one place that is not scoped by ``user_id``; everything after it is.
"""

import hashlib
import logging
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.knowledge import KnowledgeError, KnowledgeService
from topcoat_hub.models import KnowledgeFile, KnowledgeSource, Project, ProjectNode
from topcoat_hub.project_base import owned
from topcoat_hub.project_programs import DOC_PROGRAMS, program_of

log = logging.getLogger(__name__)

SOURCE = "Projekte"
KINDS = ("document", "decision", "fact")
MAX_CHARS = 200_000  # a file is cut here (the knowledge base takes at most 1 MiB)


_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def slug(text: str, limit: int = 40) -> str:
    """A file name part: lower case letters, digits and hyphens."""
    out = re.sub(r"[^a-z0-9]+", "-", text.lower().translate(_UMLAUTS))
    return out.strip("-")[:limit].strip("-") or "untitled"


def project_file(project: Project) -> tuple[str, str]:
    """The file name and the text of a project's description."""
    name = f"project-{str(project.id)[:8]}-{slug(project.title)}.md"
    text = f"# {project.title}\n\nProject, status {project.status}.\n\n{project.description}\n"
    return name, text[:MAX_CHARS]


def report_file(project: Project, node: ProjectNode) -> tuple[str, str]:
    """The file name and the text of a report. The header says where it comes from and how old it
    is, so that an answer can quote it with its date and source."""
    name = f"project-{str(project.id)[:8]}-{node.number}-{slug(node.title)}.md"
    meta = [f"project: {project.title}", f"reference: {project.title} {node.number}"]
    meta.append(f"kind: {node.report_kind}")
    if node.as_of is not None:
        meta.append(f"as of: {node.as_of.isoformat()}")
    if node.origin_app:
        meta.append(f"from: {node.origin_app}")
    text = f"# {node.title}\n\n" + "\n".join(f"- {line}" for line in meta) + f"\n\n{node.body}\n"
    return name, text[:MAX_CHARS]


def wanted_files(
    projects: list[Project],
    nodes: list[ProjectNode],
    doc_tasks: frozenset[uuid.UUID] | set[uuid.UUID] = frozenset(),
) -> dict[str, str]:
    """Every file one person's projects should have in the source. Pure. ``doc_tasks``: the tasks
    that run a doc program; the reports below them stay out."""
    live = {p.id: p for p in projects if p.trashed_at is None}
    files = dict(project_file(p) for p in live.values())
    for node in nodes:
        project = live.get(node.project_id)
        if (
            project is None
            or node.kind != "report"
            or node.report_kind not in KINDS
            or node.trashed_at is not None
            or not node.current
            or node.parent_id in doc_tasks
        ):
            continue
        name, text = report_file(project, node)
        files[name] = text
    return files


class ProjectReportIndex:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], knowledge: KnowledgeService
    ) -> None:
        self.sm = sessionmaker
        self.knowledge = knowledge

    async def sync_all(self) -> int:
        """Bring the source of every person with projects up to date; the number of files written
        or removed."""
        async with self.sm() as db:
            people = list(await db.scalars(select(Project.user_id).distinct()))
        changed = 0
        for user_id in people:
            try:
                changed += await self.sync_user(user_id)
            except Exception:
                log.exception("project reports: the sync of one person failed")
        return changed

    async def sync_user(self, user_id: uuid.UUID) -> int:
        async with self.sm() as db:
            projects = list(await db.scalars(select(Project).where(owned(Project, user_id))))
            nodes = list(
                await db.scalars(
                    select(ProjectNode).where(
                        owned(ProjectNode, user_id), ProjectNode.kind == "report"
                    )
                )
            )
            parent_ids = {n.parent_id for n in nodes if n.parent_id is not None}
            parents = (
                list(
                    await db.scalars(
                        select(ProjectNode).where(
                            owned(ProjectNode, user_id), ProjectNode.id.in_(parent_ids)
                        )
                    )
                )
                if parent_ids
                else []
            )
            source, have = await self._have(db, user_id)
        doc_tasks = {p.id for p in parents if program_of(p.body) in DOC_PROGRAMS}
        wanted = wanted_files(projects, nodes, doc_tasks)
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
                log.warning("project reports: %s was not indexed: %s", name, exc.message)
                continue
            source_id = source_row.id
            changed += 1
        if source_id is not None:
            for name in sorted(set(have) - set(wanted)):
                if await self.knowledge.drop_upload(user_id, source_id, name):
                    changed += 1
        return changed

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
