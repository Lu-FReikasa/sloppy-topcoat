"""Topcoat's own wiki (``wiki_store.py``): the tree, a page with its versions and their diff, and
the take-over of a project's document (preview, then the click that does it; the new version is
the page from then on). Every route is scoped to the caller (D20): another person's page or node
is a 404, whatever its id. There is no route that writes a page's text by hand."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.api.reports import version_view
from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.db import get_db
from topcoat_hub.report_store import ReportError
from topcoat_hub.wiki_store import (
    ABLAGE_KINDS,
    Page,
    WikiError,
    WikiStore,
    plan_ablage,
    publish_ablage,
)

router = APIRouter(prefix="/api/wiki")
project_router = APIRouter(prefix="/api/project")


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def page_view(p: Page) -> dict[str, Any]:
    r = p.report
    return {
        "id": str(p.row.id),
        "section": p.row.section,
        "folder": p.row.section,
        "categories": list(p.row.categories or []),
        "title": p.row.title,
        "path": p.row.path,
        "current_version": r.current_version,
        "source_node_id": str(p.row.source_node_id) if p.row.source_node_id else None,
        "created_at": _iso(p.row.created_at),
        "updated_at": _iso(p.row.updated_at),
    }


def _refused(exc: WikiError | ReportError) -> HTTPException:
    if isinstance(exc, WikiError):
        return HTTPException(exc.status, detail={"code": exc.code, "message": exc.message})
    message = str(exc)
    status = 404 if message.startswith("there is no such") else 409
    return HTTPException(
        status, detail={"code": "not_found" if status == 404 else "conflict", "message": message}
    )


@router.get("/pages")
async def list_pages(
    user: CurrentUser = Depends(api_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """The tree: the folders in order, each with its pages, and every category in use."""
    sections: dict[str, list[dict[str, Any]]] = {}
    categories: dict[str, str] = {}
    for page in await WikiStore(db, user.uuid).pages():
        sections.setdefault(page.row.section, []).append(page_view(page))
        for c in page.row.categories or []:
            categories.setdefault(c.casefold(), c)
    return {
        "sections": [{"name": name, "pages": pages} for name, pages in sections.items()],
        "categories": sorted(categories.values(), key=str.casefold),
    }


class PageUpdate(BaseModel):
    """The person's order: only the fields that are present change."""

    model_config = ConfigDict(extra="forbid")

    folder: str | None = Field(None, max_length=400)
    title: str | None = Field(None, max_length=200)
    categories: list[str] | None = Field(None, max_length=40)


@router.patch("/pages/{page_id}")
async def update_page(
    page_id: uuid.UUID,
    body: PageUpdate,
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Move a page to a folder, rename it or give it categories (the person's click)."""
    try:
        page = await WikiStore(db, user.uuid).update(
            page_id, folder=body.folder, title=body.title, categories=body.categories
        )
    except (WikiError, ReportError) as exc:
        raise _refused(exc) from exc
    view = page_view(page)
    await db.commit()
    return view


class FolderRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old: str = Field(min_length=1, max_length=400)
    new: str = Field(min_length=1, max_length=400)


@router.post("/folders/rename")
async def rename_folder(
    body: FolderRename, user: CurrentUser = Depends(api_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """A folder with everything in it gets another name or goes into another folder."""
    try:
        moved = await WikiStore(db, user.uuid).rename_folder(body.old, body.new)
    except WikiError as exc:
        raise _refused(exc) from exc
    await db.commit()
    return {"moved": moved}


@router.get("/pages/{page_id}")
async def get_page(
    page_id: uuid.UUID,
    version: int | None = Query(default=None, ge=1),
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """A page: the version asked for (else the current one) and the list of versions."""
    store = WikiStore(db, user.uuid)
    try:
        page = await store.page(page_id)
        shown = await store.reports.version(page.report.id, version or page.report.current_version)
        versions = await store.reports.versions(page.report.id)
    except (WikiError, ReportError) as exc:
        raise _refused(exc) from exc
    return {
        **page_view(page),
        "shown": version_view(shown, body=True),
        "versions": [version_view(v) for v in versions],
    }


@router.get("/pages/{page_id}/diff")
async def get_diff(
    page_id: uuid.UUID,
    old: int = Query(ge=1),
    new: int = Query(ge=1),
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    store = WikiStore(db, user.uuid)
    try:
        page = await store.page(page_id)
        return await store.reports.diff(page.report.id, old, new)
    except (WikiError, ReportError) as exc:
        raise _refused(exc) from exc


@project_router.get("/nodes/{node_id}/wiki")
async def preview_take_over(
    node_id: uuid.UUID, user: CurrentUser = Depends(api_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """What "take into the wiki" would do with this document: the target path, a new page or the
    next version of one, the length. Changes nothing."""
    try:
        plan = await WikiStore(db, user.uuid).plan(node_id)
    except (WikiError, ReportError) as exc:
        raise _refused(exc) from exc
    return plan.view()


@project_router.post("/nodes/{node_id}/wiki", status_code=201)
async def take_over(
    node_id: uuid.UUID, user: CurrentUser = Depends(api_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """The person's click on the preview card: the document becomes a new page or the next
    version of it, which the wiki shows and the assistants of Projekte and Verwaltung read."""
    try:
        page, version = await WikiStore(db, user.uuid).publish(node_id)
    except (WikiError, ReportError) as exc:
        raise _refused(exc) from exc
    view = {**page_view(page), "version": version}
    await db.commit()
    return view


# --- a document of the Ablage (a research report, the report of a task) -------------------------


def _kind(kind: str) -> str:
    if kind not in ABLAGE_KINDS:
        raise HTTPException(404, detail={"code": "not_found", "message": "unknown kind"})
    return kind


def _language(lang: str | None) -> str:
    return lang if lang in ("de", "en") else "en"


@router.get("/ablage/{kind}/{doc_id}")
async def preview_ablage(
    kind: str,
    doc_id: uuid.UUID,
    lang: str | None = Query(default=None, max_length=8),
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """What "take into the wiki" would do with a document of the Ablage: the folder of the
    application that made it, a new page or the next version. Changes nothing."""
    try:
        plan = await plan_ablage(WikiStore(db, user.uuid), _kind(kind), doc_id, _language(lang))
    except (WikiError, ReportError) as exc:
        raise _refused(exc) from exc
    return plan.view()


@router.post("/ablage/{kind}/{doc_id}", status_code=201)
async def take_over_ablage(
    kind: str,
    doc_id: uuid.UUID,
    lang: str | None = Query(default=None, max_length=8),
    user: CurrentUser = Depends(api_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The person's click after the preview: the document becomes a wiki page (or its next
    version), which every assistant that reads the wiki finds."""
    try:
        page, version = await publish_ablage(
            WikiStore(db, user.uuid), _kind(kind), doc_id, _language(lang)
        )
    except (WikiError, ReportError) as exc:
        raise _refused(exc) from exc
    view = {**page_view(page), "version": version}
    await db.commit()
    return view
