"""The reports of the Ablage and their versions (D102). Personal (D20): every query is scoped by
``user_id``.

A version is immutable, like a commit: a revision is a new version whose ``parent_version`` is the
one it revised; the text of a version never changes. The diff between two versions is computed
(``difflib``). The status (draft, reviewed, released) belongs to the person's clicks and applies to
the current version; ``released_version`` keeps the version that was released last, so that what
leaves the Ablage does not change while a newer draft is being worked on.

The pages of the wiki are reports of the application ``wiki`` (``wiki_store.py``). A store sees
either the Ablage (the default) or the wiki (``wiki=True``), never both: a wiki page changes only
through the wiki's own routes, never through the Ablage's.
"""

import difflib
import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.models import Report, ReportComment, ReportVersion

TITLE_MAX = 200
BODY_MAX = 400_000
COMMENT_MAX = 4000
STATUSES = ("draft", "reviewed", "released")
KINDS = ("agent", "revision", "person")
# the clicks of the person: from -> allowed targets
WIKI = "wiki"  # the application of the wiki's reports
TRANSITIONS = {
    "draft": ("reviewed",),
    "reviewed": ("released", "draft"),
    "released": ("draft",),
}


class ReportError(ValueError):
    """The request cannot be done (unknown report, a status that cannot follow)."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ReportStore:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID, wiki: bool = False) -> None:
        self.db = db
        self.user_id = user_id
        self.wiki = wiki

    def _mine(self) -> Any:
        """The person's reports of this store's side (the Ablage or the wiki)."""
        side = Report.application == WIKI if self.wiki else Report.application != WIKI
        return (Report.user_id == self.user_id) & side

    # --- reports --------------------------------------------------------------------------------

    async def create(
        self,
        *,
        title: str,
        question: str,
        language: str,
        depth: str,
        body: str,
        meta: dict[str, Any],
        task_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
        application: str = "research",
    ) -> Report:
        """A new report with its first version (of the application ``wiki`` in a wiki store). One
        report per task: asked again for the same
        task, it returns the report that is there (a continued task makes a new version)."""
        if task_id is not None:
            existing = await self.by_task(task_id)
            if existing is not None:
                await self.add_version(existing.id, body=body, meta=meta, kind="agent")
                return existing
        report = Report(
            user_id=self.user_id,
            application=WIKI if self.wiki else application,
            title=title.strip()[:TITLE_MAX] or "Report",
            question=question.strip(),
            language=language if language in ("de", "en") else "en",
            depth=depth,
            task_id=task_id,
            conversation_id=conversation_id,
        )
        self.db.add(report)
        await self.db.flush()
        self.db.add(self._version(report.id, 1, None, "agent", body, meta, ""))
        await self.db.flush()
        return report

    def _version(
        self,
        report_id: uuid.UUID,
        version: int,
        parent: int | None,
        kind: str,
        body: str,
        meta: dict[str, Any],
        note: str,
    ) -> ReportVersion:
        if kind not in KINDS:
            raise ReportError(f"unknown kind of change: {kind}")
        text = body[:BODY_MAX]
        return ReportVersion(
            report_id=report_id,
            version=version,
            user_id=self.user_id,
            parent_version=parent,
            kind=kind,
            body=text,
            meta=meta,
            content_hash=digest(text),
            note=note.strip(),
        )

    async def get(self, report_id: uuid.UUID) -> Report:
        report = await self.db.scalar(select(Report).where(Report.id == report_id, self._mine()))
        if report is None:
            raise ReportError("there is no such report")
        return report

    async def by_task(self, task_id: uuid.UUID) -> Report | None:
        return await self.db.scalar(select(Report).where(Report.task_id == task_id, self._mine()))

    async def list(self, limit: int = 100, status: str | None = None) -> Sequence[Report]:
        stmt = select(Report).where(self._mine())
        if status is not None:
            stmt = stmt.where(Report.status == status)
        rows = await self.db.scalars(stmt.order_by(Report.updated_at.desc()).limit(limit))
        return rows.all()

    async def delete(self, report_id: uuid.UUID) -> None:
        report = await self.get(report_id)
        await self.db.execute(delete(Report).where(Report.id == report.id))

    async def mark_read(self, report_id: uuid.UUID) -> None:
        report = await self.get(report_id)
        if report.read_at is None:
            report.read_at = utcnow()

    # --- versions -------------------------------------------------------------------------------

    async def add_version(
        self,
        report_id: uuid.UUID,
        *,
        body: str,
        meta: dict[str, Any],
        kind: str = "revision",
        note: str = "",
        parent: int | None = None,
    ) -> ReportVersion:
        """A new version after the current one (or after ``parent``, which names the version it
        revised). The report is a draft again and unread. A text equal to the current version is
        refused: a version that changes nothing is no version."""
        report = await self.get(report_id)
        text = body[:BODY_MAX]
        current = await self.version(report.id, report.current_version)
        if current.content_hash == digest(text) and kind != "agent":
            raise ReportError("the text did not change")
        number = report.current_version + 1
        base = report.current_version if parent is None else parent
        version = self._version(report.id, number, base, kind, body, meta, note)
        self.db.add(version)
        report.current_version = number
        report.status = "draft"
        report.updated_at, report.read_at = utcnow(), None
        await self.db.flush()
        return version

    async def version(self, report_id: uuid.UUID, number: int) -> ReportVersion:
        row = await self.db.scalar(
            select(ReportVersion).where(
                ReportVersion.report_id == report_id,
                ReportVersion.version == number,
                ReportVersion.user_id == self.user_id,
            )
        )
        if row is None:
            raise ReportError("there is no such version")
        return row

    async def versions(self, report_id: uuid.UUID) -> Sequence[ReportVersion]:
        await self.get(report_id)
        rows = await self.db.scalars(
            select(ReportVersion)
            .where(ReportVersion.report_id == report_id, ReportVersion.user_id == self.user_id)
            .order_by(ReportVersion.version)
        )
        return rows.all()

    async def diff(self, report_id: uuid.UUID, old: int, new: int) -> dict[str, Any]:
        """The lines that changed from version ``old`` to ``new``: a unified diff and the counts."""
        await self.get(report_id)
        a = (await self.version(report_id, old)).body.splitlines(keepends=False)
        b = (await self.version(report_id, new)).body.splitlines(keepends=False)
        lines = list(difflib.unified_diff(a, b, f"v{old}", f"v{new}", lineterm="", n=2))
        added = sum(1 for x in lines if x.startswith("+") and not x.startswith("+++"))
        removed = sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))
        return {
            "from": old,
            "to": new,
            "added": added,
            "removed": removed,
            "diff": "\n".join(lines),
        }

    # --- status ---------------------------------------------------------------------------------

    async def set_status(self, report_id: uuid.UUID, status: str) -> Report:
        """The person's click: draft to reviewed, reviewed to released (or back to draft); a
        released report can be withdrawn to a draft. Releasing remembers the version, withdrawing
        forgets it."""
        report = await self.get(report_id)
        before = report.status
        if status == "draft" and before == "draft" and report.released_version is not None:
            # a newer draft is being worked on and an older version is still released: the person
            # takes that release back
            report.released_version, report.released_at = None, None
            report.updated_at = utcnow()
            return report
        if status not in STATUSES or status not in TRANSITIONS.get(before, ()):
            raise ReportError(f"a {before} report cannot become {status}")
        report.status = status
        report.updated_at = utcnow()
        if status == "released":
            report.released_version, report.released_at = report.current_version, utcnow()
        elif before == "released":  # withdrawn: nothing of it leaves the Ablage any more
            report.released_version, report.released_at = None, None
        return report

    # --- comments -------------------------------------------------------------------------------

    async def add_comment(
        self,
        report_id: uuid.UUID,
        *,
        text: str,
        anchor: str = "",
        version: int | None = None,
        author: str = "person",
    ) -> ReportComment:
        report = await self.get(report_id)
        text = text.strip()
        if not text:
            raise ReportError("a comment needs a text")
        number = report.current_version if version is None else version
        await self.version(report.id, number)
        comment = ReportComment(
            report_id=report.id,
            user_id=self.user_id,
            version=number,
            anchor=anchor.strip()[:120],
            text=text[:COMMENT_MAX],
            author=author if author in ("person", "agent") else "person",
        )
        self.db.add(comment)
        await self.db.flush()
        return comment

    async def comments(
        self, report_id: uuid.UUID, version: int | None = None, open_only: bool = False
    ) -> Sequence[ReportComment]:
        await self.get(report_id)
        stmt = select(ReportComment).where(
            ReportComment.report_id == report_id, ReportComment.user_id == self.user_id
        )
        if version is not None:
            stmt = stmt.where(ReportComment.version == version)
        if open_only:
            stmt = stmt.where(ReportComment.resolved.is_(False))
        rows = await self.db.scalars(stmt.order_by(ReportComment.created_at))
        return rows.all()

    async def resolve_comment(self, comment_id: uuid.UUID, resolved: bool) -> ReportComment:
        comment = await self.db.scalar(
            select(ReportComment).where(
                ReportComment.id == comment_id, ReportComment.user_id == self.user_id
            )
        )
        if comment is None:
            raise ReportError("there is no such comment")
        comment.resolved = resolved
        return comment

    async def count(self) -> int:
        return int(
            await self.db.scalar(select(func.count()).select_from(Report).where(self._mine())) or 0
        )
