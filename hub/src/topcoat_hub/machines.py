"""The physical machines behind the nodes (the Administration overview).

A node is a program on a machine, not the machine: a tool node, a dev node and a compute node can
all run on one host, and a VM runs on another. The overview shows the machines, with the nodes below
the machine they run on and the virtual machines below their host.

Nothing has to be configured to start: a node belongs to the machine its name points at
(``tools-alpha`` and ``node-alpha`` are on ``alpha``; a name without such a prefix is its own
machine). What an admin sets is stored in ``hub_settings`` (``machines``): the machines with a
display name and a kind (``physical`` or ``vm`` with the ``host`` it runs on), and which node runs
on which machine where the name does not say. Real names live in that setting, never in the repo.
"""

import re
from dataclasses import dataclass
from typing import Any

SETTING_KEY = "machines"
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
KINDS = ("physical", "vm")
# what a node name starts with before the name of its machine
NODE_PREFIXES = ("node", "tools", "dev")


class MachineError(ValueError):
    """The submitted machines are not valid."""


@dataclass(frozen=True)
class Machine:
    key: str
    name: str
    kind: str = "physical"
    host: str | None = None  # a vm: the physical machine it runs on


@dataclass(frozen=True)
class MachineMap:
    machines: dict[str, Machine]
    assign: dict[str, str]  # node name -> machine key, where the name does not say

    def to_json(self) -> dict[str, Any]:
        return {
            "machines": [
                {
                    "key": m.key,
                    "name": m.name,
                    "kind": m.kind,
                    **({"host": m.host} if m.host else {}),
                }
                for m in self.machines.values()
            ],
            "assign": dict(self.assign),
        }


def default_key(node_name: str) -> str:
    """The machine a node name points at: the part after ``node-``, ``tools-`` or ``dev-``."""
    low = node_name.strip().lower()
    prefix, sep, rest = low.partition("-")
    key = rest if sep and prefix in NODE_PREFIXES and rest else low
    key = re.sub(r"[^a-z0-9-]+", "-", key).strip("-")
    return key[:40] or "unknown"


def _key(value: Any, what: str) -> str:
    if not isinstance(value, str) or not KEY_RE.match(value):
        raise MachineError(f"{what}: a key is lower-case letters, digits and hyphens")
    return value


def parse(raw: Any) -> MachineMap:
    """The stored or submitted machines; raises :class:`MachineError` for anything wrong."""
    if raw is None:
        return MachineMap({}, {})
    if not isinstance(raw, dict) or set(raw) - {"machines", "assign"}:
        raise MachineError("machines: an object with 'machines' and 'assign'")
    machines: dict[str, Machine] = {}
    for item in raw.get("machines") or []:
        if not isinstance(item, dict) or set(item) - {"key", "name", "kind", "host"}:
            raise MachineError("a machine is {key, name, kind, host}")
        key = _key(item.get("key"), "machine")
        if key in machines:
            raise MachineError(f"machine {key} is listed twice")
        kind = item.get("kind", "physical")
        if kind not in KINDS:
            raise MachineError(f"{key}: kind is physical or vm")
        name = str(item.get("name") or key).strip()[:60] or key
        host = item.get("host") or None
        if kind == "physical" and host:
            raise MachineError(f"{key}: a physical machine has no host")
        machines[key] = Machine(key, name, kind, _key(host, f"{key}: host") if host else None)
    for m in machines.values():
        if m.kind == "vm":
            if m.host is None:
                raise MachineError(f"{m.key}: a vm needs the machine it runs on")
            if m.host == m.key:
                raise MachineError(f"{m.key} cannot run on itself")
            host = machines.get(m.host)
            if host is not None and host.kind != "physical":
                raise MachineError(f"{m.key}: its host {m.host} is itself a vm")
    assign_raw = raw.get("assign") or {}
    if not isinstance(assign_raw, dict):
        raise MachineError("assign maps node names to machine keys")
    assign = {}
    for node, key in assign_raw.items():
        if not isinstance(node, str) or not node.strip():
            raise MachineError("assign: a node name is missing")
        assign[node.strip()] = _key(key, f"assign {node}")
    return MachineMap(machines, assign)


def load(raw: Any) -> MachineMap:
    """A stored value that no longer parses counts as unset: it must not take the page down."""
    try:
        return parse(raw)
    except MachineError:
        return MachineMap({}, {})


def check_nodes(mapping: MachineMap, known: set[str]) -> None:
    missing = sorted(set(mapping.assign) - known)
    if missing:
        raise MachineError(f"unknown node: {missing[0]}")


def view(mapping: MachineMap, node_names: list[str]) -> dict[str, Any]:
    """Every machine with the nodes on it, and for every node the machine it is on. A machine the
    names point at but nobody configured appears as a physical one; a vm whose host is not a
    machine yet makes the host appear."""
    on: dict[str, str] = {n: mapping.assign.get(n) or default_key(n) for n in node_names}
    keys: dict[str, Machine] = dict(mapping.machines)
    for key in on.values():
        keys.setdefault(key, Machine(key, key))
    for m in list(keys.values()):
        if m.kind == "vm" and m.host:
            keys.setdefault(m.host, Machine(m.host, m.host))
    out = []
    for m in sorted(keys.values(), key=lambda m: (m.kind != "physical", m.name.lower())):
        out.append(
            {
                "key": m.key,
                "name": m.name,
                "kind": m.kind,
                "host": m.host,
                "configured": m.key in mapping.machines,
                "nodes": sorted(n for n, k in on.items() if k == m.key),
            }
        )
    return {"machines": out, "node_machine": on, "assign": dict(mapping.assign)}
