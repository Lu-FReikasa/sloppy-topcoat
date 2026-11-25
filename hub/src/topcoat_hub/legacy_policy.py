"""The older scheduling lists, read once and turned into the table of applications (SCH-04, D85).

Before the table, the scheduling policy held a default model per node (``defaults``), four model
roles with a list of ``(node, model)`` each (``roles``) and lists of single applications
(``app_roles``), an order of nodes for the ``/v1`` gateway (``order``), the nodes background work
prefers (``background_nodes``), nodes kept free of chat or of background work (``no_chat``,
``no_background``), the per-task routes of an even older version (``routes``) and six environment
variables. Nothing writes them any more. :func:`convert` reads whatever is stored, and the
environment, and returns the table they stand for; the hub writes the result back, so the next
start reads the table only.

What a list becomes: the answer role of an application (its own list, else the hub's) is the row
of that application, models and nodes in order (``default`` read through the node's default model,
``auto`` left out: an empty field picks by speed); the roles check and background are the rows of
that name with ``same_as_answer`` at the end; the order of the gateway is the row ``api``; a node
kept free is taken out of the node list of the rows it was kept free of.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from topcoat_hub import applications
from topcoat_hub.node_policy import (
    ALL_NODES,
    AUTO,
    DEFAULT,
    RETIRED_TASKS,
    ROLE_OF_TASK,
    ROLES,
    SAME_AS_ANSWER,
    AppRow,
    Entry,
    NodePolicy,
    PolicyError,
    load_policy,
)

log = logging.getLogger(__name__)

LEGACY_KEYS = frozenset(
    {
        "order",
        "background_nodes",
        "no_chat",
        "no_background",
        "roles",
        "app_roles",
        "defaults",
        "routes",
    }
)
# the environment variables that used to steer model choice
ENV_CHAT_CHAIN = "TOPCOAT_CHAT_NODE_CHAIN"
ENV_BACKGROUND_NODES = "TOPCOAT_GATEWAY_BACKGROUND_NODES"
ENV_BACKGROUND_MODEL = "TOPCOAT_GATEWAY_BACKGROUND_MODEL"
ENV_MEMORY_MODEL = "TOPCOAT_MEMORY_MODEL"
ENV_REVIEW_MODEL = "TOPCOAT_REVIEW_MODEL"
ENV_NODE_ORDER = "TOPCOAT_GATEWAY_NODE_ORDER"
LEGACY_ENV = (
    ENV_CHAT_CHAIN,
    ENV_BACKGROUND_NODES,
    ENV_BACKGROUND_MODEL,
    ENV_MEMORY_MODEL,
    ENV_REVIEW_MODEL,
    ENV_NODE_ORDER,
)


@dataclass
class Lists:
    """The older lists as they were stored."""

    order: list[str] | None = None
    background_nodes: list[str] | None = None
    no_chat: frozenset[str] = frozenset()
    no_background: frozenset[str] = frozenset()
    defaults: dict[str, str] = field(default_factory=dict)
    roles: dict[str, list[Entry]] = field(default_factory=dict)
    app_roles: dict[str, dict[str, list[Entry]]] = field(default_factory=dict)

    def empty(self) -> bool:
        return not (
            self.order
            or self.background_nodes
            or self.no_chat
            or self.no_background
            or self.roles
            or self.app_roles
        )


def _entries(value: Any, what: str) -> list[Entry]:
    """A list of ``{"node": ..., "model": ...}``; the model is a name, ``default`` or ``auto``
    (missing = ``default``)."""
    if not isinstance(value, list):
        raise PolicyError(f"{what} must be a list of entries")
    out: list[Entry] = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"node", "model"}:
            raise PolicyError(f"{what}: an entry is {{node, model}}")
        node, model = item.get("node"), item.get("model", DEFAULT)
        if not isinstance(node, str) or not node.strip():
            raise PolicyError(f"{what}: an entry needs a node")
        if not isinstance(model, str) or not model.strip() or len(model) > 255 or "@" in model:
            raise PolicyError(f"{what}: the model of {node} is not a name")
        entry = (node.strip(), model.strip())
        if entry in out:
            raise PolicyError(f"{what} lists {entry[0]} with {entry[1]} twice")
        out.append(entry)
    return out


def _roles(value: Any, what: str) -> dict[str, list[Entry]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError(f"{what} must map roles to lists")
    out: dict[str, list[Entry]] = {}
    for role, entries in value.items():
        if role not in ROLES:
            raise PolicyError(f"{what}: unknown role {role!r}")
        if entries:  # an empty list is "none stored": the role falls back
            out[role] = _entries(entries, f"{what}.{role}")
    return out


def _app_roles(value: Any) -> dict[str, dict[str, list[Entry]]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError("app_roles must map applications to roles")
    known = applications.keys()
    out: dict[str, dict[str, list[Entry]]] = {}
    for app, roles in value.items():
        if app not in known:
            raise PolicyError(f"app_roles: unknown application {app!r}")
        parsed = _roles(roles, f"app_roles.{app}")
        if parsed:
            out[app] = parsed
    return out


def _names(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise PolicyError(f"{what} must be a list of node names")
    names = [x.strip() for x in value]
    if len(set(names)) != len(names):
        raise PolicyError(f"{what} lists a node twice")
    return names


def _defaults(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError("defaults must map node names to model names")
    out: dict[str, str] = {}
    for node, model in value.items():
        if not isinstance(node, str) or not node.strip():
            raise PolicyError("defaults: a node name is missing")
        if not isinstance(model, str) or len(model.strip()) > 255:
            raise PolicyError(f"defaults: the model of {node} is not a name")
        if model.strip():  # an empty model means "no default" for that node
            out[node.strip()] = model.strip()
    return out


def _legacy_routes(value: Any) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError("routes must map tasks to lists of node names")
    out: dict[str, list[str]] = {}
    for task, nodes in value.items():
        if task in RETIRED_TASKS:
            continue
        if task not in ROLE_OF_TASK:
            raise PolicyError(f"routes: unknown task {task!r}")
        out[task] = _names(nodes, f"routes.{task}")
    return out


def _roles_from_legacy(
    routes: dict[str, list[str]], defaults: dict[str, str]
) -> dict[str, list[Entry]]:
    """The nodes of a task, each with its default model, as the role that took the task over."""
    out: dict[str, list[Entry]] = {}
    for task, nodes in routes.items():
        entries = [(n, defaults[n]) for n in nodes if n in defaults]
        if entries:
            out[ROLE_OF_TASK[task]] = entries
    return out


def _split(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def _chain(text: str) -> list[Entry]:
    """``node:model,node:model`` of the old ``TOPCOAT_CHAT_NODE_CHAIN``."""
    out: list[Entry] = []
    for entry in text.split(","):
        node, sep, model = entry.strip().partition(":")
        if sep and node.strip() and model.strip():
            out.append((node.strip(), model.strip()))
    return out


def parse_lists(raw: dict[str, Any]) -> Lists:
    """The older keys of a stored policy; a part that does not parse is dropped and logged (a
    stored value is trusted less than code, and the hub must start)."""

    def safe(call: Any, what: str, default: Any) -> Any:
        try:
            return call()
        except PolicyError as exc:
            log.warning("older scheduling list %s dropped: %s", what, exc)
            return default

    defaults = safe(lambda: _defaults(raw.get("defaults")), "defaults", {})
    older = safe(
        lambda: _roles_from_legacy(_legacy_routes(raw.get("routes")), defaults), "routes", {}
    )
    roles = safe(lambda: _roles(raw.get("roles"), "roles"), "roles", {})
    return Lists(
        order=safe(lambda: _names(raw["order"], "order"), "order", None)
        if raw.get("order") is not None
        else None,
        background_nodes=safe(
            lambda: _names(raw["background_nodes"], "background_nodes"), "background_nodes", None
        )
        if raw.get("background_nodes") is not None
        else None,
        no_chat=frozenset(safe(lambda: _names(raw.get("no_chat") or [], "no_chat"), "no_chat", [])),
        no_background=frozenset(
            safe(
                lambda: _names(raw.get("no_background") or [], "no_background"), "no_background", []
            )
        ),
        defaults=defaults,
        roles={**older, **roles},
        app_roles=safe(lambda: _app_roles(raw.get("app_roles")), "app_roles", {}),
    )


def apply_environment(lists: Lists, env: Mapping[str, str]) -> tuple[Lists, list[str]]:
    """Roles the old environment variables stand for, for the roles the lists do not have yet.
    Returns the lists and the names of the variables that were set."""
    used = [name for name in LEGACY_ENV if env.get(name, "").strip()]
    if not used:
        return lists, []
    roles = dict(lists.roles)
    chain = _chain(env.get(ENV_CHAT_CHAIN, ""))
    if chain and "answer" not in roles:
        roles["answer"] = chain
    background_model = env.get(ENV_BACKGROUND_MODEL, "").strip()
    nodes = _split(env.get(ENV_BACKGROUND_NODES, ""))
    if nodes and "background" not in roles:
        roles["background"] = [(n, background_model or AUTO) for n in nodes]
    for variable, role in ((ENV_MEMORY_MODEL, "background"), (ENV_REVIEW_MODEL, "check")):
        model, _, node = env.get(variable, "").strip().partition("@")
        if node and model and role not in roles:  # a model without a node names no entry
            roles[role] = [(node, model)]
    order = lists.order
    if order is None and (names := _split(env.get(ENV_NODE_ORDER, ""))):
        order = names
    lists.roles, lists.order = roles, order
    return lists, used


def table_from_lists(lists: Lists, known_nodes: list[str] | None = None) -> dict[str, AppRow]:
    """The table the lists stand for. ``known_nodes`` lets a node kept free be taken out of an
    application that had no list of its own (its row then names every other node)."""

    def split(entries: list[Entry], banned: frozenset[str]) -> tuple[list[str], list[str]]:
        models: list[str] = []
        nodes: list[str] = []
        for node, model in entries:
            if node in banned:
                continue
            if model == DEFAULT:
                model = lists.defaults.get(node, AUTO)
            if model != AUTO and model not in models:
                models.append(model)
            if node not in nodes:
                nodes.append(node)
        return models, nodes

    def others(banned: frozenset[str]) -> tuple[str, ...]:
        return tuple(n for n in (known_nodes or []) if n not in banned) if banned else ()

    def with_rest(nodes: list[str], banned: frozenset[str]) -> tuple[str, ...]:
        """A list of preferred nodes. The older lists were an order, not a fence: the rest of the
        nodes stayed usable, so ``all`` follows. Where nodes were kept free, the rest is written
        out without them (``all`` could not leave them out)."""
        if not banned:
            return (*nodes, ALL_NODES)
        return (*nodes, *(n for n in others(banned) if n not in nodes))

    rows: dict[str, AppRow] = {}
    for app in applications.keys():  # noqa: SIM118 - a function, not a dict
        entries = lists.app_roles.get(app, {}).get("answer") or lists.roles.get("answer")
        if entries:
            models, nodes = split(entries, lists.no_chat)
            if models or nodes:
                rows[app] = AppRow(models=tuple(models), nodes=with_rest(nodes, lists.no_chat))
        elif lists.no_chat and (free := others(lists.no_chat)):
            rows[app] = AppRow(nodes=free)
    for key in ("check", "background"):
        entries = lists.roles.get(key)
        banned = lists.no_background
        if entries:
            models, nodes = split(entries, banned)
            if key == "background":  # the nodes background work prefers come first
                nodes = [n for n in (lists.background_nodes or []) if n not in banned] + [
                    n for n in nodes if n not in (lists.background_nodes or [])
                ]
            rows[key] = AppRow(
                priority="low" if key == "background" else None,
                models=(*models, SAME_AS_ANSWER),
                nodes=with_rest(nodes, banned),
            )
        elif banned and (free := others(banned)):
            rows[key] = AppRow(nodes=free, priority="low" if key == "background" else None)
        elif key == "background" and lists.background_nodes:
            rows[key] = AppRow(priority="low", nodes=(*lists.background_nodes, ALL_NODES))
    if lists.order:
        rows["api"] = AppRow(nodes=(*lists.order, ALL_NODES))
    return rows


def convert(
    raw: Any,
    env: Mapping[str, str],
    known_nodes: list[str] | None = None,
) -> tuple[NodePolicy, bool, list[str]]:
    """The stored policy as a table-only :class:`NodePolicy`, whether anything older was in it
    (or in the environment) and so the result must be written back, and the variables read.
    A row that is already in the table wins over what the lists say for it."""
    stored = raw if isinstance(raw, dict) else {}
    current = load_policy({k: v for k, v in stored.items() if k not in LEGACY_KEYS})
    lists, used = apply_environment(parse_lists(stored), env)
    older = any(k in stored for k in LEGACY_KEYS)
    if not older and not used:
        return current, False, []
    rows = table_from_lists(lists, known_nodes)
    merged = {**rows, **current.applications}
    return (
        NodePolicy(
            paused_until=current.paused_until,
            embedding_model=current.embedding_model,
            applications=merged,
        ),
        True,
        used,
    )
