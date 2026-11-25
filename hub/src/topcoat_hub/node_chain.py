"""Pick from an ordered list of ``(node, model)`` the first entry that can answer now.

The lists are the model roles of the node policy (model_resolver.py). An entry can answer when its
node is connected and not busy with a game or yielded; later entries answer with *their own* model,
and the answer then says which node it would normally have come from."""

from dataclasses import dataclass

from topcoat_hub.gateway import Gateway, GatewayError


@dataclass(frozen=True)
class Pick:
    node: str
    model: str
    fallback_from: str | None  # the first entry's node when a later entry answers
    fallback_state: str | None  # why that one was passed over (``busy_gaming``, ``offline`` …)


async def pick(gateway: Gateway, chain: list[tuple[str, str]]) -> Pick | None:
    """The first entry that can take a request now; ``None`` if none can (the caller then
    falls back to its normal route and its normal error).

    An entry whose node has a free place comes before one whose places are all taken (live
    2026-11-18: two items of a checklist waited for the one slot of node-nvidia while node-amd, next in
    the row with its own house model and two free slots, did nothing). When every node is full,
    the first one that can take it at all is the answer, as before: its queue decides."""
    first_state: str | None = None
    full: Pick | None = None  # the first entry that could take it, but only after a wait
    has_room = getattr(gateway, "_has_room", None)
    for i, (node, model) in enumerate(chain):
        try:
            route = await gateway.route(f"{model}@{node}")
        except GatewayError as exc:
            if i == 0:
                # a node the admin keeps free of chat (node policy) shows as "paused": the GUI
                # knows that state and it means "kept for other use"
                if exc.code == "node_kept_free":
                    first_state = "paused"
                else:
                    first_state = "busy_gaming" if "busy" in exc.message else "offline"
            continue
        # a node that is only full is no fallback: the first entry's node is still its home
        found = Pick(node, model, None, None) if i == 0 or first_state is None else (
            Pick(node, model, chain[0][0], first_state)
        )  # fmt: skip
        free = has_room is None or any(has_room(c) for c in route.candidates)
        if free:
            return found
        full = full or found
    return full
