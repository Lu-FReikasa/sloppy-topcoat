"""JSON views for the GUI API. The shapes are the contract with web/src (NodeSummary etc.)."""

from typing import Any

from topcoat_hub.models import Node, NodeBenchmark, NodeEvent, NodeInventory
from topcoat_hub.updates import is_newer, node_channel, version_key


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


# Per-process details of a loaded model (llamaserver runner) shown next to its VRAM.
_LOADED_DETAILS = ("ram_used_mb", "backend", "port", "context_length", "n_parallel")
# Optional heartbeat GPU fields passed through to the GUI when a node reports them.
_GPU_USAGE_DETAILS = (
    "shared_used_mb",
    "gpu_util",
    "foreign_vram_used_mb",
    "foreign_gpu_util",
    "foreign_shared_max_mb",
)


def _loaded_fields(lm: dict[str, Any] | None) -> dict[str, Any]:
    if lm is None:
        return {"loaded": False, "vram_used_mb": None}
    fields = {"loaded": True, "vram_used_mb": lm.get("vram_used_mb")}
    fields.update({k: lm[k] for k in _LOADED_DETAILS if k in lm})
    return fields


def node_summary(
    node: Node,
    inv: NodeInventory | None,
    connected: bool,
    latest_release: str | None = None,
) -> dict[str, Any]:
    inventory: dict[str, Any] = (inv.inventory if inv else None) or {}
    status: dict[str, Any] = (inv.status if inv else None) or {}
    hardware: dict[str, Any] = inventory.get("hardware") or {}

    # Heartbeat usage is merged into the inventory's GPU list; iGPUs also report the
    # system memory they use (shared_used_mb) separately from dedicated VRAM, and nodes
    # report what other programs use where they can measure it (E1-06).
    usage = {g["index"]: g for g in status.get("gpus", [])}
    gpus = []
    for g in hardware.get("gpus", []):
        used = usage.get(g["index"], {})
        gpu = {**g, "vram_used_mb": used.get("vram_used_mb")}
        gpu.update({k: used[k] for k in _GPU_USAGE_DETAILS if k in used})
        gpus.append(gpu)

    # The latest heartbeat is fresher than the inventory for "what is loaded".
    loaded: list[dict[str, Any]] = status.get("loaded", inventory.get("loaded", []))
    loaded_by_name = {m["name"]: m for m in loaded}
    models = []
    for model in inventory.get("models", []):
        lm = loaded_by_name.get(model["name"])
        models.append({**model, **_loaded_fields(lm)})
    known = {m["name"] for m in models}
    for lm in loaded:
        if lm["name"] not in known:
            models.append({"name": lm["name"], **_loaded_fields(lm)})

    return {
        "id": str(node.id),
        "name": node.name,
        "os": node.os,
        "arch": node.arch,
        "state": node.state if connected else "offline",
        "connected": connected,
        "last_seen": _iso(node.last_seen),
        "agent_version": node.agent_version,
        "cpu": hardware.get("cpu"),
        "ram_total_mb": hardware.get("ram_total_mb"),
        "ram_used_mb": status.get("ram_used_mb"),
        # all processors together in percent (a node that cannot measure it sends none)
        "cpu_util": status.get("cpu_util"),
        # the drive that holds the model directory (E2-05, D27); refreshed with the inventory
        "disk_total_mb": hardware.get("disk_total_mb"),
        "disk_free_mb": hardware.get("disk_free_mb"),
        # physical network interfaces (wired first): what Wake-on-LAN can reach (E2-05 E)
        "network": hardware.get("network") or [],
        # unicast IPs of the node: the candidates for a direct model transfer (E2-07, D51)
        "addresses": hardware.get("addresses") or [],
        # what the hub lets this node run (the GUI greys out targets without models.receive)
        "allowed_actions": list((node.config or {}).get("allowed_actions") or []),
        # "gpu" = a compute node that holds and serves models, "tools" = a tool node (E2-04),
        # "dev" = a dev workshop node (E6-01)
        "roles": list((node.config or {}).get("roles") or []),
        # what a tool node offers (E2-04), e.g. web.search, docker.logs
        "capabilities": list((node.config or {}).get("capabilities") or []),
        "gpus": gpus,
        "runner": inventory.get("runner"),
        "models": models,
        "loaded": loaded,
        # Models the node is loading right now (E1-05; heartbeat "loading").
        "loading": list(status.get("loading", [])),
        # Signed updates (E2-06): what the node reports about itself and the newest release of
        # its channel that the hub holds for its platform.
        "update": _update_view(node, inventory, latest_release),
    }


def _update_view(
    node: Node, inventory: dict[str, Any], latest_release: str | None
) -> dict[str, Any]:
    reported: dict[str, Any] = inventory.get("update") or {}
    raw = reported.get("updater_version")
    updater_version = raw if isinstance(raw, str) and raw else None
    return {
        "channel": reported.get("channel") or node_channel(node.config),
        "capable": bool(reported.get("capable")),
        "updater": reported.get("updater"),
        # the version the running updater reports (node 0.2.6); an updater older than the node may
        # not apply a new build, then the program is installed once by hand (live 2026-11-09)
        "updater_version": updater_version,
        "updater_behind": updater_behind(updater_version, node.agent_version),
        "key_ids": reported.get("key_ids") or [],
        "available": latest_release,
        "newer": latest_release is not None and is_newer(latest_release, node.agent_version),
        "action_allowed": "node.update" in ((node.config or {}).get("allowed_actions") or []),
    }


def updater_behind(updater: str | None, agent: str | None) -> bool:
    """The updater runs an older release than the node. A version that is no release number (a git
    build) or none on either side says nothing (no warning on a guess)."""
    if not updater or not agent:
        return False
    try:
        return version_key(updater) < version_key(agent)
    except ValueError:
        return False


def node_event(event: NodeEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "kind": event.kind,
        "data": event.data,
        "ts": _iso(event.ts),
        "received_at": _iso(event.received_at),
    }


def node_benchmark(row: NodeBenchmark) -> dict[str, Any]:
    return {
        "benchmark_id": str(row.benchmark_id),
        "model": row.model,
        "backend": row.backend,
        "threads": row.threads,
        "n_gpu_layers": row.n_gpu_layers,
        "pp_tps": row.pp_tps,
        "tg_tps": row.tg_tps,
        "ts": _iso(row.ts),
    }
