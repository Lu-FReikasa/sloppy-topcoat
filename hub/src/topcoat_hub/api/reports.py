"""The reports of the Ablage (D102): list, read, versions and their diff, comments, the person's
status clicks. Every route is scoped to the caller (D20): the report of another user is 404,
whatever its id. The report's text is Markdown; the interface makes the view from it."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.agent_service import AgentError
from topcoat_hub.artifact_store import AUTO_ORIGINS
from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.db import get_db
from topcoat_hub.models import AgentTask, Report, ReportComment, ReportImage, ReportVersion
from topcoat_hub.report_service import revision_prompt
from topcoat_hub.report_store import COMMENT_MAX, ReportError, ReportStore

router = APIRouter(prefix="/api/reports")


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def report_view(r: Report, auto: bool = False) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "application": r.application,
        "auto": auto,
        "title": r.title,
        "question": r.question,
        "language": r.language,
        "depth": r.depth,
        "status": r.status,
        "current_version": r.current_version,
        "released_version": r.released_version,
        "task_id": str(r.task_id) if r.task_id else None,
        "conversation_id": str(r.conversation_id) if r.conversation_id else None,
        "unread": r.read_at is None,
        "created_at": _iso(r.created_at),
        "updated_at": _iso(r.updated_at),
        "released_at": _iso(r.released_at),
    }


def version_view(v: ReportVersion, body: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "version": v.version,
        "parent_version": v.parent_version,
        "kind": v.kind,
        "note": v.note,
        "content_hash": v.content_hash,
        "meta": v.meta,
        "created_at": _iso(v.created_at),
    }
    if body:
        out["body"] = v.body
    return out


def comment_view(c: ReportComment) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "version": c.version,
        "anchor": c.anchor,
        "text": c.text,
        "author": c.author,
        "resolved": c.resolved,
        "created_at": _iso(c.created_at),
    }


def _error(exc: ReportError) -> HTTPException:
    message = str(exc)
    status = 404 if message.startswith("there is no such") else 409
    return HTTPException(
        status, detail={"code": "not_found" if status == 404 else "conflict", "message": message}
    )


class CommentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=COMMENT_MAX)
    anchor: str = Field(default="", max_length=120)
    version: int | None = Field(default=None, ge=1)


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolved: bool = True


class ReviseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=1000)


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str


@router.get("")
async def list_reports(
    task_id: uuid.UUID | None = Query(default=None),
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    store = ReportStore(db, user.uuid)
    if task_id is not None:
        one = await store.by_task(task_id)
        return {"reports": [report_view(one)] if one else []}
    reports = await store.list(limit=300)
    task_ids = [r.task_id for r in reports if r.task_id is not None]
    origins: dict[uuid.UUID, str] = {}
    if task_ids:
        rows = await db.execute(
            select(AgentTask.id, AgentTask.origin).where(
                AgentTask.id.in_(task_ids), AgentTask.user_id == user.uuid
            )
        )
        origins = {tid: origin for tid, origin in rows.all()}
    # no task: a block program filed it by itself; a task of the hub's own work: made for a program
    return {
        "reports": [
            report_view(r, r.task_id is None or origins.get(r.task_id) in AUTO_ORIGINS)
            for r in reports
        ]
    }


@router.get("/images/{image_id}")
async def get_image(
    image_id: uuid.UUID,
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """A picture of a report (``research.report.image``), only for the person whose run kept it
    (D20): another person's picture is a 404, whatever its id."""
    image = await db.scalar(
        select(ReportImage).where(ReportImage.id == image_id, ReportImage.user_id == user.uuid)
    )
    if image is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "unknown picture"})
    return Response(
        content=image.data,
        media_type=image.content_type,
        headers={
            "Cache-Control": "private, max-age=86400",
            # a picture is data: never sniffed into something that runs
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.get("/{report_id}")
async def get_report(
    report_id: uuid.UUID,
    version: int | None = Query(default=None, ge=1),
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    store = ReportStore(db, user.uuid)
    try:
        report = await store.get(report_id)
        shown = await store.version(report.id, version or report.current_version)
        comments = await store.comments(report.id, shown.version)
    except ReportError as exc:
        raise _error(exc) from exc
    return {
        **report_view(report),
        "shown": version_view(shown, body=True),
        "comments": [comment_view(c) for c in comments],
    }


@router.get("/{report_id}/versions")
async def list_versions(
    report_id: uuid.UUID,
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        rows = await ReportStore(db, user.uuid).versions(report_id)
    except ReportError as exc:
        raise _error(exc) from exc
    return {"versions": [version_view(v) for v in rows]}


@router.get("/{report_id}/diff")
async def get_diff(
    report_id: uuid.UUID,
    old: int = Query(alias="from", ge=1),
    new: int = Query(alias="to", ge=1),
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return await ReportStore(db, user.uuid).diff(report_id, old, new)
    except ReportError as exc:
        raise _error(exc) from exc


@router.post("/{report_id}/comments", status_code=201)
async def add_comment(
    report_id: uuid.UUID,
    body: CommentBody,
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        comment = await ReportStore(db, user.uuid).add_comment(
            report_id, text=body.text, anchor=body.anchor, version=body.version
        )
    except ReportError as exc:
        raise _error(exc) from exc
    await db.commit()
    return comment_view(comment)


@router.get("/{report_id}/comments")
async def list_comments(
    report_id: uuid.UUID,
    version: int | None = Query(default=None, ge=1),
    open_only: bool = Query(default=False),
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        rows = await ReportStore(db, user.uuid).comments(report_id, version, open_only)
    except ReportError as exc:
        raise _error(exc) from exc
    return {"comments": [comment_view(c) for c in rows]}


@router.patch("/{report_id}/comments/{comment_id}")
async def resolve_comment(
    report_id: uuid.UUID,
    comment_id: uuid.UUID,
    body: ResolveBody,
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    store = ReportStore(db, user.uuid)
    try:
        await store.get(report_id)
        comment = await store.resolve_comment(comment_id, body.resolved)
        if comment.report_id != report_id:
            raise ReportError("there is no such comment")
    except ReportError as exc:
        raise _error(exc) from exc
    await db.commit()
    return comment_view(comment)


async def _sync(request: Request, user: CurrentUser) -> None:
    """A release changed: the released reports in the knowledge base are brought up to date."""
    index = getattr(request.app.state, "report_index", None)
    if index is not None:
        await index.try_sync_user(user.uuid)


@router.post("/{report_id}/status")
async def set_status(
    report_id: uuid.UUID,
    body: StatusBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The person's click: draft to reviewed, reviewed to released or back, released to draft.
    A release (and its withdrawal) changes what the knowledge base holds of the reports."""
    try:
        report = await ReportStore(db, user.uuid).set_status(report_id, body.status)
    except ReportError as exc:
        raise _error(exc) from exc
    await db.commit()
    view = report_view(report)
    await _sync(request, user)
    return view


@router.post("/{report_id}/revise", status_code=202)
async def revise(
    report_id: uuid.UUID,
    body: ReviseBody,
    request: Request,
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Have the report revised: a new research task gets the current version and the open
    comments as its order. When it ends, its result is the next version of this report (D102)."""
    store = ReportStore(db, user.uuid)
    try:
        report = await store.get(report_id)
        current = await store.version(report.id, report.current_version)
        comments = await store.comments(report.id, report.current_version, open_only=True)
    except ReportError as exc:
        raise _error(exc) from exc
    agents = getattr(request.app.state, "agents", None)
    if agents is None:
        raise HTTPException(503, detail={"code": "agent_disabled", "message": "no agent"})
    prompt = revision_prompt(
        report_id=report.id,
        version=report.current_version,
        title=report.title,
        language=report.language,
        body=current.body,
        comments=comments,
        note=body.note,
        question=report.question,
        # a version that ended early is researched again, not revised: its text is the write-up
        # of a run that did not finish, and revising it would build on that
        thin=bool(current.meta.get("stop_reason")),
    )
    try:
        task = await agents.start(user, prompt, None, origin="report", application="research")
    except AgentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from exc
    return {
        "task_id": str(task.id),
        "from_version": report.current_version,
        "open_comments": len(comments),
    }


@router.post("/{report_id}/read", status_code=204)
async def mark_read(
    report_id: uuid.UUID,
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await ReportStore(db, user.uuid).mark_read(report_id)
    except ReportError as exc:
        raise _error(exc) from exc
    await db.commit()


@router.delete("/{report_id}", status_code=204)
async def delete_report(
    report_id: uuid.UUID,
    request: Request,
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await ReportStore(db, user.uuid).delete(report_id)
    except ReportError as exc:
        raise _error(exc) from exc
    await db.commit()
    await _sync(request, user)
