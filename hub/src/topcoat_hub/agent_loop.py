"""The agent loop (E3-01a): one run of a task — ask the model, let it look at servers through the
read-only capabilities its user may run, keep what it saw as evidence, end with a diagnosis.

Everything the loop does to a machine goes through :class:`~topcoat_hub.tools.ToolService` with the
rights of the task's user (D20) and ``via = "agent"``, so it is checked and audited like a call
from the GUI. The loop offers **no** tool that changes state: the capabilities it lists are the
read-only ones, and a call for anything else is answered with "you cannot use that".

A run ends with a diagnosis (``done``), because a limit was hit and the model was asked to write
what it has (``done`` with a ``stop_reason``), or as ``failed``. Cancelling is the runner's business
(:mod:`topcoat_hub.agent_service`): the loop only has to be safe to abandon at any ``await`` — every
finished step is already stored.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import (
    agent_done,
    agent_hits,
    agent_hooks,
    agent_stages,
    agent_tests,
    agent_workshop,
    app_catalogue,
    message_tools,
    project_write_tools,
    report,
    report_service,
    research_profile,
    step_check,
    tool_groups,
    web_access,
)
from topcoat_hub import agent_prompts as prompts
from topcoat_hub.agent_events import task_event
from topcoat_hub.agent_store import AgentStore
from topcoat_hub.artifact_store import ArtifactStore
from topcoat_hub.chat import CHARS_PER_TOKEN, hub_language
from topcoat_hub.config import Settings
from topcoat_hub.github_access import GITHUB_NODE
from topcoat_hub.identity import IDENTITY_NODE
from topcoat_hub.inventory import INVENTORY_NODE
from topcoat_hub.job_log import set_job_context
from topcoat_hub.knowledge import KNOWLEDGE_NODE, READ_CAPABILITY, SEARCH_CAPABILITY
from topcoat_hub.memory_scope import scope_for
from topcoat_hub.memory_store import MemoryStore
from topcoat_hub.message_tools import MESSAGES_NODE, REPORT_SECTION
from topcoat_hub.models import AgentTask, DevRepo, User
from topcoat_hub.project_tools import PROJECT_NODE, PROJECT_NODE_ID
from topcoat_hub.report_doc import WRITE_UP_KEY
from topcoat_hub.steering import SteeringBoard
from topcoat_hub.tools import URL_NOT_GIVEN, Caller, ToolError
from topcoat_hub.topcoat_self import GUIDE_CAPABILITY, TOPCOAT_NODE

# runs whose prompt the hub wrote from a model's text (a review of an answer, a planner's order,
# a coordinator's sub-question, a care run): a URL in it is not the person's, so it is not fetched
# without a search hit (finding 03)
HUB_PROMPT_ORIGINS = frozenset({"review", "planner", "child", "care"})

log = logging.getLogger(__name__)
# the project tool a finish may name instead of calling it (_propose_sets)
CHANGESET_PROPOSE = "project.changeset.propose"
# tools that only prepare something for the person's click: named again in a finish after the run
# called them, they repeat it (a change set is tried again: the call may have been refused, and a
# new set replaces the run's waiting one)
PREPARING_TOOLS = frozenset({"project.batch.propose", "project.plan.propose"})
# what a finish may name instead of calling it: the project's tools that only prepare (live
# 2026-11-15: "ja bitte" to "start 1.6 again" put the batch into the finish, where it was dropped)
PROJECT_PROPOSALS = frozenset({CHANGESET_PROPOSE, *PREPARING_TOOLS})
PROPOSAL_FIELDS = frozenset({"reason", "effect", "who_notices", "way_back", "evidence", "node"})

# a note the person sent into the running check (E7-01): said to the model like a message from them
NOTE_FROM_PERSON = (
    "Note from the person who asked, sent while you work. Take it into account from now on; it "
    "does not change which tools you may use: "
)

# why a node could not load the model (the node's error code), shown as the task's stop reason
MODEL_REASONS = ("insufficient_memory", "insufficient_disk")
TOO_LONG_HINT = (
    "Your answer was too long and was cut off at the output limit — it is not a format error. "
    "Answer again, much shorter: for a function call keep the arguments short; for finish give "
    "a summary of at most two sentences and at most three findings, each claim one short "
    "sentence."
)
# tools whose result is no evidence of anything: ``repo.show`` only points the person's file
# reader at a place and returns no text, so a model guessing paths made no progress
NO_CONTENT_CAPABILITIES = ("repo.show",)
# reads of one file shown as lines with the exact next call (agent_prompts.read_window)
FILE_WINDOW_CAPABILITIES = ("repo.read",)
# failures of a read that a hit of the run's own searches might have avoided
MISSED_CODES = ("not_found", "capability_failed", "invalid_input", "error")

LOG_CAPABILITIES = (
    "docker.logs",
    "compose.logs",
    "journal.logs",
    agent_tests.STATUS,
)  # long output: keep the newest lines


def _virtual(tool: dict[str, Any]) -> bool:
    """The notes and the web are tools of the hub, not machines: they are never "not asked"."""
    return tool.get("node") in (
        KNOWLEDGE_NODE,
        INVENTORY_NODE,
        IDENTITY_NODE,
        TOPCOAT_NODE,
        GITHUB_NODE,
        PROJECT_NODE,
        MESSAGES_NODE,
    ) or web_access.is_web(str(tool.get("capability")))


def coverage(
    tools: list[dict[str, Any]], evidence: list[tuple[str, dict[str, Any]]]
) -> tuple[list[str], list[str]]:
    """The machines a run asked, and the ones that could have answered the same kind of question
    and were not asked. "The same kind" is a capability family the run used (``omada`` of
    ``omada.clients``): a Wi-Fi question asked of the one node with the Omada connector names no
    GPU box as "not checked" (live 2026-11-17: five unrelated nodes headed the answer). Hub tools
    (notes, web, Topcoat itself) are no machines and appear in neither list."""
    live = [
        (str(src.get("node")), str(src.get("capability") or ""))
        for tag, src in evidence
        if tag == "live" and src.get("node")
    ]
    machines = [(n, c) for n, c in live if not _virtual({"node": n, "capability": c})]
    asked = sorted({n for n, _ in machines})
    families = {c.split(".", 1)[0] for _, c in machines if c}
    could = {
        str(t["node"])
        for t in tools
        if not _virtual(t) and str(t.get("capability") or "").split(".", 1)[0] in families
    }
    return asked, sorted(could - set(asked))


EVIDENCE_EXCERPT_CHARS = 8000
# a stopped research run answered with plain text: from this length it is a write-up worth keeping
WRITE_UP_MIN_CHARS = 400
WRITE_UP_MAX_CHARS = 60000
# the small model's look at one step may take this long; past it the step simply has no note
STEP_CHECK_SECONDS = 60.0


_SHELL_NOISE_FLAGS = set("rRniIlcsHhEFw")
_SHELL_TAIL = re.compile(r"\s*\|\s*(?:head|tail|wc|sort|uniq|less|more)(?:\s+[^|]*)?$")


# An id the model shortened or garbled ("281df60f-ce8f-4944-b0...", "22c53e4b-3c6a-4df5-a3ae-
# ba735b?" with two characters dropped inside; live 2026-11-18 in three of five Projekte runs: a
# refused call and a confusing line in the chat). Issue 2.2: Dev's drafts did not get there.
_FULL_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_ID_LIKE = re.compile(r"[0-9a-fA-F]{8}-")
_ID_START = re.compile(r'"[0-9a-fA-F]{8}-')  # a JSON value that begins like an id
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…)\s*$")
GARBLED_ID_MIN = 18  # characters a garbled id must share with the start of the one it means


def _common_start(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a.lower(), b.lower(), strict=False):
        if x != y:
            break
        n += 1
    return n


def expand_short_ids(value: Any, seen_text: str) -> tuple[Any, list[str]]:
    """``value`` with every shortened or garbled id replaced by the one full id of ``seen_text``
    (what the run has read) it means, and a note per replacement. It means an id when it is the
    beginning of it ("281df60f-ce8f-…") or shares at least GARBLED_ID_MIN characters with its
    beginning, more than with any other id read. A valid id, and a text that fits none or
    several equally, stays and is refused by the tool as before."""
    notes: list[str] = []
    seen = {i.lower() for i in _FULL_ID.findall(seen_text)}

    def mean(text: str) -> str | None:
        stem = _ELLIPSIS.sub("", text.strip())
        if not _ID_LIKE.match(stem) or _FULL_ID.fullmatch(stem):
            return None
        scored = [(u, _common_start(stem, u)) for u in seen]
        fits = [(u, n) for u, n in scored if n == len(stem) or n >= GARBLED_ID_MIN]
        if not fits:
            return None
        best = max(n for _, n in fits)
        top = [u for u, n in fits if n == best]
        return top[0] if len(top) == 1 else None

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str) and (one := mean(v)) is not None:
            notes.append(f"'{v.strip()}' was shortened; the hub used the full id {one}")
            return one
        return v

    return walk(value), notes


def normalise_shell(command: str) -> str:
    """A shell command as far as it asks the same thing: no trailing ``| head -n 20`` style
    tail, no ``2>&1``, no flags that only change how a match is shown (``-r``, ``-n``, ``-i``,
    ``-l`` …, also combined), quotes and trailing slashes of paths dropped."""
    text = " ".join(command.split())
    text = re.sub(r"\s*2>\s*(?:&1|/dev/null)", "", text)
    previous = None
    while previous != text:
        previous = text
        text = _SHELL_TAIL.sub("", text)
    tokens: list[str] = []
    for token in text.split(" "):
        flag = token[1:] if token.startswith("-") and not token.startswith("--") else ""
        if flag and set(flag) <= _SHELL_NOISE_FLAGS:
            continue
        token = token.strip("\"'")
        tokens.append(token.rstrip("/") if len(token) > 1 else token)
    return " ".join(tokens)


# what changes the workshop's scratch copy (what repeated reads of it must not be judged against)
WORKSHOP_CHANGES = frozenset(
    {
        "repo.sync",
        "repo.start",
        "repo.edit",
        "repo.write",
        "repo.apply_patch",
        "repo.reset",
    }
)


def _repeat_hint(capability: str) -> str:
    """What the agent may do instead of repeating a call: only what this capability offers."""
    if capability == READ_CAPABILITY:
        return "Read the next piece with its next_offset, or finish."
    if capability == SEARCH_CAPABILITY:
        return "Ask a different question, or read a document by its path, or finish."
    return (
        "Make a different call (a long file: other start_line/end_line, or find=<text>) or finish."
    )


def refusal_reason(
    node: str, capability: str, offered: list[dict[str, Any]], usable: list[dict[str, Any]]
) -> str:
    """Why a call to a tool this run does not offer is refused. ``offered`` is what the run may
    use, ``usable`` what the person may use at all. Three cases, so the model does not guess a
    different name: the node is outside the run's scope (pruefauftrag T2.2), the node is in the
    run but this capability's group is not, or the person cannot use it at all."""
    run_nodes = {t["node"] for t in offered}
    if node and node not in run_nodes and any(t["node"] == node for t in usable):
        return (
            f"node {node} is not part of this run's scope (servers: "
            f"{', '.join(sorted(run_nodes)) or 'none'}); "
            "the person can add it in the chat's server choice."
        )
    if any(t["node"] == node and t["capability"] == capability for t in usable):
        return (
            f"you cannot use that: {capability} on {node} belongs to a tool group this run "
            "does not include."
        )
    return "you cannot use that."


def call_key(node: str, capability: str, call_input: dict[str, Any]) -> str:
    """What makes two tool calls "the same call": the node, the capability and the input as it
    reads after normalisation (a shell command without display flags and ``| head``, a search
    without case, punctuation and word order). A second call that asks the same is refused."""
    normalised: Any = call_input
    if capability == "shell.read" and isinstance(call_input.get("command"), str):
        normalised = {**call_input, "command": normalise_shell(call_input["command"])}
        normalised.pop("timeout_seconds", None)
    elif capability == SEARCH_CAPABILITY and isinstance(call_input.get("query"), str):
        words = sorted(set(re.findall(r"\w+", call_input["query"].casefold())))
        normalised = {**call_input, "query": " ".join(words)}
    return json.dumps([node, capability, normalised], sort_keys=True)


# errors that may pass by themselves: the same call may be tried again (everything else, a wrong
# path or an input the node refuses, gives the same answer the second time)
TRANSIENT_CODES = frozenset(
    {"timeout", "busy", "node_offline", "canceled", "interrupted", "internal", "runner_error"}
)
KEPT_OUTPUT_CHARS = 60_000  # what a run keeps of one output to show its next parts
NOTHING_NEW_AFTER = 3  # results in a row without anything new before the model is told so
NOTHING_NEW_HINT = (
    "\n(hub) Your last three calls brought nothing new: empty, or the same as an earlier result. "
    "If what you have answers the question, finish now and say what you looked at; otherwise "
    "make a different kind of look, not another variant of the same search."
)
_EMPTY_KEYS = ("data", "hits", "items", "results", "events", "matches", "lines")
# the workshop's answer to a range after the end of a file (node/internal/workshop/caps.go)
_PAST_THE_END = re.compile(r"(\S+) has only (\d+) lines")


def says_nothing(output: Any) -> bool:
    """A tool output that holds no finding: an empty list under its usual key, or a count of 0."""
    if not isinstance(output, dict):
        return output in (None, "", [], {})
    if output.get("count") == 0:
        return True
    return any(k in output and output[k] in ([], {}, None, "", 0) for k in _EMPTY_KEYS)


CUT_NOTE = "cut for the prompt; the same call again shows the next part"
DROPPED_MARK = "[output folded to save room"
_REF_HEAD = re.compile(r"^\[(E\d+)\]")
_START_CUT = "\n[… cut]"
_END_CUT = "[… earlier lines left out]\n"


@dataclass
class KeptOutput:
    """One tool output as the run keeps it: what was shown first, and where the part not yet
    shown begins. A log was cut at its start (the newest lines are shown), so its next part lies
    before what was shown; anything else goes on after it."""

    text: str
    first: str
    newest: bool
    total: int
    pos: int = -1  # forward: next start; backward (a log): end of the part not yet shown

    def __post_init__(self) -> None:
        if self.pos >= 0:
            return
        if self.newest:  # the kept text is the tail of the output (see ``kept``)
            cut = self.first.startswith(_END_CUT)
            self.pos = len(self.text) - (len(self.first) - len(_END_CUT)) if cut else 0
        else:
            cut = self.first.endswith(_START_CUT)
            self.pos = len(self.first) - len(_START_CUT) if cut else len(self.text)

    @classmethod
    def kept(cls, full: str, shown: str, newest: bool) -> "KeptOutput":
        text = full[-KEPT_OUTPUT_CHARS:] if newest else full[:KEPT_OUTPUT_CHARS]
        return cls(text, shown, newest, len(full))

    def next_part(self, limit: int) -> tuple[str, str] | None:
        """The next part not yet shown and a note for its header, or ``None`` when all is shown."""
        limit = max(limit, 500)
        if self.newest:
            if self.pos <= 0:
                return None
            start = max(0, self.pos - limit)
            part, self.pos = self.text[start : self.pos], start
            more = "\n[… earlier lines: ask the same again]" if start > 0 else ""
            return part + more, f"continued: earlier lines, {len(part)} characters"
        if self.pos >= len(self.text):
            return None
        part = self.text[self.pos : self.pos + limit]
        first, self.pos = self.pos + 1, self.pos + len(part)
        if self.pos < len(self.text):
            part += "\n[… cut; ask the same again for the next part]"
        elif len(self.text) < self.total:
            part += f"\n[the rest of the {self.total} characters is not kept]"
        return part, f"continued: characters {first}-{self.pos} of {self.total}"


CUT_CALL_SHOWN = 300
CONTEXT_MARGIN_TOKENS = 512  # chat template, role markers and the rounding of the estimate
MIN_PROMPT_CHARS = 8000  # below this a run cannot work at all; the context note says so


def replayable_call(call: dict[str, Any]) -> dict[str, Any]:
    """A native call as it may be sent back to the model. A call that was cut off at the output
    limit carries half a JSON text as its arguments, and a llama-server refuses the whole next
    request (HTTP 500 "Failed to parse tool call arguments") when its history holds one: the run
    would end as "model unavailable" because the model wrote too much. The id stays (the error
    that answers it needs it); the arguments become ``{}``."""
    args = call.get("arguments")
    if isinstance(args, str):
        try:
            if isinstance(json.loads(args), dict):
                return call
        except ValueError:
            pass
    elif isinstance(args, dict):
        return call
    return {**call, "arguments": "{}"}


def _asked_before(seen: dict[str, str], node: str, capability: str, limit: int = 8) -> str:
    """The last few things this tool was already asked, for the refusal of a repeat: a small model
    that is told only "you asked that" asks a near-identical thing next; a list of what it has
    asked lets it choose something else (or see that it has enough and finish)."""
    asked: list[str] = []
    for key in seen:
        try:
            n, c, given = json.loads(key)
        except ValueError:
            continue
        if n != node or c != capability or not isinstance(given, dict):
            continue
        value = next((v for v in given.values() if isinstance(v, str) and v), None)
        if value:
            asked.append(" ".join(value.split())[:80])
    if len(asked) < 2:
        return ""
    return " Already asked with this tool: " + "; ".join(f'"{x}"' for x in asked[-limit:]) + "."


def file_read_key(node: str, capability: str, call_input: dict[str, Any]) -> str | None:
    """Which file a read call opens, whatever line range or ``find`` it asks for; ``None`` when the
    call reads no single file. A model that cannot find what it looks for moves the range of the
    same file again and again; the exact-call check never sees that, a count per file does."""
    path = call_input.get("path")
    if not isinstance(path, str) or not path:
        return None
    if capability == "repo.read":
        return json.dumps([node, capability, call_input.get("repo"), path])
    if capability == "github.read" and call_input.get("what") == "file":
        return json.dumps([node, capability, call_input.get("repo"), call_input.get("ref"), path])
    return None


def range_hint(full: str, shown: str) -> str:
    """The note after an output that was cut: which lines are shown, how many there are, and how
    to read the next range (``sed -n``) instead of asking for the whole file again."""
    total = full.count("\n") + 1
    last = shown.count("\n") + 1
    nxt = f"sed -n '{last + 1},{min(total, last + last)}p' FILE"
    return f"\n[lines 1-{last} of {total} shown; next range: {nxt}]"


@dataclass(frozen=True)
class Limits:
    """One run's limits (a later "Continue" starts a new run with fresh ones)."""

    max_turns: int = 24
    max_tool_calls: int = 16
    time_budget_seconds: float = 600.0
    # upper limit of one model answer, 0 = none: a run ends on a silent model, not on the clock
    model_timeout_seconds: float = 0.0
    model_retry_seconds: float = 10.0
    observation_chars: int = 3500
    context_chars: int = 20000
    max_format_errors: int = 3
    max_repeats: int = 3
    # reads of one file (other ranges or ``find`` count) before the next one is refused as a repeat
    max_file_reads: int = 4
    # the most a reply may have. A finish carries the summary and every finding as JSON, so
    # this is not small: a cut-off finish was the commonest way a good run ended in bad_format
    max_output_tokens: int = 1536
    native_output_tokens: int = 2048  # a native call may follow a long reasoning
    tools_wait_seconds: float = 60.0
    # tool calls one model answer may carry (a model that knows it needs several independent
    # looks asks for them together: one model turn instead of one per call); 1 = one at a time
    max_parallel_calls: int = 4
    min_context: int = 16384  # the context of one slot an agent with tools needs; 0 = no hint
    # the internet (web.search / web.fetch) is asked a little: a simple question must not become
    # nine calls (E3-04); a usable page ends it earlier (web_access.WebBudget)
    max_web_searches: int = 3
    max_web_fetches: int = 2
    # one repo.test_status call of the agent waits for its test run up to this long (the hub asks
    # the node again and again, 15 s at a time): the model does not spend a turn per 15 seconds
    test_wait_seconds: float = 120.0
    # the small model that looks at each tool step of a Gründlich run (step_check.py); "" = no look
    step_check_model: str = ""
    # research (research_profile.py): a run that makes no progress for this many model turns in a
    # row (no new evidence) ends; 0 = off. The web budget can stop asking for an answer at the
    # first usable page (a server check) or let the run read on (research).
    max_stalled_turns: int = 0
    # hooks a block program may switch (agent_hooks.py): the rules on the answer before it is
    # delivered, and the second model that reads the draft against the tool results
    check_claims: bool = True
    second_reader: bool = True
    stop_web_at_usable: bool = True
    max_web_attempts: int = web_access.MAX_WEB_ATTEMPTS

    @classmethod
    def of(cls, s: Settings) -> "Limits":
        return cls(
            max_turns=s.agent_max_turns,
            max_tool_calls=s.agent_max_tool_calls,
            time_budget_seconds=s.agent_time_budget_seconds,
            model_timeout_seconds=s.agent_model_hard_cap_seconds,
            model_retry_seconds=s.agent_model_retry_seconds,
            observation_chars=s.agent_observation_chars,
            context_chars=s.agent_context_chars,
            tools_wait_seconds=s.agent_tools_wait_seconds,
            min_context=s.agent_min_context,
            max_web_searches=s.agent_max_web_searches,
            max_web_fetches=s.agent_max_web_fetches,
        )


@dataclass
class Completion:
    text: str
    tokens: int = 0
    model: str = ""
    # the model's native tool calls (OpenAI ``tool_calls`` entries), if it made any
    tool_calls: list[dict[str, Any]] | None = None
    # why the model stopped: "stop", "tool_calls", "length" (max_tokens hit: the answer is cut off)
    finish_reason: str = ""
    # what the model is loaded with (context of one slot) and where, when the hub knows
    context_length: int | None = None
    node: str | None = None
    prompt_tokens: int = 0  # what the model counted for the prompt of this request (0 = unknown)


class ModelFailure(Exception):
    """No model answer: nothing can serve it, it timed out, it answered garbage. ``retry`` says
    that asking again a little later may work (no node could serve the model right now)."""

    def __init__(self, message: str, retry: bool = False, reason: str = "") -> None:
        super().__init__(message)
        self.retry = retry
        # why, where a node said so: insufficient_memory, insufficient_disk, … (see the gateway)
        self.reason = reason


class NativeUnsupported(ModelFailure):
    """The model (or the server behind it) refuses native tool calls: ``tools`` is not accepted."""


class Truncated(prompts.ActionError):
    """The model hit the output limit (``finish_reason`` length) in the middle of its answer: it
    is not "an invalid action", it is too long. ``call`` is the cut native call, if there was
    one."""

    def __init__(self, message: str, call: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.call = call


class CallError(prompts.ActionError):
    """A native tool call that is no valid action; ``call`` is what the model sent (its id is
    answered with the error)."""

    def __init__(self, message: str, call: dict[str, Any]) -> None:
        super().__init__(message)
        self.call = call


class ModelPort(Protocol):
    """``complete`` asks once. With ``tools`` (OpenAI function definitions) the model may answer
    with ``tool_calls``. A port may also have ``tool_mode(model)`` -> auto | native | json (how
    this model is talked to; absent = json) and ``prefer_json(model)`` (remember that native did
    not work)."""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str | None,
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> Completion: ...


class ToolsPort(Protocol):
    async def available(self, caller: Caller) -> list[dict[str, Any]]: ...

    async def invoke(
        self,
        caller: Caller,
        node_id: uuid.UUID,
        capability: str,
        tool_input: dict[str, Any],
        task_id: uuid.UUID | None = None,
    ) -> dict[str, Any]: ...


Publish = Callable[[str, dict[str, Any]], None]


def utcnow() -> datetime:
    return datetime.now(UTC)


class AgentLoop:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        model: ModelPort,
        tools: ToolsPort,
        publish: Publish,
        limits: Limits,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.model = model
        self.tools = tools
        self.publish = publish
        self.limits = limits
        # the live settings (set by the agent service): Research reads its frame's numbers there
        self.settings: Settings | None = None
        self._sleep = sleep
        # set by the agent service when proposals (E3-01b) are switched on
        self.proposals: Any = None
        # E7-01: notes and pauses from the person, read before each turn (set by main)
        self.steering: SteeringBoard | None = None
        # a second model reads the draft before it goes out (PreAnswerCheck); None = off
        self.precheck: Any = None
        # a chat check starts with the tool groups its question names (set from the settings)
        self.auto_groups = True
        # told when a task starts or ends (``started``, ``done``, ``failed``, ``canceled``), so a
        # request that waits for it can be answered (PPH-05); a failure here never fails the task
        self.lifecycle: Callable[[uuid.UUID, uuid.UUID, str], Awaitable[None]] | None = None

    # --- storage helpers: short sessions, every write through the owner's store ------------

    @asynccontextmanager
    async def _store(self, user_id: uuid.UUID) -> AsyncIterator[AgentStore]:
        async with self.sessionmaker() as db:
            yield AgentStore(db, user_id)

    def _announce(self, task: AgentTask) -> None:
        self.publish("agent_task", task_event(task))

    async def _step(
        self, user_id: uuid.UUID, task_id: uuid.UUID, kind: str, data: dict[str, Any]
    ) -> None:
        async with self._store(user_id) as store:
            task = await store.get(task_id)
            if task is None:
                return
            step = await store.add_step(task, kind, data)
            self.publish(
                "agent_step", {"task_id": str(task_id), "seq": step.seq, "_user": str(user_id)}
            )

    async def _steer(self, s: "_Run") -> float:
        """What the person said to the running check, read before a turn: a pause holds the check
        here (the time it waited is returned, so it does not eat the budget), notes become a
        message in the transcript like a tool result — text from the person who asked, nothing
        that widens the check's rights or tools."""
        board = self.steering
        if board is None:
            return 0.0
        waited = 0.0
        if board.is_paused(s.task_id):
            await self._step(s.user_id, s.task_id, "note", {"text": "steer: paused by the person"})
            waited = await board.wait_while_paused(s.task_id)
            await self._step(s.user_id, s.task_id, "note", {"text": "steer: resumed"})
        for text in board.take(s.task_id):
            await self._observe(s, "user_note", f"{NOTE_FROM_PERSON}{text}")
        return waited

    async def _finish(
        self, user_id: uuid.UUID, task_id: uuid.UUID, status: str, **fields: Any
    ) -> None:
        if self.steering is not None and status in ("done", "failed", "canceled"):
            self.steering.forget(task_id)
        async with self._store(user_id) as store:
            task = await store.get(task_id)
            if task is None or task.status == "canceled":  # a cancel wins over a late result
                return
            await store.set_status(task, status, **fields)
            research_done = status == "done" and task.application == research_profile.APPLICATION
            # a thorough research becomes a report (report_service), not a library copy as well
            files_report = (
                research_done and research_profile.depth_in_prompt(task.prompt) != "quick"
            )
            if status == "done" and task.origin == "tasks" and not files_report:
                await self._file_report(store, task)
            self._announce(task)
        if research_done:  # a thorough research ends as a report in the Ablage (D102)
            await report_service.try_file_for_task(
                self.sessionmaker, user_id, task_id, self._titler(user_id, task_id)
            )
        if status in ("done", "failed", "canceled"):
            await self.tell(user_id, task_id, status)

    def _titler(self, user_id: uuid.UUID, task_id: uuid.UUID) -> report_service.Titler:
        """A report's title from one short call to the model that ran the research."""

        async def make(
            language: str, question: str, summary: str, chapters: list[str]
        ) -> str | None:
            async with self._store(user_id) as store:
                task = await store.get(task_id)
                model = task.model if task is not None else None
            msgs = report_service.title_messages(language, question, summary, chapters)
            c = await self.model.complete(msgs, model, TITLE_TOKENS)
            title = report_service.clean_title(c.text)
            if title is None:
                log.info(
                    "report: no usable title from %s (finish %s): %r",
                    c.model or model,
                    c.finish_reason,
                    (c.text or "")[-200:],
                )
            return title

        return make

    async def tell(self, user_id: uuid.UUID, task_id: uuid.UUID, event: str) -> None:
        if self.lifecycle is None:
            return
        try:
            await self.lifecycle(user_id, task_id, event)
        except Exception:
            log.exception("agent: the lifecycle hook failed for task %s (%s)", task_id, event)

    async def _file_report(self, store: AgentStore, task: AgentTask) -> None:
        """The report of a handed-over task goes to the library, not into a chat. A failure to
        file it never fails the task."""
        try:
            card = report.build(task, await store.evidence(task.id))
            body = report.text(card) if card else (task.summary or "")
            if not body.strip():
                return
            await ArtifactStore(store.db, task.user_id).put_report(task.id, task.title, body)
            await store.db.commit()
        except Exception:
            log.exception("agent: could not file the report of task %s", task.id)
            await store.db.rollback()

    # --- the run ---------------------------------------------------------------------------

    async def run(
        self,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        limits: Limits | None = None,
        hooks: dict[str, Any] | None = None,
    ) -> None:
        lim = limits or self.limits
        started = time.monotonic()
        deadline = started + lim.time_budget_seconds
        async with self._store(user_id) as store:
            task = await store.get(task_id)
            if task is None or task.status in ("done", "failed", "canceled"):
                return
            research_depth: str | None = None
            if task.application == research_profile.APPLICATION:
                # Research is not run with the limits of a server check; a request names its
                # frame in its prompt, so a task started again reads the same one
                research_depth = research_profile.depth_in_prompt(task.prompt)
                if limits is None and self.settings is not None:
                    lim = research_profile.limits_for(self.settings, research_depth, lim)
                    deadline = started + lim.time_budget_seconds
            if hooks:  # what a block program switched on top of the application's frame
                lim = agent_hooks.apply(
                    lim, hooks, getattr(self.settings, "step_check_model", "") or ""
                )
            async with self.sessionmaker() as db:
                user = await db.get(User, user_id)
            # an API key never runs with admin rights, and neither does a task it started (D29)
            admin = bool(user and user.role == "admin" and task.via == "gui")
            await store.set_status(
                task, "running", runs=task.runs + 1, started_at=task.started_at or utcnow()
            )
            self._announce(task)
            prompt, model, scope = task.prompt, task.model, task.scope
            application = task.application
            allowed = frozenset(task.capabilities) if task.capabilities is not None else None
            origin = task.origin
            steps = list(await store.steps(task_id))
            evidence = list(await store.evidence(task_id))
        await self.tell(user_id, task_id, "started")
        # a task of no application of its own (a chat check) keeps the application of the chat
        # that started it, so the model of its row is used (SCH-04)
        set_job_context(
            user_id=user_id,
            task_id=task_id,
            **({"application": application} if application else {}),
            # a reviewer run is the second look at a message: its model comes from the check row
            role="check" if origin == "review" else None,
        )
        caller = Caller(
            user_id,
            admin,
            "agent",
            application,
            # a chat check and a planner run prepare their writes; the person's click applies them
            stage_writes=origin in ("chat", "planner"),
            task_id=task_id,
            allowed_capabilities=allowed,
        )

        narrowed: list[str] = []
        if origin == "chat" and self.auto_groups:
            scope, narrowed = tool_groups.auto_scope(
                prompt, scope, await self.tools.available(caller)
            )
        tools = await self._wait_for_tools(caller, lim, scope)
        if not tools:
            await self._finish(
                user_id,
                task_id,
                "failed",
                stop_reason="no_tools",
                error="no capability this user may run is available (is a tool node connected?)",
            )
            return

        # the first thing a run settles: has it the tools for what it is asked? Not after minutes
        # of thinking, but before the first model turn (a resumed run was settled when it began)
        # (a run of the hub's own — a review, a planner — is told what it is to do, not asked)
        lacking = (
            []
            if steps or origin in ("review", "planner")
            else tool_groups.missing_tools(prompt, tools)
        )
        if lacking:
            have = tool_groups.tools_summary(tools)
            await self._step(
                user_id,
                task_id,
                "note",
                {"text": f"No tool for: {', '.join(lacking)}. The agent can look at: {have}."},
            )
            await self._finish(
                user_id,
                task_id,
                "failed",
                stop_reason="tools_missing",
                error=(
                    f"the agent has no tool for {', '.join(lacking)} here (it can look at: {have}) "
                    "— switch the tool group on in the conversation or connect a node that "
                    "offers it"
                ),
            )
            return

        transcript: list[dict[str, Any]] = []
        for s in steps:
            if s.kind == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": s.data.get("content", "")}
                if isinstance(s.data.get("native"), dict):
                    item["call"] = replayable_call(s.data["native"])
                transcript.append(item)
            elif s.kind == "observation":
                item = {"role": "user", "content": s.data.get("text", "")}
                if s.data.get("tool_call_id"):
                    item["call_id"] = s.data["tool_call_id"]
                transcript.append(item)
        tags = {e.ref: e.tag for e in evidence}
        seen = {
            e.source.get("key", ""): e.ref for e in evidence if isinstance(e.source.get("key"), str)
        }
        state = _Run(task_id, user_id, caller, prompt, model, transcript, tags, seen, lim)
        state.branches.of_prompt(prompt)
        state.workshop.of_prompt(prompt)
        if any(str(t["capability"]).startswith("repo.") for t in tools):
            state.repos = await self._repos(user_id)
        state.name_notes = await self._name_notes(user_id, application, prompt)
        state.scope = scope
        # Topcoat's own state reads the chat's scope from the caller (pruefauftrag T2.3)
        state.caller = replace(caller, scope=scope)
        state.deadline = deadline
        if narrowed and not steps:
            await self._step(
                user_id,
                task_id,
                "note",
                {
                    "text": "The question names a topic, so the agent was offered only these "
                    f"tool groups: {', '.join(narrowed)}."
                },
            )
        state.proposable = await self._proposable(caller, origin)
        if application == "project":  # Projekte writes tasks: which application can do what
            async with self.sessionmaker() as db:
                state.catalogue = app_catalogue.section(await app_catalogue.catalogue(db))
        state.can_write = any(writes(str(t.get("capability") or "")) for t in tools)
        state.web = web_access.WebBudget(
            lim.max_web_searches,
            lim.max_web_fetches,
            stop_at_usable=lim.stop_web_at_usable,
            max_attempts=lim.max_web_attempts,
        )
        if origin not in HUB_PROMPT_ORIGINS:
            # the URLs the person gave with the question may be fetched; the run finds the rest
            # through its own searches (finding 03: no URL the model made up)
            state.web.origins.of_person(prompt)
        state.research_depth = research_depth
        state.progress_mark = state.informative
        mode = await self._tool_mode(model)
        state.mode, state.auto = ("json" if mode == "json" else "native"), mode == "auto"
        if (
            steps
            and steps[-1].kind == "assistant"
            and isinstance(steps[-1].data.get("native"), dict)
        ):
            state.pending_call = steps[-1].data["native"].get("id")

        if (
            not steps
            and origin in ("chat", "tasks", "block")  # a check with a question, not the hub's own
            and research_depth is None
            and getattr(self.settings, "agent_staged", False)
        ):  # Phase 1: plan the check in steps, each with a fresh context (agent_stages.py)
            await self._plan(state, tools)

        # resuming: an answer that asked for a tool (or to finish) and was never acted on
        if steps and steps[-1].kind == "assistant":
            try:
                pending = prompts.parse_action(steps[-1].data.get("content", ""))
            except prompts.ActionError:
                pending = None
            if isinstance(pending, prompts.Finish):
                await self._conclude(state, pending, None)
                return
            if isinstance(pending, prompts.ToolAction):
                await self._tool(state, pending, tools)

        format_errors = 0
        while True:
            deadline += await self._steer(state)  # a pause does not use up the time budget
            if state.turns >= lim.max_turns:
                await self._wrap_up(state, "max_turns", deadline)
                return
            if state.calls >= lim.max_tool_calls:
                await self._wrap_up(state, "max_tool_calls", deadline)
                return
            st = state.stages
            if st is not None and state.calls - st.start_calls >= st.budget():
                if st.final:  # the answer step looked enough: it writes the answer now
                    await self._wrap_up(state, STAGE_ANSWER, deadline)
                    return
                await self._end_stage(state, None)  # the step used its calls: the next one goes on
                continue
            if lim.max_stalled_turns and state.turns > state.counted_turns:
                state.counted_turns = state.turns
                if state.informative > state.progress_mark:
                    state.progress_mark, state.stalled = state.informative, 0
                else:
                    state.stalled += 1
                if state.stalled >= lim.max_stalled_turns:
                    await self._wrap_up(state, "no_progress", deadline)
                    return
            if time.monotonic() > deadline:
                await self._wrap_up(state, "time_budget", deadline)
                return
            tools = await self._available(caller, scope) or tools
            offered = (
                agent_stages.step_tools(state.stages, tools) if state.stages is not None else tools
            )
            # a web tool whose every call would be refused is not offered any more
            offered = [t for t in offered if not state.web.closed(t["capability"])]
            try:
                completion = await self._ask(state, offered, deadline, extra=budget_note(state))
            except NativeUnsupported as exc:
                if state.auto:
                    await self._to_json(state, str(exc))
                    continue
                await self._model_failed(state, exc)
                return
            except ModelFailure as exc:
                await self._model_failed(state, exc)
                return
            state.turns += 1
            state.author = completion.model or state.author
            if state.model is None and completion.model:
                # the run stays with the model its first turn got: the next turns would else go
                # to whichever node of the row has a free place, another model with another
                # style of calls and no cache of this prompt (node_chain prefers free places)
                state.model = completion.model
            await self._note_context(state, completion)
            try:
                action, call = self._parse(state, completion)
            except prompts.ActionError as exc:
                call = exc.call if isinstance(exc, (CallError, Truncated)) else None
                await self._assistant(state, completion, call, None)
                format_errors += 1
                state.cut_replies += isinstance(exc, Truncated)
                if isinstance(exc, Truncated):
                    hint = TOO_LONG_HINT
                elif state.mode == "native" and call is None:
                    hint = (
                        "You answered in text. Answer with a function call: one of the tools, "
                        'or "finish" when you know enough.'
                    )
                else:
                    hint = f"Your reply was not a valid action ({exc}). " + (
                        "Call one of the functions with valid arguments."
                        if state.mode == "native"
                        else "Reply with exactly one JSON object as described in the instructions."
                    )
                await self._observe(state, "error", hint)
                if state.auto and state.mode == "native" and format_errors >= 2:
                    await self._to_json(state, "the model does not make native tool calls")
                    format_errors = state.cut_replies = 0
                    continue
                if format_errors >= lim.max_format_errors:
                    await self._finish(
                        user_id,
                        task_id,
                        "failed",
                        stop_reason="bad_format",
                        error=(
                            f"the model's answer was cut off at the output limit "
                            f"{format_errors} times in a row (too long)"
                            if state.cut_replies >= format_errors
                            else f"no valid action from the model {format_errors} times in a row"
                        ),
                    )
                    return
                continue
            format_errors = state.cut_replies = 0
            await self._assistant(state, completion, call, action)
            if (
                isinstance(action, prompts.Finish)
                and state.stages is not None
                and not state.stages.final
            ):
                await self._end_stage(state, action)  # a step's result, not the answer yet
                continue
            if isinstance(action, prompts.Finish):
                if not state.tags and not state.nudged and state.calls == 0:
                    # a diagnosis nobody looked for: send the model to look first, once
                    state.nudged = True
                    await self._observe(
                        state,
                        "error",
                        "You finished without looking at anything, so nothing you say can be "
                        "tagged live. Look first: make at least one tool call that can show "
                        "whether your suspicion is true. Finish without looking only if no "
                        "available tool could possibly help, and then tag every claim "
                        '"assumption".',
                    )
                    continue
                if await self._sent_back(state, action):
                    continue
                await self._conclude(state, action, None)
                return
            await self._tool(state, action, tools)
            await self._more_calls(state, completion, tools)
            if state.repeats >= lim.max_repeats:
                await self._wrap_up(state, "repeated_calls", deadline)
                return

    async def _plan(self, s: "_Run", tools: list[dict[str, Any]]) -> None:
        """One model call that splits the check into steps; a plan that cannot be used leaves the
        run as one loop (a planning failure never ends a run)."""
        try:
            c = await self.model.complete(
                agent_stages.plan_messages(s.prompt, tools), s.model, PLAN_TOKENS
            )
        except ModelFailure:
            return
        plan = agent_stages.parse_plan(c.text or "", tools)
        if len(plan) < 2:  # one step is the plain loop: nothing to split
            await self._step(
                s.user_id, s.task_id, "note", {"text": "One step: no plan needed.", "plan": []}
            )
            return
        limit = int(getattr(self.settings, "agent_stage_calls", 6) or 6)
        s.stages = agent_stages.Stages(plan, max_calls=limit)
        s.task_prompt = s.prompt
        s.prompt = agent_stages.step_prompt(s.task_prompt, s.stages)
        s.stage_refs = len(s.tags)
        await self._step(
            s.user_id,
            s.task_id,
            "note",
            {
                "text": agent_stages.plan_note(plan),
                "plan": [st.__dict__ for st in plan],
                "plan_tokens": c.tokens,
            },
        )

    async def _end_stage(self, s: "_Run", f: prompts.Finish | None) -> None:
        """The current step is done (its finish) or out of calls: keep its result, start the next
        step with an empty transcript."""
        st = s.stages
        if st is None:
            return
        found = self._stage_found(s)
        if f is not None:
            st.results.append(agent_stages.result_text(st, f.summary, f.findings, found))
        else:
            st.results.append(agent_stages.incomplete_text(st, found))
        st.index += 1
        st.start_calls = s.calls
        s.stage_refs = len(s.tags)
        s.stage_shown = set()
        s.transcript = []  # the fresh context: the next step sees the results, not the outputs
        s.prompt = agent_stages.step_prompt(s.task_prompt, st)
        goal = "write the answer" if st.final else st.steps[st.index].goal
        await self._step(
            s.user_id, s.task_id, "note", {"text": f"Step {st.index + 1} of {st.total}: {goal}"}
        )

    @staticmethod
    def _stage_found(s: "_Run") -> list[tuple[str, str, str]]:
        """What the current step collected: mark, the call, the first characters of the output."""
        refs = set(list(s.tags)[s.stage_refs :])
        out = []
        for key, ref in s.seen.items():
            if ref not in refs:
                continue
            try:
                _node, cap, given = json.loads(key)
            except (ValueError, TypeError):
                _node, cap, given = "", key, {}
            call = f"{cap} {json.dumps(given, ensure_ascii=False)[:100]}"
            out.append((ref, call, " ".join(str(s.texts.get(ref, "")).split())[:200]))
        return out

    async def _sent_back(self, s: "_Run", f: prompts.Finish) -> bool:
        """Checks of a draft before it is delivered; ``True`` = the draft goes back to the model
        once with what is wrong (the run goes on), ``False`` = deliver it.

        1. The hub's own check, no model: a statement that tests passed needs a ``repo.test_status``
           of this task that showed ``passed`` (and the numbers it names).
        2. A second model (``PreAnswerCheck``) reads draft and tool results when the task used one
           of the tools it is switched on for. A finding that says the draft states something the
           results contradict or do not contain sends the draft back; everything else becomes a
           remark in the diagnosis. Never blocks: no reviewer or no answer in time = a remark."""
        texts = [f.summary, *[str(x.get("claim") or "") for x in f.findings]]
        claims = s.limits.check_claims
        problem = agent_tests.unbacked_claim(texts, s.test_runs) if claims else None
        if problem:
            if not s.test_nudged:
                s.test_nudged = True
                await self._observe(s, "error", problem)
                return True
            s.hints.append(
                "a statement about passing tests is not backed by a test run of this task"
            )
        problem = agent_done.unbacked_branch(texts, s.branches) if claims else None
        if problem:
            if not s.branch_nudged:
                s.branch_nudged = True
                await self._observe(s, "error", problem)
                return True
            s.hints.append("the branch named in the answer was not shown by a repo result")
        problem = prompts.blanket_evidence(f)
        if problem and not s.blanket_nudged:
            s.blanket_nudged = True  # once: a second draft like it is delivered (and checked)
            await self._observe(s, "error", problem)
            return True
        if s.refused_writes and not s.write_nudged:
            # live 2026-11-18: a proposal was refused for two invented fields, and the run ended
            # with "Task 3.1.1 updated" - nothing was prepared, the person found nothing to apply
            s.write_nudged = True
            (capability, why), *_ = s.refused_writes.items()
            await self._observe(
                s,
                "error",
                f"Your call {capability} was refused, so nothing is prepared: {why}\n"
                "Make the call again with the fix the message names. Finish only after it went "
                "through, or say plainly that the change was not prepared and why.",
            )
            return True
        if s.can_write and not s.wrote and not s.change_nudged:
            said = agent_done.claims_change([*texts, f.next])
            offered = {str(p.get("capability")) for p in s.proposable}
            stray = [str(p.get("capability")) for p in f.proposals if isinstance(p, dict)]
            stray = [c for c in stray if c not in offered]
            if said or stray:
                # live 2026-11-18: "a changeset has been prepared" with the change written into the
                # finish as a proposal of project.node.update - no proposal was ever made
                s.change_nudged = True
                what = f'"{said}"' if said else f"a change of {stray[0]} in your finish"
                await self._observe(
                    s,
                    "error",
                    f"Your draft says a change is prepared ({what}), but no call of this run "
                    "prepared one: nothing waits for the person's click. A change is prepared only "
                    "by calling the tool for it (project.changeset.propose for a project). Call it "
                    "now, then finish; or say plainly that you did not prepare a change.",
                )
                return True
        problem = agent_done.unfinished(texts) if claims else None
        if problem:
            if not s.announce_nudged:
                s.announce_nudged = True
                await self._observe(s, "error", problem)
                return True
            s.hints.append("the answer announces a step that was not taken")
        if s.research_depth == "thorough" and s.pages_nudged < message_tools.PAGES_SEND_BACKS:
            # a thorough report rests on pages, not on snippets (live 2026-11-13: one page read,
            # finished after 99 s): sent back like a chapter, at most twice
            async with self._store(s.user_id) as store:
                rows = list(await store.evidence(s.task_id))
            pages, hosts = message_tools._pages_read(rows)
            if pages < message_tools.MIN_PAGES or hosts < message_tools.MIN_HOSTS:
                s.pages_nudged += 1
                await self._observe(
                    s,
                    "error",
                    f"Not finished yet: you read {pages} page(s) from {hosts} site(s). "
                    + message_tools.SENT_BACK["few_pages"]
                    + " Then write the chapters with research.report.section and finish.",
                )
                return True
        check = self.precheck
        if check is None or not s.limits.second_reader or s.prechecked or not check.wanted(s.used):
            return False
        s.prechecked = True
        async with self._store(s.user_id) as store:
            rows = list(await store.evidence(s.task_id))
        evidence = [
            (e.ref, str(e.source.get("capability") or ""), e.excerpt or "")
            for e in rows
            if e.tag == "live"
        ]
        try:
            found = await check.check(
                s.prompt, f.summary, f.findings, evidence, s.author or s.model
            )
        except Exception:  # a second look must never cost the answer
            log.exception("pre-answer check failed")
            found = None
        if found is None:
            s.hints.append("not cross-checked by a second model before this answer")
            return False
        major = [x for x in found if x["severity"] == "major"]
        for x in found:
            if x["severity"] != "major":
                s.hints.append(f"second model: {x['claim']} - {x['reason']}")
        if major:
            lines = "; ".join(f'"{x["claim"]}" ({x["reason"]})' for x in major)
            await self._observe(
                s,
                "error",
                "A second model read your draft against the tool results and found: "
                f"{lines}. Correct the draft: say only what the results show, or look again.",
            )
            return True
        return False

    async def _repos(self, user_id: uuid.UUID) -> list[tuple[str, str]]:
        """The repositories this person registered, ``(name, mode)``: the hint names them, so the
        model does not have to find out (or guess) which `repo` value the tools take."""
        try:
            async with self.sessionmaker() as db:
                rows = await db.execute(
                    select(DevRepo.name, DevRepo.mode)
                    .where(DevRepo.user_id == user_id)
                    .order_by(DevRepo.name)
                )
                return [(str(n), str(m)) for n, m in rows.all()]
        except Exception:  # a hint must never cost the run
            log.exception("agent: could not list the registered repositories")
            return []

    async def _name_notes(
        self, user_id: uuid.UUID, application: str | None, prompt: str
    ) -> dict[str, list[str]]:
        """The person's own active notes that mention a name the question puts in quotes
        ("Luxel"): what they told Topcoat about it, such as which device that is. Read through
        the memory store, so the program's scope (D74) applies; at most two notes per name."""
        names = prompts.quoted_names(prompt.split("\n\n(", 1)[0])
        if not names:
            return {}
        try:
            async with self.sessionmaker() as db:
                store = MemoryStore(db, user_id, scope=await scope_for(db, user_id, application))
                found: dict[str, list[str]] = {}
                for name in names:
                    notes = await store.items(q=name, status="active", limit=2)
                    if notes:
                        found[name] = [n.text for n in notes]
                return found
        except Exception:  # a hint must never cost the run
            log.exception("agent: could not read the notes about the names of the question")
            return {}

    async def chat_actions(self, caller: Caller) -> list[dict[str, Any]]:
        """What a check of a chat may propose for this caller (:meth:`_proposable`): the only
        changes the chat's assistant may offer (#223)."""
        return await self._proposable(caller, "chat")

    async def _proposable(self, caller: Caller, origin: str) -> list[dict[str, Any]]:
        """The state-changing actions this task's user could confirm (E3-01b), only when the hub
        has proposals switched on. A task from the tasks page may propose all of them (the card
        is on that page). A check of a chat may propose the file changes of the workshop (D99):
        the report card under the answer shows each with its diff and the one click (TaskProposals);
        a restart or any other action stays with the tasks page."""
        port = getattr(self.tools, "proposable", None)
        if (
            self.proposals is None
            or not self.proposals.enabled
            or port is None
            or origin not in ("tasks", "chat")
        ):
            return []
        try:
            found = list(await port(caller))
        except Exception:
            log.exception("agent: could not list the actions that may be proposed")
            return []
        if origin == "chat":
            return [t for t in found if t.get("capability") in agent_workshop.CHANGES]
        return found

    async def _more_calls(self, s: "_Run", c: Completion, tools: list[dict[str, Any]]) -> None:
        """The other calls of an answer that asked for several (native mode): each is stored and
        run like a call of its own, in the model's order, so the transcript stays valid for any
        chat template — only the model turns in between are saved. Calls beyond
        ``max_parallel_calls`` or the call budget, and anything that is not a valid tool call,
        are left out."""
        extra = (c.tool_calls or [])[1 : s.limits.max_parallel_calls]
        for raw in extra:
            if s.calls >= s.limits.max_tool_calls:
                return
            fn = raw.get("function") if isinstance(raw.get("function"), dict) else {}
            args = fn.get("arguments")
            call = {
                "id": str(raw.get("id") or f"call_{uuid.uuid4().hex[:8]}"),
                "name": str(fn.get("name") or ""),
                "arguments": args if isinstance(args, str) else json.dumps(args or {}),
            }
            try:
                action = prompts.parse_native(raw, s.tool_index)
            except prompts.ActionError:
                continue
            if not isinstance(action, prompts.ToolAction):
                continue
            await self._assistant(s, replace(c, tokens=0, text=""), call, action)
            await self._tool(s, action, tools)

    async def _model_failed(
        self, s: "_Run", exc: ModelFailure, stop_reason: str | None = None
    ) -> None:
        """No model answered. The task says why — "not enough free RAM on node-amd" is an answer
        the operator can act on, "no model could answer" is not."""
        reason = exc.reason if exc.reason in MODEL_REASONS else ""
        await self._step(
            s.user_id, s.task_id, "note", {"text": f"The model could not be reached: {exc}"}
        )
        await self._finish(
            s.user_id,
            s.task_id,
            "failed",
            stop_reason=reason or stop_reason or "model_unavailable",
            error=str(exc),
        )

    async def _available(
        self, caller: Caller, scope: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """What the task may use now: the capabilities the user may run, narrowed to the task's
        scope (the nodes and tool groups chosen in the conversation; it only ever removes)."""
        return tool_groups.apply_scope(await self.tools.available(caller), scope)

    async def _wait_for_tools(
        self, caller: Caller, lim: Limits, scope: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """After a hub restart the tool nodes take a moment to reconnect: wait for them."""
        waited = 0.0
        while True:
            tools = await self._available(caller, scope)
            if tools or waited >= lim.tools_wait_seconds:
                return tools
            await self._sleep(2.0)
            waited += 2.0

    # --- the model -------------------------------------------------------------------------

    async def _tool_mode(self, model: str | None) -> str:
        fn = getattr(self.model, "tool_mode", None)
        mode = await fn(model) if fn else "json"
        return mode if mode in prompts.TOOL_MODES else "auto"

    async def _to_json(self, s: "_Run", why: str) -> None:
        """This model cannot (or does not) make native tool calls: the prompt-based protocol for
        the rest of the run, and — where the port remembers — for the next ones."""
        s.mode, s.auto = "json", False
        remember = getattr(self.model, "prefer_json", None)
        if remember:
            await remember(s.model)
        await self._step(
            s.user_id,
            s.task_id,
            "note",
            {"text": f"Switching to the prompt-based JSON protocol for this model ({why})."},
        )

    def _messages(self, s: "_Run", tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        native = s.mode == "native"
        system = (
            prompts.system_prompt_native(tools, s.proposable, s.caller.application)
            if native
            else prompts.system_prompt(tools, s.proposable, s.caller.application)
        )
        if s.research_depth is not None:
            system = research_profile.personalise(system, s.research_depth)
        system += s.catalogue
        head = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": prompts.task_message(
                    s.prompt,
                    s.clock,
                    "\n\n".join(
                        h
                        for h in (
                            prompts.service_hint(s.prompt, tools),
                            prompts.tools_hint(s.prompt, tools),
                            prompts.workshop_hint(s.prompt, tools, s.repos),
                            prompts.wifi_hint(s.prompt, tools),
                            prompts.names_hint(s.name_notes),
                        )
                        if h
                    ),
                ),
            },
        ]
        rendered = head + prompts.render_messages(s.transcript, native)
        fitted = prompts.fit_messages(rendered, s.limits.context_chars)
        # which results the model can no longer see: a repeat of one of them is shown again once
        s.dropped = {
            m.group(1)
            for msg in fitted
            if DROPPED_MARK in str(msg.get("content") or "")
            and (m := _REF_HEAD.match(str(msg.get("content") or "")))
        }
        return prompts.merge_roles(fitted)

    async def _ask(
        self, s: "_Run", tools: list[dict[str, Any]], deadline: float, extra: str | None = None
    ) -> Completion:
        messages = self._messages(s, tools)
        if extra:
            messages = prompts.merge_roles([*messages, {"role": "user", "content": extra}])
        defs: list[dict[str, Any]] | None = None
        if s.mode == "native":
            defs, s.tool_index = prompts.native_tools(tools, s.proposable)
            if defs and not s.measured:
                s.measured = True
                size = len(json.dumps(defs, ensure_ascii=False))
                s.tools_chars = size
                log.info(
                    "agent task %s: %d tool definitions, %d characters (about %d tokens)",
                    s.task_id,
                    len(defs),
                    size,
                    size // CHARS_PER_TOKEN,
                )
        s.sent_chars = len(json.dumps(messages, ensure_ascii=False)) + s.tools_chars
        if not s.prompt_parts:  # the base load before any result: what a run costs at its start
            system = sum(
                len(str(m.get("content") or "")) for m in messages if m["role"] == "system"
            )
            s.prompt_parts = {
                "system_chars": system,
                "tools_chars": s.tools_chars,
                "task_chars": s.sent_chars - system - s.tools_chars,
                "tools": len(defs) if defs else len(tools),
                "mode": s.mode,
            }
        max_tokens = s.limits.native_output_tokens if defs else s.limits.max_output_tokens
        last = "no answer"
        for attempt in range(3):
            timeout = s.limits.model_timeout_seconds or None
            try:
                if defs:
                    call = self.model.complete(messages, s.model, max_tokens, tools=defs)
                else:
                    call = self.model.complete(messages, s.model, max_tokens)
                completion = await asyncio.wait_for(call, timeout)
            except TimeoutError:
                raise ModelFailure("the model did not answer in time") from None
            except NativeUnsupported:
                raise
            except ModelFailure as exc:
                last = str(exc)
                if attempt < 2 and exc.retry:
                    await self._sleep(s.limits.model_retry_seconds)
                    continue
                raise
            return completion
        raise ModelFailure(last)

    def _parse(
        self, s: "_Run", c: Completion
    ) -> tuple[prompts.ToolAction | prompts.Finish, dict[str, Any] | None]:
        """The action in a model's answer and, for a native call, the call as it will be stored
        (id, name, arguments). A native call comes first; a text answer that holds the JSON
        action is accepted in either mode. An answer cut off at the output limit is rescued
        when it is a finish whose summary is complete (a diagnosis without findings), else it
        is :class:`Truncated`."""
        try:
            return self._parse_whole(s, c)
        except prompts.ActionError as exc:
            if c.finish_reason != "length":
                raise
            call = exc.call if isinstance(exc, CallError) else None
            name = args = None
            if c.tool_calls:
                fn = c.tool_calls[0].get("function")
                fn = fn if isinstance(fn, dict) else {}
                name = str(fn.get("name") or "")
                raw = fn.get("arguments")
                args = raw if isinstance(raw, str) else json.dumps(raw or {})
            rescued = prompts.rescue_finish(args if args is not None else c.text, name)
            if rescued is not None:
                return rescued, call
            raise Truncated("the answer was cut off at the output limit", call) from exc

    def _parse_whole(
        self, s: "_Run", c: Completion
    ) -> tuple[prompts.ToolAction | prompts.Finish, dict[str, Any] | None]:
        if c.tool_calls:
            raw = c.tool_calls[0]
            fn = raw.get("function") if isinstance(raw.get("function"), dict) else {}
            args = fn.get("arguments")
            call = {
                "id": str(raw.get("id") or f"call_{uuid.uuid4().hex[:8]}"),
                "name": str(fn.get("name") or ""),
                "arguments": args if isinstance(args, str) else json.dumps(args or {}),
            }
            try:
                return prompts.parse_native(raw, s.tool_index), call
            except prompts.ActionError as exc:
                raise CallError(str(exc), call) from exc
        return prompts.parse_action(c.text), None

    async def _assistant(
        self,
        s: "_Run",
        c: Completion,
        call: dict[str, Any] | None = None,
        action: prompts.ToolAction | prompts.Finish | None = None,
    ) -> None:
        # the stored text of a native call is the JSON action the prompt-based protocol uses, so a
        # run can change tool mode and the GUI reads both alike
        content = prompts.action_text(action) if call and action else (c.text or "")
        if call and not action:
            content = f"{call['name']}({str(call['arguments'])[:CUT_CALL_SHOWN]})"
        if call:
            call = replayable_call(call)
        item: dict[str, Any] = {"role": "assistant", "content": content}
        data: dict[str, Any] = {"content": content, "tokens": c.tokens, "model": c.model}
        if call:
            item["call"] = call
            data["native"] = call
        s.transcript.append(item)
        s.pending_call = call["id"] if call else None
        await self._step(s.user_id, s.task_id, "assistant", data)

    async def _observe(
        self,
        s: "_Run",
        kind: str,
        text: str,
        evidence: str | None = None,
        tool: dict[str, Any] | None = None,
    ) -> None:
        item: dict[str, Any] = {"role": "user", "content": text}
        data: dict[str, Any] = {"kind": kind, "text": text, "evidence": evidence, "tool": tool}
        if s.pending_call:  # the answer to a native call
            item["call_id"] = data["tool_call_id"] = s.pending_call
            s.pending_call = None
        s.transcript.append(item)
        await self._step(s.user_id, s.task_id, "observation", data)

    # --- a tool call -----------------------------------------------------------------------

    async def _tool(self, s: "_Run", a: prompts.ToolAction, tools: list[dict[str, Any]]) -> None:
        label_in = json.dumps(a.input, ensure_ascii=False, separators=(",", ":"))[:120]
        notes: list[str] = []
        if not a.node.strip():
            # no node named: the one node that offers this capability is meant (the project tools
            # live on one node only; a model leaves it out)
            offering = sorted({t["node"] for t in tools if t["capability"] == a.capability})
            if not offering and "." not in a.capability:
                # a bare "list": the one node whose capabilities hold exactly one ending in it
                offering = sorted(
                    {
                        t["node"]
                        for t in tools
                        if str(t["capability"]).rsplit(".", 1)[-1] == a.capability.strip()
                    }
                )
            if len(offering) == 1:
                a.node = offering[0]
        node, note = prompts.resolve_node(a.node, tools)
        if node is not None:
            if note:
                notes.append(note)
            cap, note = prompts.resolve_capability(
                a.capability, [t["capability"] for t in tools if t["node"] == node]
            )
            if cap is not None:
                if note:
                    notes.append(note)
                a.node, a.capability = node, cap
        label = f"{a.capability} on {a.node} {label_in}"
        info = {"node": a.node, "capability": a.capability, "input": a.input}
        match = next(
            (t for t in tools if t["node"] == a.node and t["capability"] == a.capability), None
        )
        if match is None:
            listing = "; ".join(
                f"{n}: {', '.join(sorted(t['capability'] for t in tools if t['node'] == n))}"
                for n in sorted({t["node"] for t in tools})
            )
            reason = refusal_reason(
                a.node, a.capability, tools, await self.tools.available(s.caller)
            )
            await self._observe(
                s,
                "error",
                prompts.observation(None, label, f"{reason} You can use — {listing}"),
                tool={**info, "ok": False, "code": "unknown_tool"},
            )
            return
        raw_input = json.dumps(a.input, ensure_ascii=False) if isinstance(a.input, dict) else ""
        if _ID_START.search(raw_input):
            seen = "\n".join(str(m.get("content") or "") for m in s.transcript)
            seen += "\n" + "\n".join(s.texts.values()) + "\n" + s.task_prompt
            a.input, id_notes = expand_short_ids(a.input, seen)
            if id_notes:
                notes += id_notes
                info = {**info, "input": a.input}
        resolved = s.hits.resolve(a.capability, a.input)
        if resolved is not None:  # the read means a hit of the run's own search (agent_hits.py)
            a.input, hit_note = resolved
            notes.append(hit_note)
            info = {**info, "input": a.input}
        polling = a.capability == agent_tests.STATUS
        if polling and isinstance(a.input, dict):
            # asking for a running test again and again is how one waits: the node holds the call
            # for up to 15 s, and the hub asks again (see _wait_for_test)
            a.input = {
                **a.input,
                "wait_seconds": max(10, min(int(a.input.get("wait_seconds") or 15), 15)),
            }
        key = call_key(a.node, a.capability, a.input)
        if key in s.failed_keys and not polling:
            # the same call failed a moment ago; running it again gives the same error (live
            # 2026-11-08: twelve reads of one wrong path, each one a real call, until no_progress)
            s.repeats += 1
            await self._observe(
                s,
                "error",
                f"This exact call failed a moment ago ({s.failed_keys[key]}). The same call "
                f"gives the same answer: change the input or finish. {_repeat_hint(a.capability)}",
                tool={**info, "ok": False, "code": "repeat"},
            )
            return
        if key in s.seen and not polling:
            ref = s.seen[key]
            kept = s.outputs.get(key)
            earlier = s.stages is not None and ref in list(s.tags)[: s.stage_refs]
            if earlier and kept and ref not in s.stage_shown:
                # a step starts with an empty transcript: what an earlier step saw is shown again,
                # not refused (live R4 2026-11-11: steps were refused their own earlier results)
                s.stage_shown.add(ref)
                await self._observe(
                    s,
                    "tool",
                    prompts.observation(ref, label, kept.first, note="from an earlier step"),
                    evidence=ref,
                    tool={**info, "ok": True, "code": "shown_again"},
                )
                return
            more = kept.next_part(s.limits.observation_chars) if kept else None
            if more is not None:
                # the result was cut for the prompt: asking again shows its next part instead of
                # a refusal (live: project.get asked three times for the rest it never got)
                text, note = more
                # reading on is progress (live 2026-11-17: three parts of a client list and one
                # repeat ended the run as no_progress before the phone in part four was seen)
                s.informative += 1
                await self._observe(
                    s,
                    "tool",
                    prompts.observation(ref, label, text, note=note),
                    evidence=ref,
                    tool={**info, "ok": True, "code": "continued"},
                )
                return
            if kept and ref in s.dropped and ref not in s.reshown:
                # the result was dropped from the prompt to save room: show it once more
                s.reshown.add(ref)
                await self._observe(
                    s,
                    "tool",
                    prompts.observation(ref, label, kept.first, note="shown again"),
                    evidence=ref,
                    tool={**info, "ok": True, "code": "shown_again"},
                )
                return
            s.repeats += 1
            await self._observe(
                s,
                "error",
                f"You already made this call: see [{s.seen[key]}]. A call that asks the same "
                f"counts as this one. {_repeat_hint(a.capability)}"
                + _asked_before(s.seen, a.node, a.capability),
                tool={**info, "ok": False, "code": "repeat"},
            )
            return
        file_key = (
            file_read_key(a.node, a.capability, a.input) if isinstance(a.input, dict) else None
        )
        reads_on = (
            file_key is not None
            and isinstance(a.input, dict)
            and s.file_next.get(file_key) is not None
            and a.input.get("start_line") == s.file_next[file_key]
        )  # the next part the hub itself named: a long file read in order is no search
        if (
            file_key
            and not reads_on
            and len(s.file_reads.get(file_key, [])) >= s.limits.max_file_reads
        ):
            s.repeats += 1
            refs = ", ".join(f"[{r}]" for r in s.file_reads[file_key])
            await self._observe(
                s,
                "error",
                f"You already read this file {len(s.file_reads[file_key])} times: see {refs}. "
                "Reading another range of it will not show more than that. Work with what you "
                "have, say what is missing in your finding, or finish.",
                tool={**info, "ok": False, "code": "repeat"},
            )
            return
        rule = s.workshop.refusal(
            a.capability, a.input, {t["capability"] for t in tools if t["node"] == a.node}
        )
        if rule:  # the workshop's copy is shared and nothing in it is committed: the hub guards it
            await self._observe(
                s,
                "error",
                prompts.observation(None, label, f"workshop_rule: {rule}"),
                tool={**info, "ok": False, "code": "workshop_rule"},
            )
            return
        why_not = s.web.refusal(a.capability, a.input)
        if why_not:  # the internet is asked a little; a refused call costs nothing
            # and is no failure: the hub's own limit, not a page that could not be read (live
            # 2026-11-12: the answer opened with "could not read Wikipedia (status 202)")
            await self._observe(
                s,
                "error",
                prompts.observation(None, label, f"web_limit: {why_not}"),
                tool={**info, "ok": False, "code": "web_limit"},
            )
            return
        s.web.note_call(a.capability, a.input)
        if not polling:  # asking after a running test costs no call of the budget
            s.calls += 1
        s.used.add(a.capability)
        try:
            # finding 03: a page only from this run's hits or the person's message
            vouched = (
                a.capability == web_access.FETCH
                and isinstance(a.input, dict)
                and s.web.origins.is_person(str(a.input.get("url") or ""))
            )
            not_given = s.web.url_refusal(a.capability, a.input)
            result = await self.tools.invoke(
                s.caller,
                uuid.UUID(match["node_id"]),
                a.capability,
                a.input,
                s.task_id,
                **({"person_url": True} if vouched else {}),
                **({"url_refusal": not_given} if not_given else {}),
            )
            if polling and result.get("ok"):
                result = await self._wait_for_test(s, match["node_id"], a, result)
        except ToolError as exc:
            if exc.code in ("private_data", URL_NOT_GIVEN):
                # the refused input is not kept: it held private data, or may (a made-up URL)
                label, info = f"{a.capability} on {a.node}", {**info, "input": {}}
            s.web.note_failure(a.capability)
            s.web.note_refused(a.input, exc.message)
            self._failure(
                s,
                a,
                exc.code,
                "refused: it would send private data"
                if exc.code == "private_data"
                else exc.message,
            )
            await self._observe(
                s,
                "error",
                prompts.observation(None, label, f"{exc.code}: {exc.message}")
                + (s.hits.listing(a.capability) if exc.code in MISSED_CODES else ""),
                tool={**info, "ok": False, "code": exc.code},
            )
            await self._step_check(s, a.capability, False, exc.code, exc.message, a.input)
            await self._count_call(s)
            return
        await self._count_call(s)
        if not result.get("ok"):
            err = dict(result.get("error") or {})
            if isinstance(err.get("message"), str):  # a Windows error as one line, not CLIXML
                err["message"] = prompts.plain_error(err["message"])
            if err.get("code") == "private_data":  # refused by the node: the input is not kept
                label, info = f"{a.capability} on {a.node}", {**info, "input": {}}
            if str(err.get("code", "error")) not in TRANSIENT_CODES and not polling:
                s.failed_keys[key] = (
                    f"{err.get('code', 'error')}: {str(err.get('message', ''))[:200]}"
                )
            end = _PAST_THE_END.search(str(err.get("message", "")))
            if end:
                # a range after the end of a file is the end, not a failure (live 2026-11-10: "has
                # only 67 lines" stood under "Not retrieved" and the run ended in no_progress)
                await self._observe(
                    s,
                    "error",
                    prompts.observation(
                        None,
                        label,
                        f"end of file: {end.group(1)} has {end.group(2)} lines, nothing comes "
                        "after them. You have read all of it.",
                    ),
                    tool={**info, "ok": False, "code": "end_of_file"},
                )
                return
            s.web.note_failure(a.capability)
            s.web.note_refused(a.input, str(err.get("message", "")))
            self._failure(
                s,
                a,
                str(err.get("code", "error")),
                "refused: it would send private data"
                if err.get("code") == "private_data"
                else str(err.get("message", "")),
            )
            await self._observe(
                s,
                "error",
                prompts.observation(
                    None, label, f"{err.get('code', 'error')}: {err.get('message', '')}"
                )
                + (s.hits.listing(a.capability) if err.get("code") in MISSED_CODES else ""),
                tool={
                    **info,
                    "ok": False,
                    "code": err.get("code", "error"),
                    "call_id": result.get("call_id"),
                },
            )
            await self._step_check(
                s,
                a.capability,
                False,
                str(err.get("code", "error")),
                str(err.get("message", "")),
                a.input,
            )
            return
        s.refused_writes.pop(a.capability, None)  # it went through after all
        if writes(a.capability):
            s.wrote = True
        full = prompts.output_text(result.get("output"))
        window = (
            prompts.read_window(
                result.get("output"),
                max(s.limits.observation_chars, prompts.FILE_OBSERVATION_CHARS),
            )
            if a.capability in FILE_WINDOW_CAPABILITIES
            else None
        )
        if window is not None:
            # a file as lines with the exact next call; reading on from there is no repeat
            full, nxt = window[0], window[1]
            shown, was_cut = full, False
            if file_key:
                s.file_next[file_key] = nxt
        else:
            shown, was_cut = prompts.cut(
                full, s.limits.observation_chars, newest=a.capability in LOG_CAPABILITIES
            )
        if was_cut and a.capability == "shell.read":
            shown += range_hint(full, shown)
        excerpt, _ = prompts.cut(
            full, EVIDENCE_EXCERPT_CHARS, newest=a.capability in LOG_CAPABILITIES
        )
        async with self._store(s.user_id) as store:
            task = await store.get(s.task_id)
            if task is None:
                return
            # notes of the operator are documents ([doc]), pages of the internet are [web];
            # everything a machine showed is [live]
            tag = (
                "doc"
                if a.capability in (SEARCH_CAPABILITY, READ_CAPABILITY, GUIDE_CAPABILITY)
                else "web"
                if web_access.is_web(a.capability)
                else "live"
            )
            url = web_access.source_url(a.capability, result.get("output"))
            ev = await store.add_evidence(
                task,
                tag,
                {
                    **info,
                    **({"url": url} if url else {}),
                    "key": key,
                    "at": result.get("at") or utcnow().isoformat(),
                    "call_id": result.get("call_id"),
                    "node_id": match["node_id"],
                },
                excerpt,
                int(result.get("bytes") or len(full)),
            )
        s.tags[ev.ref] = tag
        s.texts[ev.ref] = full[:KEPT_OUTPUT_CHARS]  # what a finding's quote is checked against
        # a result that brings nothing new (empty, or the same as an earlier one) is counted; after
        # a few in a row the model is told so (live 2026-11-09: the answer was in E2, the run went
        # on with eleven more variants of the same search until it was stopped for repeats)
        digest = hashlib.sha256(full.encode("utf-8", "replace")).hexdigest()
        same_as = s.digests.get(digest)
        s.digests.setdefault(digest, ev.ref)
        if same_as:
            notes.append(f"the same result as [{same_as}]")
        nothing_new = bool(same_as) or says_nothing(result.get("output"))
        s.nothing_new = s.nothing_new + 1 if nothing_new else 0
        # only a result with something new is progress for the progress guard (live 2026-11-20:
        # a Development check searched 49 steps for function names that do not exist; each empty
        # search counted as progress, so the guard never ended it)
        if a.capability not in NO_CONTENT_CAPABILITIES and not nothing_new:
            s.informative += 1
        s.test_runs.note(a.capability, result.get("output"))
        s.branches.note(a.capability, result.get("output"))
        s.workshop.note(a.capability, a.input, result.get("output"))
        if a.capability in WORKSHOP_CHANGES:
            # the workshop changed: a repeated repo.list, repo.status or repo.read now shows
            # something else, so it is not "the same call" any more
            prefix = json.dumps([a.node, "repo."])[:-2]
            s.seen = {k: v for k, v in s.seen.items() if not k.startswith(prefix)}
            reads = json.dumps([a.node, "repo.read"])[:-1]
            s.file_reads = {k: v for k, v in s.file_reads.items() if not k.startswith(reads)}
        s.seen[key] = ev.ref
        s.outputs[key] = KeptOutput.kept(full, shown, a.capability in LOG_CAPABILITIES)
        if file_key:
            s.file_reads.setdefault(file_key, []).append(ev.ref)
        s.web.note_result(a.capability, result.get("output"), ev.ref, a.input)
        s.hits.note(a.capability, result.get("output"), ev.ref)
        await self._observe(
            s,
            "tool",
            prompts.observation(
                ev.ref,
                label,
                shown,
                note=f"{ev.bytes} bytes"
                + (
                    ""
                    if not was_cut
                    else ", cut for the prompt"
                    if a.capability == "shell.read"
                    else f", {CUT_NOTE}"
                )
                + "".join(f" · {n}" for n in notes),
            )
            + (
                NOTHING_NEW_HINT if s.nothing_new and s.nothing_new % NOTHING_NEW_AFTER == 0 else ""
            ),
            evidence=ev.ref,
            tool={**info, "ok": True, "call_id": result.get("call_id")},
        )
        await self._step_check(s, a.capability, True, "", full, a.input)

    async def _step_check(
        self,
        s: "_Run",
        tool: str,
        ok: bool,
        code: str,
        output: str,
        tool_input: dict[str, Any],
    ) -> None:
        """Gründlich only: the notes on one tool step go into the run's steps while it goes on
        (step_check.py). A failed call gets its rule note; a result gets the small model's look."""
        model = s.limits.step_check_model
        if not model:
            return
        notes = step_check.rule_notes(tool, ok, code, output)
        if ok:

            async def complete(messages: list[dict[str, Any]], name: str, max_tokens: int) -> str:
                done = await asyncio.wait_for(
                    self.model.complete(messages, name, max_tokens), STEP_CHECK_SECONDS
                )
                return done.text

            note = await step_check.model_note(complete, model, s.prompt, tool, tool_input, output)
            if note:
                notes.append(note)
        for text in notes:
            await self._step(s.user_id, s.task_id, "step_check", {"tool": tool, "note": text})

    async def _wait_for_test(
        self, s: "_Run", node_id: str, a: prompts.ToolAction, result: dict[str, Any]
    ) -> dict[str, Any]:
        """``repo.test_status`` for a run that is still going: ask the node again (it holds each
        call for up to 15 s) until the run is over, ``test_wait_seconds`` have passed or the task's
        time is nearly used up. The model gets one answer, not one turn per 15 seconds."""
        until = min(time.monotonic() + s.limits.test_wait_seconds, s.deadline - 10)
        while agent_tests.state_of(result.get("output")) == "running" and time.monotonic() < until:
            try:
                again = await self.tools.invoke(
                    s.caller, uuid.UUID(node_id), a.capability, a.input, s.task_id
                )
            except ToolError:
                break
            if not again.get("ok"):
                break
            result = again
        return result

    @staticmethod
    def _failure(s: "_Run", a: prompts.ToolAction, code: str, message: str) -> None:
        """Remember a call that gave no result (at most 8, each call once)."""
        if writes(a.capability):  # a change the run wanted to prepare and did not
            s.refused_writes[a.capability] = " ".join(message.split())[:600]
            # a change made again with the refusal's fix is progress, not a stall (live
            # 2026-11-18: two corrected proposals in a row and the guard ended the run)
            s.informative += 1
        target = (
            str(a.input.get("url") or a.input.get("query") or "")
            if isinstance(a.input, dict)
            else ""
        )
        entry = {
            "capability": a.capability,
            "node": a.node,
            "target": target[:200],
            "code": code[:40],
            "message": " ".join(message.split())[:200],
        }
        key = (entry["capability"], entry["node"], entry["target"])
        if len(s.failed) < 8 and all(
            (f["capability"], f["node"], f["target"]) != key for f in s.failed
        ):
            s.failed.append(entry)

    async def _count_call(self, s: "_Run") -> None:
        async with self._store(s.user_id) as store:
            task = await store.get(s.task_id)
            if task is not None:
                task.tool_calls += 1
                await store.db.commit()

    # --- the end ---------------------------------------------------------------------------

    async def _conclude(self, s: "_Run", f: prompts.Finish, stop_reason: str | None) -> None:
        if s.stages is not None and s.task_prompt:
            s.prompt = s.task_prompt  # the checks below read the task, not the last step's text
        diagnosis = prompts.check_findings(f, s.tags, s.texts)
        if "table" in diagnosis and not prompts.asks_for_table(s.prompt):
            # a table nobody asked for (live 2026-11-10: a one-column table with an empty row)
            diagnosis.pop("table")
        await self._scope(s, diagnosis)
        diagnosis["notes"].extend(
            f"what a source can show: {n}" for n in prompts.source_notes(s.texts)
        )
        if not s.tags:
            diagnosis["notes"].append(
                "no tool result was collected: everything in this diagnosis is an assumption"
            )
        if stop_reason:
            diagnosis["notes"].append(f"the run stopped early: {stop_reason}")
        diagnosis["notes"].extend(s.hints)
        if s.prompt_parts:
            diagnosis["prompt_parts"] = s.prompt_parts
        if s.failed:
            diagnosis["failed_calls"] = s.failed
        others = await self._propose_sets(s, f.proposals or [], diagnosis)
        if others and self.proposals is not None:
            diagnosis["notes"].extend(
                await self.proposals.create_from_finish(s.caller, s.task_id, others, s.tags)
            )
        await self._step(s.user_id, s.task_id, "note", {"text": "diagnosis written"})
        await self._finish(
            s.user_id,
            s.task_id,
            "done",
            summary=diagnosis["summary"],
            diagnosis=diagnosis,
            stop_reason=stop_reason,
        )

    async def _propose_sets(
        self, s: "_Run", proposals: list[dict[str, Any]], diagnosis: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """A change set the model put into its finish instead of calling the tool is proposed now,
        with the same checks as the call (it only prepares; the person applies it). Live
        2026-11-14: a finished set with three task texts stood in the finish and was dropped
        as "not an action you may confirm", because the finish takes only actions of real nodes.
        The same for a batch or a plan of the project. Returns the other proposals, for the usual
        path."""
        others: list[dict[str, Any]] = []
        for item in proposals:
            cap = str(item.get("capability") or "").replace("_", ".")
            data = item.get("input")
            if cap in PREPARING_TOOLS and cap in s.used:
                continue  # prepared by a call of this run already: the finish only repeats it
            if cap not in PROJECT_PROPOSALS or not isinstance(data, dict):
                others.append(item)
                continue
            # the fields of a proposal (why, who notices) are not fields of the set
            data = {k: v for k, v in data.items() if k not in PROPOSAL_FIELDS}
            try:
                result = await self.tools.invoke(s.caller, PROJECT_NODE_ID, cap, data, s.task_id)
            except ToolError as exc:
                diagnosis["notes"].append(f"the {cap} of the finish was refused: {exc.message}")
                continue
            except Exception:  # a finish must be written whatever its proposal holds
                log.exception("agent task %s: the change set of the finish failed", s.task_id)
                diagnosis["notes"].append(f"the {cap} of the finish could not be prepared")
                continue
            if result.get("ok") is False:
                message = str((result.get("error") or {}).get("message") or "refused")
                diagnosis["notes"].append(f"the {cap} of the finish was refused: {message}")
            elif cap == CHANGESET_PROPOSE:
                diagnosis["notes"].append("the change set of the finish was prepared")
            else:
                diagnosis["notes"].append(f"the {cap} of the finish was prepared")
        return others

    async def _scope(self, s: "_Run", diagnosis: dict[str, Any]) -> None:
        """What the findings hold for (E3-07 D4): the machines that were asked and the ones the task
        could have asked and did not. A claim that something is absent names the first group."""
        try:
            tools = [t for t in await self._available(s.caller, s.scope) if not _virtual(t)]
            async with self._store(s.user_id) as store:
                evidence = list(await store.evidence(s.task_id))
        except Exception:
            log.exception("agent: could not work out which machines were asked")
            return
        asked, not_asked = coverage(tools, [(e.tag, e.source or {}) for e in evidence])
        language = hub_language("", s.prompt.split("\n\n(", 1)[0])  # the question, not our notes
        prompts.scope_findings(diagnosis, asked, not_asked, language)

    @staticmethod
    def _fit_to_context(s: "_Run", c: Completion) -> None:
        """The prompt budget never exceeds what the loaded model can hold: the setting
        (``context_chars``) is a wish, the slot of the model (say 16384 tokens on an 8 GB card) is a
        fact. Prompt + tool definitions + the room for the answer must fit into it, or the gateway
        answers "context full" in the middle of a run and the run is lost."""
        if c.prompt_tokens and s.sent_chars:
            # the tokenizer decides, not a rule of thumb: JSON and German text cost ~2.2 characters
            # per token, prose in English ~4
            s.chars_per_token = min(max(s.sent_chars / c.prompt_tokens, 1.5), 4.0)
        if not c.context_length:
            return
        per = s.chars_per_token
        # the answer may take at most an eighth of the slot; a section of 400 words is ~700 tokens
        cap = max(c.context_length // 8, 1024)
        if s.limits.native_output_tokens > cap or s.limits.max_output_tokens > cap:
            s.limits = replace(
                s.limits,
                native_output_tokens=min(s.limits.native_output_tokens, cap),
                max_output_tokens=min(s.limits.max_output_tokens, cap),
            )
        answer = s.limits.native_output_tokens if s.mode == "native" else s.limits.max_output_tokens
        room = c.context_length - answer - int(s.tools_chars / per) - CONTEXT_MARGIN_TOKENS
        chars = max(int(room * per), MIN_PROMPT_CHARS)
        if chars < s.limits.context_chars:
            log.info(
                "agent task %s: prompt budget %d -> %d characters (context %d tokens)",
                s.task_id,
                s.limits.context_chars,
                chars,
                c.context_length,
            )
            s.limits = replace(s.limits, context_chars=chars)

    async def _note_context(self, s: "_Run", c: Completion) -> None:
        """Once per run: the model is loaded with less context than an agent with tools needs.
        The hub asks a node for ``agent_min_context`` when it loads for an agent, but a model
        that is already running keeps what it was loaded with."""
        self._fit_to_context(s, c)
        floor = s.limits.min_context
        if s.context_noted or not floor or c.context_length is None or c.context_length >= floor:
            return
        s.context_noted = True
        text = (
            f"Note: {c.model} is loaded{f' on {c.node}' if c.node else ''} with a context of "
            f"{c.context_length} tokens; an agent with tools needs at least {floor} (tool "
            "results and the diagnosis must fit). Raise runner.model_context_lengths (or "
            "default_context_length) in that node's config and load the model again."
        )
        s.hints.append(text.removeprefix("Note: "))
        await self._step(s.user_id, s.task_id, "note", {"text": text})

    async def _wrap_up(self, s: "_Run", reason: str, deadline: float) -> None:
        """A limit was hit: one more question — write the diagnosis with what you have."""
        ask = (
            f"LIMIT reached ({reason}). You can look at nothing more in this run. Reply now "
            'with a "finish" action: summary, findings (tag what you did not see as '
            '"assumption") and advice.'
        )
        if reason == "repeated_calls":
            ask += (
                " You asked the same thing again, so do not ask it a third time: say what the "
                "results you already have show, even if they are incomplete, and name plainly "
                "what is missing. Do not answer only that you could not do it."
            )
        try:
            # only "finish" is offered: a model offered its tools calls one more of them (live
            # 2026-11-18: gpt-oss searched again at the stop, and the run ended without findings)
            completion = await self._ask(s, [], max(deadline, time.monotonic()), extra=ask)
        except ModelFailure as exc:
            await self._model_failed(s, exc, stop_reason=reason)
            return
        await self._observe(s, "note", ask)
        try:
            action, call = self._parse(s, completion)
        except prompts.ActionError as exc:
            action = None
            await self._assistant(
                s, completion, exc.call if isinstance(exc, (CallError, Truncated)) else None, None
            )
        else:
            await self._assistant(s, completion, call, action)
        if isinstance(action, prompts.Finish):
            await self._conclude(s, action, None if reason == STAGE_ANSWER else reason)
            return
        written = (completion.text or "").strip() if action is None else ""
        if s.research_depth is not None and len(written) >= WRITE_UP_MIN_CHARS:
            # Research: the model answered the stop with a text of its own instead of the action
            # that was asked for. That is the work of the run, and it is kept: as a section of
            # the report (research_profile.py), never as a diagnosis the hub vouches for.
            await self._step(
                s.user_id,
                s.task_id,
                "note",
                {
                    "text": "The model wrote up what it had read after the stop.",
                    REPORT_SECTION: {"title": WRITE_UP_KEY, "text": written[:WRITE_UP_MAX_CHARS]},
                },
            )
        fallback = prompts.Finish(
            f"The run stopped ({reason}) before a diagnosis was written. The evidence collected so "
            "far is listed with the task; Continue starts a new run with fresh limits.",
            [],
            "",
        )
        await self._conclude(s, fallback, None if reason == STAGE_ANSWER else reason)


PLAN_TOKENS = 700  # the plan is a short JSON object
TITLE_TOKENS = 1500  # a report title: a reasoning model thinks first (400 were used up live)
STAGE_ANSWER = "stage_answer"  # the answer step of a check in steps used its calls (no stop)
BUDGET_NOTE_AT = 3  # from this many calls or turns left the model is told how much is left


def writes(capability: str) -> bool:
    """A capability that prepares or makes a change of the person's data (a proposal, a write)."""
    return capability.endswith(".propose") or capability in project_write_tools.WRITE_NAMES


def budget_note(s: "_Run") -> str | None:
    """Tell the model what is left of its budget once little is: a limit is the safety net, not
    how a run should end. A model that knows it has two calls left answers with what it has
    instead of being cut off in the middle of a look. Said only near the end and only for a
    budget that can be planned (a run without counts, like Research, never gets it); the note is
    part of that one request and is not stored."""
    lim = s.limits
    calls = lim.max_tool_calls - s.calls
    turns = lim.max_turns - s.turns
    if lim.max_tool_calls < 5 or min(calls, turns) > BUDGET_NOTE_AT:
        return None
    return (
        f"Budget: {max(calls, 0)} tool call(s) and {max(turns, 0)} turn(s) are left in this run. "
        "Do not spend them on what you already know. If you can answer now, reply with a "
        '"finish" action and tag what you did not see as "assumption"; if one more look is '
        "essential, make it count."
    )


@dataclass
class _Run:
    """What one run carries between its steps."""

    task_id: uuid.UUID
    user_id: uuid.UUID
    caller: Caller
    prompt: str
    model: str | None
    transcript: list[dict[str, Any]]
    tags: dict[str, str]
    seen: dict[str, str]
    limits: Limits
    # The time the task message shows. Taken once per run: a clock read at every step changes the
    # second message of the prompt each minute and the model server then computes the whole
    # transcript behind it again (llama-server only reuses a byte-identical start of the prompt).
    clock: datetime = field(default_factory=lambda: utcnow())
    turns: int = 0
    calls: int = 0
    repeats: int = 0
    file_reads: dict[str, list[str]] = field(default_factory=dict)  # file key -> refs of its reads
    file_next: dict[str, int | None] = field(default_factory=dict)  # file key -> next start line
    failed_keys: dict[str, str] = field(default_factory=dict)  # call key -> "code: message"
    outputs: dict[str, "KeptOutput"] = field(default_factory=dict)  # call key -> its output
    dropped: set[str] = field(default_factory=set)  # refs whose output left the prompt
    reshown: set[str] = field(default_factory=set)  # refs shown again after they left it
    digests: dict[str, str] = field(default_factory=dict)  # output hash -> first ref
    texts: dict[str, str] = field(default_factory=dict)  # ref -> its output (for quotes)
    nothing_new: int = 0  # results in a row that were empty or the same as an earlier one
    nudged: bool = False
    # capability -> why its change was refused; a change that went through later is removed
    refused_writes: dict[str, str] = field(default_factory=dict)
    write_nudged: bool = False
    can_write: bool = False  # a tool that prepares or makes a change is offered
    catalogue: str = ""  # Projekte: the applications a task can run in, and what they can use
    wrote: bool = False  # such a call went through in this run
    change_nudged: bool = False
    cut_replies: int = 0  # answers in a row that hit the output limit
    context_noted: bool = False
    measured: bool = False  # the size of the tool definitions was logged once
    tools_chars: int = 0  # the size of the tool definitions sent with every request
    sent_chars: int = 0  # characters of the last request (messages and tool definitions)
    prompt_parts: dict[str, Any] = field(default_factory=dict)  # the first request, in parts
    hits: agent_hits.Hits = field(default_factory=lambda: agent_hits.Hits())
    stages: agent_stages.Stages | None = None  # the plan of a check in steps (Phase 1)
    task_prompt: str = ""  # the task as asked, while ``prompt`` is the current step's text
    stage_refs: int = 0  # how many evidence marks there were when the current step began
    stage_shown: set[str] = field(default_factory=set)  # earlier steps' results shown again
    chars_per_token: float = CHARS_PER_TOKEN  # measured from the model's own count, see _fit
    hints: list[str] = field(default_factory=list)  # remarks that end up in the diagnosis' notes
    # what this task saw of test runs, the tools it used, and whether the draft was already sent
    # back once for a claim the runs do not back / for the second model's finding
    test_runs: agent_tests.TestRuns = field(default_factory=agent_tests.TestRuns)
    used: set[str] = field(default_factory=set)
    test_nudged: bool = False
    branches: agent_done.Branches = field(default_factory=agent_done.Branches)
    workshop: agent_workshop.Workshop = field(default_factory=agent_workshop.Workshop)
    repos: list[tuple[str, str]] = field(default_factory=list)  # (name, mode) the user registered
    # the person's notes about names the question quotes ("Luxel" -> which device that is)
    name_notes: dict[str, list[str]] = field(default_factory=dict)
    branch_nudged: bool = False
    announce_nudged: bool = False
    blanket_nudged: bool = False  # a draft citing everything it read was sent back once
    prechecked: bool = False
    pages_nudged: int = 0  # a thorough research sent back for too few pages read
    author: str | None = None  # the model that wrote the answers of this run
    deadline: float = 0.0
    # how this run talks to the model: native tool calls or the JSON protocol; ``auto`` = fall back
    # to JSON when the model cannot do native calls
    mode: str = "json"
    auto: bool = False
    tool_index: dict[str, str] = field(default_factory=dict)
    pending_call: str | None = None  # the id of a native call that still wants its answer
    scope: dict[str, Any] | None = None  # nodes / tool groups the task may use (E3-04)
    proposable: list[dict[str, Any]] = field(default_factory=list)  # E3-01b
    web: web_access.WebBudget = field(default_factory=web_access.WebBudget)
    # Research: its frame (quick, default, thorough) and the progress guard (a model turn that
    # brings no new evidence counts as stalled)
    research_depth: str | None = None
    progress_mark: int = 0
    informative: int = 0  # results that carry content: what the progress guard counts
    counted_turns: int = 0
    stalled: int = 0
    # calls that did not deliver a result (a link that did not load, a refused or failed tool):
    # the diagnosis, the card and the answer say so instead of passing over them
    failed: list[dict[str, Any]] = field(default_factory=list)
