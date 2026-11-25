"""Parameters an admin can change while the hub runs (E7-03, D58).

Tuning a model or a flow used to mean editing ``hub.env`` and restarting — or building a new
image. Most numbers and switches of the hub are read at the moment they are used, so they can be
changed in place. This module is the allowlist of those that may be, with their type, bounds and
group; a value an admin sets is validated, stored (``hub_settings``, key ``live.<name>``), written
into the running :class:`~topcoat_hub.config.Settings` and noted in ``setting_changes`` (who, when,
from what to what), so every change can be read back and undone. A reset puts the value from the
environment back.

Not everything is live, on purpose:

* Secrets (tokens, keys, the database URL) and anything that opens a port or a file are not in the
  list and cannot be reached through it, whatever the request says.
* A value that is read once at start (a node's slots, the schedulers' first interval) is not
  listed. A value that was copied into an object (the agent's limits) is refreshed by a *hook* of
  its group, so the change reaches the next run.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.config import Settings
from topcoat_hub.models import HubSetting, SettingChange

log = logging.getLogger(__name__)

PREFIX = "live."
Kind = Literal["int", "float", "bool", "str", "enum", "json"]


@dataclass(frozen=True)
class Param:
    key: str  # the name of the field in Settings
    group: str
    kind: Kind
    low: float | None = None
    high: float | None = None
    choices: tuple[str, ...] = ()
    unit: str = ""  # s, chars, tokens … : the browser shows it next to the number
    text: str = ""  # one sentence: what it does and what changing it does


def _p(key: str, group: str, kind: Kind, text: str, **kw: Any) -> Param:
    return Param(key=key, group=group, kind=kind, text=text, **kw)


# the allowlist (E7-03): every entry is read when it is used, or its group has a hook
PARAMS: tuple[Param, ...] = (
    # --- the check (agent loop): one run's limits ---------------------------------------------
    _p(
        "step_check_enabled",
        "agent",
        "bool",
        "Gründlich: a small model on the checking node looks at each tool step during the run.",
    ),
    _p(
        "effort_thorough_revisions",
        "agent",
        "int",
        "Gründlich: how often an answer is revised after its check (0 = checked, not revised).",
        low=0,
        high=3,
    ),
    _p(
        "step_check_model",
        "agent",
        "str",
        "The model that looks at each tool step (Gründlich); it should be small and fast.",
    ),
    _p("agent_max_turns", "agent", "int", "Model turns one check may take.", low=1, high=200),
    _p("agent_max_tool_calls", "agent", "int", "Tool calls one check may make.", low=1, high=200),
    _p(
        "agent_max_web_searches", "agent", "int", "Web searches one check may make.", low=0, high=20
    ),
    _p("agent_max_web_fetches", "agent", "int", "Web pages one check may read.", low=0, high=20),
    _p(
        "agent_time_budget_seconds",
        "agent",
        "float",
        "Time one check may take.",
        low=30,
        high=7200,
        unit="s",
    ),
    # --- Research: no fixed limits, a run ends by its frame or by standing still ---------------
    _p(
        "research_stall_turns",
        "research",
        "int",
        "A research run ends when this many model turns in a row bring no new source and no new "
        "evidence (0 = never; then only the limits below end it).",
        low=0,
        high=200,
    ),
    _p(
        "research_max_turns",
        "research",
        "int",
        "Model turns one research run may take (0 = no limit).",
        low=0,
        high=100000,
    ),
    _p(
        "research_max_tool_calls",
        "research",
        "int",
        "Tool calls one research run may make (0 = no limit).",
        low=0,
        high=100000,
    ),
    _p(
        "research_time_budget_seconds",
        "research",
        "float",
        "Time one default or thorough research run may take (0 = no limit).",
        low=0,
        high=604800,
        unit="s",
    ),
    _p(
        "research_quick_seconds",
        "research",
        "float",
        "Soft time frame of a quick look (0 = no limit).",
        low=0,
        high=7200,
        unit="s",
    ),
    _p(
        "research_output_tokens",
        "research",
        "int",
        "Tokens of one model answer in a research run (a long report is written in sections).",
        low=1024,
        high=32768,
        unit="tokens",
    ),
    _p(
        "research_observation_chars",
        "research",
        "int",
        "Characters of one page or search result the model sees in a research run.",
        low=2000,
        high=100000,
        unit="chars",
    ),
    _p(
        "research_context_chars",
        "research",
        "int",
        "Characters of the whole transcript the model reads in a research run.",
        low=8000,
        high=400000,
        unit="chars",
    ),
    _p(
        "research_min_context",
        "research",
        "int",
        "Context a model slot needs for a research run (0 = no hint).",
        low=0,
        high=262144,
        unit="tokens",
    ),
    _p(
        "agent_model_timeout_seconds",
        "agent",
        "float",
        "How long one model call may take.",
        low=30,
        high=3600,
        unit="s",
    ),
    _p(
        "agent_model_retry_seconds",
        "agent",
        "float",
        "Wait before a failed model call is tried again.",
        low=0,
        high=300,
        unit="s",
    ),
    _p(
        "agent_observation_chars",
        "agent",
        "int",
        "Characters of one tool result the model reads.",
        low=500,
        high=30000,
        unit="chars",
    ),
    _p(
        "agent_staged",
        "agent",
        "bool",
        "Plan a chat check in steps, each with a fresh context (Phase 1).",
    ),
    _p(
        "agent_stage_calls",
        "agent",
        "int",
        "Tool calls of one working step of a check in steps.",
        low=2,
        high=20,
        unit="calls",
    ),
    _p(
        "agent_context_chars",
        "agent",
        "int",
        "Characters of the whole transcript the model reads.",
        low=4000,
        high=400000,
        unit="chars",
    ),
    _p(
        "agent_tools_wait_seconds",
        "agent",
        "float",
        "How long a check waits for a tool node to be connected.",
        low=0,
        high=600,
        unit="s",
    ),
    _p(
        "agent_min_context",
        "agent",
        "int",
        "Context a model slot needs to be used for a check (0 = no hint).",
        low=0,
        high=262144,
        unit="tokens",
    ),
    _p(
        "agent_effort",
        "agent",
        "enum",
        "Effort level the agent's own model calls use; its entry in the thinking effort table "
        "(chat_effort) decides whether the model thinks.",
        choices=("fast", "balanced", "thorough"),
    ),
    _p(
        "agent_max_active_per_user",
        "agent",
        "int",
        "Checks one person may run at the same time.",
        low=1,
        high=20,
    ),
    _p(
        "agent_tool_mode",
        "agent",
        "enum",
        "How the model is asked to call tools.",
        choices=("auto", "native", "json"),
    ),
    # --- one question over several machines ----------------------------------------------------
    _p(
        "coordinator_parallel",
        "coordinator",
        "int",
        "Checks of one question that run side by side.",
        low=1,
        high=8,
    ),
    _p(
        "coordinator_max_children",
        "coordinator",
        "int",
        "Checks one question may start.",
        low=1,
        high=20,
    ),
    _p(
        "coordinator_time_budget_seconds",
        "coordinator",
        "float",
        "Time one question may take over all machines.",
        low=30,
        high=7200,
        unit="s",
    ),
    _p(
        "coordinator_child_turns",
        "coordinator",
        "int",
        "Model turns of one machine's check.",
        low=1,
        high=100,
    ),
    _p(
        "coordinator_child_tool_calls",
        "coordinator",
        "int",
        "Tool calls of one machine's check.",
        low=1,
        high=100,
    ),
    _p(
        "coordinator_child_time_seconds",
        "coordinator",
        "float",
        "Time of one machine's check.",
        low=30,
        high=3600,
        unit="s",
    ),
    # --- the model gateway ---------------------------------------------------------------------
    _p(
        "gateway_first_byte_timeout_seconds",
        "gateway",
        "float",
        "Wait for the first byte of an answer.",
        low=10,
        high=3600,
        unit="s",
    ),
    _p(
        "gateway_background_first_byte_timeout_seconds",
        "gateway",
        "float",
        "The same for background work.",
        low=10,
        high=7200,
        unit="s",
    ),
    _p(
        "gateway_idle_timeout_seconds",
        "gateway",
        "float",
        "A silent stream is ended after this.",
        low=10,
        high=3600,
        unit="s",
    ),
    _p(
        "gateway_load_timeout_seconds",
        "gateway",
        "float",
        "A model that is loaded for a request may take this long.",
        low=30,
        high=3600,
        unit="s",
    ),
    _p(
        "gateway_pause_timeout_seconds",
        "gateway",
        "float",
        "How long an answer may stand still while a node yields.",
        low=30,
        high=14400,
        unit="s",
    ),
    _p(
        "gateway_retry_after_seconds",
        "gateway",
        "int",
        "What a busy gateway tells a caller to wait.",
        low=1,
        high=600,
        unit="s",
    ),
    _p(
        "gateway_typical_reply_tokens",
        "gateway",
        "int",
        "Reply length the node choice plans with.",
        low=50,
        high=4000,
        unit="tokens",
    ),
    # --- chat ----------------------------------------------------------------------------------
    _p(
        "chat_default_context_tokens",
        "chat",
        "int",
        "Context assumed when a node does not say.",
        low=1024,
        high=262144,
        unit="tokens",
    ),
    _p(
        "chat_sampling",
        "chat",
        "json",
        "Sampling per model family: {family: {temperature, top_p, …}}.",
    ),
    _p("chat_effort", "chat", "json", "Thinking effort per model family and quality level."),
    _p(
        "issue_project",
        "chat",
        "str",
        "The project (by title) that issues from the running system are filed in.",
    ),
    _p(
        "segment_idle_seconds",
        "chat",
        "float",
        "A pause this long starts a new segment of a conversation.",
        low=60,
        high=86400,
        unit="s",
    ),
    _p("review_enabled", "chat", "bool", "A second model reads finished answers."),
    _p(
        "review_hours",
        "chat",
        "int",
        "How old an answer may be to still be reviewed.",
        low=1,
        high=720,
        unit="h",
    ),
    _p("review_batch", "chat", "int", "Answers reviewed per round.", low=1, high=50),
    _p("reactions_enabled", "chat", "bool", "The assistant may answer with an emoji."),
    # --- memory --------------------------------------------------------------------------------
    _p(
        "memory_idle_seconds",
        "memory",
        "float",
        "Memory work waits until the hub was idle this long.",
        low=0,
        high=3600,
        unit="s",
    ),
    _p(
        "memory_run_max_tokens",
        "memory",
        "int",
        "Tokens one memory run may read.",
        low=500,
        high=100000,
        unit="tokens",
    ),
    _p(
        "memory_run_max_seconds",
        "memory",
        "float",
        "Time one memory run may take.",
        low=30,
        high=7200,
        unit="s",
    ),
    _p(
        "memory_agent_learns",
        "memory",
        "bool",
        "The assistant may learn a rule or style the person states.",
    ),
    _p(
        "memory_auto_settle",
        "memory",
        "bool",
        "Contradicting notes are settled by the hub instead of asking the person.",
    ),
    _p(
        "memory_stage_sure",
        "memory",
        "float",
        "Reliability from which a note is stated plainly.",
        low=0,
        high=1,
    ),
    _p(
        "memory_stage_older",
        "memory",
        "float",
        "Reliability below which a note is said to be older.",
        low=0,
        high=1,
    ),
    _p(
        "memory_check_threshold",
        "memory",
        "float",
        "Reliability below which care checks a note.",
        low=0,
        high=1,
    ),
    _p(
        "memory_care_hour",
        "memory",
        "int",
        "Hour of the night when memory care runs.",
        low=0,
        high=23,
        unit="h",
    ),
    _p("memory_care_max_notes", "memory", "int", "Notes care checks per run.", low=1, high=200),
    _p(
        "memory_halflife_operational_days",
        "memory",
        "float",
        "Days after which an operational note is half as reliable.",
        low=0.1,
        high=3650,
        unit="d",
    ),
    _p(
        "memory_halflife_dev_days",
        "memory",
        "float",
        "Days after which a note of the Dev application is half as reliable.",
        low=0.1,
        high=3650,
        unit="d",
    ),
    # --- inventory and knowledge ---------------------------------------------------------------
    _p(
        "inventory_interval_seconds",
        "inventory",
        "float",
        "How often the hub reads the machines (0 = off).",
        low=0,
        high=86400,
        unit="s",
    ),
    _p(
        "knowledge_reindex_seconds",
        "knowledge",
        "float",
        "How often the knowledge base is read again (0 = off).",
        low=0,
        high=604800,
        unit="s",
    ),
    _p(
        "knowledge_hit_chars",
        "knowledge",
        "int",
        "Characters of one knowledge hit the model reads.",
        low=200,
        high=10000,
        unit="chars",
    ),
)

BY_KEY: dict[str, Param] = {p.key: p for p in PARAMS}
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(p.group for p in PARAMS))


class SettingError(ValueError):
    """The value is not acceptable; ``code`` says why (``unknown``, ``invalid``, ``range``)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _validate(param: Param, value: Any, adapter: TypeAdapter[Any]) -> Any:
    if param.kind == "bool" and not isinstance(value, bool):
        raise SettingError("invalid", f"{param.key} is a switch (true or false)")
    if param.kind in ("int", "float") and (
        isinstance(value, bool) or not isinstance(value, (int, float))
    ):
        raise SettingError("invalid", f"{param.key} is a number")
    if param.kind == "int" and isinstance(value, float) and not value.is_integer():
        raise SettingError("invalid", f"{param.key} is a whole number")
    try:
        checked = adapter.validate_python(value)
    except ValidationError as exc:
        raise SettingError("invalid", f"{param.key}: {exc.errors()[0]['msg']}") from exc
    if param.kind == "enum" and checked not in param.choices:
        raise SettingError("invalid", f"{param.key} is one of {', '.join(param.choices)}")
    if param.kind in ("int", "float"):
        below = param.low is not None and checked < param.low
        above = param.high is not None and checked > param.high
        if below or above:
            raise SettingError(
                "range", f"{param.key} must be between {param.low:g} and {param.high:g}"
            )
    return checked


Hook = Callable[[Settings], None]


@dataclass
class LiveSettings:
    """The overrides of one running hub. ``settings`` is the very object the services hold, so a
    change is seen by whoever reads a value the next time."""

    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession] | None = None
    hooks: dict[str, Hook] = field(default_factory=dict)
    defaults: dict[str, Any] = field(init=False)
    overrides: dict[str, Any] = field(init=False, default_factory=dict)
    _adapters: dict[str, TypeAdapter[Any]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.defaults = {k: getattr(self.settings, k) for k in BY_KEY}
        for key in BY_KEY:  # the type of the field in Settings is the type of the value
            self._adapters[key] = TypeAdapter(Settings.model_fields[key].annotation)

    # --- applying ------------------------------------------------------------------------------

    def _apply(self, key: str, value: Any) -> None:
        setattr(self.settings, key, value)
        hook = self.hooks.get(BY_KEY[key].group)
        if hook is not None:
            try:
                hook(self.settings)
            except Exception:
                log.exception("live settings: the hook of group %s failed", BY_KEY[key].group)

    def effective(self, key: str) -> Any:
        return getattr(self.settings, key)

    def check(self, key: str, value: Any) -> Any:
        param = BY_KEY.get(key)
        if param is None:
            raise SettingError("unknown", f"{key} cannot be changed while the hub runs")
        return _validate(param, value, self._adapters[key])

    # --- the database --------------------------------------------------------------------------

    async def load(self) -> int:
        """At start: the overrides of the database are applied. A value that no longer fits (a
        bound changed, a name was removed) is skipped and logged, never fatal."""
        if self.sessionmaker is None:
            return 0
        async with self.sessionmaker() as db:
            rows = (
                await db.scalars(select(HubSetting).where(HubSetting.key.like(f"{PREFIX}%")))
            ).all()
        applied = 0
        for row in rows:
            key = row.key.removeprefix(PREFIX)
            try:
                value = self.check(key, row.value)
            except SettingError as exc:
                log.warning("live settings: %s skipped: %s", row.key, exc)
                continue
            self.overrides[key] = value
            setattr(self.settings, key, value)
            applied += 1
        for group in {BY_KEY[k].group for k in self.overrides}:
            hook = self.hooks.get(group)
            if hook is not None:
                hook(self.settings)
        return applied

    async def set(self, key: str, value: Any, user_id: Any) -> Any:
        value = self.check(key, value)
        old = self.effective(key)
        if self.sessionmaker is not None:
            async with self.sessionmaker() as db:
                await _store(db, f"{PREFIX}{key}", value)
                db.add(SettingChange(key=key, old_value=old, new_value=value, user_id=user_id))
                await db.commit()
        self.overrides[key] = value
        self._apply(key, value)
        return value

    async def reset(self, key: str, user_id: Any) -> Any:
        if key not in BY_KEY:
            raise SettingError("unknown", f"{key} cannot be changed while the hub runs")
        old = self.effective(key)
        default = self.defaults[key]
        if self.sessionmaker is not None:
            async with self.sessionmaker() as db:
                await _store(db, f"{PREFIX}{key}", None)
                db.add(SettingChange(key=key, old_value=old, new_value=None, user_id=user_id))
                await db.commit()
        self.overrides.pop(key, None)
        self._apply(key, default)
        return default

    async def history(self, key: str, limit: int = 30) -> list[dict[str, Any]]:
        if key not in BY_KEY:
            raise SettingError("unknown", f"{key} cannot be changed while the hub runs")
        if self.sessionmaker is None:
            return []
        async with self.sessionmaker() as db:
            rows = (
                await db.scalars(
                    select(SettingChange)
                    .where(SettingChange.key == key)
                    .order_by(SettingChange.id.desc())
                    .limit(limit)
                )
            ).all()
        return [
            {
                "id": r.id,
                "old": r.old_value,
                "new": r.new_value,
                "reset": r.new_value is None,
                "by": str(r.user_id) if r.user_id else None,
                "at": r.at.isoformat() if r.at else None,
            }
            for r in rows
        ]

    # --- what the browser shows ----------------------------------------------------------------

    def view(self) -> dict[str, Any]:
        return {
            "groups": list(GROUPS),
            "params": [
                {
                    "key": p.key,
                    "group": p.group,
                    "kind": p.kind,
                    "text": p.text,
                    "unit": p.unit,
                    "low": p.low,
                    "high": p.high,
                    "choices": list(p.choices),
                    "default": self.defaults[p.key],
                    "value": self.effective(p.key),
                    "overridden": p.key in self.overrides,
                }
                for p in PARAMS
            ],
        }

    def export(self) -> dict[str, Any]:
        """The overrides alone, as one document that can be kept and put back later (a preset)."""
        return {"version": 1, "values": dict(sorted(self.overrides.items()))}


async def _store(db: AsyncSession, key: str, value: Any | None) -> None:
    from topcoat_hub.chat_store import set_hub_setting

    await set_hub_setting(db, key, value)
