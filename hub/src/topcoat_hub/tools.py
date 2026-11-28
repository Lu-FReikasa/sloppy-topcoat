"""Tool nodes (E2-04): who may run which read-only capability where, and the audit of every call.

A capability call goes through :meth:`ToolService.invoke` whoever asks — the GUI, an API key, later
the agent (stage 3). The checks, in this order, all happen here and again on the node:

1. the node exists and its config allows ``capability.invoke`` and switches the capability on;
2. the capability is one the node offers and that is **read-only** (D15: anything that changes
   state needs the confirmation of that single action, which is a different path);
3. the user may use it (D20): an admin on the GUI may; everybody else — an API key included, even
   the key of an admin — needs a grant for that node and capability;
4. the input fits the capability's schema.

Every call that got past "the node exists" is written to ``tool_calls`` — refused ones too.
"""

import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import jsonschema
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import memory_filter
from topcoat_hub.agent_workshop import CHANGES as WORKSHOP_EDITS
from topcoat_hub.auth import CurrentUser
from topcoat_hub.config import Settings
from topcoat_hub.live import ActionTimeout, NodeOffline, NodeReadOnly, NodeSessions
from topcoat_hub.models import CapabilityGrant, Node, NodeInventory, ToolCall, User

log = logging.getLogger(__name__)

_MASKED = re.compile(r"«masked:[a-z_]+»")
MAX_INPUT_BYTES = 16 * 1024  # an input is a container name and a few numbers, never a document

ALL = "*"  # a grant for every read-only capability the node enables
# The read-only shell (shell.read) is not covered by "*": it runs arbitrary commands (the OS user
# that runs them can only read, but what it may read is broad), so a non-admin needs a grant that
# names it, per node. It is also off by default: no node config lists it until an admin adds it.
# The same holds for the Windows read capabilities (windows.*): event logs, services, installed
# programs and the like of a personal PC are not something "*" should hand out.
# files.list / files.read (HOST-01) are named-only as well: the folders are released per node by a
# person, and which user may look into them is decided per node, never by a blanket "*".
NAMED_ONLY_PREFIXES = ("shell.", "windows.", "repo.", "files.")


def granted(caller: "Caller", grants: set[str], capability: str) -> bool:
    """May this caller run this capability on a node, by grants alone? An admin on the GUI may run
    everything the node enables; ``*`` covers every read-only capability except the shell."""
    if caller.admin or capability in grants:
        return True
    return ALL in grants and not capability.startswith(NAMED_ONLY_PREFIXES)


ACTION = "capability.invoke"
# state-changing capabilities (E3-01b) run through this action only, after the owner's click
EXECUTE = "capability.execute"
# the person's own steps on local repositories of the dev workshop (D76): a click on the hub's
# page, never an assistant; the node runs only capabilities marked ``manage`` through it
MANAGE = "workshop.manage"
# the steps of the build service, run from Administration → Releases only (NS-01)
BUILD_MANAGE = "build.manage"
# the roll-out of the hub on the production host, from Administration → Releases only
HUB_MANAGE = "hub.manage"


def _strings(value: Any) -> list[str]:
    """Every string in a JSON value, keys included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for k, v in value.items() for t in (*_strings(k), *_strings(v))]
    if isinstance(value, list):
        return [t for v in value for t in _strings(v)]
    return []


class ToolError(Exception):
    """A refused or failed call; ``status`` is the HTTP status the API answers with."""

    def __init__(self, status: int, code: str, message: str, call_id: int | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.call_id = call_id  # the audit row of a refused confirmed run


# the capabilities whose URL leaves the network and whose node may check where it comes from: its
# own search hits, or ``url_origin: "person"`` from the hub (security audit 2026-11-27, finding 03)
URL_ORIGIN_CAPABILITIES = frozenset({"web.fetch", "web.image"})
URL_NOT_GIVEN = "url_not_given"  # the code of a refused URL of that kind


@dataclass(frozen=True)
class Caller:
    """Who asks. ``admin`` is the dev-token admin on the GUI; a key is never one."""

    user_id: uuid.UUID
    admin: bool
    via: str  # gui | api | agent
    application: str | None = None  # the application the run belongs to (project: its tools)
    # a run for a chat prepares writing project calls instead of making them (project_changes.py)
    stage_writes: bool = False
    task_id: uuid.UUID | None = None  # the agent task the call belongs to, when there is one
    # when set, the only capabilities this run is offered and may call (a reviewer run, PPH-05)
    allowed_capabilities: frozenset[str] | None = None
    # the chat's scope of the run (nodes / groups): Topcoat's own state reports what is outside it
    scope: dict[str, Any] | None = None

    @classmethod
    def of(cls, user: CurrentUser, via: str | None = None) -> "Caller":
        return cls(
            user.uuid, user.is_admin and not user.via_key, via or ("api" if user.via_key else "gui")
        )


def offered(inventory: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The capabilities a node build offers, by name (from its last inventory)."""
    return {c["name"]: c for c in (inventory or {}).get("capabilities", []) or []}


def enabled(config: dict[str, Any]) -> list[str]:
    """What the node's config switches on — empty when ``capability.invoke`` is not allowed."""
    if ACTION not in config.get("allowed_actions", []):
        return []
    return list(config.get("capabilities", []))


def runs_without_click(caller: "Caller", info: dict[str, Any]) -> bool:
    """May this capability run on a plain ``capability.invoke``? A read-only one always. One that
    changes only the scratch area of a dev node (``scratch``, D52: the workshop worktrees) runs
    without a click for an admin on the GUI or in the agent that acts for them — never for an
    API key or any other user."""
    if info.get("manage") is True:
        return False  # the person's own step (D76): never a plain call
    if info.get("read_only") is True:
        return True
    return info.get("scratch") is True and info.get("read_only") is not True and caller.admin


def usable(
    caller: Caller, grants: set[str], config: dict[str, Any], inventory: dict[str, Any] | None
) -> list[str]:
    """The capabilities this caller may run on this node right now."""
    off = offered(inventory)
    names = [n for n in enabled(config) if runs_without_click(caller, off.get(n, {}))]
    return [n for n in names if granted(caller, grants, n)]


def writable(
    caller: Caller, grants: set[str], config: dict[str, Any], inventory: dict[str, Any] | None
) -> list[str]:
    """The state-changing capabilities this caller may have *proposed and executed* on this node:
    the node allows ``capability.execute``, switches the capability on and offers it as not
    read-only. A grant for everything (``*``) never covers them — only an admin on the GUI or a
    grant that names the capability (D15)."""
    if EXECUTE not in config.get("allowed_actions", []):
        return []
    off = offered(inventory)
    names = [
        n
        for n in config.get("capabilities", [])
        if n in off
        and off[n].get("read_only") is False
        and (off[n].get("scratch") is not True or n in WORKSHOP_EDITS)  # D99: edits are proposed
        and off[n].get("manage") is not True  # never proposed: the person runs it (D76)
    ]
    if caller.admin:
        return names
    return [n for n in names if n in grants]


class ToolService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        sessions: NodeSessions,
        settings: Settings,
        input_guard: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        repo_spec: Callable[[uuid.UUID, str, Any], Awaitable[dict[str, str]]] | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.sessions = sessions
        self.settings = settings
        # Refuses an input that must not leave the network (web.search / web.fetch); runs for every
        # call, so no path (agent, chat check, REST) skips it, and a refusal is audited like any.
        self.input_guard = input_guard
        # hands a dev node the repository of a repo.* call (devrepos.DevRepoStore.call_spec)
        self.repo_spec = repo_spec

    # --- grants --------------------------------------------------------------------

    async def grants_of(self, db: AsyncSession, user_id: uuid.UUID, node_id: uuid.UUID) -> set[str]:
        rows = await db.scalars(
            select(CapabilityGrant.capability).where(
                CapabilityGrant.user_id == user_id, CapabilityGrant.node_id == node_id
            )
        )
        return set(rows)

    async def grants_on(self, db: AsyncSession, node_id: uuid.UUID) -> list[dict[str, Any]]:
        """Every grant on a node, per user (admin view)."""
        rows = (
            await db.execute(
                select(CapabilityGrant, User.name)
                .join(User, User.id == CapabilityGrant.user_id)
                .where(CapabilityGrant.node_id == node_id)
                .order_by(User.name, CapabilityGrant.capability)
            )
        ).all()
        by_user: dict[uuid.UUID, dict[str, Any]] = {}
        for g, name in rows:
            entry = by_user.setdefault(
                g.user_id, {"user_id": str(g.user_id), "user": name, "capabilities": []}
            )
            entry["capabilities"].append(g.capability)
        return list(by_user.values())

    async def set_grants(
        self,
        db: AsyncSession,
        node_id: uuid.UUID,
        user_id: uuid.UUID,
        caps: list[str],
        by: uuid.UUID,
    ) -> None:
        """Replace the grants of one user on one node."""
        await db.execute(
            delete(CapabilityGrant).where(
                CapabilityGrant.user_id == user_id, CapabilityGrant.node_id == node_id
            )
        )
        for cap in sorted(set(caps)):
            db.add(CapabilityGrant(user_id=user_id, node_id=node_id, capability=cap, granted_by=by))
        await db.commit()

    async def usable_tools(self, caller: Caller) -> list[dict[str, Any]]:
        """What the caller may run right now, for the agent to choose from: one entry per
        connected node and usable capability (name, description, input schema). The same rules
        as :meth:`invoke` — the agent sees nothing its user could not run by hand."""
        async with self.sessionmaker() as db:
            nodes = (await db.scalars(select(Node).order_by(Node.name))).all()
            inventories = {
                i.node_id: i.inventory for i in (await db.scalars(select(NodeInventory))).all()
            }
            granted: dict[uuid.UUID, set[str]] = {}
            for node_id, cap in (
                await db.execute(
                    select(CapabilityGrant.node_id, CapabilityGrant.capability).where(
                        CapabilityGrant.user_id == caller.user_id
                    )
                )
            ).all():
                granted.setdefault(node_id, set()).add(cap)
        out: list[dict[str, Any]] = []
        for node in nodes:
            if self.sessions.get(node.id) is None:
                continue
            inventory = inventories.get(node.id)
            off = offered(inventory)
            for name in usable(caller, granted.get(node.id, set()), node.config, inventory):
                out.append(
                    {
                        "node_id": str(node.id),
                        "node": node.name,
                        "capability": name,
                        "description": off[name].get("description", ""),
                        "input_schema": off[name].get("input_schema", {}),
                    }
                )
        return out

    async def proposable_tools(self, caller: Caller) -> list[dict[str, Any]]:
        """The state-changing capabilities the caller's agent may *propose* (never run): one entry
        per connected node and capability, like :meth:`usable_tools`."""
        async with self.sessionmaker() as db:
            nodes = (await db.scalars(select(Node).order_by(Node.name))).all()
            inventories = {
                i.node_id: i.inventory for i in (await db.scalars(select(NodeInventory))).all()
            }
            granted: dict[uuid.UUID, set[str]] = {}
            for node_id, cap in (
                await db.execute(
                    select(CapabilityGrant.node_id, CapabilityGrant.capability).where(
                        CapabilityGrant.user_id == caller.user_id
                    )
                )
            ).all():
                granted.setdefault(node_id, set()).add(cap)
        out: list[dict[str, Any]] = []
        for node in nodes:
            if self.sessions.get(node.id) is None:
                continue
            inventory = inventories.get(node.id)
            off = offered(inventory)
            for name in writable(caller, granted.get(node.id, set()), node.config, inventory):
                out.append(
                    {
                        "node_id": str(node.id),
                        "node": node.name,
                        "capability": name,
                        "description": off[name].get("description", ""),
                        "input_schema": off[name].get("input_schema", {}),
                    }
                )
        return out

    # --- audit ---------------------------------------------------------------------

    async def recent_calls(
        self, db: AsyncSession, caller: Caller, node_id: uuid.UUID | None, limit: int
    ) -> list[dict[str, Any]]:
        """The audit rows a caller may read: their own, or all of them for an admin (D20)."""
        q = select(ToolCall, User.name).join(User, User.id == ToolCall.user_id)
        if not caller.admin:
            q = q.where(ToolCall.user_id == caller.user_id)
        if node_id is not None:
            q = q.where(ToolCall.node_id == node_id)
        rows = (await db.execute(q.order_by(ToolCall.id.desc()).limit(limit))).all()
        return [call_view(c, name) for c, name in rows]

    async def _skip_audit(self, row: ToolCall) -> int | None:
        """The audit of a call that must leave no trace: nothing is written."""
        return None

    async def _audit(self, row: ToolCall) -> int | None:
        try:
            async with self.sessionmaker() as db:
                db.add(row)
                await db.commit()
                return row.id
        except Exception:  # the call happened; a failing audit must be loud, not fatal
            log.exception("could not write the tool-call audit row")
            return None

    def _excerpt(self, output: Any) -> tuple[int, str | None]:
        """Size of the output and what the audit keeps of it (never anything the secret filter
        flags: the node masks, this is the second line)."""
        text = json.dumps(output, ensure_ascii=False)
        size = len(text.encode("utf-8"))
        limit = self.settings.tool_audit_output_bytes
        if limit <= 0:
            return size, None
        # what the node already masked is not a secret: take the markers out before asking
        # (and ask about the strings, not the JSON: its escapes would look like a value)
        rule = memory_filter.check(_MASKED.sub("", "\n".join(_strings(output))))
        if rule and rule != "long_token":
            return size, f"[not kept in the audit log: it looks like it holds a secret ({rule})]"
        return size, text[:limit]

    # --- the call ------------------------------------------------------------------

    async def invoke(
        self,
        caller: Caller,
        node_id: uuid.UUID,
        capability: str,
        tool_input: dict[str, Any],
        task_id: uuid.UUID | None = None,
        quiet: bool = False,
        unrecorded: bool = False,
        private: str | None = None,
        max_input_bytes: int = MAX_INPUT_BYTES,
        wait_seconds: float | None = None,
        person_url: bool = False,
        url_refusal: str | None = None,
    ) -> dict[str, Any]:
        """Run one read-only capability. ``quiet`` is for the hub's own periodic reads (the
        inventory, E3-10): a call that works leaves no audit row (it would be one per node per
        few minutes), a refused or failed one is audited like any other. ``unrecorded`` is for
        the incognito folder: nothing is audited, whatever the outcome (the input may be the
        person's private question); a call that carries private data is still refused. ``private``
        keeps the call in the audit but neither its input nor its output (the voice path, D127:
        audio and what was said are never stored); its text says why. ``max_input_bytes`` and
        ``wait_seconds`` are for the few capabilities whose input is a recording, not a name.
        ``person_url``: the URL of a ``web.fetch`` / ``web.image`` is the person's own (from their
        message, or built by the hub from their request), not one a model made up; a call the
        person makes by hand (GUI, API) always is (finding 03, :data:`URL_ORIGIN_CAPABILITIES`).
        ``url_refusal``: the run's reason why the URL may not be read (it came from no search hit
        of the run and not from the person, ``web_access.WebOrigins``); checked after the
        private-data guard, refused as ``url_not_given`` and audited without the input."""
        audit = self._skip_audit if unrecorded else self._audit
        async with self.sessionmaker() as db:
            node = await db.get(Node, node_id)
            if node is None:
                raise ToolError(404, "not_found", "unknown node")
            inv_row = await db.get(NodeInventory, node_id)
            inventory = inv_row.inventory if inv_row else None
            grants = await self.grants_of(db, caller.user_id, node_id)
            config, node_name = node.config, node.name

        audited_input: dict[str, Any] = tool_input if private is None else {"withheld": private}

        def row(**kw: Any) -> ToolCall:
            return ToolCall(
                user_id=caller.user_id,
                node_id=node_id,
                node_name=node_name,
                capability=capability,
                input=audited_input,
                via=caller.via,
                task_id=task_id,
                **kw,
            )

        async def refuse(status: int, code: str, message: str) -> ToolError:
            await audit(row(ok=False, code=code, error=message))
            return ToolError(status, code, message)

        if not granted(caller, grants, capability):
            # not even "it exists here": a user without a grant learns nothing about the node
            raise await refuse(403, "not_granted", f"you may not use {capability} on this node")
        if ACTION not in config.get("allowed_actions", []):
            raise await refuse(403, "not_allowed", "this node does not allow capability calls")
        if capability not in config.get("capabilities", []):
            raise await refuse(
                403, "capability_disabled", f"{capability} is not switched on for this node"
            )
        info = offered(inventory).get(capability)
        if info is None:
            raise await refuse(
                409,
                "unknown_capability",
                f"this node does not offer {capability} (connect it once to learn)",
            )
        if not runs_without_click(caller, info):
            raise await refuse(
                403,
                "needs_confirmation",
                f"{capability} changes state: it runs only after the user confirms that action",
            )
        if len(json.dumps(tool_input).encode("utf-8")) > max_input_bytes:
            raise await refuse(422, "invalid_input", f"the input of {capability} is too large")
        try:
            jsonschema.Draft202012Validator(info["input_schema"]).validate(tool_input)
        except jsonschema.ValidationError as exc:
            raise await refuse(
                422, "invalid_input", f"input does not fit {capability}: {exc.message}"
            ) from exc
        except jsonschema.SchemaError:
            log.warning("node %s sent an invalid input schema for %s", node_name, capability)

        if self.input_guard is not None:
            try:
                await self.input_guard(capability, tool_input)
            except ToolError as exc:
                if exc.code == "private_data":  # the audit keeps that it happened, not the secret
                    audited_input = {"withheld": "the input held private data and is not kept"}
                raise await refuse(exc.status, exc.code, exc.message) from exc
        if url_refusal:
            # a URL the model made up may carry what the run read (finding 03): not kept either
            audited_input = {"withheld": "the URL came from no search and not from the person"}
            raise await refuse(422, URL_NOT_GIVEN, url_refusal)

        args: dict[str, Any] = {"capability": capability, "input": tool_input}
        if (
            capability in URL_ORIGIN_CAPABILITIES
            and info.get("url_origin") is True
            and (person_url or caller.via != "agent")
        ):
            # a node that checks where a URL comes from fetches only its own search hits unless
            # the hub vouches for the URL; outside the input, so no model can set it (finding 03)
            args["url_origin"] = "person"
        if capability.startswith("repo."):
            # the dev workshop (D52): the hub says which repository this is and hands the node its
            # address and token with this one call; the audit row above holds the input only
            if self.repo_spec is None:
                raise await refuse(409, "repo_unavailable", "no repositories are registered here")
            try:
                spec = await self.repo_spec(caller.user_id, capability, tool_input.get("repo"))
            except ToolError as exc:
                raise await refuse(exc.status, exc.code, exc.message) from exc
            pinned = spec.pop("node_id", "")
            if pinned and pinned != str(node_id):
                # a local repository lives in the workshop of one dev node (D76)
                raise await refuse(
                    409, "wrong_node", "this repository lives on another dev node: call it there"
                )
            args["input"] = {**tool_input, "repo": spec.pop("name")}
            args["repo"] = spec

        started = time.monotonic()
        try:
            result = await self.sessions.dispatch(
                node_id,
                ACTION,
                args,
                wait_seconds or self.settings.tool_timeout_seconds,
            )
        except NodeOffline as exc:
            raise await refuse(409, "node_offline", "the node is not connected") from exc
        except NodeReadOnly as exc:
            raise await refuse(
                409, "upgrade_required", "the node speaks an unsupported protocol"
            ) from exc
        except ActionTimeout as exc:
            raise await refuse(504, "timeout", "the node did not answer in time") from exc
        took = int((time.monotonic() - started) * 1000)

        if not result.get("ok"):
            err = result.get("error") or {}
            if err.get("code") == "private_data":  # the node's own guard: keep no secret either
                audited_input = {"withheld": "the input held private data and is not kept"}
            call_id = await audit(
                row(
                    ok=False,
                    code=err.get("code", "error"),
                    error=err.get("message"),
                    duration_ms=took,
                )
            )
            return {"ok": False, "call_id": call_id, "capability": capability, "error": err}
        body = result.get("result") or {}
        output = body.get("output")
        size, excerpt = self._excerpt(output)
        if private is not None:
            excerpt = None
        call_id = (
            None
            if quiet
            else await audit(
                row(
                    ok=True,
                    output_bytes=size,
                    output_excerpt=excerpt,
                    duration_ms=body.get("duration_ms", took),
                )
            )
        )
        return {
            "ok": True,
            "call_id": call_id,
            "capability": capability,
            "output": output,
            "bytes": size,
            "duration_ms": body.get("duration_ms", took),
            "at": body.get("at"),
        }

    async def execute_confirmed(
        self,
        caller: Caller,
        node_id: uuid.UUID,
        capability: str,
        tool_input: dict[str, Any],
        proposal_id: uuid.UUID,
        content_hash: str,
        task_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Run ONE state-changing capability the owner confirmed (E3-01b). Only the proposal
        service calls this, after it checked hash, state, expiry and owner; what is checked here is
        what :meth:`invoke` checks for a read — grant, node config, offered, input schema — mirrored
        for a capability that is *not* read-only, and the node checks it all once more. The audit
        row is written for every outcome, with ``via="proposal"``."""
        async with self.sessionmaker() as db:
            node = await db.get(Node, node_id)
            if node is None:
                raise ToolError(404, "not_found", "unknown node")
            inv_row = await db.get(NodeInventory, node_id)
            inventory = inv_row.inventory if inv_row else None
            grants = await self.grants_of(db, caller.user_id, node_id)
            config, node_name = node.config, node.name

        def row(**kw: Any) -> ToolCall:
            return ToolCall(
                user_id=caller.user_id,
                node_id=node_id,
                node_name=node_name,
                capability=capability,
                input=tool_input,
                via="proposal",
                task_id=task_id,
                **kw,
            )

        async def refuse(status: int, code: str, message: str) -> ToolError:
            call_id = await self._audit(row(ok=False, code=code, error=message))
            return ToolError(status, code, message, call_id)

        if caller.via != "gui" or not (caller.admin or capability in grants):
            raise await refuse(403, "not_granted", f"you may not run {capability} on this node")
        if EXECUTE not in config.get("allowed_actions", []):
            raise await refuse(403, "not_allowed", "this node does not allow confirmed actions")
        if capability not in config.get("capabilities", []):
            raise await refuse(
                403, "capability_disabled", f"{capability} is not switched on for this node"
            )
        info = offered(inventory).get(capability)
        if info is None:
            raise await refuse(409, "unknown_capability", f"this node does not offer {capability}")
        if info.get("read_only") is not False:
            raise await refuse(
                403, "not_a_change", f"{capability} is read-only: it does not need a confirmation"
            )
        # A change of the workshop's scratch copy is a scratch capability for the node: it runs it
        # with capability.invoke, and capability.execute refuses it (node/agent/capability.go).
        # The confirmation is the hub's: everything above (grant, hash, owner, expiry) has been
        # checked, and the node still checks the capability, its input and the repository.
        action = ACTION if capability in WORKSHOP_EDITS else EXECUTE
        try:
            jsonschema.Draft202012Validator(info["input_schema"]).validate(tool_input)
        except jsonschema.ValidationError as exc:
            raise await refuse(
                422, "invalid_input", f"input does not fit {capability}: {exc.message}"
            ) from exc
        except jsonschema.SchemaError:
            log.warning("node %s sent an invalid input schema for %s", node_name, capability)

        args: dict[str, Any] = {"capability": capability, "input": tool_input}
        if (
            action == EXECUTE
        ):  # the node writes the click (id, hash) to its log; invoke takes no more
            args.update(proposal=str(proposal_id), hash=content_hash)
        if capability.startswith("repo."):
            # a confirmed push (D52): the node gets the repository and its token with this call
            if self.repo_spec is None:
                raise await refuse(409, "repo_unavailable", "no repositories are registered here")
            try:
                spec = await self.repo_spec(caller.user_id, capability, tool_input.get("repo"))
            except ToolError as exc:
                raise await refuse(exc.status, exc.code, exc.message) from exc
            pinned = spec.pop("node_id", "")
            if pinned and pinned != str(node_id):
                # a local repository lives in the workshop of one dev node (D76)
                raise await refuse(
                    409, "wrong_node", "this repository lives on another dev node: call it there"
                )
            args["input"] = {**tool_input, "repo": spec.pop("name")}
            args["repo"] = spec

        started = time.monotonic()
        try:
            result = await self.sessions.dispatch(
                node_id,
                action,
                args,
                self.settings.tool_timeout_seconds,
            )
        except NodeOffline as exc:
            raise await refuse(409, "node_offline", "the node is not connected") from exc
        except NodeReadOnly as exc:
            raise await refuse(
                409, "upgrade_required", "the node speaks an unsupported protocol"
            ) from exc
        except ActionTimeout as exc:
            raise await refuse(
                504, "timeout", "the node did not answer in time: check whether it ran"
            ) from exc
        took = int((time.monotonic() - started) * 1000)
        if not result.get("ok"):
            err = result.get("error") or {}
            call_id = await self._audit(
                row(
                    ok=False,
                    code=err.get("code", "error"),
                    error=err.get("message"),
                    duration_ms=took,
                )
            )
            return {"ok": False, "call_id": call_id, "capability": capability, "error": err}
        body = result.get("result") or {}
        output = body.get("output")
        size, excerpt = self._excerpt(output)
        call_id = await self._audit(
            row(
                ok=True,
                output_bytes=size,
                output_excerpt=excerpt,
                duration_ms=body.get("duration_ms", took),
            )
        )
        return {
            "ok": True,
            "call_id": call_id,
            "capability": capability,
            "output": output,
            "bytes": size,
            "duration_ms": body.get("duration_ms", took),
            "at": body.get("at"),
        }


async def manage(
    tools: "ToolService",
    caller: Caller,
    node_id: uuid.UUID,
    capability: str,
    tool_input: dict[str, Any],
    repo: dict[str, Any] | None = None,
    action: str = MANAGE,
) -> dict[str, Any]:
    """Run one of the person's own steps on a local repository (D76: create, clone, branch,
    merge, publish, pull, remove) on a dev node, through the action ``workshop.manage``. Only the
    signed-in admin on the GUI (never an API key, never an assistant), only a capability the node
    offers as ``manage``; every call is audited like any other. ``repo`` is the call's repository
    (a remote's address and token, or a local one), handed to the node with this call only."""
    async with tools.sessionmaker() as db:
        node = await db.get(Node, node_id)
        if node is None:
            raise ToolError(404, "not_found", "unknown node")
        inv_row = await db.get(NodeInventory, node_id)
        inventory = inv_row.inventory if inv_row else None
        config, node_name = node.config, node.name

    def row(**kw: Any) -> ToolCall:
        return ToolCall(
            user_id=caller.user_id,
            node_id=node_id,
            node_name=node_name,
            capability=capability,
            input=tool_input,
            via=caller.via,
            **kw,
        )

    async def refuse(status: int, code: str, message: str) -> ToolError:
        await tools._audit(row(ok=False, code=code, error=message))
        return ToolError(status, code, message)

    if caller.via != "gui" or not caller.admin:
        raise await refuse(403, "gui_only", "only the signed-in admin manages repositories")
    if action not in (MANAGE, BUILD_MANAGE, HUB_MANAGE):
        raise await refuse(403, "not_allowed", f"{action} is not a step a person can run")
    if action not in config.get("allowed_actions", []):
        raise await refuse(403, "not_allowed", f"this node does not allow {action}")
    if capability not in config.get("capabilities", []):
        raise await refuse(
            403, "capability_disabled", f"{capability} is not switched on for this node"
        )
    info = offered(inventory).get(capability)
    if info is None or info.get("manage") is not True:
        raise await refuse(
            409, "unknown_capability", f"this node does not offer {capability} as a workshop step"
        )
    try:
        jsonschema.Draft202012Validator(info["input_schema"]).validate(tool_input)
    except jsonschema.ValidationError as exc:
        raise await refuse(
            422, "invalid_input", f"input does not fit {capability}: {exc.message}"
        ) from exc
    except jsonschema.SchemaError:
        log.warning("node %s sent an invalid input schema for %s", node_name, capability)
    args: dict[str, Any] = {"capability": capability, "input": tool_input}
    if repo is not None:
        args["repo"] = repo
    started = time.monotonic()
    try:
        result = await tools.sessions.dispatch(
            node_id, action, args, tools.settings.tool_timeout_seconds
        )
    except NodeOffline as exc:
        raise await refuse(409, "node_offline", "the node is not connected") from exc
    except NodeReadOnly as exc:
        raise await refuse(
            409, "upgrade_required", "the node speaks an unsupported protocol"
        ) from exc
    except ActionTimeout as exc:
        raise await refuse(504, "timeout", "the node did not answer in time") from exc
    took = int((time.monotonic() - started) * 1000)
    if not result.get("ok"):
        err = result.get("error") or {}
        await tools._audit(
            row(ok=False, code=err.get("code", "error"), error=err.get("message"), duration_ms=took)
        )
        status = 422 if err.get("code") == "invalid_args" else 502
        raise ToolError(status, err.get("code", "error"), str(err.get("message") or "refused"))
    body = result.get("result") or {}
    output = body.get("output")
    size, excerpt = tools._excerpt(output)
    took = body.get("duration_ms", took)
    await tools._audit(row(ok=True, output_bytes=size, output_excerpt=excerpt, duration_ms=took))
    return output if isinstance(output, dict) else {"output": output}


def call_view(c: ToolCall, user_name: str | None = None) -> dict[str, Any]:
    return {
        "id": c.id,
        "user_id": str(c.user_id),
        "user": user_name,
        "node_id": str(c.node_id) if c.node_id else None,
        "node": c.node_name,
        "capability": c.capability,
        "input": c.input,
        "via": c.via,
        "ok": c.ok,
        "code": c.code,
        "error": c.error,
        "output_bytes": c.output_bytes,
        "output_excerpt": c.output_excerpt,
        "duration_ms": c.duration_ms,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }
