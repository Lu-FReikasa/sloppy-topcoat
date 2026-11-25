"""Export and templates of the Projekte application (PPH-04, 04j).

Same authorization as the other project routes: a signed-in session, or an API key under the scoping
it has there. Every query is scoped to the caller; another person's project or template is a 404.
"""

import json
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from topcoat_hub.api.project import _refused
from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.project_events import PERSON
from topcoat_hub.project_export import ProjectExporter, slug, to_zip
from topcoat_hub.project_store import ProjectError, ProjectStore
from topcoat_hub.project_templates import TemplateStore, view
from topcoat_hub.project_views import project_view

router = APIRouter(prefix="/api/project")


class TemplateSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=400)


class FromTemplate(BaseModel):
    """``start_at`` (ISO 8601 with an offset) is the date the template's earliest date falls on;
    without it the new project has no dates."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    start_at: datetime | None = None
    identifier: str | None = Field(default=None, max_length=10)


def _templates(request: Request) -> TemplateStore:
    return TemplateStore(
        request.app.state.sessionmaker, PERSON, request.app.state.settings.hub_timezone
    )


@router.get("/projects/{project_id}/export")
async def export_project(
    project_id: uuid.UUID,
    request: Request,
    format: Literal["json", "markdown"] = "json",
    trashed: bool = Query(False, description="include the nodes in the trash"),
    user: CurrentUser = Depends(api_user),
) -> Response:
    """The project as one JSON document (project, nodes, relations, labels, reminders, repeat rules
    and the Akte with the result of its chain check), or as a zip of Markdown files."""
    exporter = ProjectExporter(request.app.state.sessionmaker)
    try:
        doc = await exporter.document(user.uuid, project_id, include_trashed=trashed)
    except ProjectError as exc:
        raise _refused(exc) from exc
    if format == "json":
        return Response(
            content=_dump(doc),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{_name(doc)}.json"'},
        )
    return Response(
        content=to_zip(doc),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{_name(doc)}.zip"'},
    )


def _name(doc: dict[str, Any]) -> str:
    return f"project-{slug(doc['project']['title'])}"


def _dump(doc: dict[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=False, default=str)


@router.get("/templates")
async def list_templates(request: Request, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    rows = await _templates(request).list_templates(user.uuid)
    return {"templates": [view(r) for r in rows]}


@router.post("/templates", status_code=201)
async def save_template(
    body: TemplateSave, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Save the structure of one of the person's projects as a template."""
    try:
        row = await _templates(request).save(
            user.uuid, body.project_id, name=body.name, description=body.description
        )
    except ProjectError as exc:
        raise _refused(exc) from exc
    return view(row, full=True)


@router.get("/templates/{template_id}")
async def read_template(
    template_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    try:
        return view(await _templates(request).get(user.uuid, template_id), full=True)
    except ProjectError as exc:
        raise _refused(exc) from exc


@router.delete("/templates/{template_id}", status_code=204)
async def delete_template(
    template_id: uuid.UUID, request: Request, user: CurrentUser = Depends(api_user)
) -> None:
    try:
        await _templates(request).delete(user.uuid, template_id)
    except ProjectError as exc:
        raise _refused(exc) from exc


@router.post("/templates/{template_id}/projects", status_code=201)
async def create_from_template(
    template_id: uuid.UUID,
    body: FromTemplate,
    request: Request,
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """A new project from a template, in one transaction (all or nothing)."""
    try:
        project_id = await _templates(request).create_project(
            user.uuid,
            template_id,
            title=body.title,
            start_at=body.start_at,
            identifier=body.identifier,
        )
        store = ProjectStore(
            request.app.state.sessionmaker, PERSON, request.app.state.settings.hub_timezone
        )
        project = await store.get_project(user.uuid, project_id)
    except ProjectError as exc:
        raise _refused(exc) from exc
    return project_view(project)
