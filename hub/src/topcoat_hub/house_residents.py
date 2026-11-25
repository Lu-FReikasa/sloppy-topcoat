"""Keep a node's house model in its ``residents`` list (D117, plan step 1.6).

The house model is hub policy (``node_policy.house``); keeping a model loaded is the node's own
job, driven by the ``residents`` entries of its config (E1-06): only a resident with
``auto_reload`` is loaded again by the node after an idle unload, a game or a restart. The two
used to be kept in step only by the scheduling page, which set the resident next to the house
model. When the entry went missing (removed on the node page) or lost ``auto_reload`` (a manual
unload clears it), the node unloaded its house model on idle and never loaded it again.

So the hub keeps them in step itself:

* whenever the house models change (``PUT /api/scheduling/house``), the new house model becomes a
  resident with ``auto_reload`` and the one it replaced is no resident any more;
* whenever a node connects, its house model is made an auto-reloading resident again. A manual
  unload of the house model therefore holds until the next reconnect; to stop keeping it loaded
  for good, take the house model away.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

# the priority the scheduling page gives a house model (web/src/pressure.ts)
HOUSE_PRIORITY = 50


def house_mutation(model: str | None, previous: str | None = None) -> Callable[[dict[str, Any]], bool]:
    """A ``change_config`` mutation: ``model`` becomes an auto-reloading resident, ``previous``
    (the house model it replaces) stops being one. Returns False when nothing changes."""

    def mutate(config: dict[str, Any]) -> bool:
        residents: list[dict[str, Any]] = config.get("residents") or []
        changed = False
        if previous and previous != model:
            kept = [r for r in residents if r.get("model") != previous]
            changed = len(kept) != len(residents)
            residents = kept
        if model:
            for r in residents:
                if r.get("model") == model:
                    if r.get("auto_reload", True) is not True:
                        r["auto_reload"] = True
                        changed = True
                    break
            else:
                residents.append({"model": model, "priority": HOUSE_PRIORITY, "auto_reload": True})
                changed = True
        if changed:
            config["residents"] = residents
        return changed

    return mutate


async def keep_house_resident(
    service: Any,
    schemas: Any,
    node_id: uuid.UUID,
    model: str | None,
    previous: str | None = None,
    *,
    source: str,
) -> tuple[dict[str, Any], str, bool]:
    """Apply :func:`house_mutation` to a node's stored config (validated, pushed if the node is
    connected) and record a ``config_changed`` event when it changed something.

    Returns ``(config, config_hash, changed)`` like ``NodeService.change_config``.
    """
    inner = house_mutation(model, previous)

    def mutate(config: dict[str, Any]) -> bool:
        if not inner(config):
            return False
        schemas.validate("node_config", config)
        return True

    config, new_hash, changed = await service.change_config(node_id, mutate)
    if changed:
        log.info("node %s: house model %s kept loaded (%s)", node_id, model, source)
        data = {"source": source, "model": model, "previous": previous}
        await service.record_event(node_id, "config_changed", data)
    return config, new_hash, changed
