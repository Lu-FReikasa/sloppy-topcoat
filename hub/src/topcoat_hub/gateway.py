"""Model gateway (E1-05, ARCHITECTURE D18): routes OpenAI-style requests to a node that has
the model and streams the answer back through the node session. The hub never connects to a
node; ``llama-server`` stays on the node's loopback interface.
"""

import asyncio
import base64
import codecs
import contextlib
import contextvars
import functools
import json
import logging
import os
import re
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import legacy_policy, model_profiles, node_rename
from topcoat_hub.config import Settings
from topcoat_hub.job_log import NO_RECORD_WORK, JobLog, job_context, utcnow
from topcoat_hub.job_queue import (
    PRIORITY_ANSWER,
    PRIORITY_CHAT,
    PRIORITY_IDLE,
    JobQueue,
    can_start,
    clamp_place,
)
from topcoat_hub.live import STREAM_LOST, ActionTimeout, NodeOffline, NodeReadOnly, NodeSessions
from topcoat_hub.model_facts import PROOF_KEY, proven_kinds
from topcoat_hub.models import HubSetting, Node, NodeBenchmark, NodeInventory, NodeSpeed
from topcoat_hub.node_policy import ALL_NODES, POLICY_KEY, SAME_AS_ANSWER, NodePolicy
from topcoat_hub.pool_queue import PoolQueue, QueueFull
from topcoat_hub.protocol import envelope
from topcoat_hub.views import node_summary

log = logging.getLogger(__name__)

# States in which a node must not get inference work: the GPU belongs to a game or to
# another program (E1-06), or the runner does not work.
BLOCKED_STATES = {"busy_gaming", "yielded", "paused"}

RANK_LOADED, RANK_LOADING, RANK_ON_DISK = 0, 1, 2
# hub_settings: "alt" (the old order, default) or "neu" (the profile picks the first node,
# choice.py)
CHOICE_KEY = "gateway_choice"
# a measurement counts only from this size (shorter ones are mostly overhead)
MIN_SPEED_TOKENS = 30
MIN_SPEED_SECONDS = 0.5
MIN_LOAD_SECONDS = 2.0
_SAMPLES = {"tg_tps": "tg_samples", "load_mb_s": "load_samples"}


# What llama-server says when the context window is used up: mid-answer ("Context size has been
# exceeded", the KV window is shared by the requests on the model) or before the answer (the
# request alone is longer than a slot, type exceed_context_size_error).
_CONTEXT_FULL_MARKERS = (
    "context size has been exceeded",
    "exceeds the available context size",
    "exceed_context_size_error",
)

CONTEXT_FULL_MESSAGE = (
    "The model's context is full: other requests on this model occupy it, or this conversation "
    "no longer fits into one slot. Try again in a moment; if it keeps happening, use a shorter "
    "conversation, raise the model's context (runner.model_context_lengths) or give it fewer "
    "parallel slots (runner.n_parallel) on its node."
)


_IN_BACKGROUND_WORK: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "topcoat_background_work", default=False
)


_CONTEXT_KEYS = ("application", "conversation_id", "message_id", "task_id")


def _context_facts() -> dict[str, Any]:
    """What the caller said about the request (person and place), as text, for the live view.
    Only what is set; the person is not part of it (the view decides what an admin may see)."""
    ctx = job_context()
    return {k: str(v) for k in _CONTEXT_KEYS if (v := getattr(ctx, k)) is not None}


def caller_chain(depth: int = 6) -> str:
    """The functions that asked for a gateway request, innermost first, for the log only: which
    part of the hub loaded or used a model (``check<-summarise<-tick``)."""
    names: list[str] = []
    frame = sys._getframe(1)
    while frame is not None and len(names) < depth:
        if frame.f_globals.get("__name__") != __name__:
            names.append(frame.f_code.co_name)
        frame = frame.f_back
    return "<-".join(names)


_WORK_LABEL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "topcoat_work_label", default=None
)

# The hub's own chores: the answer is a short text or one JSON object, and a model that thinks first
# spends its token limit on thoughts (the hand-over note came back empty, 300 tokens out of 300, for
# weeks, and was tried again every five minutes). The thinking switch is sent for these unless the
# caller set one itself; a model without the switch ignores it.
CHORE_LABELS = frozenset(
    {
        "memory",
        "review",
        "digest_summary",
        "digest_check",
        "core_review",
        "care",
        "handover",
        "quality",  # the verdict on an answer (400 tokens, one JSON object)
        "precheck",  # the second model's look at an agent's draft (400 tokens, one JSON object)
        "theme_workshop",  # a patch to a theme the person shapes live (api/theme_workshop.py)
    }
)


def chore_payload(payload: dict[str, Any], label: str, path: str) -> dict[str, Any]:
    """``payload`` with thinking switched off when ``label`` names a chore of the hub."""
    if (
        label in CHORE_LABELS
        and path.endswith("/chat/completions")
        and "chat_template_kwargs" not in payload
        and "reasoning_effort" not in payload
    ):
        return {**payload, "chat_template_kwargs": {"enable_thinking": False}}
    return payload


@contextlib.contextmanager
def work(label: str) -> Iterator[None]:
    """Name what the gateway requests inside the block are for (``digest_summary``, ``care``,
    ...), so the nodes overview can say what a node is doing. Nothing else depends on it."""
    token = _WORK_LABEL.set(label)
    try:
        yield
    finally:
        _WORK_LABEL.reset(token)


# The places in a node's queue (lower goes first, see job_queue.py): a chat request, work that
# belongs to an answer that has just been given (summary, second look), and all other
# background work.

_PRIORITY: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "topcoat_queue_priority", default=None
)


def current_priority() -> int:
    """The queue place of a request made now: the one a block asked for, else a chat request
    (0) or, inside :func:`background_work`, the last place (2)."""
    chosen = _PRIORITY.get()
    if chosen is not None:
        return chosen
    return PRIORITY_IDLE if _IN_BACKGROUND_WORK.get() else PRIORITY_CHAT


@contextlib.contextmanager
def interactive_work(priority: int = PRIORITY_ANSWER) -> Iterator[None]:
    """Count the gateway requests inside the block as interactive (node policy ``no_chat``,
    normal model order) even when the surrounding task is background work. For a step that runs
    on the node that has just served a chat, so the node is evidently free and has the model.
    It queues behind chat requests (``priority``) but before other background work."""
    token = _IN_BACKGROUND_WORK.set(False)
    ptoken = _PRIORITY.set(priority)
    try:
        yield
    finally:
        _PRIORITY.reset(ptoken)
        _IN_BACKGROUND_WORK.reset(token)


def background_work(
    fn: Callable[..., Awaitable[Any]] | None = None, *, priority: int = PRIORITY_IDLE
) -> Any:
    """Mark a coroutine (and everything it awaits or spawns) as background work: its gateway
    requests prefer ``gateway_background_nodes`` (e.g. the laptop) over the gaming PC. Used bare
    it takes the last place in the node's queue (``PRIORITY_IDLE``, it waits until nothing more
    important is there); ``@background_work(priority=PRIORITY_ANSWER)`` is for work that belongs
    to an answer that was just given, such as the second look."""

    def decorate(inner: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(inner)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            token = _IN_BACKGROUND_WORK.set(True)
            ptoken = _PRIORITY.set(priority)
            try:
                return await inner(*args, **kwargs)
            finally:
                _PRIORITY.reset(ptoken)
                _IN_BACKGROUND_WORK.reset(token)

        return wrapper

    return decorate if fn is None else decorate(fn)


def pin_target(model: str, node: str | None) -> str:
    """``model@node`` for the node that wrote an answer, else the plain model name. A flow's answer
    carries the node ``"hub"``, which is no node a request can be pinned to."""
    return f"{model}@{node}" if node and node != "hub" and "@" not in model else model


def is_context_full(text: str) -> bool:
    """True if a model's error text means that its context window is full."""
    low = text.lower()
    return any(m in low for m in _CONTEXT_FULL_MARKERS)


CUT = "cut"  # GatewayError.reason: the answer broke off on the node's side (see call_streamed)


# how often a request that woke a node looks whether it has connected
WAKE_POLL_SECONDS = 1.0


class GatewayError(Exception):
    """An error answered to the client in OpenAI format."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        retry_after: int | None = None,
        reason: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after
        # the node's own reason when no node could serve ("insufficient_memory"), else ""
        self.reason = reason


# the context a load gets when neither the request nor the node's config says (the runner's)
DEFAULT_NODE_CONTEXT = 8192
# a loaded model that has less context than a request needs is reloaded with more at most this
# often per node and model (a reload that could not give more must not be tried on every request)
UPSIZE_RETRY_SECONDS = 600.0


@dataclass
class Candidate:
    node_id: uuid.UUID
    name: str
    rank: int
    can_load: bool
    context_length: int | None = None
    # Estimated seconds to a typical reply (load time + generation), None without a measured
    # speed for this node and model.
    est_seconds: float | None = None
    size_mb: float | None = None
    # requests the node serves at once: the sum of n_parallel of its loaded models (absent
    # counts 1); None when the node has no loaded model to report from
    slots: int | None = None


@dataclass
class Route:
    """Where a request for a model can go, best first, and which node the user would
    normally expect (the first in the configured order that has the model, whatever its
    state): answering elsewhere is a fallback the GUI shows."""

    candidates: list[Candidate]
    preferred: str | None
    preferred_state: str | None

    @property
    def context_length(self) -> int | None:
        return self.candidates[0].context_length if self.candidates else None


@dataclass
class Placement:
    """The node chosen for a chat request before its prompt is built (SCH-03 step 2): the model is
    loaded there and the context length is the one that node really has, so the prompt is sized
    for the node that runs the answer. No place in the queue is held: other model calls of the same
    answer (the memory search, a check) may need the node meanwhile, and a held place could block
    them. :meth:`Gateway.forward` tries this node first and waits for its place as always."""

    cand: Candidate
    route: Route
    model: str

    @property
    def context_length(self) -> int | None:
        return self.cand.context_length


@dataclass(frozen=True)
class StreamStatus:
    """The node says the answer stands still (``paused``: the person at that machine came first,
    E2-05 B) or goes on (``running``)."""

    state: str
    reasons: tuple[str, ...] = ()


@dataclass
class Upstream:
    """A started proxy stream: status and first data are known. ``close`` ends it early
    (cancels it on the node) and is safe to call more than once and from any context.

    ``body`` yields the bytes of the answer. A consumer that wants to show that the node
    paused takes ``status_body()`` instead (once, never both): it yields the same bytes plus
    :class:`StreamStatus` items. ``ended`` holds the error of the node's ``proxy_end`` (if any)
    once the body is exhausted."""

    status: int
    content_type: str
    body: AsyncIterator[bytes]
    node: str
    close: Callable[[], None]
    fallback_from: str | None = None
    fallback_state: str | None = None
    status_body: Callable[[], AsyncIterator[bytes | StreamStatus]] | None = None
    ended: dict[str, Any] = field(default_factory=dict)
    # set when the body ended because the node stayed silent for the idle limit (not because
    # the answer was complete): the part read so far is cut off
    stalled: list[bool] = field(default_factory=list)


def _stream_status(msg: dict[str, Any]) -> StreamStatus:
    body = msg["body"]
    return StreamStatus(body["state"], tuple(body.get("reasons") or ()))


TAIL_BYTES = 8192
_STAT_PATTERNS = {
    "tokens": re.compile(rb'"completion_tokens":\s*(\d+)'),
    "prompt_tokens": re.compile(rb'"prompt_tokens":\s*(\d+)'),
    "cached_tokens": re.compile(rb'"(?:cached_tokens|cache_n)":\s*(\d+)'),
    "prompt_ms": re.compile(rb'"prompt_ms":\s*([\d.]+)'),
    "predicted_ms": re.compile(rb'"predicted_ms":\s*([\d.]+)'),
}
_FINISH = re.compile(rb'"finish_reason":\s*"([a-z_]+)"')


class StatsTail:
    """The last bytes of an answer as it passes through, and what the model said about itself there
    (llama.cpp ends an answer with ``usage`` and ``timings``). It reads nothing while the answer
    runs and costs one small copy per chunk; the numbers are looked up once, when the request is
    over. This is what fills the tokens and times of the record of a request."""

    __slots__ = ("buf",)

    def __init__(self) -> None:
        self.buf = b""

    def feed(self, data: bytes) -> None:
        self.buf = (self.buf + data)[-TAIL_BYTES:]

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, pattern in _STAT_PATTERNS.items():
            found = pattern.findall(self.buf)
            if found:
                out[key] = int(float(found[-1]))
        finish = _FINISH.findall(self.buf)
        if finish:
            out["finish_reason"] = finish[-1].decode("ascii")[:16]
        return out


class StreamAssembler:
    """Puts the ``data:`` events of a streamed chat completion back together into the JSON a
    non-streaming request returns (content, tool calls by index, finish reason, usage)."""

    def __init__(self) -> None:
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.text: list[str] = []
        self.reasoning: list[str] = []  # what a reasoning model thought (reasoning_content)
        self.calls: dict[int, dict[str, Any]] = {}
        self.finish_reason = ""
        self.usage: dict[str, Any] = {}
        self.done = False
        self.error = ""

    def feed(self, data: bytes) -> None:
        self._buffer += self._decoder.decode(data).replace("\r\n", "\n")
        while "\n\n" in self._buffer:
            block, self._buffer = self._buffer.split("\n\n", 1)
            self._event(block)

    def _event(self, block: str) -> None:
        payload = "\n".join(
            line[5:].lstrip() for line in block.split("\n") if line.startswith("data:")
        )
        if not payload:
            return
        if payload == "[DONE]":
            self.done = True
            return
        try:
            obj = json.loads(payload)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        if "error" in obj:
            err = obj["error"]
            self.error = str(err.get("message") if isinstance(err, dict) else err)[:300]
            return
        usage = obj.get("usage")
        if isinstance(usage, dict) and usage:
            self.usage = usage
        timings = obj.get("timings")
        if isinstance(timings, dict) and not self.usage:  # llama.cpp without usage
            self.usage = {
                "prompt_tokens": timings.get("prompt_n"),
                "completion_tokens": timings.get("predicted_n"),
            }
        choices = obj.get("choices")
        if not (isinstance(choices, list) and choices and isinstance(choices[0], dict)):
            return  # a progress event (prompt_progress) carries no choice: it only shows life
        choice = choices[0]
        delta = choice.get("delta")
        if isinstance(delta, dict):
            if isinstance(delta.get("content"), str):
                self.text.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                self.reasoning.append(delta["reasoning_content"])
            raw = delta.get("tool_calls")
            for call in raw if isinstance(raw, list) else []:
                if isinstance(call, dict):
                    self._call(call)
        if isinstance(choice.get("finish_reason"), str) and choice["finish_reason"]:
            self.finish_reason = choice["finish_reason"]

    def _call(self, piece: dict[str, Any]) -> None:
        index = piece.get("index")
        index = index if isinstance(index, int) else len(self.calls)
        call = self.calls.setdefault(
            index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
        )
        if isinstance(piece.get("id"), str) and piece["id"]:
            call["id"] = piece["id"]
        fn = piece.get("function")
        if isinstance(fn, dict):
            if isinstance(fn.get("name"), str):
                call["function"]["name"] += fn["name"]
            if isinstance(fn.get("arguments"), str):
                call["function"]["arguments"] += fn["arguments"]

    def result(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": "".join(self.text)}
        if self.reasoning:
            message["reasoning_content"] = "".join(self.reasoning)
        if self.calls:
            message["tool_calls"] = [self.calls[i] for i in sorted(self.calls)]
        return {
            "choices": [
                {"index": 0, "message": message, "finish_reason": self.finish_reason or "stop"}
            ],
            "usage": self.usage,
        }


_background: set[asyncio.Task[None]] = set()


def spawn(coro: Awaitable[None]) -> "asyncio.Future[None]":
    """Run cleanup that must not be cut short by a cancelled request (a client that went
    away cancels the response task; awaiting in a ``finally`` there would be cancelled too), or
    work that outlives its request (an answer, E3-07). Returns the task."""
    task = asyncio.ensure_future(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def drain_background(grace: float = 3.0) -> int:
    """End the fire-and-forget tasks of **this** event loop before the hub goes down: they get
    ``grace`` seconds to finish, what is left is cancelled and awaited, so no task is dropped
    in the middle of a query with its database connection checked out (an abandoned
    connection stays "idle in transaction" and blocks everything that needs a lock on its
    tables). Returns how many had to be cancelled. A task of another loop (the set is
    module-wide) is not ours to await and is left alone."""
    loop = asyncio.get_running_loop()
    mine = [t for t in _background if t.get_loop() is loop and not t.done()]
    if not mine:
        return 0
    _, pending = await asyncio.wait(mine, timeout=grace)
    for task in pending:
        task.cancel()
    await asyncio.gather(*mine, return_exceptions=True)
    return len(pending)


@dataclass
class _NodeView:
    id: uuid.UUID
    name: str
    state: str
    connected: bool
    allowed: set[str]
    models: dict[str, bool] = field(default_factory=dict)  # name -> loaded
    loading: set[str] = field(default_factory=set)
    contexts: dict[str, int] = field(default_factory=dict)  # loaded model -> context length
    sizes_mb: dict[str, float] = field(default_factory=dict)  # model -> size on disk
    speeds: dict[str, float] = field(default_factory=dict)  # model -> measured tokens per second
    load_speeds: dict[str, float] = field(default_factory=dict)  # model -> measured MB per second
    parallel: dict[str, int] = field(default_factory=dict)  # loaded model -> n_parallel (absent: 1)
    # model -> the slots the node's config asks for when it loads it (runner.model_n_parallel,
    # else runner.n_parallel); absent: the runner's default, one
    configured_parallel: dict[str, int] = field(default_factory=dict)
    # model -> what the header of its file states (the node reports it; absent: not read)
    gguf: dict[str, dict[str, Any]] = field(default_factory=dict)
    # memory a model may still take (MB): RAM free, and VRAM free too when the node reports it
    # (the smaller of the two); None when the node says nothing about its memory
    memory_free_mb: float | None = None
    ram_free_mb: float | None = None  # free RAM alone (the floor of a node is kept on this)

    def slots(self) -> int | None:
        """Requests this node serves at once: the sum of ``n_parallel`` over its loaded models."""
        return sum(self.parallel.values()) or None


NodeView = _NodeView


def configured_slots(
    config: Mapping[str, Any] | None, models: list[dict[str, Any]]
) -> dict[str, int]:
    """The slots a node will start each of its models with: ``runner.model_n_parallel[model]``,
    else ``runner.n_parallel`` (the node's own rule, protocol/config.go ``ParallelFor``)."""
    runner = (config or {}).get("runner") or {}
    per_model = runner.get("model_n_parallel") or {}
    default = runner.get("n_parallel") or 0
    out: dict[str, int] = {}
    for m in models:
        name = m.get("name")
        n = per_model.get(name) or default
        if name and isinstance(n, int) and n > 1:
            out[name] = n
    return out


def memory_budgets(summary: dict[str, Any]) -> tuple[float | None, float | None]:
    """Free RAM and free VRAM of a node (MB), each ``None`` when the node does not report it.
    VRAM is summed over the GPUs."""
    ram: float | None = None
    total, used = summary.get("ram_total_mb"), summary.get("ram_used_mb")
    if isinstance(total, int | float) and isinstance(used, int | float):
        ram = float(total) - float(used)
    vram: float | None = None
    for gpu in summary.get("gpus") or []:
        total, used = gpu.get("vram_total_mb"), gpu.get("vram_used_mb")
        if isinstance(total, int | float) and isinstance(used, int | float):
            vram = (vram or 0.0) + float(total) - float(used)
    return ram, vram


def free_memory(summary: dict[str, Any]) -> float | None:
    """What a model may take on a node in total (MB): free RAM plus free VRAM. This is the summed
    rule, used while the node does not say how a model is split between GPU and RAM.
    ``None``: nothing reported."""
    parts = [part for part in memory_budgets(summary) if part is not None]
    return sum(parts) if parts else None


def plan_room(free_mb: float, need_mb: float, idle: list[tuple[str, float]]) -> list[str] | None:
    """The idle models to unload, in the order given (the least recently used first), so that
    ``need_mb`` fits into what is free; ``[]`` when it fits already, ``None`` when even all of
    the idle models would not make room (the load waits)."""
    if free_mb >= need_mb:
        return []
    victims: list[str] = []
    available = free_mb
    for name, size in idle:
        victims.append(name)
        available += size
        if available >= need_mb:
            return victims
    return None


def plan_load(
    ram_free_mb: float | None,
    total_free_mb: float,
    need_mb: float | None,
    buffer_percent: float,
    min_free_ram_mb: float,
    idle: list[tuple[str, float]],
) -> list[str] | None:
    """The idle models to unload before a model of ``need_mb`` loads, or ``None`` when it waits.
    Two rules: a node with less than ``min_free_ram_mb`` free RAM loads nothing (idle models go
    until the floor is reached), and the model with ``buffer_percent`` of its size must fit into
    the free memory (RAM plus VRAM). ``need_mb`` ``None``: the size is unknown, only the floor
    applies. Unloaded sizes count as freed in both rules, an approximation: a model in VRAM frees
    VRAM, not RAM."""
    sizes = dict(idle)
    victims: list[str] = []
    freed = 0.0
    if ram_free_mb is not None and ram_free_mb < min_free_ram_mb:
        floor_plan = plan_room(ram_free_mb, min_free_ram_mb, idle)
        if floor_plan is None:
            return None
        victims = list(floor_plan)
        freed = sum(sizes[m] for m in victims)
    if need_mb is not None:
        rest = [(m, s) for m, s in idle if m not in victims]
        memory_plan = plan_room(total_free_mb + freed, need_mb * (1 + buffer_percent / 100), rest)
        if memory_plan is None:
            return None
        victims.extend(memory_plan)
    return victims


def split_model(requested: str) -> tuple[str, str | None]:
    """``model@node`` pins a node, ``model@a,b`` lets the request go to either; the model name
    itself never contains ``@``."""
    model, sep, node = requested.rpartition("@")
    if not sep:
        return requested, None
    # a node that was renamed is still reached by its earlier name (node_rename.py)
    return model, ",".join(node_rename.current_name(n) for n in node.split(","))


class Gateway:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        sessions: NodeSessions,
        settings: Settings,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.sessions = sessions
        self.settings = settings
        self._loads: dict[tuple[uuid.UUID, str], asyncio.Future[None]] = {}
        self._last_used: dict[
            tuple[str, str], float
        ] = {}  # (node, model) -> monotonic end of the last request
        # nodes that refused ``min_context_length`` (older than consolidation step 2): they get
        # the explicit ``context_length`` the hub always sent
        self._legacy_nodes: set[uuid.UUID] = set()
        self._resolver: Any = None
        self._upsize_locks: dict[tuple[uuid.UUID, str], asyncio.Lock] = {}
        self._upsize_tried: dict[tuple[uuid.UUID, str], tuple[int, float]] = {}
        self._slots: dict[uuid.UUID, JobQueue] = {}
        # the record of requests (SCH-03 step 3); None = not kept (tests, tools)
        self.job_log: JobLog | None = None
        self._busy: dict[int, dict[str, Any]] = {}  # what the nodes do for the hub right now
        self._busy_seq = 0
        # what an admin set in the GUI (hub_settings) replaces the environment values
        self._policy = NodePolicy()
        self._apply_policy()
        # what the probes proved (model_probes.py): {model: {probe: {"ok", "node", "at"}}}
        self.proofs: dict[str, dict[str, Any]] = {}
        # the best setting per model and kind of work (model_profiles.py), taken by the admin
        self.profiles: dict[str, dict[str, str]] = {}
        # "alt" (the old order, default) or "neu" (the profile of the work picks the first node,
        # choice.py); an admin switches it, see api/scheduling.py
        self.choice_mode: str = "alt"
        # wakes a sleeping node (Wake-on-LAN; wake.demand_waker): (node id, model) -> packets
        # sent. None = the hub never wakes a node on demand (tests, tools)
        self.waker: Callable[[uuid.UUID, str], Awaitable[int]] | None = None
        self._waking: dict[uuid.UUID, float] = {}  # node -> monotonic end of its wake wait
        # newest current score of the "answer" suite per model, from the bench (refreshed with
        # the proofs, so a new rating is used after the next load_proofs)
        self.answer_quality: dict[str, float] = {}
        # one queue for the nodes that can serve a request: the node is chosen late (pool_queue.py)
        self.free_listeners: list[Callable[[], None]] = []
        self._pool = PoolQueue(
            lambda offer: self._slot(offer.node_id, getattr(offer, "slots", None)),
            max_waiting=settings.gateway_queue_max_waiting,
            max_per_source=settings.gateway_queue_max_waiting_per_source,
            aging_seconds=settings.gateway_queue_aging_seconds,
        )

    @property
    def resolver(self) -> Any:
        """Which node and model a piece of work uses (model_resolver.py)."""
        if self._resolver is None:
            from topcoat_hub.model_resolver import ModelResolver

            self._resolver = ModelResolver(self)
        return self._resolver

    async def node_views(self, all_models: bool = False) -> list[NodeView]:
        """What the hub knows about every node: state, models, sizes, measured speeds. Only the
        models of the node's pool (``NodePolicy.pools``) are in it, unless ``all_models``."""
        return await self._nodes(all_models=all_models)

    def _apply_policy(self) -> None:
        # the order of the nodes for requests that name no application: the row ``api``
        self._order = {name: i for i, name in enumerate(self._policy.row_nodes("api"))}
        # the nodes background work tries first: those of the row ``background``
        self._background_nodes = set(self._policy.row_nodes("background"))
        # background chat work asks for the one model of its row (the first that is named), so that
        # the node it prefers keeps one model loaded and never loads a second one next to it
        background = self._policy.applications.get("background")
        self._background_model = next(
            (m for m in (background.models if background else ()) if m != SAME_AS_ANSWER), ""
        )

    @property
    def policy(self) -> NodePolicy:
        return self._policy

    def set_policy(self, policy: NodePolicy) -> None:
        """Use the admin's scheduling choices from now on (the next request sees them)."""
        self._policy = policy
        self._apply_policy()

    async def load_policy(self, env: Mapping[str, str] | None = None) -> None:
        """Read the stored scheduling choices (at startup); a missing or broken value is unset.

        The older lists (a default model per node, the model roles, lists per application, the
        order of nodes, nodes kept free, the node list per task) and the old environment variables
        (``legacy_policy.LEGACY_ENV``) become rows of the table here, once: the result is written
        back, so the next start reads the table only."""
        async with self.sessionmaker() as db:
            row = await db.get(HubSetting, POLICY_KEY)
            stored = row.value if row else None
            names = list((await db.scalars(select(Node.name))).all())
            policy, changed, used = legacy_policy.convert(
                stored, os.environ if env is None else env, names
            )
            if changed:
                log.warning(
                    "node policy: the older lists%s became the table of applications; "
                    "the stored policy is rewritten%s",
                    " and the environment" if used else "",
                    f" (variables read once, remove them from the environment: {', '.join(used)})"
                    if used
                    else "",
                )
                value = policy.to_json() or None
                if row is None:
                    db.add(HubSetting(key=POLICY_KEY, value=value))
                else:
                    row.value = value
                await db.commit()
        self.set_policy(policy)
        await self.load_proofs()

    async def load_proofs(self) -> None:
        """The probe results and the profiles stored in hub_settings; broken = none."""
        async with self.sessionmaker() as db:
            row = await db.get(HubSetting, PROOF_KEY)
            stored = row.value if row else None
        self.proofs = stored if isinstance(stored, dict) else {}
        async with self.sessionmaker() as db:
            row = await db.get(HubSetting, model_profiles.PROFILE_KEY)
        try:
            self.profiles = model_profiles.check(row.value if row else None)
        except model_profiles.ProfileError:
            self.profiles = {}
        async with self.sessionmaker() as db:
            mode = await db.get(HubSetting, CHOICE_KEY)
        self.choice_mode = mode.value if mode and mode.value in ("alt", "neu") else "alt"
        from topcoat_hub.bench.service import latest_ratings

        quality: dict[str, float] = {}
        for r in await latest_ratings(self.sessionmaker):
            if r["suite"] == "answer" and r["current"] and not r["variant"]:
                quality[r["model"]] = max(quality.get(r["model"], 0.0), float(r["score"]))
        self.answer_quality = quality

    @property
    def proven_kinds(self) -> dict[str, tuple[str, ...]]:
        return proven_kinds(self.proofs)

    @property
    def order(self) -> list[str]:
        """The node order in use (the admin's, else the environment's)."""
        return sorted(self._order, key=lambda n: self._order[n])

    @property
    def background_nodes(self) -> list[str]:
        """Nodes that background work prefers (the admin's choice, else the environment's)."""
        return sorted(self._background_nodes)

    def background_allowed(self) -> set[str] | None:
        """The nodes background work may use by the node policy, or ``None`` for all of them.
        The same rule as the gateway applies to a background request (``_allowed_by_policy``):
        a checker that is chosen outside this set would be refused, so the choice uses it too."""
        key = "background"
        row = self._policy.applications.get(key)
        named = self._policy.row_nodes(key)
        if named and row is not None and ALL_NODES not in row.nodes:
            return set(named)
        return None

    # --- what the nodes are doing ---------------------------------------------------------

    def _track(self, node: str, model: str, kind: str | None = None) -> int:
        """Note that ``node`` works for the hub (a request, or a model load as ``load``)."""
        self._busy_seq += 1
        label = kind or _WORK_LABEL.get() or ("background" if _IN_BACKGROUND_WORK.get() else "chat")
        self._busy[self._busy_seq] = {
            "node": node,
            "model": model,
            "kind": label,
            "since": time.time(),
            **_context_facts(),
        }
        return self._busy_seq

    def _untrack(self, key: int) -> None:
        entry = self._busy.pop(key, None)
        if entry is not None and entry.get("kind") != "load":
            self._last_used[(entry["node"], entry["model"])] = time.monotonic()

    def waiting(self, node_id: uuid.UUID) -> dict[str, int]:
        """The requests that wait for a free place on this node, by place in its queue."""
        slot = self._slots.get(node_id)
        places = slot.waiting() if slot is not None else []
        return {
            "chat": places.count(PRIORITY_CHAT),
            "answer": places.count(PRIORITY_ANSWER),
            "idle": places.count(PRIORITY_IDLE),
        }

    def activity(self) -> list[dict[str, Any]]:
        """The requests and loads running now, oldest first: node, model, kind, seconds."""
        now = time.time()
        return [
            {
                "node": b["node"],
                "model": b["model"],
                "kind": b["kind"],
                "seconds": max(0, int(now - b["since"])),
                **{k: b[k] for k in _CONTEXT_KEYS if k in b},
            }
            for b in sorted(self._busy.values(), key=lambda b: b["since"])
        ]

    def waiting_jobs(self, node_id: uuid.UUID) -> list[dict[str, Any]]:
        """The requests that wait for a place on this node, with what they are and for how long."""
        slot = self._slots.get(node_id)
        return slot.waiting_jobs() if slot is not None else []

    # --- node view ------------------------------------------------------------------------

    async def _nodes(self, all_models: bool = False) -> list[_NodeView]:
        async with self.sessionmaker() as db:
            rows = await db.execute(
                select(Node, NodeInventory).outerjoin(
                    NodeInventory, NodeInventory.node_id == Node.id
                )
            )
            out = []
            speeds: dict[uuid.UUID, dict[str, float]] = {}
            bench = await db.execute(
                select(NodeBenchmark.node_id, NodeBenchmark.model, NodeBenchmark.tg_tps).order_by(
                    NodeBenchmark.ts.desc(), NodeBenchmark.id.desc()
                )
            )
            for bnode, bmodel, tg in bench.all():
                if tg and tg > 0:
                    speeds.setdefault(bnode, {}).setdefault(bmodel, float(tg))  # newest wins
            load_speeds: dict[uuid.UUID, dict[str, float]] = {}
            for sp in (await db.scalars(select(NodeSpeed))).all():
                if sp.tg_tps and sp.tg_tps > 0:  # measured in real answers: before the benchmark
                    speeds.setdefault(sp.node_id, {})[sp.model] = float(sp.tg_tps)
                if sp.load_mb_s and sp.load_mb_s > 0:
                    load_speeds.setdefault(sp.node_id, {})[sp.model] = float(sp.load_mb_s)
            for node, inv in rows.all():
                connected = self.sessions.is_connected(node.id)
                summary = node_summary(node, inv, connected)
                session = self.sessions.get(node.id)
                out.append(
                    _NodeView(
                        id=node.id,
                        name=node.name,
                        state=summary["state"],
                        connected=connected and session is not None and not session.read_only,
                        allowed=set(node.config.get("allowed_actions", [])),
                        models={
                            m["name"]: bool(m.get("loaded"))
                            for m in summary["models"]
                            if all_models or self._policy.in_pool(node.name, m["name"])
                        },
                        loading={
                            m
                            for m in summary.get("loading", [])
                            if all_models or self._policy.in_pool(node.name, m)
                        },
                        sizes_mb={
                            m["name"]: m["size_bytes"] / 1048576
                            for m in summary["models"]
                            if isinstance(m.get("size_bytes"), int | float)
                        },
                        memory_free_mb=free_memory(summary),
                        ram_free_mb=memory_budgets(summary)[0],
                        speeds=speeds.get(node.id, {}),
                        load_speeds=load_speeds.get(node.id, {}),
                        contexts={
                            m["name"]: m["context_length"]
                            for m in summary["models"]
                            if m.get("loaded") and m.get("context_length")
                        },
                        # the header facts, plus the projector the node loads with the model
                        # (D125): model_facts reads "can see" from it
                        gguf={
                            m["name"]: {
                                **(m.get("gguf") if isinstance(m.get("gguf"), dict) else {}),
                                **({"projector": m["projector"]} if m.get("projector") else {}),
                            }
                            for m in summary["models"]
                            if isinstance(m.get("gguf"), dict) or m.get("projector")
                        },
                        parallel={
                            m["name"]: max(1, int(m.get("n_parallel") or 1))
                            for m in summary["models"]
                            if m.get("loaded")
                        },
                        configured_parallel=configured_slots(node.config, summary["models"]),
                    )
                )
            return out

    def _place(self, base: int) -> int:
        """The queue place of a request after the priority of its row in the table of applications
        (SCH-04): ``high`` is one place better, ``low`` one worse, ``normal`` and no priority leave
        it. The row is the one of the application the work is for (the job context), else, for
        background work, the row ``background``."""
        rows = self._policy.applications
        row = rows.get(job_context().application or "")
        if (row is None or row.priority is None) and _IN_BACKGROUND_WORK.get():
            row = rows.get("background")
        if row is None or row.priority is None:
            return base
        return clamp_place(base + {"high": -1, "normal": 0, "low": 1}[row.priority])

    def _order_key(self, name: str) -> tuple[int, str]:
        return (self._order.get(name, len(self._order)), name)

    def _lane(self, name: str) -> int:
        """0 for a node background work tries first (only inside :func:`background_work`)."""
        return 0 if not _IN_BACKGROUND_WORK.get() or name in self._background_nodes else 1

    async def list_models(self) -> list[dict[str, Any]]:
        """Models on connected nodes that allow gateway requests, OpenAI ``/v1/models`` style."""
        by_model: dict[str, list[dict[str, Any]]] = {}
        for n in await self._nodes():
            if not n.connected or "models.proxy" not in n.allowed:
                continue
            for model, loaded in n.models.items():
                by_model.setdefault(model, []).append(
                    {"node": n.name, "loaded": loaded, "state": n.state}
                )
        return [
            {
                "id": model,
                "object": "model",
                "created": 0,
                "owned_by": "topcoat",
                "topcoat": {"nodes": sorted(nodes, key=lambda x: self._order_key(x["node"]))},
            }
            for model, nodes in sorted(by_model.items())
        ]

    def background_model(self, model: str, pin: str | None) -> str:
        """The model a background chat request is answered by: the configured background model
        (``gateway_background_model``) instead of the one asked for; a pinned request
        (``model@node``) is left alone."""
        wanted = self._background_model
        if wanted and pin is None and _IN_BACKGROUND_WORK.get():
            return wanted
        return model

    async def route(self, requested: str) -> Route:
        """Plan a request for ``requested`` (``model`` or ``model@node``); raises GatewayError
        if no node can serve it."""
        model, pin = split_model(requested)
        return await self.candidates(self.background_model(model, pin), pin)

    async def preview(self, requested: str, background: bool) -> Route:
        """The route a request would take now, for the scheduling view; nothing is started."""
        token = _IN_BACKGROUND_WORK.set(background)
        try:
            return await self.route(requested)
        finally:
            _IN_BACKGROUND_WORK.reset(token)

    async def candidates(self, model: str, pin: str | None) -> Route:
        """Nodes that can answer for ``model``, best first. Raises GatewayError if none.

        When no connected node can serve and the model is the house model of a node that is
        offline (asleep), that node is woken and the request waits for it (plan step 1.7, E2):
        it shows as ``waking`` in the activity, and ends with ``wake_timeout`` when the node does
        not come within ``wake_timeout_seconds``.
        """
        try:
            return await self._candidates(model, pin)
        except GatewayError as exc:
            if exc.code != "model_unavailable" or self.waker is None:
                raise
            asleep = await self._sleeping_house_nodes(model, pin)
            if not asleep:
                raise
            return await self._wake_and_wait(asleep, model, pin, exc)

    async def _sleeping_house_nodes(self, model: str, pin: str | None) -> list[_NodeView]:
        """Offline nodes whose house model is ``model`` (and that the pin allows)."""
        wanted = set(pin.split(",")) if pin else None
        return [
            n
            for n in await self._nodes()
            if not self.sessions.is_connected(n.id)
            and self._policy.house.get(n.name) == model
            and (wanted is None or n.name in wanted)
        ]

    async def _wake_and_wait(
        self, nodes: list[_NodeView], model: str, pin: str | None, first: GatewayError
    ) -> Route:
        timeout = self.settings.wake_timeout_seconds
        if timeout <= 0 or self.waker is None:
            raise first
        now = time.monotonic()
        woken: list[str] = []
        for n in nodes:
            if self._waking.get(n.id, 0.0) > now:
                woken.append(n.name)  # another request woke it already: wait with it
                continue
            try:
                sent = await self.waker(n.id, model)
            except OSError as exc:
                log.warning("waking %s failed: %s", n.name, exc)
                continue
            if sent:
                self._waking[n.id] = now + timeout
                woken.append(n.name)
                log.info("woke %s for %s (%d packets)", n.name, model, sent)
        if not woken:
            raise first
        keys = [self._track(name, model, "waking") for name in woken]
        deadline = max(self._waking.get(n.id, now) for n in nodes)
        try:
            while True:
                try:
                    return await self._candidates(model, pin)
                except GatewayError as exc:
                    if exc.code != "model_unavailable" or time.monotonic() >= deadline:
                        break
                await asyncio.sleep(WAKE_POLL_SECONDS)
        finally:
            for key in keys:
                self._busy.pop(key, None)
        names = ", ".join(woken)
        raise GatewayError(
            503,
            "wake_timeout",
            f"{names} did not wake within {int(timeout)} s to serve {model!r} "
            "(Wake-on-LAN sent; is the machine on standby with Wake-on-LAN enabled, and does the "
            "packet reach its LAN?)",
            retry_after=self.settings.gateway_retry_after_seconds,
        )

    def waking_until(self, node_id: uuid.UUID) -> float | None:
        """The monotonic end of the wait for a node being woken (None: not being woken)."""
        until = self._waking.get(node_id)
        return until if until is not None and until > time.monotonic() else None

    async def _candidates(self, model: str, pin: str | None) -> Route:
        nodes = await self._nodes()
        if pin is not None:
            # ``a,b``: the request may go to any of these nodes (a row of the table that names
            # several), the pool's queue then takes the first of them that is free
            wanted = pin.split(",")
            nodes = [n for n in nodes if n.name in wanted]
            if not nodes:
                raise GatewayError(404, "node_not_found", f"unknown node {pin!r}")
        having = [n for n in nodes if model in n.models or model in n.loading]
        having = self._allowed_by_policy(model, having)
        # the nodes of a several-node pin come in the order the row names them: that order, not the
        # alphabet, says which node the person would expect
        listed = pin.split(",") if pin else []

        def row_order(name: str) -> int:
            return listed.index(name) if name in listed else len(listed)

        first = min(
            having, key=lambda n: (row_order(n.name), *self._order_key(n.name)), default=None
        )
        out: list[Candidate] = []
        blocked = False
        skipped: list[str] = []  # why each node that has the model was not taken (for the refusal)
        for n in having:
            if not n.connected or "models.proxy" not in n.allowed:
                skipped.append(f"{n.name}: not connected or no proxy right")
                continue
            if n.state in BLOCKED_STATES:
                blocked = True
                skipped.append(f"{n.name}: {n.state}")
                continue
            if n.state != "available":
                skipped.append(f"{n.name}: {n.state}")
                continue
            if n.models.get(model):
                rank = RANK_LOADED
            elif model in n.loading:
                rank = RANK_LOADING
            else:
                rank = RANK_ON_DISK
            can_load = "models.load" in n.allowed
            if rank != RANK_LOADED and not can_load:
                skipped.append(f"{n.name}: not loaded and no load right")
                continue
            out.append(
                Candidate(
                    n.id,
                    n.name,
                    rank,
                    can_load,
                    n.contexts.get(model),
                    self._estimate(n, model, rank),
                    n.sizes_mb.get(model),
                    n.slots(),
                )
            )
        if len(out) > 1 and all(c.est_seconds is not None for c in out):
            # Measured speeds on every candidate: the shortest time to a reply wins, a slow
            # loaded node no longer beats a fast one that only has to load.
            out.sort(
                key=lambda c: (
                    self._lane(c.name),
                    row_order(c.name),
                    c.est_seconds or 0.0,
                    *self._order_key(c.name),
                )
            )
        else:
            out.sort(
                key=lambda c: (
                    self._lane(c.name),
                    row_order(c.name),
                    c.rank,
                    *self._order_key(c.name),
                )
            )
        if out and self.choice_mode == "neu" and len(out) > 1:
            # the profile of the work decides the first node (choice.py); the others stay behind
            # it in the old order as the fallbacks. "alt" (the default) keeps the old order.
            from topcoat_hub import choice as choice_rules

            options = [
                choice_rules.Option(
                    name=c.name,
                    model=model,
                    loaded=c.rank == RANK_LOADED,
                    quality=self.answer_quality.get(model),
                    seconds=c.est_seconds,
                )
                for c in out
            ]
            pick = choice_rules.first_choice(
                options, choice_rules.profile_for(_WORK_LABEL.get() or "chat")
            )
            names = choice_rules.move_first([c.name for c in out], pick)
            by_name = {c.name: c for c in out}
            out = [by_name[n] for n in names]
        if out:
            return Route(out, first.name if first else None, first.state if first else None)
        if blocked:
            raise GatewayError(
                503,
                "model_unavailable",
                f"model {model!r} is on a node that is busy (game or other programs); "
                "try again later",
                retry_after=self.settings.gateway_retry_after_seconds,
            )
        if having:
            detail = "; ".join(skipped) or "no node answers"
            raise GatewayError(
                503,
                "model_unavailable",
                f"no connected node can serve {model!r} right now ({detail})",
                retry_after=self.settings.gateway_retry_after_seconds,
            )
        raise GatewayError(404, "model_not_found", f"model {model!r} is not on any node")

    def _allowed_by_policy(self, model: str, having: list[_NodeView]) -> list[_NodeView]:
        """The nodes the admin's scheduling choices leave for this kind of work (interactive or
        background). Raises 503 when they leave none of the nodes that have ``model``."""
        policy = self._policy
        background = _IN_BACKGROUND_WORK.get()
        if background and policy.paused() and having:
            until = policy.paused_until
            wait = max(1, int((until - datetime.now(UTC)).total_seconds())) if until else 60
            raise GatewayError(
                503,
                "background_paused",
                "background work is paused by an admin",
                retry_after=min(wait, 3600),
            )
        # a node that is not in the node list of the row of the work is kept free of it: the row
        # ``background`` for background work, the row of the application for the rest
        key = "background" if background else (job_context().application or "")
        row = policy.applications.get(key)
        named = policy.row_nodes(key)
        if background and named:
            named = [*named, *(w for w in policy.workers if w not in named)]  # a worker's job
        if named and row is not None and ALL_NODES not in row.nodes:
            left = [n for n in having if n.name in named]
        else:
            left = list(having)
        if having and not left:
            raise GatewayError(
                503,
                "node_kept_free",
                f"the nodes that have {model!r} are not in the row {key!r} of the table of "
                f"applications (node policy)",
                retry_after=self.settings.gateway_retry_after_seconds,
            )
        return left

    def _estimate(self, n: _NodeView, model: str, rank: int) -> float | None:
        """Seconds to a typical reply on ``n``: the load (none if loaded; a model that is still
        loading counts as a full load) plus the generation at the measured speed."""
        tps = n.speeds.get(model)
        if not tps:
            return None
        load = 0.0
        if rank != RANK_LOADED:
            size = n.sizes_mb.get(model)
            if size is None:
                return None
            load = size / (n.load_speeds.get(model) or self.settings.gateway_load_mb_per_second)
        return load + self.settings.gateway_typical_reply_tokens / tps

    # --- measured speeds ------------------------------------------------------------------

    async def _note(self, node_id: uuid.UUID, model: str, **fields: float) -> None:
        """Fold a measurement into the moving average (weight 0.3 for the new one) of the node
        and model; never raises (a measurement must not break an answer)."""
        try:
            async with self.sessionmaker() as db:
                row = await db.scalar(
                    select(NodeSpeed).where(NodeSpeed.node_id == node_id, NodeSpeed.model == model)
                )
                if row is None:
                    row = NodeSpeed(node_id=node_id, model=model, tg_samples=0, load_samples=0)
                    db.add(row)
                for key, value in fields.items():
                    old, count = getattr(row, key), getattr(row, _SAMPLES[key]) or 0
                    setattr(
                        row, key, value if old is None or count == 0 else 0.7 * old + 0.3 * value
                    )
                    setattr(row, _SAMPLES[key], count + 1)
                await db.commit()
        except Exception:
            log.exception("gateway: could not note a speed")

    async def note_speed(self, node_name: str, model: str, tokens: int, seconds: float) -> None:
        """A real answer: ``tokens`` generated in ``seconds`` (without the wait for the first
        token). Short or very quick answers say little about the speed and are left out."""
        if tokens < MIN_SPEED_TOKENS or seconds < MIN_SPEED_SECONDS:
            return
        for n in await self._nodes():
            if n.name == node_name:
                await self._note(n.id, split_model(model)[0], tg_tps=tokens / seconds)
                return

    async def note_load(
        self, node_id: uuid.UUID, model: str, size_mb: float, seconds: float
    ) -> None:
        """A real load of ``size_mb`` that took ``seconds`` (a load that returned at once, because
        the model was already there, says nothing)."""
        if seconds >= MIN_LOAD_SECONDS and size_mb > 0:
            await self._note(node_id, model, load_mb_s=size_mb / seconds)

    # --- loading --------------------------------------------------------------------------

    async def configured_context(self, node_id: uuid.UUID, model: str) -> int:
        """The context length the node's own config gives a load of ``model`` (a resident entry,
        else ``runner.model_context_lengths``, else ``runner.default_context_length``); 0 =
        nothing configured, the node's runner default (8192) applies."""
        async with self.sessionmaker() as db:
            node = await db.get(Node, node_id)
        cfg = node.config if node is not None else {}
        for r in cfg.get("residents") or []:
            if isinstance(r, dict) and r.get("model") == model and r.get("context_length"):
                return int(r["context_length"])
        runner = cfg.get("runner") or {}
        per_model = (runner.get("model_context_lengths") or {}).get(model)
        return int(per_model or runner.get("default_context_length") or 0)

    async def _load_args(self, cand: Candidate, model: str, min_context: int) -> dict[str, Any]:
        """The arguments of ``models.load``. ``min_context`` is a floor: the node sizes the context
        itself, at least this, at least what its own config would have used (docs/PROTOCOL.md,
        "Context sized by the node"). A node from before that gets the old explicit value, and only
        when its own config gives less."""
        args: dict[str, Any] = {"model": model}
        if min_context <= 0:
            return args
        if cand.node_id not in self._legacy_nodes:
            args["min_context_length"] = min_context
            return args
        configured = await self.configured_context(cand.node_id, model)
        if (configured or DEFAULT_NODE_CONTEXT) < min_context:
            args["context_length"] = min_context
        return args

    async def upsize(self, cand: Candidate, model: str, min_context: int) -> bool:
        """Reload a loaded model that has less context than ``min_context``: unload it and load it
        with the floor, so that the node sizes it again. Only while the node does nothing else for
        the hub, at most once per ``UPSIZE_RETRY_SECONDS`` for the same need, and if the load
        fails the model is brought back as it was. Returns True when it was reloaded."""
        key = (cand.node_id, model)
        last = self._upsize_tried.get(key)
        if last and last[0] >= min_context and time.monotonic() - last[1] < UPSIZE_RETRY_SECONDS:
            return False
        lock = self._upsize_locks.setdefault(key, asyncio.Lock())
        async with lock:
            view = next((n for n in await self._nodes() if n.id == cand.node_id), None)
            if view is None or not view.connected or view.state != "available":
                return False
            have = view.contexts.get(model)
            if not have or have >= min_context:
                return False  # unknown, or somebody was quicker
            if not {"models.load", "models.unload"} <= view.allowed:
                return False
            if any(b["node"] == cand.name for b in self._busy.values()):
                return False  # not while it answers somebody
            self._upsize_tried[key] = (min_context, time.monotonic())
            try:
                gone = await self.sessions.dispatch(
                    cand.node_id,
                    "models.unload",
                    {"model": model},
                    self.settings.gateway_load_timeout_seconds,
                )
            except (NodeOffline, NodeReadOnly, ActionTimeout):
                return False
            if not gone.get("ok"):
                return False
            log.info(
                "gateway: %s on %s had %d tokens of context, loading it for %d",
                model,
                cand.name,
                have,
                min_context,
            )
            failures = (GatewayError, NodeOffline, NodeReadOnly, ActionTimeout)
            try:
                await self.ensure_loaded(cand, model, min_context)
            except failures:
                with contextlib.suppress(*failures):
                    await self.ensure_loaded(cand, model, 0)  # the way it was
                return False
            return True

    async def unload_others(self, node: str, keep: str) -> list[str]:
        """Unload every model of ``node`` except ``keep`` (a small card holds one at a time).
        Skipped while the node answers somebody else; returns what was unloaded."""
        view = next((n for n in await self._nodes(all_models=True) if n.name == node), None)
        if view is None or not view.connected or not {"models.unload"} <= view.allowed:
            return []
        if any(b["node"] == node for b in self._busy.values()):
            return []
        gone: list[str] = []
        for model, loaded in view.models.items():
            if not loaded or model == keep:
                continue
            with contextlib.suppress(NodeOffline, NodeReadOnly, ActionTimeout):
                result = await self.sessions.dispatch(
                    view.id,
                    "models.unload",
                    {"model": model},
                    self.settings.gateway_load_timeout_seconds,
                )
                if result.get("ok"):
                    gone.append(model)
        return gone

    async def _make_room(self, cand: Candidate, model: str) -> None:
        """Keep to the node's limit of loaded models (``NodePolicy.max_loaded``): before ``model``
        is loaded, unload the others that are idle. A model with a request in flight stays; a model
        used a moment ago stays too when the load is background work (nobody waits for that), so
        a research run is not thrown out between two of its steps. When nothing can go, the
        request is told to try again: a node that holds two big models at once crashes."""
        limit = self._policy.max_loaded.get(cand.name)
        view = next((n for n in await self._nodes(all_models=True) if n.id == cand.node_id), None)
        if view is None or (not limit and view.memory_free_mb is None):
            return  # nothing to keep to: no count limit, and the node reports no memory
        others = sorted(m for m, loaded in view.models.items() if loaded and m != model)
        now = time.monotonic()
        keep_for = self.settings.gateway_keep_loaded_seconds if _IN_BACKGROUND_WORK.get() else 0.0

        def idle(m: str) -> bool:
            if any(
                b["node"] == cand.name and b["model"] == m and b["kind"] != "load"
                for b in self._busy.values()
            ):
                return False
            return now - self._last_used.get((cand.name, m), -1e9) >= keep_for

        victims: list[str] = []
        need = view.sizes_mb.get(model)
        if view.memory_free_mb is not None:
            # the memory rule: the model and its buffer must fit, and a node below the RAM floor
            # loads nothing; idle models go, least recently used first, and when they would not
            # make room the load waits (nothing is loaded into memory that is not there)
            by_age = sorted(
                (m for m in others if idle(m)),
                key=lambda m: self._last_used.get((cand.name, m), -1e9),
            )
            plan = plan_load(
                view.ram_free_mb,
                view.memory_free_mb,
                need,
                self.settings.gateway_load_buffer_percent,
                self.settings.gateway_min_free_ram_mb,
                [(m, view.sizes_mb.get(m, 0.0)) for m in by_age],
            )
            if plan is None:
                raise GatewayError(
                    503,
                    "node_memory_full",
                    f"{cand.name} has no room for {model!r} with its buffer: "
                    f"{model!r} waits until memory is free",
                    retry_after=self.settings.gateway_retry_after_seconds,
                )
            victims.extend(plan)
        if limit:  # the count limit of the node, where the policy sets one
            remaining = [m for m in others if m not in victims]
            while len(remaining) + 1 > limit:
                free = [m for m in remaining if idle(m)]
                if not free:
                    raise GatewayError(
                        503,
                        "node_holds_other_model",
                        f"{cand.name} may hold {limit} model(s) loaded and the other one is in "
                        f"use: {model!r} waits until it is free",
                        retry_after=self.settings.gateway_retry_after_seconds,
                    )
                victims.append(free[0])
                remaining.remove(free[0])
        for victim in victims:
            log.info("gateway: unloading %s on %s to make room for %s", victim, cand.name, model)
            try:
                gone = await self.sessions.dispatch(
                    cand.node_id,
                    "models.unload",
                    {"model": victim},
                    self.settings.gateway_load_timeout_seconds,
                )
            except (NodeOffline, NodeReadOnly, ActionTimeout) as exc:
                raise GatewayError(
                    503, "unload_failed", f"could not unload {victim!r}: {exc}"
                ) from exc
            if not gone.get("ok"):
                reason = str((gone.get("error") or {}).get("message") or "")
                # the node's own idle unload may have been faster: the room is there all the same
                if "not loaded" not in reason:
                    raise GatewayError(
                        503, "unload_failed", f"could not unload {victim!r}: {reason or 'refused'}"
                    )

    async def ensure_loaded(self, cand: Candidate, model: str, min_context: int = 0) -> None:
        """Load ``model`` on the node once, however many requests wait for it. The node
        itself also returns the running load if one is in progress (resident reload).
        ``min_context`` (an agent run, a chat with tools): the node is asked for at least this
        much context per slot and sizes the rest itself (see :meth:`_load_args`)."""
        key = (cand.node_id, model)
        pending = self._loads.get(key)
        if pending is not None:
            await asyncio.shield(pending)
            return
        await self._make_room(cand, model)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._loads[key] = future
        tracked = self._track(cand.name, model, "load")
        try:
            args = await self._load_args(cand, model, min_context)
            started = time.monotonic()
            result = await self.sessions.dispatch(
                cand.node_id,
                "models.load",
                args,
                self.settings.gateway_load_timeout_seconds,
            )
            if (
                not result.get("ok")
                and (result.get("error") or {}).get("code") == "invalid_args"
                and "min_context_length" in args
            ):
                # a node from before the argument existed: remember it, ask the old way
                self._legacy_nodes.add(cand.node_id)
                args = await self._load_args(cand, model, min_context)
                result = await self.sessions.dispatch(
                    cand.node_id,
                    "models.load",
                    args,
                    self.settings.gateway_load_timeout_seconds,
                )
            if result.get("ok") and cand.size_mb:
                await self.note_load(cand.node_id, model, cand.size_mb, time.monotonic() - started)
            if not result.get("ok"):
                err = result.get("error") or {}
                raise GatewayError(
                    503, err.get("code", "load_failed"), err.get("message", "load failed")
                )
            future.set_result(None)
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            self._untrack(tracked)
            self._loads.pop(key, None)
            # Consume the exception if nobody else awaited it.
            if future.done() and not future.cancelled():
                future.exception()

    # --- proxying -------------------------------------------------------------------------

    def _record(
        self,
        record: dict[str, Any],
        asked_mono: float,
        *,
        started: float | None,
        outcome: str,
        code: str | None,
    ) -> None:
        """Write the record of one request (SCH-03 step 3): never content, never raises."""
        if self.job_log is None or record.get("work") in NO_RECORD_WORK:
            return
        now = time.monotonic()
        ctx = job_context()
        values = {
            **record,
            "user_id": ctx.user_id,
            "conversation_id": ctx.conversation_id,
            "message_id": ctx.message_id,
            "task_id": ctx.task_id,
            "application": ctx.application,
            "outcome": outcome,
            "error_code": code,
            "waited_ms": int(((started if started is not None else now) - asked_mono) * 1000),
            "duration_ms": int((now - started) * 1000) if started is not None else 0,
            "finished_at": utcnow(),
        }
        spawn(self.job_log.write(**values))

    def _slot_freed(self) -> None:
        """A place of a node was given back: the queue serves the next request, then the free
        listeners run (the chat digester starts the chats it had waiting)."""
        self._pool.poke()
        for listener in list(self.free_listeners):
            listener()

    def _slot(self, node_id: uuid.UUID, capacity: int | None = None) -> JobQueue:
        """The queue of a node. Its size is what the node reports (``n_parallel`` of its loaded
        models); while that is not known the queue keeps the size it has (1 at first)."""
        slot = self._slots.get(node_id)
        if slot is None:
            slot = JobQueue(capacity or 1, aging_seconds=self.settings.gateway_queue_aging_seconds)
            slot.on_change = self._slot_freed  # a freed place serves the next request in line
            self._slots[node_id] = slot
        elif capacity:
            slot.set_capacity(capacity)
        return slot

    async def _start(
        self,
        cand: Candidate,
        model: str,
        method: str,
        path: str,
        body: bytes,
        accept: str,
        held_place: int,
        asked_mono: float,
    ) -> Upstream | tuple[int, dict[str, Any]]:
        """Send proxy_request; return the running Upstream, or (status, error) when the node
        answered with proxy_end and no data. The caller holds a place on the node (``held_place``,
        taken from the pool's queue); it is given back here on every way out except a returned
        Upstream, which gives it back when it is closed. ``asked_mono`` is when the request was
        made, so the record counts the time in the queue."""
        slot = self._slot(cand.node_id, cand.slots)
        session = self.sessions.get(cand.node_id)
        if session is None or session.read_only:
            slot.release(held_place)
            raise NodeOffline()
        first_byte = (
            self.settings.gateway_background_first_byte_timeout_seconds
            if _IN_BACKGROUND_WORK.get()
            else self.settings.gateway_first_byte_timeout_seconds
        )
        priority = held_place
        asked_at = utcnow() - timedelta(seconds=max(0.0, time.monotonic() - asked_mono))
        record = {
            "work": (_WORK_LABEL.get() or ("background" if _IN_BACKGROUND_WORK.get() else "chat"))[
                :40
            ],
            "caller": caller_chain()[:200],
            "requested": model[:255],
            "chosen_model": model[:255],
            "chosen_node": cand.name,
            "place": priority,
            "model_loaded": cand.rank == RANK_LOADED,
            "created_at": asked_at,
        }
        started_mono = time.monotonic()
        record["started_at"] = utcnow()
        stream_id = str(uuid.uuid4())
        queue = session.open_stream(stream_id)
        tracked = self._track(cand.name, model)
        early: list[StreamStatus] = []  # proxy_status seen before the first data
        pause_limit = self.settings.gateway_pause_timeout_seconds
        closed = False
        finished = False  # the node ended the stream (or is gone): nothing to cancel

        async def send_cancel() -> None:
            with contextlib.suppress(RuntimeError, OSError):
                await session.send(envelope("proxy_cancel", {"stream_id": stream_id}))

        end: dict[str, str] = {}  # how it went, for the record: "code" of a failure
        tail = StatsTail()  # what the model says about itself at the end of the answer

        def close() -> None:
            """End the stream: cancel it on the node unless it ended, free the slot."""
            nonlocal closed
            if closed:
                return
            closed = True
            self._untrack(tracked)
            session.close_stream(stream_id)
            slot.release(priority)
            if not finished:
                spawn(send_cancel())
            outcome = "failed" if end.get("code") else ("done" if finished else "cancelled")
            record.update(tail.stats())
            self._record(
                record, asked_mono, started=started_mono, outcome=outcome, code=end.get("code")
            )

        headers = {"content-type": "application/json"}
        if accept:
            headers["accept"] = accept[:1024]
        try:
            await session.send(
                envelope(
                    "proxy_request",
                    {
                        "stream_id": stream_id,
                        "model": model,
                        "method": method,
                        "path": path,
                        "headers": headers,
                        "body": base64.b64encode(body).decode(),
                    },
                )
            )
            first = await asyncio.wait_for(queue.get(), first_byte)
            # The node may pause (its user came back) while it still reads the prompt.
            while first is not STREAM_LOST and first["type"] == "proxy_status":
                status = _stream_status(first)
                early.append(status)
                limit = pause_limit if status.state == "paused" else first_byte
                first = await asyncio.wait_for(queue.get(), limit)
        except TimeoutError as exc:
            end["code"] = "timeout"
            close()
            raise GatewayError(504, "timeout", f"node {cand.name} did not answer in time") from exc
        except BaseException:
            close()
            raise
        if first is STREAM_LOST:
            finished = True
            end["code"] = "node_offline"
            close()
            raise NodeOffline()
        if first["type"] == "proxy_end":
            finished = True
            if first["body"]["status"] >= 400:
                end["code"] = f"http_{first['body']['status']}"
            close()
            return first["body"]["status"], first["body"].get("error") or {}

        chunk = first["body"]
        idle = self.settings.gateway_idle_timeout_seconds
        ended: dict[str, Any] = {}
        stalled: list[bool] = []

        def make_body(with_status: bool) -> AsyncIterator[bytes | StreamStatus]:
            async def body_iter() -> AsyncIterator[bytes | StreamStatus]:
                nonlocal finished
                paused = bool(early) and early[-1].state == "paused"
                try:
                    if with_status:
                        for st in early:
                            yield st
                    first_data = base64.b64decode(chunk["data"])
                    tail.feed(first_data)
                    yield first_data
                    while True:
                        # A paused node is silent on purpose; it says when it goes on.
                        limit = pause_limit if paused else idle
                        try:
                            msg = await asyncio.wait_for(queue.get(), limit)
                        except TimeoutError:
                            log.warning("gateway: node %s stalled, ending stream", cand.name)
                            stalled.append(True)
                            return
                        if msg is STREAM_LOST:
                            finished = True  # the node is gone; nothing to cancel
                            end["code"] = "node_offline"
                            return
                        if msg["type"] == "proxy_status":
                            status = _stream_status(msg)
                            paused = status.state == "paused"
                            if with_status:
                                yield status
                            continue
                        if msg["type"] == "proxy_end":
                            finished = True
                            if err := msg["body"].get("error"):
                                ended.update(err)
                                end["code"] = str(err.get("code") or "stream_error")[:40]
                                log.warning("gateway: stream from %s ended with %s", cand.name, err)
                            return
                        data = base64.b64decode(msg["body"]["data"])
                        tail.feed(data)
                        yield data
                finally:
                    close()  # cancels on the node if the client went away or the stream stalled

            return body_iter()

        return Upstream(
            status=chunk.get("status", 200),
            content_type=chunk.get("content_type") or "application/octet-stream",
            body=make_body(False),  # type: ignore[arg-type]  # no StreamStatus items without status
            node=cand.name,
            close=close,
            status_body=lambda: make_body(True),
            ended=ended,
            stalled=stalled,
        )

    async def reserve(self, requested: str, min_context: int = 0) -> Placement:
        """Choose the node a chat request runs on and have the model loaded there (SCH-03 step 2),
        so the prompt can be built with the context length of that node. Raises the errors
        :meth:`forward` raises when no node can serve. Pass the result to :meth:`forward`."""
        model, pin = split_model(requested)
        model = self.background_model(model, pin)
        route = await self.candidates(model, pin)
        errors: list[str] = []
        reasons: list[str] = []
        busy = False
        # a node with a free place first (the order inside each group stays): the request does not
        # wait behind a busy node while another suitable node is idle
        for cand in sorted(route.candidates, key=lambda c: not self._has_room(c)):
            session = self.sessions.get(cand.node_id)
            if session is None or session.read_only:
                errors.append(f"{cand.name}: offline")
                continue
            if cand.rank == RANK_LOADED:
                return Placement(cand, route, model)
            try:
                await self.ensure_loaded(cand, model, min_context)
            except (NodeOffline, NodeReadOnly):
                errors.append(f"{cand.name}: offline")
                continue
            except ActionTimeout:
                errors.append(f"{cand.name}: load timed out")
                continue
            except GatewayError as exc:
                errors.append(f"{cand.name}: {exc.message}")
                if exc.code in ("insufficient_memory", "insufficient_disk"):
                    reasons.append(exc.code)
                busy = busy or exc.code in ("busy", "model_unavailable")
                continue
            # the node reports the real context length and slots once the model is loaded
            fresh = await self.candidates(model, pin)
            loaded = next((c for c in fresh.candidates if c.node_id == cand.node_id), cand)
            return Placement(loaded, route, model)
        raise GatewayError(
            503,
            "model_unavailable",
            f"no node could serve {model!r}: " + "; ".join(errors),
            retry_after=self.settings.gateway_retry_after_seconds if busy or errors else None,
            reason=reasons[0] if reasons else "",
        )

    async def forward(
        self,
        requested: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        accept: str = "",
        min_context: int = 0,
        placement: Placement | None = None,
    ) -> Upstream:
        """Route a request for ``requested`` (``model`` or ``model@node``) and start it. With a
        ``placement`` (:meth:`reserve`) the node it names is tried first."""
        model, pin = split_model(requested)
        if path == "/v1/chat/completions":  # embeddings and the like keep their own model
            model = self.background_model(model, pin)
        body = b""
        if payload is not None:
            if path == "/v1/chat/completions":  # the setting the bench showed best for this kind
                payload = model_profiles.apply(
                    payload,
                    self.profiles.get(model),
                    model_profiles.function_of(payload, _WORK_LABEL.get() or ""),
                )
                payload = model_profiles.thinking(model, payload)  # the switch per family (2.23)
            body = json.dumps({**payload, "model": model}).encode()
        errors: list[str] = []
        reasons: list[str] = []
        busy = False
        timed_out: GatewayError | None = None
        route = await self.candidates(model, pin)
        order = route.candidates
        if placement is not None:
            first = next((c for c in order if c.node_id == placement.cand.node_id), None)
            if first is not None:
                # the prompt was built for the context of the placed node: another node may take
                # the request only if it cannot give less context than that
                need = placement.cand.context_length
                rest = [
                    c
                    for c in order
                    if c is not first
                    and (need is None or c.context_length is None or c.context_length >= need)
                ]
                order = [first, *rest]
        asked_mono = time.monotonic()
        pending = list(order)
        tries: dict[uuid.UUID, int] = {}
        while pending:
            # wait here, not on a node: whichever node of the pool has a free place takes the
            # request (no timer; a full queue refuses at once)
            cand, place = await self._take_room(pending, model)
            attempt = tries.get(cand.node_id, 0)
            tries[cand.node_id] = attempt + 1
            handed = False  # the place is given to _start, which gives it back
            try:
                if (
                    min_context
                    and cand.rank == RANK_LOADED
                    and cand.context_length
                    and cand.context_length < min_context
                    and attempt == 0
                ):
                    await self.upsize(cand, model, min_context)
                if (cand.rank != RANK_LOADED or attempt > 0) and not (
                    attempt == 0 and await self._loaded_meanwhile(cand, model)
                ):
                    await self.ensure_loaded(cand, model, min_context)
                handed = True
                started = await self._start(
                    cand, model, method, path, body, accept, place, asked_mono
                )
            except (NodeOffline, NodeReadOnly):
                errors.append(f"{cand.name}: offline")
                pending.remove(cand)
                continue
            except ActionTimeout:
                errors.append(f"{cand.name}: load timed out")
                pending.remove(cand)
                continue
            except GatewayError as exc:
                errors.append(f"{cand.name}: {exc.message}")
                pending.remove(cand)
                if exc.status == 504:
                    timed_out = exc  # a node that did not answer in time: try the next one
                    continue
                if exc.code in ("insufficient_memory", "insufficient_disk"):
                    reasons.append(exc.code)
                busy = busy or exc.code in ("busy", "model_unavailable")
                continue
            finally:
                if not handed:
                    self._slot(cand.node_id, cand.slots).release(place)
            if isinstance(started, Upstream):
                log.info(
                    "gateway: %s %s -> %s (from %s, pin=%s, preferred=%s)",
                    path,
                    model,
                    cand.name,
                    caller_chain(),
                    pin,
                    route.preferred,
                )
                if route.preferred and route.preferred != cand.name and (pin is None or "," in pin):
                    started.fallback_from = route.preferred
                    started.fallback_state = route.preferred_state
                return started
            status, err = started
            code = err.get("code", "")
            if code == "not_loaded" and attempt == 0:
                continue  # unloaded meanwhile (idle, pressure): load and retry once
            if code == "busy":
                busy = True
                errors.append(f"{cand.name}: busy")
                pending.remove(cand)
                continue
            raise GatewayError(status, code or "upstream_error", err.get("message", ""))
        if timed_out is not None and len(errors) == 1:
            raise timed_out  # the only node asked did not answer in time
        raise GatewayError(
            503,
            "model_unavailable",
            f"no node could serve {model!r}: " + "; ".join(errors),
            retry_after=self.settings.gateway_retry_after_seconds if busy or errors else None,
            reason=reasons[0] if reasons else "",
        )

    async def _loaded_meanwhile(self, cand: Candidate, model: str) -> bool:
        """Did another request load the model on this node while this one waited for its place?
        (The candidates were made before the wait; the load is then done, not to be asked again.)"""
        for view in await self._nodes():
            if view.id == cand.node_id:
                return bool(view.models.get(model))
        return False

    def _has_room(self, cand: Candidate) -> bool:
        """Would a request of the current place start on this node right now?"""
        queue = self._slots.get(cand.node_id)
        if queue is None:
            return True
        place = clamp_place(self._place(current_priority()))
        return can_start(place, queue.running(), queue.capacity)

    def _source(self) -> tuple[str, str]:
        """Who asks, for the caps of the queue: the kind of work (chat, agent, background, bench)
        and the person it is for."""
        ctx = job_context()
        label = _WORK_LABEL.get() or ""
        if label == "bench":
            source = "bench"
        elif ctx.task_id is not None:
            source = "agent"
        elif _IN_BACKGROUND_WORK.get():
            source = "background"
        else:
            source = "chat"
        return source, str(ctx.user_id) if ctx.user_id is not None else ""

    async def _take_room(self, offers: list[Candidate], model: str) -> tuple[Candidate, int]:
        """Wait in the pool's queue for a free place on any of ``offers`` and return the node whose
        place is now held, with the place. A full queue refuses at once."""
        source, owner = self._source()
        place = self._place(current_priority())
        label = _WORK_LABEL.get() or ("background" if _IN_BACKGROUND_WORK.get() else "chat")
        try:
            offer, taken = await self._pool.take(
                offers,
                place,
                source=source,
                owner=owner,
                info={"kind": label, "model": model, **_context_facts()},
            )
        except QueueFull as exc:
            raise GatewayError(
                503,
                "queue_full",
                f"the queue is full ({exc.depth} of {exc.limit} waiting"
                f"{' for this source' if exc.scope == 'source' else ''}); try again later",
                retry_after=self.settings.gateway_retry_after_seconds,
            ) from exc
        assert isinstance(offer, Candidate)  # noqa: S101 - the queue hands back what it was given
        return offer, taken

    def pool_waiting_jobs(self) -> list[dict[str, Any]]:
        """The requests that wait for a node of the pool, in the order they will be served."""
        return self._pool.waiting_jobs()

    async def call(
        self,
        requested: str,
        path: str,
        payload: dict[str, Any],
        accept: str = "",
        min_context: int = 0,
        label: str = "",
    ) -> dict[str, Any]:
        """A whole non-streaming request for the hub's own use (embeddings, memory
        extraction): routed like any client request, the JSON answer returned. ``label`` names
        the work for the nodes overview unless an outer ``work()`` block already did."""
        payload = chore_payload(payload, label, path)
        with work(label) if label and _WORK_LABEL.get() is None else contextlib.nullcontext():
            upstream = await self.forward(
                requested, "POST", path, payload, accept=accept, min_context=min_context
            )
        try:
            raw = b"".join([chunk async for chunk in upstream.body])
        finally:
            upstream.close()
        if upstream.status != 200:
            if is_context_full(raw.decode("utf-8", "replace")):
                raise GatewayError(
                    503,
                    "context_full",
                    CONTEXT_FULL_MESSAGE,
                    retry_after=self.settings.gateway_retry_after_seconds,
                )
            raise GatewayError(
                502, "model_error", f"the model answered {upstream.status}: {raw[:200]!r}"
            )
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise GatewayError(502, "model_error", "the model did not answer with JSON") from exc
        if not isinstance(data, dict):
            raise GatewayError(502, "model_error", "the model answered with an unexpected shape")
        return data

    async def call_streamed(
        self,
        requested: str,
        path: str,
        payload: dict[str, Any],
        min_context: int = 0,
        label: str = "",
    ) -> dict[str, Any]:
        """Like :meth:`call`, but the request is a stream and the pieces are put together again
        into the answer a non-streaming request would have returned.

        The point is the clock: a whole answer from a slow node can take minutes, and nothing
        reaches the hub meanwhile. Every piece (llama.cpp also sends the progress of reading the
        prompt, ``return_progress``) shows that the model is alive, so the only reasons to give
        up are a node that is gone and a node that stands still for ``gateway_idle_timeout_seconds``
        — not the time an answer takes."""
        body = {
            **chore_payload(payload, label, path),
            "stream": True,
            "stream_options": {"include_usage": True},
            "return_progress": True,
        }
        with work(label) if label and _WORK_LABEL.get() is None else contextlib.nullcontext():
            upstream = await self.forward(
                requested, "POST", path, body, accept="text/event-stream", min_context=min_context
            )
        assembler = StreamAssembler()
        raw = b""
        try:
            async for piece in upstream.body:
                if upstream.status != 200:
                    raw += piece  # the error body, not an event stream
                    continue
                assembler.feed(piece)
        finally:
            upstream.close()
        if upstream.status != 200:
            if is_context_full(raw.decode("utf-8", "replace")):
                raise GatewayError(
                    503,
                    "context_full",
                    CONTEXT_FULL_MESSAGE,
                    retry_after=self.settings.gateway_retry_after_seconds,
                )
            raise GatewayError(
                502, "model_error", f"the model answered {upstream.status}: {raw[:200]!r}"
            )
        # ``reason`` "cut": the answer broke off on the node's side (the model was unloaded, the
        # node stood still, the stream stopped early). Another try on another place may work; a
        # caller that may repeat a request reads it (blocks/service.py, issue 2.18)
        if upstream.ended:
            raise GatewayError(
                502,
                "model_error",
                "the node ended the answer: " + str(upstream.ended)[:200],
                reason=CUT,
            )
        if upstream.stalled:
            raise GatewayError(
                504, "timeout", f"node {upstream.node} stopped answering", reason=CUT
            )
        if assembler.error:
            raise GatewayError(502, "model_error", assembler.error)
        if not assembler.done and not assembler.finish_reason:
            raise GatewayError(
                502, "model_error", "the answer ended before it was complete", reason=CUT
            )
        return assembler.result()

    async def any_node_available(self) -> bool:
        """True if a connected node is ``available`` (no game, no VRAM pressure): the
        condition for background work such as the memory consolidation."""
        if self._policy.paused():
            return False
        allowed = self._policy.row_nodes("background")
        return any(
            n.connected and n.state == "available" and (not allowed or n.name in allowed)
            for n in await self._nodes()
        )
