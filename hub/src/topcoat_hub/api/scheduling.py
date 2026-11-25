"""Who is responsible for what (admin): the node order, the nodes background work prefers, nodes
kept free of chat or of background work, a pause for all background work, and a dry run that
shows which node each standing task would use right now."""

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import chat_policy, model_facts, model_profiles
from topcoat_hub import model_switches as model_switches_view
from topcoat_hub.api import memory as memory_api
from topcoat_hub.auth import CurrentUser, require_admin
from topcoat_hub.chat_store import get_hub_setting, set_hub_setting
from topcoat_hub.db import get_db
from topcoat_hub.gateway import PRIORITY_IDLE, GatewayError, interactive_work, spawn
from topcoat_hub.house_residents import keep_house_resident
from topcoat_hub.model_facts import PROOF_KEY
from topcoat_hub.model_probes import run_probes
from topcoat_hub.models import ChatReview, Conversation, JobRecord, Node, NodeInventory
from topcoat_hub.protocol import ProtocolError
from topcoat_hub.node_policy import (
    MAX_PAUSE_HOURS,
    POLICY_KEY,
    PRIORITIES,
    SYSTEM_ROWS,
    NodePolicy,
    PolicyError,
    check_nodes,
    load_policy,
    parse_policy,
    row_keys,
)
from topcoat_hub.views import node_summary

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class PolicyUpdate(BaseModel):
    """The settings besides the table: the embedding model of the notes and the knowledge base
    ("" = the first served bge-m3); missing = keep what is stored."""

    model_config = ConfigDict(extra="forbid")

    embedding_model: str | None = None


class PoolsUpdate(BaseModel):
    """The pools of all nodes: a node left out has no pool (every model)."""

    model_config = ConfigDict(extra="forbid")

    pools: dict[str, dict[str, Any]]


class MaxLoadedUpdate(BaseModel):
    """How many models a node may hold at once; a node left out has no limit."""

    model_config = ConfigDict(extra="forbid")

    max_loaded: dict[str, int]


class HouseUpdate(BaseModel):
    """The house model of each node (a node left out has none) and the background workers."""

    model_config = ConfigDict(extra="forbid")

    house: dict[str, str]
    workers: list[str] = []
    # the house order (the first node's house model serves work no row steers); left out: the
    # nodes keep the order they had, new ones follow
    order: list[str] | None = None


class KindsUpdate(BaseModel):
    """Kinds set by hand per model; a model left out is detected from its name again."""

    model_config = ConfigDict(extra="forbid")

    model_kinds: dict[str, list[str]]


class TableUpdate(BaseModel):
    """The whole table: a row left out is a row cleared."""

    model_config = ConfigDict(extra="forbid")

    rows: dict[str, dict[str, Any]]


class PauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 0 resumes background work at once
    minutes: int = Field(ge=0, le=MAX_PAUSE_HOURS * 60)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status)


async def standing_roles(state: Any, db: AsyncSession) -> dict[str, str | None]:
    """The model each standing task of the hub uses (a ``model@node`` pin is kept)."""
    chat_model = await state.gateway.resolver.default_model()
    routed = {task: await state.memory.routed_model(task) for task in ("chat", "checker", "care")}
    return {
        "chat": routed["chat"] or chat_model,
        "digest": routed["chat"] or await state.memory._model(),
        "checker": routed["checker"],
        "care": routed["care"] or await state.memory_care._deep_model(),
    }


EMBEDDING_HINTS = ("bge", "embed", "e5", "gte", "nomic", "minilm")


def _embedding_choices(nodes: list[dict[str, Any]], chosen: str) -> list[str]:
    """Models on the compute nodes that look like embedding models, plus the chosen one."""
    names = {
        m
        for n in nodes
        if n["kind"] == "compute"
        for m in n["models"]
        if any(h in m.lower() for h in EMBEDDING_HINTS)
    }
    if chosen:
        names.add(chosen)
    return sorted(names)


async def _view(request: Request, db: AsyncSession) -> dict[str, Any]:
    state = request.app.state
    policy: NodePolicy = state.gateway.policy
    rows = await db.execute(
        select(Node, NodeInventory)
        .outerjoin(NodeInventory, NodeInventory.node_id == Node.id)
        .order_by(Node.name)
    )
    nodes = []
    for node, inv in rows.all():
        summary = node_summary(node, inv, state.sessions.is_connected(node.id))
        roles = list((node.config or {}).get("roles") or [])
        nodes.append(
            {
                "id": str(node.id),
                "name": node.name,
                "state": summary["state"],
                "connected": summary["connected"],
                "kind": "compute" if "gpu" in roles else ("dev" if "dev" in roles else "tools"),
                "loaded": [m["name"] for m in summary["loaded"]],
                # the models on the node's disk: what a row of the table can name
                "models": sorted({m["name"] for m in summary.get("models") or []}),
            }
        )
    stored = await get_hub_setting(db, POLICY_KEY)
    return {
        "embedding_model": policy.embedding_model,
        "embedding_choices": _embedding_choices(nodes, policy.embedding_model),
        "nodes": nodes,
        "paused_until": policy.paused_until.isoformat()
        if policy.paused() and policy.paused_until
        else None,
        "customised": bool(stored),
        "history": state.scheduling_log.recent(),
    }


@router.get("/scheduling")
async def get_scheduling(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = Depends(require_admin),
) -> Any:
    return await _view(request, db)


async def _store(request: Request, db: AsyncSession, policy: NodePolicy) -> Any:
    await set_hub_setting(db, POLICY_KEY, policy.to_json() or None)
    await db.commit()
    request.app.state.gateway.set_policy(policy)
    return await _view(request, db)


def _describe(policy: NodePolicy) -> str:
    parts = []
    if policy.embedding_model:
        parts.append("embedding_model=" + policy.embedding_model)
    return "; ".join(parts)


async def _known_nodes(db: AsyncSession) -> set[str]:
    return set((await db.scalars(select(Node.name))).all())


@router.put("/scheduling")
async def put_scheduling(
    body: PolicyUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> Any:
    current: NodePolicy = request.app.state.gateway.policy
    try:
        policy = parse_policy(
            {
                **current.to_json(),
                "embedding_model": current.embedding_model
                if body.embedding_model is None
                else body.embedding_model,
            }
        )
        check_nodes(policy, await _known_nodes(db))
    except PolicyError as exc:
        return _error(400, "invalid_policy", str(exc))
    log.info("scheduling changed by %s: %s", user.name, policy.to_json())
    request.app.state.scheduling_log.add("policy", user.name, _describe(policy))
    view = await _store(request, db, policy)
    if policy.embedding_model != current.embedding_model:
        embedder = getattr(request.app.state, "embedder", None)
        refresh = getattr(embedder, "refresh", None)
        if refresh is not None:
            spawn(refresh())
    return view


async def _row_preview(state: Any, key: str) -> dict[str, Any]:
    """What a piece of work of this row would use now, and why (starts nothing)."""
    resolver = state.gateway.resolver
    if key == "api":
        return {
            "model": None,
            "node": None,
            "reason": "requests of the /v1 gateway name their model",
        }
    role = key if key in ("check", "background") else "answer"
    application = None if key in SYSTEM_ROWS else key
    try:
        # a check or a summary follows an answer: the one the chat would give now stands in for it
        same_as = None
        if key in ("check", "background"):
            same_as = (await resolver.resolve("answer", application="chat", if_none="first")).model
        # "first": a row that no node can serve now still shows its first entry and says so
        choice = await resolver.resolve(
            role, application=application, if_none="first", same_as=same_as
        )
    except Exception as exc:  # a preview never fails the page
        return {"model": None, "node": None, "reason": f"not known now ({type(exc).__name__})"}
    return {"model": choice.model, "node": choice.node, "reason": choice.reason}


async def _table_view(request: Request) -> dict[str, Any]:
    state = request.app.state
    policy: NodePolicy = state.gateway.policy
    keys = list(row_keys())
    return {
        "keys": keys,
        "system_keys": list(SYSTEM_ROWS),
        "priorities": list(PRIORITIES),
        "rows": {k: row.to_json() for k, row in policy.applications.items()},
        "preview": {k: await _row_preview(state, k) for k in keys},
    }


@router.get("/scheduling/applications")
async def get_table(request: Request, _user: CurrentUser = Depends(require_admin)) -> Any:
    """The table of applications (SCH-04): what is stored, what the older lists say as a table,
    and for every row what it would use right now and why."""
    return await _table_view(request)


@router.put("/scheduling/applications")
async def put_table(
    body: TableUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> Any:
    """Replace the table. Every node named must exist and every row must parse; a row that has
    models or nodes decides for its application, an empty row leaves it to the older lists."""
    current: NodePolicy = request.app.state.gateway.policy
    try:
        policy = replace(
            current, applications=parse_policy({"applications": body.rows}).applications
        )
        check_nodes(policy, await _known_nodes(db))
    except PolicyError as exc:
        return _error(400, "invalid_table", str(exc))
    log.info("scheduling table changed by %s: %s", user.name, policy.to_json().get("applications"))
    request.app.state.scheduling_log.add(
        "table",
        user.name,
        "; ".join(
            f"{k}: {','.join(r.models) or '-'} on {','.join(r.nodes) or 'all'}"
            for k, r in policy.applications.items()
        ),
    )
    await _store(request, db, policy)
    return await _table_view(request)


async def _pools_view(request: Request) -> dict[str, Any]:
    gateway = request.app.state.gateway
    policy: NodePolicy = gateway.policy
    nodes = []
    for view in await gateway.node_views(all_models=True):
        nodes.append(
            {
                "node": view.name,
                "node_id": str(view.id),
                "connected": view.connected,
                "restricted": view.name in policy.pools,
                "max_loaded": policy.max_loaded.get(view.name),
                # the model the node keeps loaded for all text work, and whether it is a worker
                "house": policy.house.get(view.name),
                "house_rank": (
                    list(policy.house).index(view.name) + 1 if view.name in policy.house else None
                ),
                "worker": view.name in policy.workers,
                "models": [
                    {
                        "name": name,
                        "loaded": loaded,
                        "available": policy.in_pool(view.name, name),
                        "kinds": list(
                            model_facts.kinds_of(
                                name, policy.model_kinds, gateway.proven_kinds, view.gguf.get(name)
                            )
                        ),
                        "tags": model_facts.tags(
                            name,
                            view.gguf.get(name),
                            model_facts.kinds_of(
                                name, policy.model_kinds, gateway.proven_kinds, view.gguf.get(name)
                            ),
                        ),
                        "kinds_source": model_facts.source_of(
                            name, policy.model_kinds, gateway.proven_kinds, view.gguf.get(name)
                        ),
                        # what the probes proved: tools, embedding (True, False, or None = never)
                        "capabilities": model_facts.capabilities(gateway.proofs, name),
                        # the setting the admin took from the bench, per kind of work
                        "profile": gateway.profiles.get(name, {}),
                        "size_mb": round(view.sizes_mb[name]) if name in view.sizes_mb else None,
                        # tokens per second measured in real answers (else a benchmark), if any
                        "tps": round(view.speeds[name], 1) if name in view.speeds else None,
                    }
                    for name, loaded in sorted(view.models.items())
                ],
            }
        )
    return {
        "nodes": sorted(nodes, key=lambda n: n["node"]),
        # what work gets when no row of the table decides: the best answering model the pools offer
        "default_model": await gateway.resolver.default_model(),
    }


@router.get("/scheduling/pools")
async def get_pools(request: Request, _user: CurrentUser = Depends(require_admin)) -> Any:
    """Per node: its models and which of them programs may use (PRG-00). A node that is not
    ``restricted`` offers all of its models."""
    return await _pools_view(request)


@router.put("/scheduling/pools")
async def put_pools(
    body: PoolsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> Any:
    """Replace the pools. Every node must exist; an empty list (or no entry) means every model."""
    current: NodePolicy = request.app.state.gateway.policy
    try:
        policy = replace(current, pools=parse_policy({"pools": body.pools}).pools)
        check_nodes(policy, await _known_nodes(db))
    except PolicyError as exc:
        return _error(400, "invalid_pools", str(exc))
    log.info("scheduling pools changed by %s: %s", user.name, policy.to_json().get("pools"))
    request.app.state.scheduling_log.add(
        "pools",
        user.name,
        "; ".join(f"{n}: {len(m)} models" for n, m in policy.pools.items()) or "no pools",
    )
    await _store(request, db, policy)
    return await _pools_view(request)


@router.put("/scheduling/house")
async def put_house(
    body: HouseUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> Any:
    """Replace the house models and the workers. With house models the model lists of the table
    no longer decide (model_resolver.house_entries); keeping the model loaded is the node's
    ``residents`` entry, which the page sets next to this."""
    current: NodePolicy = request.app.state.gateway.policy
    try:
        order = body.order
        if order is None:
            order = [n for n in current.house if n in body.house]
            order += [n for n in body.house if n not in order]
        parsed = parse_policy({"house": body.house, "house_order": order, "workers": body.workers})
        policy = replace(current, house=parsed.house, workers=parsed.workers)
        check_nodes(policy, await _known_nodes(db))
    except PolicyError as exc:
        return _error(400, "invalid_house", str(exc))
    log.info("house models changed by %s: %s, workers %s", user.name, policy.house, policy.workers)
    request.app.state.scheduling_log.add(
        "house",
        user.name,
        ", ".join(f"{n}={m}" for n, m in policy.house.items()) or "none",
    )
    await _store(request, db, policy)
    await _sync_house_residents(request, db, current.house, policy.house)
    return await _pools_view(request)


async def _sync_house_residents(
    request: Request, db: AsyncSession, before: dict[str, str], after: dict[str, str]
) -> None:
    """Each changed house model becomes the node's auto-reloading resident; the one it replaced
    stops being one (house_residents, plan step 1.6)."""
    changed = {n for n in set(before) | set(after) if before.get(n) != after.get(n)}
    if not changed:
        return
    rows = await db.execute(select(Node.name, Node.id).where(Node.name.in_(changed)))
    ids = {name: node_id for name, node_id in rows.all()}
    state = request.app.state
    for name in sorted(changed):
        node_id = ids.get(name)
        if node_id is None:
            continue
        try:
            await keep_house_resident(
                state.nodes,
                state.schemas,
                node_id,
                after.get(name),
                before.get(name),
                source="house",
            )
        except (LookupError, ProtocolError) as exc:
            log.warning("node %s: house resident not updated: %s", name, exc)
            continue
        await state.nodes.publish_node(node_id)


@router.put("/scheduling/max-loaded")
async def put_max_loaded(
    body: MaxLoadedUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> Any:
    """Replace the limits of loaded models per node (a small card holds one)."""
    current: NodePolicy = request.app.state.gateway.policy
    try:
        policy = replace(
            current, max_loaded=parse_policy({"max_loaded": body.max_loaded}).max_loaded
        )
        check_nodes(policy, await _known_nodes(db))
    except PolicyError as exc:
        return _error(400, "invalid_max_loaded", str(exc))
    log.info("max loaded models changed by %s: %s", user.name, policy.max_loaded)
    request.app.state.scheduling_log.add(
        "max_loaded",
        user.name,
        ", ".join(f"{n}={c}" for n, c in policy.max_loaded.items()) or "none",
    )
    await _store(request, db, policy)
    return await _pools_view(request)


@router.put("/scheduling/model-kinds")
async def put_model_kinds(
    body: KindsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> Any:
    """Replace the kinds set by hand (``model_facts.KINDS``); returns the pools view."""
    current: NodePolicy = request.app.state.gateway.policy
    try:
        policy = replace(
            current, model_kinds=parse_policy({"model_kinds": body.model_kinds}).model_kinds
        )
    except PolicyError as exc:
        return _error(400, "invalid_model_kinds", str(exc))
    log.info("model kinds changed by %s: %s", user.name, policy.to_json().get("model_kinds"))
    request.app.state.scheduling_log.add(
        "model_kinds",
        user.name,
        ", ".join(f"{m}={'+'.join(k)}" for m, k in policy.model_kinds.items()) or "none",
    )
    await _store(request, db, policy)
    return await _pools_view(request)


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=255)
    node: str = Field(min_length=1, max_length=255)


@router.post("/scheduling/model-probes")
async def run_model_probes(
    body: ProbeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> Any:
    """Prove what a model on one node can do (tool call, embedding). The probe waits behind every
    real request, and the gateway never puts a model on a game's node. Stored as ``proven``."""
    gateway = request.app.state.gateway
    view = {v.name: v for v in await gateway.node_views()}.get(body.node)  # the pool only
    if view is None or not view.connected:
        return _error(409, "node_unavailable", f"{body.node} is not connected")
    if body.model not in view.models:
        return _error(409, "model_not_in_pool", f"{body.model} is not in the pool of {body.node}")
    try:
        with interactive_work(PRIORITY_IDLE):
            results = await run_probes(gateway.call, f"{body.model}@{body.node}", node=body.node)
    except GatewayError as exc:
        return _error(409, exc.code, exc.message)
    stored = dict(gateway.proofs)
    stored[body.model] = {**stored.get(body.model, {}), **results}
    await set_hub_setting(db, PROOF_KEY, stored)
    await db.commit()
    await gateway.load_proofs()
    log.info("model probes by %s: %s on %s %s", user.name, body.model, body.node, results)
    return {"model": body.model, "node": body.node, "results": results}


class ChatPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: dict[str, Any]


def _chat_policy_view(doc: dict[str, Any]) -> dict[str, Any]:
    """The stored document and what it comes to at each effort level."""
    return {
        "policy": doc,
        "levels": {
            level: {
                k: v for k, v in vars(chat_policy.from_doc(doc, level)).items() if v is not None
            }
            for level in chat_policy.LEVELS
        },
        "numbers": {k: list(v) for k, v in chat_policy.NUMBERS.items()},
    }


@router.get("/scheduling/chat-policy")
async def get_chat_policy(
    db: AsyncSession = Depends(get_db), _user: CurrentUser = Depends(require_admin)
) -> Any:
    """How the chat answers (limits of a check, second look, recall, notes, Nachlese, reactions,
    placement), by effort: the stored document and what it says at each level. An empty document
    is what the hub did before the policy existed."""
    return _chat_policy_view(await chat_policy.stored(db))


@router.put("/scheduling/chat-policy")
async def put_chat_policy(
    body: ChatPolicyUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> Any:
    """Replace the chat policy. A key left out is the hub's own rule; a value may be a table with
    one value per effort level."""
    try:
        doc = chat_policy.check(body.policy)
    except chat_policy.PolicyError as exc:
        return _error(400, "invalid_chat_policy", str(exc))
    log.info("chat policy changed by %s: %s", user.name, sorted(doc))
    request.app.state.scheduling_log.add(
        "chat_policy", user.name, ", ".join(sorted(doc)) or "reset"
    )
    await chat_policy.save(db, doc)
    await db.commit()
    return _chat_policy_view(doc)


FLOOR_KEY = "quality_floor"  # hub_settings: {application: lowest share of green looks, 0 to 1}
# fewer second looks than this say too little about a model to judge it by
MIN_LOOKS = 10


async def _floors(db: AsyncSession) -> dict[str, float]:
    stored = await get_hub_setting(db, FLOOR_KEY)
    if not isinstance(stored, dict):
        return {}
    return {
        k: float(v)
        for k, v in stored.items()
        if isinstance(k, str) and isinstance(v, int | float) and not isinstance(v, bool)
    }


@router.get("/scheduling/quality")
async def quality_counters(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = Depends(require_admin),
) -> Any:
    """How the second look judged the answers of each model in each application over the last
    ``days`` (QLT-01): counts of green, orange and red looks. Only counts, no person and no
    content. A floor the admin set for an application marks the models below it; the hub never
    changes a model because of it (D90)."""
    r = ChatReview
    since = datetime.now(UTC) - timedelta(days=days)
    rows = await db.execute(
        select(
            Conversation.application,
            r.author_model,
            func.count().label("looks"),
            func.count().filter(r.verdict == "ok").label("ok"),
            func.count().filter(r.verdict == "hint").label("hint"),
            func.count().filter(r.verdict == "error").label("error"),
        )
        .join(Conversation, Conversation.id == r.conversation_id)
        .where(r.created_at >= since, r.author_model.is_not(None))
        .group_by(Conversation.application, r.author_model)
        .order_by(Conversation.application, r.author_model)
    )
    floors = await _floors(db)
    out = []
    for row in rows.all():
        app = row.application or "chat"
        share = row.ok / row.looks if row.looks else None
        floor = floors.get(app)
        out.append(
            {
                "application": app,
                "model": row.author_model,
                "looks": row.looks,
                "ok": row.ok,
                "hint": row.hint,
                "error": row.error,
                "ok_share": round(share, 3) if share is not None else None,
                "floor": floor,
                # enough looks to judge by, and fewer green ones than the floor asks for
                "below_floor": bool(
                    floor is not None
                    and share is not None
                    and row.looks >= MIN_LOOKS
                    and share < floor
                ),
            }
        )
    return {"days": days, "min_looks": MIN_LOOKS, "floors": floors, "rows": out}


class FloorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    application: str = Field(min_length=1, max_length=40)
    floor: float | None = Field(None, ge=0, le=1)  # null = no floor


@router.put("/scheduling/quality-floor")
async def put_quality_floor(
    body: FloorUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> Any:
    """Set (or remove) the lowest share of green second looks an application should see. It only
    marks models in the counters; it never moves the work to another model."""
    floors = await _floors(db)
    if body.floor is None:
        floors.pop(body.application, None)
    else:
        floors[body.application] = body.floor
    log.info("quality floor changed by %s: %s=%s", user.name, body.application, body.floor)
    request.app.state.scheduling_log.add(
        "quality_floor", user.name, f"{body.application}: {body.floor}"
    )
    await set_hub_setting(db, FLOOR_KEY, floors or None)
    await db.commit()
    return {"floors": floors}


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=255)
    function: str = Field(min_length=1, max_length=20)
    variant: str | None = Field(None, max_length=40)  # null = back to the model as it is


@router.put("/scheduling/model-profile")
async def put_model_profile(
    body: ProfileUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> Any:
    """Take a setting the bench showed best (one of its fixed presets) for a model and a kind of
    work, or give it back. The gateway adds it to the requests of that kind; what a caller set
    itself is kept. Returns the pools view."""
    gateway = request.app.state.gateway
    profiles = {m: dict(s) for m, s in gateway.profiles.items()}
    settings = profiles.setdefault(body.model, {})
    if body.variant:
        settings[body.function] = body.variant
    else:
        settings.pop(body.function, None)
    try:
        clean = model_profiles.check(profiles)
    except model_profiles.ProfileError as exc:
        return _error(400, "invalid_profile", str(exc))
    log.info(
        "model profile changed by %s: %s %s=%s", user.name, body.model, body.function, body.variant
    )
    request.app.state.scheduling_log.add(
        "model_profile", user.name, f"{body.model}: {body.function}={body.variant or 'default'}"
    )
    await set_hub_setting(db, model_profiles.PROFILE_KEY, clean or None)
    await db.commit()
    await gateway.load_proofs()
    return await _pools_view(request)


def _rate(amount: Any, millis: Any) -> float | None:
    """Units per second from a sum and the milliseconds they took; None without figures."""
    if not amount or not millis:
        return None
    return round(float(amount) * 1000.0 / float(millis), 1)


@router.get("/scheduling/measurements")
async def measurements(
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = Depends(require_admin),
) -> Any:
    """What the models did in everyday use (SCH-06): per application, model and node over the last
    ``days``, from the record of requests (SCH-03). Only figures: counts, speeds and times; no
    person, no conversation, no content leaves the records."""
    j = JobRecord
    since = datetime.now(UTC) - timedelta(days=days)
    rows = await db.execute(
        select(
            j.application,
            j.chosen_model,
            j.chosen_node,
            func.count().label("requests"),
            func.count().filter(j.outcome == "failed").label("failed"),
            func.count().filter(j.outcome == "cancelled").label("cancelled"),
            func.count().filter(j.finish_reason == "length").label("cut_off"),
            func.avg(j.waited_ms).label("waited_ms"),
            func.sum(j.tokens).label("tokens"),
            func.sum(j.predicted_ms).label("predicted_ms"),
            func.sum(j.prompt_tokens - func.coalesce(j.cached_tokens, 0)).label("prompt_tokens"),
            func.sum(j.prompt_ms).label("prompt_ms"),
            func.sum(j.cached_tokens).label("cached_tokens"),
            func.sum(j.prompt_tokens).label("all_prompt_tokens"),
        )
        .where(j.created_at >= since, j.chosen_node != "", j.chosen_model != "")
        .group_by(j.application, j.chosen_model, j.chosen_node)
        .order_by(j.application, j.chosen_model, j.chosen_node)
    )
    out = []
    for r in rows.all():
        out.append(
            {
                "application": r.application or "",
                "model": r.chosen_model,
                "node": r.chosen_node,
                "requests": r.requests,
                "failed": r.failed,
                "cancelled": r.cancelled,
                "cut_off": r.cut_off,
                "waited_seconds": round(float(r.waited_ms) / 1000.0, 1) if r.waited_ms else 0.0,
                "generated_tps": _rate(r.tokens, r.predicted_ms),
                "prompt_tps": _rate(r.prompt_tokens, r.prompt_ms),
                "cached_share": round(float(r.cached_tokens) / float(r.all_prompt_tokens), 2)
                if r.cached_tokens and r.all_prompt_tokens
                else None,
            }
        )
    return {"days": days, "rows": out}


@router.put("/scheduling/choice-mode")
async def put_choice_mode(
    body: dict[str, str],
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> Any:
    """Switch how the gateway picks the first node: "alt" (the old order, default) or "neu" (the
    profile of the work decides, choice.py). Only these two values are accepted."""
    from topcoat_hub.gateway import CHOICE_KEY

    mode = body.get("mode")
    if mode not in ("alt", "neu"):
        return _error(422, "invalid_mode", "mode must be alt or neu")
    log.info("gateway choice switched to %s by %s", mode, user.name)
    request.app.state.scheduling_log.add("choice", user.name, mode)
    await set_hub_setting(db, CHOICE_KEY, mode)
    await db.commit()
    await request.app.state.gateway.load_proofs()
    return {"mode": request.app.state.gateway.choice_mode}


@router.get("/scheduling/model-switches")
async def model_switches(
    request: Request,
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = Depends(require_admin),
) -> Any:
    """How often the nodes loaded a model or changed it, per day and node, and which work caused
    the loads (P2a of the plan: measured, nothing is changed by it). Only figures, like the
    measurements above: no person, no conversation, no content."""
    j = JobRecord
    since = datetime.now(UTC) - timedelta(days=days)
    rows = await db.execute(
        select(
            j.created_at,
            j.chosen_node,
            j.chosen_model,
            j.model_loaded,
            j.work,
            j.reason,
            j.waited_ms,
        ).where(j.created_at >= since, j.chosen_node != "", j.chosen_model != "")
    )
    summary = model_switches_view.summarize([dict(r._mapping) for r in rows.all()])
    summary["review_load_share"] = model_switches_view.review_load_share(summary["totals"])
    return {"days": days, "choice_mode": request.app.state.gateway.choice_mode, **summary}


@router.delete("/scheduling")
async def reset_scheduling(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> Any:
    """Back to the environment's order and background nodes, nothing kept free, no pause."""
    log.info("scheduling reset by %s", user.name)
    request.app.state.scheduling_log.add("reset", user.name)
    return await _store(request, db, NodePolicy())


@router.post("/scheduling/pause")
async def pause_background(
    body: PauseRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> Any:
    """Pause all background work (summaries, memory care, the second look) for some minutes —
    the nodes then stay free for what the admin does by hand. Models already loaded stay loaded."""
    current: NodePolicy = request.app.state.gateway.policy
    until = datetime.now(UTC) + timedelta(minutes=body.minutes) if body.minutes else None
    log.info("background work paused by %s until %s", user.name, until)
    request.app.state.scheduling_log.add(
        "pause" if until else "resume", user.name, until.isoformat() if until else ""
    )
    return await _store(
        request, db, load_policy({**current.to_json(), "paused_until": until and until.isoformat()})
    )


# the background jobs the admin can start by hand, and the call that starts each of them
RUNNABLE = {
    "digest": memory_api.digest_now,
    "care": memory_api.care_now,
    "consolidate": memory_api.consolidate_now,
}


@router.post("/scheduling/run/{task}", status_code=202)
async def run_background_task(
    task: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_admin),
) -> Any:
    """Start one background job now (the admin's own notes and chats, like the buttons on the
    memory page). A paused hub or a missing node answers with the reason, which goes to the
    history as well."""
    starter = RUNNABLE.get(task)
    if starter is None:
        return _error(404, "unknown_task", f"unknown task: {task}")
    gateway_policy: NodePolicy = request.app.state.gateway.policy
    log_ = request.app.state.scheduling_log
    if gateway_policy.paused():
        log_.add("run", user.name, task, ok=False)
        return _error(409, "background_paused", "background work is paused")
    try:
        result = await starter(request, db, user)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        log_.add("run", user.name, f"{task}: {detail.get('code', exc.status_code)}", ok=False)
        raise
    log_.add("run", user.name, task)
    log.info("background task %s started by %s", task, user.name)
    return result
