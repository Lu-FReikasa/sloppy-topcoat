"""Versions of a node's title and body, as diffs with keyframes (PPH-04, 04a).

A snapshot is written for the first version, for every 20th version after the last snapshot,
and when the caller asks for one (a report was finalised or replaced); in between the change is a
line diff (an edit script from :mod:`difflib`, stored as JSON, no new dependency). ``state_at``
takes the nearest snapshot at or before a revision and applies the diffs after it. The revision of
a version is the node's ``revision`` after the change; revisions that changed neither title nor
body have no version, so the numbers have gaps.
"""

import difflib
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.models import ProjectNode, ProjectNodeVersion

APPLICATION = "project"
SNAPSHOT_EVERY = 20

Ops = list[list[Any]]  # [[i1, i2, [new lines]], ...] over the lines of the old text


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def make_diff(old: str, new: str) -> Ops:
    """The edit script that turns ``old`` into ``new``: for every changed stretch of lines, the
    range of old lines it replaces and the new lines that take their place."""
    a, b = _lines(old), _lines(new)
    ops: Ops = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            ops.append([i1, i2, b[j1:j2]])
    return ops


def apply_diff(old: str, ops: Ops) -> str:
    a = _lines(old)
    out: list[str] = []
    pos = 0
    for i1, i2, new_lines in ops:
        out.extend(a[pos:i1])
        out.extend(new_lines)
        pos = i2
    out.extend(a[pos:])
    return "".join(out)


def display_diff(old: str, new: str) -> list[dict[str, Any]]:
    """The difference of two texts for the page: runs of ``equal`` / ``replace`` / ``insert`` /
    ``delete`` with the old and the new lines of each."""
    a, b = _lines(old), _lines(new)
    return [
        {"tag": tag, "old": a[i1:i2], "new": b[j1:j2]}
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    ]


class VersionLog:
    """The only writer of ``project_node_versions``; called by the store in the transaction of the
    change, after the node's revision was set."""

    @staticmethod
    async def record(
        db: AsyncSession,
        node: ProjectNode,
        *,
        event_id: uuid.UUID,
        old_title: str | None = None,
        old_body: str | None = None,
        snapshot: bool = False,
    ) -> ProjectNodeVersion | None:
        """Write the version for the node's current revision. ``old_*`` is what the node held
        before (None for a new node). Nothing is written when title and body did not change."""
        previous = await VersionLog._latest(db, node.id)
        title_changed = old_title is not None and old_title != node.title
        body_changed = old_body is not None and old_body != node.body
        if previous is not None and not (title_changed or body_changed or snapshot):
            return None
        since = await VersionLog._since_snapshot(db, node.id)
        if previous is None or snapshot or since + 1 >= SNAPSHOT_EVERY:
            kind, data = "snapshot", {"title": node.title, "body": node.body}
        else:
            kind = "diff"
            data = {
                "title": [old_title, node.title] if title_changed else None,
                "body": make_diff(old_body or "", node.body) if body_changed else None,
            }
        version = ProjectNodeVersion(
            node_id=node.id,
            rev=node.revision,
            user_id=node.user_id,
            application=APPLICATION,
            kind=kind,
            data=data,
            event_id=event_id,
        )
        db.add(version)
        await db.flush()
        return version

    @staticmethod
    async def _latest(db: AsyncSession, node_id: uuid.UUID) -> ProjectNodeVersion | None:
        return await db.scalar(
            select(ProjectNodeVersion)
            .where(ProjectNodeVersion.node_id == node_id)
            .order_by(ProjectNodeVersion.rev.desc())
            .limit(1)
        )

    @staticmethod
    async def _since_snapshot(db: AsyncSession, node_id: uuid.UUID) -> int:
        """How many diffs follow the last snapshot."""
        rows = await db.scalars(
            select(ProjectNodeVersion.kind)
            .where(ProjectNodeVersion.node_id == node_id)
            .order_by(ProjectNodeVersion.rev.desc())
        )
        count = 0
        for kind in rows:
            if kind == "snapshot":
                break
            count += 1
        return count

    @staticmethod
    async def versions(
        db: AsyncSession, user_id: uuid.UUID, node_id: uuid.UUID
    ) -> list[ProjectNodeVersion]:
        rows = await db.scalars(
            select(ProjectNodeVersion)
            .where(
                ProjectNodeVersion.node_id == node_id,
                ProjectNodeVersion.user_id == user_id,
                ProjectNodeVersion.application == APPLICATION,
            )
            .order_by(ProjectNodeVersion.rev)
        )
        return list(rows)

    @staticmethod
    def state_at(versions: list[ProjectNodeVersion], rev: int) -> dict[str, Any] | None:
        """Title and body as they were at ``rev`` (the latest version at or before it), or None
        when the node had no version yet."""
        chosen = [v for v in versions if v.rev <= rev]
        if not chosen:
            return None
        start = max((i for i, v in enumerate(chosen) if v.kind == "snapshot"), default=None)
        if start is None:
            return None
        title, body = chosen[start].data["title"], chosen[start].data["body"]
        for v in chosen[start + 1 :]:
            if v.data.get("title"):
                title = v.data["title"][1]
            if v.data.get("body") is not None:
                body = apply_diff(body, v.data["body"])
        return {"rev": chosen[-1].rev, "title": title, "body": body}


def version_view(v: ProjectNodeVersion, at: Any | None) -> dict[str, Any]:
    """A version as the history shows it (the data of a diff stays out: ``diff`` shows changes)."""
    return {
        "rev": v.rev,
        "kind": v.kind,
        "event_id": None if v.event_id is None else str(v.event_id),
        "at": None if at is None else at.isoformat(),
    }
