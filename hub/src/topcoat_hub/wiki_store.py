"""Topcoat's own wiki (2026-11-12): pages on top of the Ablage (D102). Personal (D20): every query
is scoped by ``user_id``.

A page is a report of the application ``wiki`` (``report_store.py``: immutable versions, diff,
draft -> released, ``released_version``) plus a row in ``wiki_pages`` that gives it a place in the
tree: a section, a title and the path ``section/title``.

Its place is the person's: a page lies in a folder (``section``, names with ``/`` between them)
and carries categories; the person moves, renames and labels pages, and renames a folder with
everything in it. A later take-over of the same task keeps the page where the person put it.

A page's text changes in one way only: a person clicks "take into the wiki" on a project's document
(``report_kind == "document"``), after a card showed what the click does. That makes a new page or
a new version of the page with the task's title, and that version is the page from then on: what
the wiki shows and what the assistants of Projekte and Verwaltung read (``current()``). The click
is the person's confirmation; there is no second release step (the maintainer, 2026-11-12) and no
hand editor. In the report the version is kept as released, so its history stays readable. The
text is Markdown with rendered blocks (```` ```mermaid ````).
"""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.models import ProjectNode, Report, ReportVersion, WikiPage
from topcoat_hub.project_base import owned
from topcoat_hub.report_store import ReportStore, digest

# the sections of the documentation outline (2026-11-11-doku-gliederung), by the letter that opens
# a task's title ("B12 Knowledge base" -> "B Hub")
SECTIONS = {
    "A": "A Überblick",
    "B": "B Hub",
    "C": "C Node",
    "D": "D Oberfläche und Desktop",
    "E": "E Betrieb",
}
OTHER = "Weiteres"
DOC_PREFIX = "Dokument: "
_NUMBERED = re.compile(r"^\s*([A-Z])\d+[a-z]?\b")  # B5a, D3b: one part of a split page
FOLDER_DEPTH = 6
FOLDER_PART = 60
CATEGORY_LEN = 40
CATEGORIES = 12
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MADE = re.compile(r"Hub-Builds `([^`]+)` am (\d{4}-\d{2}-\d{2})")


class WikiError(ValueError):
    """The request cannot be done. ``status`` is the HTTP status the API answers with."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def utcnow() -> datetime:
    return datetime.now(UTC)


def section_of(title: str) -> str:
    """The section of a page, from the number that opens its task's title."""
    found = _NUMBERED.match(title or "")
    return SECTIONS.get(found.group(1), OTHER) if found else OTHER


def clean_folder(raw: str) -> str:
    """A folder as the person typed it ("B Hub / Gateway"): its names trimmed, empty ones left
    out; refused when a name is too long, holds a control character or a backslash, or the folder
    is deeper than FOLDER_DEPTH."""
    parts = [p.strip() for p in (raw or "").split("/") if p.strip()]
    if not parts:
        raise WikiError(422, "bad_folder", "a page needs a folder")
    if len(parts) > FOLDER_DEPTH:
        raise WikiError(422, "bad_folder", f"a folder is at most {FOLDER_DEPTH} deep")
    for p in parts:
        if len(p) > FOLDER_PART or _CONTROL.search(p) or "\\" in p:
            raise WikiError(422, "bad_folder", f"not a folder name: {p[:80]!r}")
    return "/".join(parts)


def clean_title(raw: str) -> str:
    title = (raw or "").strip()
    if not title or len(title) > 200 or "/" in title or _CONTROL.search(title):
        raise WikiError(422, "bad_title", "a title has 1 to 200 characters and no slash")
    return title


def clean_categories(raw: list[str]) -> list[str]:
    """The categories, trimmed, each once (whatever its case), in the order given."""
    out: list[str] = []
    seen: set[str] = set()
    for c in raw:
        name = (c or "").strip()
        if not name:
            continue
        if len(name) > CATEGORY_LEN or _CONTROL.search(name):
            raise WikiError(422, "bad_category", f"not a category: {name[:80]!r}")
        if name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    if len(out) > CATEGORIES:
        raise WikiError(422, "bad_category", f"at most {CATEGORIES} categories")
    return out


def page_title(task_title: str) -> str:
    title = (task_title or "").strip()
    if title.startswith(DOC_PREFIX):
        title = title[len(DOC_PREFIX) :].strip()
    return title[:200] or "Untitled"


def made(body: str) -> dict[str, str]:
    """The build and the date a doc program wrote into the page's note, if it did."""
    found = _MADE.search(body or "")
    return {"build": found.group(1), "date": found.group(2)} if found else {}


@dataclass
class Page:
    row: WikiPage
    report: Report


@dataclass
class Plan:
    """What a take-over would do: the target, new page or new version, the length."""

    node: ProjectNode
    section: str
    title: str
    path: str
    page: Page | None
    version: int  # the version the take-over makes
    unchanged: bool  # the text equals the page's current version: nothing to take over
    chars: int

    def view(self) -> dict[str, Any]:
        return {
            "node_id": str(self.node.id),
            "section": self.section,
            "title": self.title,
            "path": self.path,
            "page_id": str(self.page.row.id) if self.page else None,
            "new_page": self.page is None,
            "version": self.version,
            "unchanged": self.unchanged,
            "chars": self.chars,
        }


class WikiStore:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        self.db = db
        self.user_id = user_id
        self.reports = ReportStore(db, user_id, wiki=True)

    # --- reading --------------------------------------------------------------------------------

    async def pages(self) -> list[Page]:
        rows = await self.db.execute(
            select(WikiPage, Report)
            .join(Report, Report.id == WikiPage.report_id)
            .where(WikiPage.user_id == self.user_id, Report.user_id == self.user_id)
            .order_by(WikiPage.section, WikiPage.title)
        )
        return [Page(p, r) for p, r in rows.all()]

    async def page(self, page_id: uuid.UUID) -> Page:
        row = await self.db.scalar(
            select(WikiPage).where(WikiPage.id == page_id, WikiPage.user_id == self.user_id)
        )
        if row is None:
            raise WikiError(404, "not_found", "there is no such page")
        return Page(row, await self.reports.get(row.report_id))

    async def by_path(self, path: str) -> Page | None:
        row = await self.db.scalar(
            select(WikiPage).where(WikiPage.path == path.strip(), WikiPage.user_id == self.user_id)
        )
        return Page(row, await self.reports.get(row.report_id)) if row else None

    async def by_source(self, source: uuid.UUID) -> Page | None:
        """The page a task's documents were taken into (wherever the person moved it since)."""
        row = await self.db.scalar(
            select(WikiPage).where(
                WikiPage.source_node_id == source, WikiPage.user_id == self.user_id
            )
        )
        return Page(row, await self.reports.get(row.report_id)) if row else None

    # --- the person's order: folders, titles, categories ----------------------------------------

    async def update(
        self,
        page_id: uuid.UUID,
        folder: str | None = None,
        title: str | None = None,
        categories: list[str] | None = None,
    ) -> Page:
        """Move a page to a folder, rename it, label it; a path another page has is refused."""
        page = await self.page(page_id)
        row = page.row
        section = clean_folder(folder) if folder is not None else row.section
        name = clean_title(title) if title is not None else row.title
        path = f"{section}/{name}"
        if path != row.path:
            taken = await self.by_path(path)
            if taken is not None and taken.row.id != row.id:
                raise WikiError(409, "taken", f"there is a page {path} already")
            row.section, row.title, row.path = section, name, path
        if categories is not None:
            row.categories = clean_categories(categories)
        row.updated_at = utcnow()
        await self.db.flush()
        return page

    async def rename_folder(self, old: str, new: str) -> int:
        """A folder and everything below it gets another name or place; how many pages moved."""
        old, new = clean_folder(old), clean_folder(new)
        if old == new:
            return 0
        if new.startswith(old + "/"):
            raise WikiError(422, "bad_folder", "a folder cannot go into itself")
        moving = [
            p
            for p in await self.pages()
            if p.row.section == old or p.row.section.startswith(old + "/")
        ]
        if not moving:
            raise WikiError(404, "not_found", "there is no such folder")
        staying = {p.row.path for p in await self.pages()} - {p.row.path for p in moving}
        planned = []
        for p in moving:
            section = new + p.row.section[len(old) :]
            path = f"{section}/{p.row.title}"
            if path in staying:
                raise WikiError(409, "taken", f"there is a page {path} already")
            planned.append((p.row, section, path))
        for row, section, path in planned:
            row.section, row.path, row.updated_at = section, path, utcnow()
        await self.db.flush()
        return len(planned)

    async def current(self, page: Page) -> ReportVersion:
        """The page as it stands: its latest version."""
        return await self.reports.version(page.report.id, page.report.current_version)

    # --- taking a project's document over -------------------------------------------------------

    async def plan(self, node_id: uuid.UUID) -> Plan:
        """What a take-over of this document would do (the preview card shows it)."""
        node = await self.db.scalar(
            select(ProjectNode).where(ProjectNode.id == node_id, owned(ProjectNode, self.user_id))
        )
        if node is None:
            raise WikiError(404, "not_found", "there is no such node")
        if node.kind != "report" or node.report_kind != "document":
            raise WikiError(422, "not_a_document", "only a document of a project becomes a page")
        if node.trashed_at is not None:
            raise WikiError(409, "trashed", "the document is in the trash")
        parent = None
        if node.parent_id is not None:
            parent = await self.db.scalar(
                select(ProjectNode).where(
                    ProjectNode.id == node.parent_id, owned(ProjectNode, self.user_id)
                )
            )
        title = page_title(parent.title if parent is not None else node.title)
        section = section_of(title)
        path = f"{section}/{title}"
        # the task's page stays where the person put it; else the page at the outline's place
        page = await self.by_source(node.parent_id or node.id) or await self.by_path(path)
        if page is not None:
            section, title, path = page.row.section, page.row.title, page.row.path
        unchanged = False
        version = 1
        if page is not None:
            current = await self.reports.version(page.report.id, page.report.current_version)
            unchanged = current.content_hash == digest(node.body)
            version = page.report.current_version + 1
        return Plan(node, section, title, path, page, version, unchanged, len(node.body))

    async def publish(self, node_id: uuid.UUID) -> tuple[Page, int]:
        """The person's click: the document becomes a new page or the next version of it."""
        plan = await self.plan(node_id)
        if plan.unchanged:
            raise WikiError(409, "unchanged", "the page already has this text")
        meta: dict[str, Any] = {"node_id": str(plan.node.id), **made(plan.node.body)}
        if plan.node.parent_id is not None:
            meta["task_id"] = str(plan.node.parent_id)
        source = plan.node.parent_id or plan.node.id
        if plan.page is None:
            report = await self.reports.create(
                title=plan.title,
                question="",
                language="de",
                depth="page",
                body=plan.node.body,
                meta=meta,
            )
            row = WikiPage(
                user_id=self.user_id,
                section=plan.section,
                title=plan.title,
                path=plan.path,
                report_id=report.id,
                source_node_id=source,
            )
            self.db.add(row)
            await self.db.flush()
            await self._settle(report)
            return Page(row, report), 1
        version = await self.reports.add_version(
            plan.page.report.id, body=plan.node.body, meta=meta, kind="agent"
        )
        await self._settle(plan.page.report)
        plan.page.row.source_node_id = source
        plan.page.row.updated_at = utcnow()
        return plan.page, version.version

    async def _settle(self, report: Report) -> None:
        """The new version is the page: kept as released in the report (its history)."""
        if report.status == "draft":
            await self.reports.set_status(report.id, "reviewed")
        if report.status == "reviewed":
            await self.reports.set_status(report.id, "released")


# --- a document of the Ablage into the wiki (2026-11-13) ------------------------------------

# the folder a document of the Ablage goes into: the application that made it, in the person's
# language (a folder is the person's to rename later; a renamed one is not found again by name)
APP_FOLDERS = {
    "de": {
        "research": "Recherche",
        "dev": "Entwicklung",
        "chat": "Chat",
        "admin": "Verwaltung",
        "project": "Projekte",
    },
    "en": {
        "research": "Research",
        "dev": "Development",
        "chat": "Chat",
        "admin": "Administration",
        "project": "Projects",
    },
}
ABLAGE_KINDS = ("report", "artifact")


def app_folder(application: str | None, language: str | None) -> str:
    names = APP_FOLDERS.get(language or "en", APP_FOLDERS["en"])
    return names.get(application or "", OTHER if language == "de" else "Other")


def ablage_title(raw: str) -> str:
    """A title of the Ablage as a page title: no slash, no control character, at most 200."""
    title = _CONTROL.sub(" ", (raw or "").replace("/", "-")).strip()
    return " ".join(title.split())[:200] or "Untitled"


@dataclass
class AblagePlan:
    """What taking a document of the Ablage into the wiki would do."""

    kind: str
    doc_id: uuid.UUID
    section: str
    title: str
    path: str
    page: Page | None
    version: int
    unchanged: bool
    body: str

    def view(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": str(self.doc_id),
            "section": self.section,
            "title": self.title,
            "path": self.path,
            "page_id": str(self.page.row.id) if self.page else None,
            "new_page": self.page is None,
            "version": self.version,
            "unchanged": self.unchanged,
            "chars": len(self.body),
        }


async def _ablage_document(
    store: "WikiStore", kind: str, doc_id: uuid.UUID
) -> tuple[str, str, str | None]:
    """The title, text and application of the person's own document of the Ablage."""
    from topcoat_hub.models import AgentTask, Artifact

    if kind == "report":
        reports = ReportStore(store.db, store.user_id)
        report = await reports.get(doc_id)  # ReportError when it is not the person's
        version = await reports.version(report.id, report.current_version)
        return report.title, version.body or "", report.application
    if kind == "artifact":
        art = await store.db.scalar(
            select(Artifact).where(Artifact.id == doc_id, Artifact.user_id == store.user_id)
        )
        if art is None:
            raise WikiError(404, "not_found", "there is no such document")
        app = None
        if art.task_id is not None:
            app = await store.db.scalar(
                select(AgentTask.application).where(
                    AgentTask.id == art.task_id, AgentTask.user_id == store.user_id
                )
            )
        return art.title, art.body or "", app
    raise WikiError(422, "bad_kind", "a document of the Ablage is a report or an artifact")


async def plan_ablage(
    store: "WikiStore", kind: str, doc_id: uuid.UUID, language: str | None
) -> AblagePlan:
    """The page a document of the Ablage becomes: ``<application>/<title>``. Taken over again, the
    same document makes the next version of its page; another document with the same title gets
    a page of its own."""
    title, body, application = await _ablage_document(store, kind, doc_id)
    if not body.strip():
        raise WikiError(422, "empty", "the document has no text")
    source = f"{kind}:{doc_id}"
    section = app_folder(application, language)
    title = ablage_title(title)
    page = await store.by_path(f"{section}/{title}")
    if page is not None and (await store.current(page)).meta.get("source") != source:
        title = ablage_title(f"{title[:188]} ({str(doc_id)[:8]})")
        page = await store.by_path(f"{section}/{title}")
    version, unchanged = 1, False
    if page is not None:
        unchanged = (await store.current(page)).content_hash == digest(body)
        version = page.report.current_version + 1
    return AblagePlan(
        kind, doc_id, section, title, f"{section}/{title}", page, version, unchanged, body
    )


async def publish_ablage(
    store: "WikiStore", kind: str, doc_id: uuid.UUID, language: str | None
) -> tuple[Page, int]:
    """The person's click: the document becomes a page in its application's folder, which every
    assistant that reads the wiki finds (wiki_tools)."""
    plan = await plan_ablage(store, kind, doc_id, language)
    if plan.unchanged:
        raise WikiError(409, "unchanged", "the page already has this text")
    meta: dict[str, Any] = {"source": f"{kind}:{doc_id}", **made(plan.body)}
    if plan.page is None:
        report = await store.reports.create(
            title=plan.title,
            question="",
            language=language or "en",
            depth="page",
            body=plan.body,
            meta=meta,
        )
        row = WikiPage(
            user_id=store.user_id,
            section=plan.section,
            title=plan.title,
            path=plan.path,
            report_id=report.id,
        )
        store.db.add(row)
        await store.db.flush()
        await store._settle(report)
        return Page(row, report), 1
    version = await store.reports.add_version(
        plan.page.report.id, body=plan.body, meta=meta, kind="agent"
    )
    await store._settle(plan.page.report)
    plan.page.row.updated_at = utcnow()
    return plan.page, version.version
