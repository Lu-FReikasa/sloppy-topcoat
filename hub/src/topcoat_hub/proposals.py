"""Proposals: the changes the agent may suggest and the user may confirm (E3-01b, D15, D43).

The agent never changes anything. After its diagnosis it may attach up to a few *proposals*: the
exact action (node, capability, input), the reason (evidence it saw), what it does, who notices and
the way back. The hub checks each one mechanically — the capability must be one the task's user may
run, the input must fit its schema, the evidence must be live and must mention what the input
names — then stores it, immutable (``content_hash``), with an expiry.

Execution is a different door: :meth:`ProposalService.execute` is called only from the signed-in
user's click (the REST route refuses API keys), runs ONE proposal once, re-checks owner, state,
expiry and hash, claims the row atomically (a second click finds it ``running``), sends the one
action to the node (which checks again) and records the outcome. There is no blanket approval, no
"remember my answer", and nothing that reads a proposal's text can confirm it.

Every statement filters by ``user_id`` (D20): another user's proposal is "not found".
"""

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jsonschema
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.agent_store import AgentStore
from topcoat_hub.agent_workshop import CHANGES as WORKSHOP_EDITS
from topcoat_hub.config import Settings
from topcoat_hub.devrepos import DevRepoStore
from topcoat_hub.models import AgentEvidence, AgentProposal, User
from topcoat_hub.project_events import SYSTEM
from topcoat_hub.project_mandates import active as mandate_active
from topcoat_hub.project_store import ProjectError, ProjectStore
from topcoat_hub.tools import Caller, ToolError, ToolService

log = logging.getLogger(__name__)

Publish = Callable[[str, dict[str, Any]], None]

MAX_TEXT = 600
# What a proposal's input may name is shown to the agent by a tool that lists or describes things
# (structure), never by one that returns text somebody else wrote (logs, pages, notes, files): a
# line in a log that says "restart prod-db" must not be able to put prod-db into a proposal.
STRUCTURAL = frozenset(
    {"docker.ps", "docker.inspect", "service.status", "service.failed", "compose.projects"}
)
FINAL = ("executed", "failed", "refused", "expired")

# What grounds a file edit of the workshop's scratch copy (D99). The structure rule above is for
# actions on machines; an edit names a file and carries text of the model's own, so its ground is a
# read of that file (or a look at the repository, for a new file) in this run. The person sees the
# change as a diff before the click, and the node checks it again when it runs.
WORKSHOP_READS = frozenset({"repo.read"})
WORKSHOP_LOOKS = frozenset({"repo.list", "repo.status", "repo.read", "repo.diff"})
PREVIEW_CHARS = 20_000
PREVIEW_WRITE_LINES = 80


def _norm_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/").removeprefix("./").lstrip("/")


def workshop_ground(
    cap: str, tool_input: dict[str, Any], live: list[str], evidence: dict[str, AgentEvidence]
) -> str | None:
    """``None`` when the cited live evidence grounds the file change, else why it does not."""
    repo = tool_input.get("repo")
    if not isinstance(repo, str) or not repo:
        return "it names no repository"
    sources = [(evidence[r].source or {}) for r in live]

    def seen(capabilities: frozenset[str], path: str | None = None) -> bool:
        for src in sources:
            inp = src.get("input") if isinstance(src.get("input"), dict) else {}
            if src.get("capability") not in capabilities or inp.get("repo") != repo:
                continue
            if path is None or _norm_path(inp.get("path")) == path:
                return True
        return False

    if cap == "repo.apply_patch":
        return None if seen(WORKSHOP_LOOKS) else f"no cited result looked at {repo}"
    path = _norm_path(tool_input.get("path"))
    if not path:
        return "it names no file"
    if seen(WORKSHOP_READS, path):
        return None
    if cap == "repo.write" and seen(WORKSHOP_LOOKS):
        return None  # a new file: the repository was looked at
    return f"no cited result is a read of {path}"


def workshop_preview(cap: str, tool_input: dict[str, Any]) -> str:
    """The change as the person reviews it before the click: what goes, what comes."""
    path = _norm_path(tool_input.get("path"))
    if cap == "repo.edit":
        old, new = str(tool_input.get("old", "")), str(tool_input.get("new", ""))
        lines = [f"--- {path}", f"+++ {path}"]
        lines += [f"-{x}" for x in old.splitlines()] + [f"+{x}" for x in new.splitlines()]
        text = "\n".join(lines)
    elif cap == "repo.write":
        body = str(tool_input.get("content", "")).splitlines()
        more = len(body) - PREVIEW_WRITE_LINES
        text = "\n".join(
            [f"+++ {path} (the whole file is replaced, or created; {len(body)} lines)"]
            + [f"+{x}" for x in body[:PREVIEW_WRITE_LINES]]
            + ([f"... {more} more lines"] if more > 0 else [])
        )
    else:
        text = str(tool_input.get("patch", ""))
    return text[:PREVIEW_CHARS]


# What a button's proposal says, per capability and language. The person is asked to confirm the
# exact action, so the four sentences are fixed text with only the names filled in.
_MANUAL: dict[str, dict[str, dict[str, str]]] = {
    "docker.restart": {
        "en": {
            "reason": "You asked for this restart in the inventory.",
            "effect": "Container {container} is stopped (it gets up to {timeout} s to stop "
            "cleanly) and started again. Its service is unreachable meanwhile.",
            "who_notices": "Everyone who uses {container}: connections to it are cut for a few "
            "seconds.",
            "way_back": "Nothing to undo: the container starts again by itself. If it does not "
            "come back, the inventory shows its state and its logs.",
        },
        "de": {
            "reason": "Du hast diesen Neustart im Inventar angefordert.",
            "effect": "Der Container {container} wird angehalten (bis zu {timeout} s zum sauberen "
            "Beenden) und neu gestartet. Sein Dienst ist solange nicht erreichbar.",
            "who_notices": "Alle, die {container} nutzen: Verbindungen brechen für ein paar "
            "Sekunden ab.",
            "way_back": "Es gibt nichts zurückzunehmen: der Container startet von selbst wieder. "
            "Kommt er nicht zurück, zeigt das Inventar seinen Zustand und seine Logs.",
        },
    },
    "compose.restart": {
        "en": {
            "reason": "You asked for this restart of the whole stack in the inventory.",
            "effect": "Every running container of stack {project} is restarted one after the "
            "other, the ones others depend on first (each gets up to {timeout} s to stop). "
            "Stopped containers stay stopped. The stack's services are unreachable meanwhile.",
            "who_notices": "Everyone who uses a service of {project}: connections are cut, for "
            "the whole stack one container after the other.",
            "way_back": "Nothing to undo: the containers start again by themselves. If the node "
            "stops half-way it says which container failed; the rest of the stack is then "
            "untouched.",
        },
        "de": {
            "reason": "Du hast diesen Neustart des ganzen Stacks im Inventar angefordert.",
            "effect": "Jeder laufende Container des Stacks {project} wird nacheinander neu "
            "gestartet, zuerst die, von denen andere abhängen (je bis zu {timeout} s zum "
            "Beenden). Gestoppte Container bleiben gestoppt. Die Dienste des Stacks sind "
            "solange nicht erreichbar.",
            "who_notices": "Alle, die einen Dienst von {project} nutzen: Verbindungen brechen ab, "
            "für den ganzen Stack Container für Container.",
            "way_back": "Es gibt nichts zurückzunehmen: die Container starten von selbst wieder. "
            "Bricht der Node in der Mitte ab, nennt er den Container, der gescheitert ist; der "
            "Rest des Stacks bleibt dann unberührt.",
        },
    },
    "service.restart": {
        "en": {
            "reason": "You asked for this restart in the inventory.",
            "effect": "The systemd unit {unit} is restarted. The service is unreachable while it "
            "restarts.",
            "who_notices": "Everyone who uses the service behind {unit}.",
            "way_back": "Nothing to undo: the unit starts again by itself. If it does not, its "
            "status and journal are in the chat or on the node page.",
        },
        "de": {
            "reason": "Du hast diesen Neustart im Inventar angefordert.",
            "effect": "Die systemd-Unit {unit} wird neu gestartet. Der Dienst ist beim Neustart "
            "kurz nicht erreichbar.",
            "who_notices": "Alle, die den Dienst hinter {unit} nutzen.",
            "way_back": "Es gibt nichts zurückzunehmen: die Unit startet von selbst wieder. Tut "
            "sie es nicht, stehen Status und Journal im Chat oder auf der Node-Seite.",
        },
    },
    "repo.push": {
        "en": {
            "reason": "You asked to send the workshop's change in {repo} to GitHub.",
            "effect": "The change shown below ({files} file(s)) is committed with your message and "
            "pushed to the branch {branch} of {repo}. The default branch is not touched, nothing "
            "is forced, nothing is merged.",
            "who_notices": "Everyone who watches the repository: a new or updated branch {branch} "
            "appears on GitHub.",
            "way_back": "Delete the branch {branch} on GitHub, or throw the change away in the "
            "workshop. The default branch never changed.",
        },
        "de": {
            "reason": "Du wolltest die Änderung der Werkstatt in {repo} nach GitHub schicken.",
            "effect": "Die unten gezeigte Änderung ({files} Datei(en)) wird mit deiner Nachricht "
            "committet und auf den Branch {branch} von {repo} gepusht. Der Standard-Branch bleibt "
            "unberührt, nichts wird erzwungen, nichts wird gemergt.",
            "who_notices": "Alle, die das Repository beobachten: auf GitHub erscheint ein neuer "
            "oder aktualisierter Branch {branch}.",
            "way_back": "Den Branch {branch} auf GitHub löschen oder die Änderung in der Werkstatt "
            "verwerfen. Der Standard-Branch hat sich nie geändert.",
        },
    },
}
MANUAL_CAPABILITIES = tuple(_MANUAL)


def manual_texts(
    capability: str,
    tool_input: dict[str, Any],
    language: str | None,
    extra: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """The four sentences of a manual proposal, in the user's language (English when unknown);
    ``None`` for a capability that has no button."""
    by_lang = _MANUAL.get(capability)
    if by_lang is None:
        return None
    texts = by_lang["de" if language == "de" else "en"]
    values = {
        "container": str(tool_input.get("container", "")),
        "project": str(tool_input.get("project", "")),
        "unit": str(tool_input.get("unit", "")),
        "timeout": str(tool_input.get("timeout_seconds") or 10),
        "repo": str(tool_input.get("repo", "")),
        "branch": str(tool_input.get("branch", "")),
        "files": "?",
        **(extra or {}),
    }
    return {k: v.format(**values) for k, v in texts.items()}


@dataclass
class MandatePush:
    """What came of a push under a mandate. ``stopped`` says why it did not happen: ``refusal`` (a
    check said no: nothing to retry), ``same_error`` (the same technical error twice), ``retries``
    (technical errors until the attempts were used up), ``mandate`` (revoked, expired, no push)."""

    pushed: bool
    attempts: int
    stopped: str = ""
    code: str = ""
    message: str = ""
    proposal: dict[str, Any] | None = None


MANDATE_ATTEMPTS = 3  # the first try and two more, for technical errors only
# the codes of an executed proposal that failed for a reason of the network or the node
TECHNICAL_CODES = ("unknown", "node_offline", "node_timeout", "timeout", "unavailable")


class ProposalError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def utcnow() -> datetime:
    return datetime.now(UTC)


def content_hash(p: AgentProposal) -> str:
    """SHA-256 over everything the user is shown and everything that will be sent. A proposal that
    changes after it was stored (a DB edit) no longer matches and is not run."""
    body = {
        "v": 1,
        "user": str(p.user_id),
        "task": str(p.task_id),
        "node": str(p.node_id),
        "capability": p.capability,
        "input": p.input,
        "reason": p.reason,
        "effect": p.effect,
        "who_notices": p.who_notices,
        "way_back": p.way_back,
        "evidence": p.evidence,
    }
    if p.preview:  # only when set, so a proposal stored before the column keeps its hash
        body["preview"] = p.preview
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _text(v: Any) -> str:
    return " ".join(v.split())[:MAX_TEXT] if isinstance(v, str) else ""


def _same_action(listed: str, given: str) -> bool:
    """Whether the name the model wrote is the action the hub lists. The names use dots between
    the parts (``project.changeset.propose``); the model sometimes writes an underscore there
    (``project.changeset_propose``), which is the same action and was refused for it (live,
    2026-11-06). Only the spelling differs: the action must still be one that is listed."""
    return listed == given or listed == given.replace("_", ".")


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _strings(v)]
    if isinstance(value, list):
        return [t for v in value for t in _strings(v)]
    return []


def view(p: AgentProposal) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "task_id": str(p.task_id) if p.task_id else None,
        "origin": p.origin,
        "node": p.node_name,
        "capability": p.capability,
        "input": p.input,
        "reason": p.reason,
        "effect": p.effect,
        "who_notices": p.who_notices,
        "way_back": p.way_back,
        "evidence": p.evidence,
        "preview": p.preview,
        "hash": p.content_hash,
        "status": p.status,
        "expires_at": p.expires_at.isoformat() if p.expires_at else None,
        "decided_at": p.decided_at.isoformat() if p.decided_at else None,
        "result": p.result,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


class ProposalService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        tools: ToolService,
        settings: Settings,
        publish: Publish,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.tools = tools
        self.settings = settings
        self.publish = publish
        # the registered repositories (set by main.py); only a push proposal needs them
        self.repos: DevRepoStore | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.proposals_enabled

    def _announce(self, p: AgentProposal) -> None:
        self.publish(
            "agent_proposal",
            {
                "id": str(p.id),
                "task_id": str(p.task_id) if p.task_id else None,
                "status": p.status,
                "_user": str(p.user_id),
            },
        )

    # --- the agent proposes ---------------------------------------------------------------

    async def create_from_finish(
        self,
        caller: Caller,
        task_id: uuid.UUID,
        raw: list[dict[str, Any]],
        evidence_tags: dict[str, str],
    ) -> list[str]:
        """Check and store what the model proposed. Returns notes for the diagnosis (what was
        dropped and why). Never raises: a bad proposal is dropped, the diagnosis stays."""
        if not self.enabled or not raw:
            return []
        notes: list[str] = []
        try:
            proposable = await self.tools.proposable_tools(caller)
            async with self.sessionmaker() as db:
                evidence = {
                    e.ref: e
                    for e in await db.scalars(
                        select(AgentEvidence).where(
                            AgentEvidence.task_id == task_id,
                            AgentEvidence.user_id == caller.user_id,
                        )
                    )
                }
                # a new diagnosis replaces what an earlier run of this task had put forward
                await db.execute(
                    update(AgentProposal)
                    .where(
                        AgentProposal.task_id == task_id,
                        AgentProposal.user_id == caller.user_id,
                        AgentProposal.status == "pending",
                    )
                    .values(status="expired")
                )
                made = 0
                for item in raw[: self.settings.proposals_max_per_task]:
                    p, why = self._check(caller, task_id, item, proposable, evidence, evidence_tags)
                    if p is None:
                        notes.append(f"a proposal was dropped: {why}")
                        continue
                    db.add(p)
                    made += 1
                if len(raw) > self.settings.proposals_max_per_task:
                    notes.append(
                        f"only {self.settings.proposals_max_per_task} proposals are kept per task"
                    )
                await db.commit()
                if made:
                    for p in await db.scalars(
                        select(AgentProposal).where(
                            AgentProposal.task_id == task_id,
                            AgentProposal.user_id == caller.user_id,
                            AgentProposal.status == "pending",
                        )
                    ):
                        self._announce(p)
        except Exception:
            log.exception("agent: could not store the proposals of task %s", task_id)
            notes.append("the proposals could not be stored")
        return notes

    def _check(
        self,
        caller: Caller,
        task_id: uuid.UUID,
        item: dict[str, Any],
        proposable: list[dict[str, Any]],
        evidence: dict[str, AgentEvidence],
        tags: dict[str, str],
    ) -> tuple[AgentProposal | None, str]:
        node_name, cap = _text(item.get("node")), _text(item.get("capability"))
        tool = next(
            (
                t
                for t in proposable
                if _same_action(t["capability"], cap) and t["node"].lower() == node_name.lower()
            ),
            None,
        )
        if tool is None:
            return None, f"{cap or '?'} on {node_name or '?'} is not an action you may confirm"
        tool_input = item.get("input")
        if not isinstance(tool_input, dict):
            return None, f"{cap}: the input is not an object"
        try:
            jsonschema.Draft202012Validator(tool["input_schema"]).validate(tool_input)
        except jsonschema.ValidationError as exc:
            return None, f"{cap}: the input does not fit ({exc.message[:120]})"
        fields = {k: _text(item.get(k)) for k in ("reason", "effect", "who_notices", "way_back")}
        missing = [k for k, v in fields.items() if not v]
        if missing:
            return None, f"{cap}: missing {', '.join(missing)}"
        refs_in = item.get("evidence") if isinstance(item.get("evidence"), list) else []
        refs = [r for r in dict.fromkeys(r for r in refs_in if isinstance(r, str)) if r in evidence]
        live = [r for r in refs if tags.get(r) == "live"]
        if not live and cap in WORKSHOP_EDITS:
            # a file change rests on a read of the file, which the record of the run shows: the hub
            # takes the live results of that repository itself when the model cited none (seen
            # live: it proposed the change right and left "evidence" empty)
            repo = tool_input.get("repo")
            live = [
                ref
                for ref, e in evidence.items()
                if tags.get(ref) == "live"
                and (e.source or {}).get("capability") in WORKSHOP_LOOKS
                and isinstance((e.source or {}).get("input"), dict)
                and e.source["input"].get("repo") == repo
            ]
        if not live:
            return None, f"{cap}: it cites no live evidence"
        preview = ""
        if cap in WORKSHOP_EDITS:
            # a file change of the scratch copy: grounded in a read of the file (D99), shown as diff
            why_not = workshop_ground(cap, tool_input, live, evidence)
            if why_not:
                return None, f"{cap}: {why_not}"
            preview = workshop_preview(cap, tool_input)
        else:
            # What the action names (a container, a unit) must have been shown by a tool that lists
            # or describes things, and that result must be among the evidence cited.
            haystack = "\n".join(
                evidence[r].excerpt or ""
                for r in live
                if (evidence[r].source or {}).get("capability") in STRUCTURAL
            )
            named = [s for s in _strings(tool_input) if s]
            absent = [
                s for s in named if not re.search(rf"(?<![\w.-]){re.escape(s)}(?![\w.-])", haystack)
            ]
            if absent:
                return None, f"{cap}: no cited listing or status shows {absent[0]!r}"
        p = AgentProposal(
            id=uuid.uuid4(),
            task_id=task_id,
            user_id=caller.user_id,
            node_id=uuid.UUID(tool["node_id"]),
            node_name=tool["node"],
            capability=cap,
            input=tool_input,
            evidence=live,
            preview=preview,
            status="pending",
            expires_at=utcnow() + timedelta(seconds=self.settings.proposal_ttl_seconds),
            **fields,
        )
        p.content_hash = content_hash(p)
        return p, ""

    # --- the person proposes (the inventory's buttons) --------------------------------------

    async def create_manual(
        self,
        caller: Caller,
        node_id: uuid.UUID,
        capability: str,
        tool_input: dict[str, Any],
    ) -> dict[str, Any]:
        """A proposal the person asked for themselves (a "Restart" button in the inventory). It goes
        the same way as the agent's: stored immutable with its hash and an expiry, shown with its
        exact effect, and run only by a second, separate click on :meth:`execute`. The texts are the
        hub's own (templates in the user's language), so nothing a log or a page says is in them.
        Whoever may not run the capability on that node learns nothing about the node (404)."""
        if not self.enabled:
            raise ProposalError(403, "proposals_off", "proposals are switched off on this hub")
        if caller.via != "gui":
            raise ProposalError(403, "gui_only", "only the signed-in user asks for a change")
        tool = next(
            (
                t
                for t in await self.tools.proposable_tools(caller)
                if t["capability"] == capability and t["node_id"] == str(node_id)
            ),
            None,
        )
        if tool is None:
            raise ProposalError(
                404, "not_found", f"{capability} is not an action you may run on that node"
            )
        try:
            jsonschema.Draft202012Validator(tool["input_schema"]).validate(tool_input)
        except jsonschema.ValidationError as exc:
            raise ProposalError(
                422, "invalid_input", f"the input does not fit ({exc.message[:120]})"
            ) from exc
        async with self.sessionmaker() as db:
            user = await db.get(User, caller.user_id)
            texts = manual_texts(capability, tool_input, user.language if user else None)
            if texts is None:
                raise ProposalError(422, "no_manual_action", f"{capability} has no button")
            # the same restart asked for twice is one proposal: a second click on the button
            # while the first card is open must not stack a duplicate
            await db.execute(
                update(AgentProposal)
                .where(
                    AgentProposal.user_id == caller.user_id,
                    AgentProposal.origin == "manual",
                    AgentProposal.status == "pending",
                    AgentProposal.node_id == node_id,
                    AgentProposal.capability == capability,
                    AgentProposal.input == tool_input,
                )
                .values(status="expired")
            )
            p = AgentProposal(
                id=uuid.uuid4(),
                task_id=None,
                origin="manual",
                user_id=caller.user_id,
                node_id=node_id,
                node_name=tool["node"],
                capability=capability,
                input=tool_input,
                evidence=[],
                status="pending",
                expires_at=utcnow() + timedelta(seconds=self.settings.proposal_ttl_seconds),
                **texts,
            )
            p.content_hash = content_hash(p)
            db.add(p)
            await db.commit()
            self._announce(p)
            return view(p)

    async def create_push(
        self, caller: Caller, ref: str, message: str, node_id: uuid.UUID | None = None
    ) -> dict[str, Any]:
        """The proposal to push the workshop's change of one repository (D52): the hub reads the
        diff from the dev node itself, refuses what must not leave (nothing changed, cut off,
        secrets found, not on a ``topcoat/`` branch) and stores the diff with the proposal. The
        input carries the diff's hash, so the node commits exactly what the person saw. Pushing
        is the second click, on :meth:`execute`, like every other change (D15)."""
        if not self.enabled:
            raise ProposalError(403, "proposals_off", "proposals are switched off on this hub")
        if caller.via != "gui" or not caller.admin:
            raise ProposalError(403, "gui_only", "only the signed-in admin sends a change out")
        if self.repos is None:
            raise ProposalError(503, "unavailable", "not ready")
        msg = message.strip()
        if not 3 <= len(msg) <= 2000 or "\x00" in msg:
            raise ProposalError(
                422, "invalid_message", "the commit message needs 3 to 2000 characters"
            )
        row = await self.repos.find(caller.user_id, ref)
        if row is None:
            raise ProposalError(404, "unknown_repo", "no such repository")
        if row.mode != "test-branch":
            raise ProposalError(
                403,
                "repo_read_only",
                f"{row.name} is read only: change its mode to test-branch first",
            )
        tool = next(
            (
                t
                for t in await self.tools.proposable_tools(caller)
                if t["capability"] == "repo.push"
                and (node_id is None or t["node_id"] == str(node_id))
            ),
            None,
        )
        if tool is None:
            raise ProposalError(404, "no_dev_node", "no connected dev node can push")
        node = uuid.UUID(tool["node_id"])
        try:
            run = await self.tools.invoke(caller, node, "repo.diff", {"repo": row.name})
        except ToolError as exc:
            raise ProposalError(exc.status, exc.code, exc.message) from exc
        out = run.get("output") if run.get("ok") else None
        if not isinstance(out, dict):
            err = (run.get("error") or {}).get("message", "the node did not answer")
            raise ProposalError(502, "diff_failed", f"could not read the change: {err}")
        branch, digest = str(out.get("branch", "")), str(out.get("sha256", ""))
        for refusal in (
            (out.get("empty") is True, "no_change", "there is no change to send"),
            (
                out.get("truncated") is True,
                "too_big",
                "the change is too large to review here: make it smaller",
            ),
            (
                int(out.get("masked") or 0) > 0,
                "secrets_found",
                "the change contains something that looks like a secret: not sent",
            ),
            (
                not branch.startswith("topcoat/"),
                "not_on_work_branch",
                "the workshop is not on a topcoat/ branch: start one first",
            ),
            (len(digest) != 64, "node_too_old", "the node does not report a hash of the change"),
        ):
            if refusal[0]:
                raise ProposalError(422, refusal[1], refusal[2])
        tool_input = {"repo": row.name, "message": msg, "branch": branch, "diff_sha256": digest}
        try:
            jsonschema.Draft202012Validator(tool["input_schema"]).validate(tool_input)
        except jsonschema.ValidationError as exc:
            raise ProposalError(
                422, "invalid_input", f"the input does not fit ({exc.message[:120]})"
            ) from exc
        files = out.get("files")
        async with self.sessionmaker() as db:
            user = await db.get(User, caller.user_id)
            texts = manual_texts(
                "repo.push",
                tool_input,
                user.language if user else None,
                {"files": str(len(files) if isinstance(files, list) else "?")},
            )
            if texts is None:  # cannot happen: the table above has repo.push
                raise ProposalError(500, "no_manual_action", "repo.push has no texts")
            await db.execute(  # one open push per node: a newer diff replaces the older card
                update(AgentProposal)
                .where(
                    AgentProposal.user_id == caller.user_id,
                    AgentProposal.origin == "manual",
                    AgentProposal.status == "pending",
                    AgentProposal.node_id == node,
                    AgentProposal.capability == "repo.push",
                )
                .values(status="expired")
            )
            p = AgentProposal(
                id=uuid.uuid4(),
                task_id=None,
                origin="manual",
                user_id=caller.user_id,
                node_id=node,
                node_name=tool["node"],
                capability="repo.push",
                input=tool_input,
                preview=str(out.get("diff", "")),
                evidence=[],
                status="pending",
                expires_at=utcnow() + timedelta(seconds=self.settings.proposal_ttl_seconds),
                **texts,
            )
            p.content_hash = content_hash(p)
            db.add(p)
            await db.commit()
            self._announce(p)
            return view(p)

    # --- push under a mandate ---------------------------------------------------------------

    async def push_under_mandate(
        self, user_id: uuid.UUID, mandate_id: uuid.UUID, message: str
    ) -> MandatePush:
        """The second way to a push (PPH-05, 05d): no click now, because the person's click made a
        mandate earlier. It is the same path as the click: the proposal is made by
        :meth:`create_push` (diff read from the dev node, refused when empty, cut off, with a secret
        or off a ``topcoat/`` branch) and run by :meth:`execute` (hash, state, expiry, owner, the
        node's own checks), so nothing the click path checks is skipped. On top, every attempt
        re-checks the mandate itself: owned by this person, not revoked, not expired, push allowed,
        the repository and the branch of the diff are the mandated ones. Only technical errors are
        tried again, and not when the same error comes twice; a refusal stops and the person is
        asked. The ordinary click path is not touched."""
        store = ProjectStore(self.sessionmaker, SYSTEM)
        last_code = ""
        for attempt in range(1, MANDATE_ATTEMPTS + 1):
            out = await self._push_once(store, user_id, mandate_id, message)
            if out.pushed or out.stopped:
                out.attempts = attempt
                return out
            out.attempts = attempt
            if out.code == last_code:
                out.stopped = "same_error"
                return out
            last_code = out.code
            if attempt == MANDATE_ATTEMPTS:
                out.stopped = "retries"
                return out
        raise AssertionError("unreachable")  # pragma: no cover

    async def _push_once(
        self, store: ProjectStore, user_id: uuid.UUID, mandate_id: uuid.UUID, message: str
    ) -> MandatePush:
        try:
            mandate = await store.get_mandate(user_id, mandate_id)
        except ProjectError:
            return MandatePush(False, 1, "mandate", "no_mandate", "there is no such mandate")
        if not mandate_active(mandate) or not mandate.push:
            return MandatePush(
                False,
                1,
                "mandate",
                "mandate_ended",
                "the mandate is revoked, expired or has no push",
            )
        async with self.sessionmaker() as db:
            user = await db.get(User, user_id)
        if user is None or user.role != "admin":
            return MandatePush(False, 1, "refusal", "not_admin", "only an admin's mandate pushes")
        caller = Caller(user_id, True, "gui")
        try:
            proposal = await self.create_push(caller, mandate.repo, message)
        except ProposalError as exc:
            return self._failed(exc.status, exc.code, exc.message)
        pid = uuid.UUID(proposal["id"])
        given = proposal["input"]
        if given.get("repo") != mandate.repo or given.get("branch") != mandate.branch:
            await self.refuse(user_id, pid)
            return MandatePush(
                False, 1, "refusal", "outside_mandate",
                f"the change is on {given.get('branch')}, the mandate covers {mandate.branch}",
                proposal,
            )  # fmt: skip
        try:  # revoked while the diff was read: it does not go
            mandate = await store.get_mandate(user_id, mandate_id)
        except ProjectError:
            mandate = None
        if mandate is None or not mandate_active(mandate):
            await self.refuse(user_id, pid)
            return MandatePush(False, 1, "mandate", "mandate_ended", "the mandate ended", proposal)
        try:
            done = await self.execute(caller, pid)
        except ProposalError as exc:
            return self._failed(exc.status, exc.code, exc.message, proposal)
        if done["status"] != "executed":
            error = (done.get("result") or {}).get("error") or {}
            code = str(error.get("code") or "failed")
            result = self._failed(
                503 if code in TECHNICAL_CODES else 422, code, str(error.get("message") or ""), done
            )
            return result
        await store.use_mandate(
            user_id,
            mandate_id,
            what="push",
            payload={"proposal": proposal["id"], "diff_sha256": given.get("diff_sha256")},
        )
        return MandatePush(True, 1, proposal=done)

    @staticmethod
    def _failed(
        status: int, code: str, message: str, proposal: dict[str, Any] | None = None
    ) -> MandatePush:
        """A technical error (status 5xx, a known network code) may be tried again; everything
        else is a refusal that stops."""
        technical = status >= 500 or code in TECHNICAL_CODES
        return MandatePush(False, 1, "" if technical else "refusal", code, message, proposal)

    # --- reading --------------------------------------------------------------------------

    async def for_task(self, user_id: uuid.UUID, task_id: uuid.UUID) -> list[dict[str, Any]]:
        async with self.sessionmaker() as db:
            await self._expire(db, user_id)
            rows = await db.scalars(
                select(AgentProposal)
                .where(AgentProposal.task_id == task_id, AgentProposal.user_id == user_id)
                .order_by(AgentProposal.created_at, AgentProposal.id)
            )
            return [view(p) for p in rows]

    async def pending(self, user_id: uuid.UUID) -> list[dict[str, Any]]:
        """Every proposal of the agent that still waits for this user's click (newest first). A
        manual one is not listed: the person has the card open in front of them."""
        async with self.sessionmaker() as db:
            await self._expire(db, user_id)
            rows = await db.scalars(
                select(AgentProposal)
                .where(
                    AgentProposal.user_id == user_id,
                    AgentProposal.status == "pending",
                    AgentProposal.origin == "agent",
                )
                .order_by(AgentProposal.created_at.desc(), AgentProposal.id)
            )
            return [view(p) for p in rows]

    async def get(self, user_id: uuid.UUID, pid: uuid.UUID) -> dict[str, Any]:
        """One proposal of this user, as it is now."""
        async with self.sessionmaker() as db:
            await self._expire(db, user_id)
            return view(await self._get(db, user_id, pid))

    async def _expire(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        await db.execute(
            update(AgentProposal)
            .where(
                AgentProposal.user_id == user_id,
                AgentProposal.status == "pending",
                AgentProposal.expires_at <= utcnow(),
            )
            .values(status="expired")
        )
        await db.commit()

    async def _get(self, db: AsyncSession, user_id: uuid.UUID, pid: uuid.UUID) -> AgentProposal:
        p = await db.scalar(
            select(AgentProposal).where(AgentProposal.id == pid, AgentProposal.user_id == user_id)
        )
        if p is None:
            raise ProposalError(404, "not_found", "unknown proposal")
        return p

    # --- the user decides -----------------------------------------------------------------

    async def refuse(self, user_id: uuid.UUID, pid: uuid.UUID) -> dict[str, Any]:
        async with self.sessionmaker() as db:
            await self._expire(db, user_id)
            res = await db.execute(
                update(AgentProposal)
                .where(
                    AgentProposal.id == pid,
                    AgentProposal.user_id == user_id,
                    AgentProposal.status == "pending",
                )
                .values(status="refused", decided_at=utcnow(), decided_by=user_id)
            )
            await db.commit()
            p = await self._get(db, user_id, pid)
            if res.rowcount == 0:
                raise ProposalError(409, "not_pending", f"this proposal is {p.status}")
            await self._note(db, p, f"Proposal refused: {p.capability} on {p.node_name}")
            self._announce(p)
            return view(p)

    async def execute(self, caller: Caller, pid: uuid.UUID) -> dict[str, Any]:
        """The one click. ``caller`` is the signed-in GUI user (the route refuses an API key)."""
        if not self.enabled:
            raise ProposalError(403, "proposals_off", "proposals are switched off on this hub")
        if caller.via != "gui":
            raise ProposalError(403, "gui_only", "only the signed-in user confirms a proposal")
        async with self.sessionmaker() as db:
            await self._expire(db, caller.user_id)
            p = await self._get(db, caller.user_id, pid)
            if p.status != "pending":
                raise ProposalError(409, "not_pending", f"this proposal is {p.status}")
            if content_hash(p) != p.content_hash or p.node_id is None:
                await db.execute(
                    update(AgentProposal).where(AgentProposal.id == pid).values(status="refused")
                )
                await db.commit()
                raise ProposalError(
                    409, "content_changed", "the proposal does not match its hash: not run"
                )
            # claim: exactly one click gets past this statement
            claimed = await db.execute(
                update(AgentProposal)
                .where(
                    AgentProposal.id == pid,
                    AgentProposal.user_id == caller.user_id,
                    AgentProposal.status == "pending",
                    AgentProposal.expires_at > utcnow(),
                )
                .values(status="running", decided_at=utcnow(), decided_by=caller.user_id)
            )
            await db.commit()
            if claimed.rowcount == 0:
                raise ProposalError(409, "not_pending", "this proposal was already decided")
            node_id, cap, tool_input = p.node_id, p.capability, dict(p.input)
            task_id, digest = p.task_id, p.content_hash
        self.publish(
            "agent_proposal",
            {
                "id": str(pid),
                "task_id": str(task_id) if task_id else None,
                "status": "running",
                "_user": str(caller.user_id),
            },
        )
        outcome: dict[str, Any]
        status = "executed"
        call_id: int | None = None
        try:
            run = await self.tools.execute_confirmed(
                caller, node_id, cap, tool_input, pid, digest, task_id
            )
            call_id = run.get("call_id")
            if run.get("ok"):
                outcome = {"ok": True, "output": run.get("output"), "at": run.get("at")}
            else:
                status = "failed"
                outcome = {"ok": False, "error": run.get("error")}
        except ToolError as exc:
            status, call_id = "failed", exc.call_id
            outcome = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
        except Exception:  # the click happened: say that the outcome is unknown, never "not run"
            log.exception("proposal %s: executing failed unexpectedly", pid)
            status = "failed"
            outcome = {
                "ok": False,
                "error": {"code": "unknown", "message": "the outcome is unknown: check the node"},
            }
        async with self.sessionmaker() as db:
            await db.execute(
                update(AgentProposal)
                .where(AgentProposal.id == pid, AgentProposal.user_id == caller.user_id)
                .values(status=status, result=outcome, tool_call_id=call_id)
            )
            await db.commit()
            p = await self._get(db, caller.user_id, pid)
            verb = "executed" if status == "executed" else "failed"
            await self._note(db, p, f"Proposal {verb}: {p.capability} on {p.node_name}")
            self._announce(p)
            return view(p)

    async def _note(self, db: AsyncSession, p: AgentProposal, text: str) -> None:
        """The outcome becomes a line in the task's transcript (the steps of a finished task are
        still shown)."""
        if p.task_id is None:  # a manual proposal has no transcript; the audit log has the run
            return
        try:
            store = AgentStore(db, p.user_id)
            task = await store.get(p.task_id)
            if task is not None:
                await store.add_step(task, "note", {"text": text, "proposal": str(p.id)})
        except Exception:
            log.exception("could not note the outcome of proposal %s", p.id)
