"""Probes that prove what a model can do (PRG-00 R1-Rest): a tool call (``tools``) and an
embedding (``embedding``). A probe is a short request through the gateway at the lowest priority,
run by an admin on a node that is free (the gateway refuses a node that belongs to a game). The
answer is stored in ``hub_settings`` under ``model_proofs`` and read by ``model_facts`` as the
source ``proven``. Only a probe the model passed counts; a failed probe proves nothing and the
name rule keeps applying.

Automatic probes (the hub choosing when a node is idle) come later; here the admin starts them."""

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from topcoat_hub.gateway import GatewayError
from topcoat_hub.model_facts import PROBES

# the probe's own request: short, deterministic
_TOOL = {
    "type": "function",
    "function": {
        "name": "topcoat_probe",
        "description": "Probe: call this function once.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

# a model that cannot answer at all (node gone, model missing) ends the probe; anything else only
# means this probe is not proven
ABORTING = {
    "node_not_found",
    "model_not_found",
    "model_unavailable",
    "background_paused",
    "node_kept_free",
    "node_holds_other_model",
    "load_failed",
    "unload_failed",
    "timeout",
}

Call = Callable[..., Awaitable[dict[str, Any]]]


def tools_passed(answer: Mapping[str, Any]) -> bool:
    """True when the first answer carries at least one tool call."""
    choices = answer.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, dict) else None
    calls = message.get("tool_calls") if isinstance(message, dict) else None
    return isinstance(calls, list) and len(calls) > 0


def embedding_passed(answer: Mapping[str, Any]) -> bool:
    """True when the answer holds one non-empty vector of numbers."""
    data = answer.get("data")
    first = data[0] if isinstance(data, list) and data else {}
    vector = first.get("embedding") if isinstance(first, dict) else None
    return (
        isinstance(vector, list)
        and len(vector) > 0
        and all(isinstance(x, (int, float)) for x in vector)
    )


async def _probe(call: Call, pinned: str, probe: str) -> bool:
    """Run one probe; the gateway's own errors that end a run are raised, the rest mean no."""
    if probe == "tools":
        payload: dict[str, Any] = {
            "temperature": 0,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "Call the probe function now."}],
            "tools": [_TOOL],
            "tool_choice": "required",
        }
        path, check = "/v1/chat/completions", tools_passed
    else:
        payload = {"input": "probe"}
        path, check = "/v1/embeddings", embedding_passed
    try:
        answer = await call(pinned, path, payload, label="probe")
    except GatewayError as exc:
        if exc.code in ABORTING:
            raise
        return False
    return check(answer)


async def run_probes(
    call: Call, pinned: str, probes: tuple[str, ...] = PROBES, node: str = ""
) -> dict[str, dict[str, Any]]:
    """Every requested probe for ``pinned`` ("model@node"): {probe: {"ok", "node", "at"}}."""
    now = datetime.now(UTC).isoformat()
    out: dict[str, dict[str, Any]] = {}
    for probe in probes:
        ok = await _probe(call, pinned, probe)
        out[probe] = {"ok": ok, "node": node, "at": now}
    return out
