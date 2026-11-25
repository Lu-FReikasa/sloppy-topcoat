"""Personas, the first-start questionnaire and the count per persona (E3-06, point 11).

The templates are the household's (everybody reads them, an admin changes them); a personal persona
is its user's (D20: nobody reads another user's, not even an admin). Nothing here is reachable for a
model: the text of a persona is built from blocks and written by a person's click only."""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import personas
from topcoat_hub.api.core import _is_admin
from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.chat_store import ChatStore
from topcoat_hub.models import Conversation, User
from topcoat_hub.personas import PersonaError, PersonaStore

router = APIRouter(prefix="/api/personas")


def _http(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


def _refused(exc: PersonaError) -> HTTPException:
    return _http(exc.status, exc.code, exc.message)


async def _db(request: Request) -> Any:
    async with request.app.state.sessionmaker() as db:
        yield db


Db = Annotated[AsyncSession, Depends(_db)]


class PersonaCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=personas.NAME_MAX)
    components: dict[str, Any]
    example: dict[str, str] | str | None = None
    scope: Literal["user", "household"] = "user"  # household: an admin's template


class PersonaUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=personas.NAME_MAX)
    components: dict[str, Any] | None = None
    example: dict[str, str] | str | None = None


class DefaultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    persona_id: uuid.UUID | None = None


class WizardState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["later", "skipped"]


class Answers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: dict[str, Any] = Field(default_factory=dict)
    name: str | None = Field(None, min_length=1, max_length=personas.NAME_MAX)


async def _language(db: AsyncSession, user: CurrentUser) -> str:
    row = await db.get(User, user.uuid)
    return (row.language if row else None) or "en"


async def _wizard(db: AsyncSession, store: PersonaStore, user: CurrentUser) -> dict[str, Any]:
    """Where the first-start wizard is for this user: shown to a new user without a persona (no
    conversation yet), a reminder banner for everybody else — never a compulsion."""
    settings = await store.settings()
    state = settings.persona_wizard if settings else None
    has_persona = await store.default() is not None
    has_history = bool(
        await db.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.user_id == user.uuid)
        )
    )
    open_ = state in (None, "later") and not has_persona
    return {
        "state": state,
        "show": open_ and state is None and not has_history,
        "banner": open_ and (state == "later" or has_history),
    }


@router.get("")
async def overview(db: Db, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    store = PersonaStore(db, user.uuid, _is_admin(user))
    lang = await _language(db, user)
    default = await store.default()
    return {
        "templates": [personas.view(p, lang) for p in await store.templates()],
        "own": [personas.view(p, lang) for p in await store.own()],
        "default_id": str(default.id) if default else None,
        "max_tokens": personas.MAX_TOKENS,
        "wizard": await _wizard(db, store, user),
        "blocks": {
            "humor": list(personas.HUMORS),
            "length": list(personas.LENGTHS),
            "ask": list(personas.ASKS),
            "address": list(personas.ADDRESSES),
        },
    }


@router.get("/stats")
async def stats(db: Db, user: CurrentUser = Depends(api_user)) -> list[dict[str, Any]]:
    """Per persona the answers given and the user's own verdicts — theirs only, no comparison
    between users (D20)."""
    return await PersonaStore(db, user.uuid).stats()


@router.put("/default")
async def set_default(
    body: DefaultBody, db: Db, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The persona new conversations start with; conversations that exist keep theirs."""
    store = PersonaStore(db, user.uuid)
    try:
        await store.select_default(body.persona_id)
    except PersonaError as exc:
        raise _refused(exc) from exc
    await db.commit()
    return {"default_id": str(body.persona_id) if body.persona_id else None}


@router.put("/wizard")
async def wizard_state(
    body: WizardState, db: Db, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """ "Later" (a banner stays) or "Skip" (nothing more is offered): always allowed."""
    store = PersonaStore(db, user.uuid)
    await store.set_wizard(body.state)
    await db.commit()
    return await _wizard(db, store, user)


@router.get("/questionnaire")
async def get_questionnaire(_user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    """The scenes, answers, short questions and samples of the questionnaire (a file in the
    repository, German and English)."""
    return {k: v for k, v in personas.questionnaire().items() if not k.startswith("_")}


@router.post("/questionnaire/preview")
async def preview(body: Answers, db: Db, user: CurrentUser = Depends(api_user)) -> dict[str, Any]:
    """ "This is how I would sound": the persona these answers make (the same answers give the same
    text) and two sample sentences. Nothing is stored."""
    lang = await _language(db, user)
    try:
        components, example, role = personas.answers_to_persona(body.answers)
        samples = personas.samples_for(body.answers, lang)
    except PersonaError as exc:
        raise _refused(exc) from exc
    rendered = personas.render(components, example, lang)
    return {
        "role": role,
        "components": components,
        "text": rendered.text,
        "tokens": rendered.tokens,
        "samples": samples,
    }


@router.post("/questionnaire/confirm", status_code=201)
async def confirm(
    body: Answers, request: Request, db: Db, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """The user's click on "that is me": the persona is created (theirs), becomes their default and
    the form of address of the answers is applied. Afterwards everything can be changed under
    "Anweisungen"."""
    store = PersonaStore(db, user.uuid)
    lang = await _language(db, user)
    try:
        components, example, _role = personas.answers_to_persona(body.answers)
        name = body.name or ("Meine Persona" if lang == "de" else "My persona")
        persona = await store.create(name, components, example)
        await store.select_default(persona.id)
        await store.set_wizard("done")
    except PersonaError as exc:
        raise _refused(exc) from exc
    await db.commit()
    # the form of address: the setting and the note that mirrors it
    settings = await ChatStore(db, user.uuid).get_settings()
    address = components["address"]
    await ChatStore(db, user.uuid).set_settings(
        settings.system_prompt if settings else None,
        settings.temperature if settings else None,
        address,
        settings.memory_enabled if settings else True,
    )
    await db.commit()
    try:
        await request.app.state.core.sync_settings(
            user.uuid, settings.system_prompt if settings else None, address
        )
    except PersonaError as exc:
        raise _refused(exc) from exc
    return personas.view(persona, lang)


@router.post("", status_code=201)
async def create(
    body: PersonaCreate, db: Db, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    store = PersonaStore(db, user.uuid, _is_admin(user))
    try:
        persona = await store.create(body.name, body.components, body.example, scope=body.scope)
    except PersonaError as exc:
        raise _refused(exc) from exc
    await db.commit()
    return personas.view(persona, await _language(db, user))


@router.put("/{persona_id}")
async def update(
    persona_id: uuid.UUID, body: PersonaUpdate, db: Db, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    store = PersonaStore(db, user.uuid, _is_admin(user))
    kwargs: dict[str, Any] = {"name": body.name, "components": body.components}
    if "example" in body.model_fields_set:
        kwargs["example"] = body.example
    try:
        persona = await store.update(persona_id, **kwargs)
    except PersonaError as exc:
        raise _refused(exc) from exc
    await db.commit()
    return personas.view(persona, await _language(db, user))


@router.delete("/{persona_id}", status_code=204)
async def archive(persona_id: uuid.UUID, db: Db, user: CurrentUser = Depends(api_user)) -> None:
    store = PersonaStore(db, user.uuid, _is_admin(user))
    try:
        await store.archive(persona_id)
    except PersonaError as exc:
        raise _refused(exc) from exc
    settings = await store.settings()
    if settings is not None and settings.persona_id == persona_id:
        await store.select_default(None)
    await db.commit()
