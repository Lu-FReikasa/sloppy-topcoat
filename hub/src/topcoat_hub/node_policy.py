"""Who may do what on which node (the admin's scheduling choices).

The gateway picks a node for every request. What an admin sets in the GUI is stored in
``hub_settings`` (``node_policy``):

* the **table of applications** (``applications``, SCH-04, D85): one row per application and per
  system row (``check``, ``background``, ``api``) with a priority, an ordered list of models, an
  ordered list of nodes (a request has no timer: it waits until a node is free). A row with
  models or nodes decides for its application; a node that is not in a row's list is not used for
  that work;
* a pause for all background work ("I need the GPU now");
* the **house model** of a node (``house``, 2026-11-14): the one model it keeps loaded and serves
  every kind of text work with. When nodes have one, the model lists of the rows no longer decide:
  a request goes to the house models of the nodes its row allows, the loaded one first, and nothing
  is unloaded for another model of the same kind. A node in ``workers`` keeps a small model for
  background work (summaries, notes, titles), which goes there first;
* the embedding model the notes and the knowledge base use.

Nothing here loads or unloads a model: it only narrows the choice of node.

Earlier versions stored a default model per node, four model roles with a list each, lists per
application, an order of nodes, nodes kept free of chat or of background work and read six
environment variables. ``legacy_policy.py`` reads those once, at start, and turns them into the
table; nothing writes them any more.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from topcoat_hub import applications, model_facts

POLICY_KEY = "node_policy"
MAX_PAUSE_HOURS = 24 * 7

# What a model is needed for (model_resolver.py): the kinds of work a caller names. The table has
# a row per application; ``answer`` and ``agent`` use the row of the application, ``check`` and
# ``background`` the rows of that name.
#   answer      the answer of a chat, the first thing the person waits for
#   agent       a check with tools, the coordinator, a request from another application
#   background  summaries, the care of the memory, notes: nobody waits
#   check       the second look: another model than the author
ROLES = ("answer", "agent", "background", "check")
# the standing tasks of the oldest policy and the role each one became (memory.py still names them)
TASK_OF_ROLE = {"answer": "chat", "agent": "agent", "background": "care", "check": "checker"}
ROLE_OF_TASK = {task: role for role, task in TASK_OF_ROLE.items()}
RETIRED_TASKS = frozenset({"summary"})
AUTO = "auto"  # an empty model field of a row: the node's best model by measured speed
DEFAULT = "default"
Entry = tuple[str, str]  # (node, model); model may be AUTO

# the table of applications (SCH-04): the rows besides the applications, and its special values
SYSTEM_ROWS = ("check", "background", "api")
SAME_AS_ANSWER = "same_as_answer"  # a model of a system row: the model that wrote the answer
ALL_NODES = "all"  # a node of a row: every other node, after the ones named before it
PRIORITIES = ("high", "normal", "low")


class PolicyError(ValueError):
    """The submitted policy is not valid."""


@dataclass(frozen=True)
class AppRow:
    """One row of the table: what an application (or a system row) may use, in order of preference.
    An empty list means "whatever is there": all nodes, the model that is fast enough."""

    priority: str | None = None  # None: normal; moves the queue place by one (gateway._place)
    models: tuple[str, ...] = ()
    nodes: tuple[str, ...] = ()

    @property
    def decides(self) -> bool:
        """Does the row say anything about models or nodes? A row of a priority only does not."""
        return bool(self.models or self.nodes)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.priority is not None:
            out["priority"] = self.priority
        if self.models:
            out["models"] = list(self.models)
        if self.nodes:
            out["nodes"] = list(self.nodes)
        return out


@dataclass(frozen=True)
class NodePolicy:
    # background work waits until this moment (UTC)
    paused_until: datetime | None = None
    # the embedding model the notes and the knowledge base use ("" = the first served bge-m3)
    embedding_model: str = ""
    # the table of applications (SCH-04): a row with models or nodes decides for its application
    applications: dict[str, AppRow] = field(default_factory=dict)
    # the pool per node (PRG-00 R1): the models the admin marked available. A node without an entry
    # offers every model it has, so nothing breaks before the admin chooses.
    pools: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # models a node may hold loaded at once (a small card holds one); a node not listed: no limit
    max_loaded: dict[str, int] = field(default_factory=dict)
    # kinds the admin set by hand per model name (model_facts.py); a model not listed is detected
    model_kinds: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # the house model per node (see the module text) and the nodes that are background workers
    house: dict[str, str] = field(default_factory=dict)
    workers: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    def paused(self, now: datetime | None = None) -> bool:
        return self.paused_until is not None and self.paused_until > (now or datetime.now(UTC))

    def in_pool(self, node: str, model: str) -> bool:
        """May programs use ``model`` on ``node``? (No pool for the node: every model; the node's
        house model always.)"""
        pool = self.pools.get(node)
        return pool is None or model in pool or self.house.get(node) == model

    def row_nodes(self, key: str) -> list[str]:
        """The nodes a row names (without ``all``); empty when the row is missing or names none."""
        row = self.applications.get(key)
        return [n for n in (row.nodes if row else ()) if n != ALL_NODES]

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.paused_until is not None:
            out["paused_until"] = self.paused_until.isoformat()
        if self.embedding_model:
            out["embedding_model"] = self.embedding_model
        if self.applications:
            out["applications"] = {k: row.to_json() for k, row in self.applications.items()}
        if self.pools:
            out["pools"] = {n: {"available": list(m)} for n, m in self.pools.items()}
        if self.max_loaded:
            out["max_loaded"] = dict(self.max_loaded)
        if self.model_kinds:
            out["model_kinds"] = {m: list(k) for m, k in self.model_kinds.items()}
        if self.house:
            out["house"] = dict(self.house)
            # the order of the house models is the order of the nodes (house_default, the rows
            # without nodes): stored as a list, because the hub's jsonb column sorts the keys of an
            # object (by length, then bytes) and lost it (issue 2.27, live 2026-11-23)
            if len(self.house) > 1:
                out["house_order"] = list(self.house)
        if self.workers:
            out["workers"] = list(self.workers)
        return out


def row_keys() -> tuple[str, ...]:
    """The keys of the table: the applications, then the system rows."""
    return (*applications.keys(), *SYSTEM_ROWS)


def _row_list(value: Any, what: str, special: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PolicyError(f"{what} must be a list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 255 or "@" in item:
            raise PolicyError(f"{what}: {item!r} is not a name")
        name = item.strip()
        if name in out:
            raise PolicyError(f"{what} lists {name} twice")
        out.append(name)
    if special == ALL_NODES and special in out and out.index(special) != len(out) - 1:
        raise PolicyError(f"{what}: {special} stands for the rest and comes last")
    return tuple(out)


def _app_table(value: Any) -> dict[str, AppRow]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError("applications must map rows to their settings")
    known = row_keys()
    out: dict[str, AppRow] = {}
    for key, raw in value.items():
        if key not in known:
            raise PolicyError(f"applications: unknown row {key!r}")
        # ``wait_seconds`` is accepted and dropped: rows stored before the waiting time left the
        # table (a request waits in the queue until a node is free) still carry it
        if not isinstance(raw, dict) or set(raw) - {"priority", "models", "nodes", "wait_seconds"}:
            raise PolicyError(f"applications.{key}: a row has priority, models, nodes")
        priority = raw.get("priority")
        if priority is not None and priority not in PRIORITIES:
            raise PolicyError(f"applications.{key}: priority is one of {', '.join(PRIORITIES)}")
        models = _row_list(raw.get("models"), f"applications.{key}.models", SAME_AS_ANSWER)
        if SAME_AS_ANSWER in models and key not in ("check", "background"):
            raise PolicyError(
                f"applications.{key}: {SAME_AS_ANSWER} belongs to check and background"
            )
        row = AppRow(
            priority=priority,
            models=models,
            nodes=_row_list(raw.get("nodes"), f"applications.{key}.nodes", ALL_NODES),
        )
        if row.to_json():  # a row without any setting is no row
            out[key] = row
    return out


def _pools(value: Any) -> dict[str, tuple[str, ...]]:
    """``{"node": {"available": [model, ...]}}``; an empty list is no pool (every model)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError("pools must map nodes to their pool")
    out: dict[str, tuple[str, ...]] = {}
    for node, raw in value.items():
        if not isinstance(node, str) or not node.strip() or "@" in node:
            raise PolicyError(f"pools: {node!r} is not a node name")
        if not isinstance(raw, dict) or set(raw) - {"available"}:
            raise PolicyError(f"pools.{node}: a pool has 'available' only")
        models = _row_list(raw.get("available"), f"pools.{node}.available", "")
        if models:
            out[node] = models
    return out


def _max_loaded(value: Any) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError("max_loaded must map nodes to a number")
    out: dict[str, int] = {}
    for node, count in value.items():
        if not isinstance(node, str) or not node.strip() or "@" in node:
            raise PolicyError(f"max_loaded: {node!r} is not a node name")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 16:
            raise PolicyError(f"max_loaded.{node}: a number from 1 to 16")
        out[node.strip()] = count
    return out


def _model_kinds(value: Any) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError("model_kinds must map models to their kinds")
    out: dict[str, tuple[str, ...]] = {}
    for model, kinds in value.items():
        if not isinstance(model, str) or not model.strip() or "@" in model or len(model) > 255:
            raise PolicyError(f"model_kinds: {model!r} is not a model name")
        parsed = _row_list(kinds, f"model_kinds.{model}", "")
        bad = [k for k in parsed if k not in model_facts.KINDS]
        if bad:
            raise PolicyError(f"model_kinds.{model}: unknown kind {bad[0]!r}")
        if not parsed:
            raise PolicyError(f"model_kinds.{model}: at least one kind (or remove the entry)")
        out[model.strip()] = parsed
    return out


def _house(value: Any, order: Any = None) -> dict[str, str]:
    """``{"node": "model"}``: the model each node keeps loaded, in ``order`` (a list of nodes: the
    house order; nodes it does not name follow in the order of ``value``)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError("house must map nodes to a model")
    out: dict[str, str] = {}
    for node, model in value.items():
        if not isinstance(node, str) or not node.strip() or "@" in node:
            raise PolicyError(f"house: {node!r} is not a node name")
        if not isinstance(model, str) or not model.strip() or "@" in model or len(model) > 255:
            raise PolicyError(f"house.{node}: {model!r} is not a model name")
        out[node.strip()] = model.strip()
    if order is None:
        return out
    ranked = _row_list(order, "house_order", "")
    unknown = [n for n in ranked if n not in out]
    if unknown:
        raise PolicyError(f"house_order: {unknown[0]} has no house model")
    return {**{n: out[n] for n in ranked}, **{n: m for n, m in out.items() if n not in ranked}}


def _model_name(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value.strip()) > 255 or "@" in value:
        raise PolicyError("embedding_model must be a model name")
    return value.strip()


def parse_policy(raw: Any) -> NodePolicy:
    """The stored or submitted policy; unknown keys are an error. (The older lists are not
    accepted here: ``legacy_policy.convert`` turns a stored policy that has them into a table
    before this is called.)"""
    if raw is None:
        return NodePolicy()
    if not isinstance(raw, dict):
        raise PolicyError("the policy must be an object")
    unknown = set(raw) - {
        "paused_until",
        "embedding_model",
        "applications",
        "pools",
        "model_kinds",
        "max_loaded",
        "house",
        "house_order",
        "workers",
    }
    if unknown:
        raise PolicyError(f"unknown setting: {sorted(unknown)[0]}")
    paused: datetime | None = None
    if raw.get("paused_until") is not None:
        try:
            paused = datetime.fromisoformat(str(raw["paused_until"]))
        except ValueError as exc:
            raise PolicyError("paused_until must be an ISO timestamp") from exc
        if paused.tzinfo is None:
            paused = paused.replace(tzinfo=UTC)
    return NodePolicy(
        paused_until=paused,
        embedding_model=_model_name(raw.get("embedding_model")),
        applications=_app_table(raw.get("applications")),
        pools=_pools(raw.get("pools")),
        model_kinds=_model_kinds(raw.get("model_kinds")),
        max_loaded=_max_loaded(raw.get("max_loaded")),
        house=_house(raw.get("house"), raw.get("house_order")),
        workers=_row_list(raw.get("workers"), "workers", ""),
    )


def load_policy(raw: Any) -> NodePolicy:
    """A stored policy that no longer parses must not take the hub down: it counts as unset."""
    try:
        return parse_policy(raw)
    except PolicyError:
        return NodePolicy()


def check_nodes(policy: NodePolicy, known: set[str]) -> None:
    """Every node named in a submitted policy exists (a typo would silently do nothing)."""
    named = {n for row in policy.applications.values() for n in row.nodes if n != ALL_NODES}
    named |= set(policy.pools) | set(policy.max_loaded) | set(policy.house) | set(policy.workers)
    missing = sorted(named - known)
    if missing:
        raise PolicyError(f"unknown node: {missing[0]}")
