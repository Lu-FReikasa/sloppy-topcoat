"""Short history of the nodes' RAM and GPU load for the overview (admin)."""

from typing import Any

from fastapi import APIRouter, Depends, Request

from topcoat_hub.auth import CurrentUser, require_admin
from topcoat_hub.metrics import WINDOW_SECONDS

router = APIRouter(prefix="/api")


@router.get("/nodes/metrics")
async def node_metrics(
    request: Request, _user: CurrentUser = Depends(require_admin)
) -> dict[str, Any]:
    """The last half hour of heartbeat numbers per connected node (node id -> points, oldest
    first). A node that is not connected has none: the GUI shows no data for an offline node."""
    state = request.app.state
    return {
        "window_seconds": WINDOW_SECONDS,
        "nodes": {
            str(node_id): state.metrics.points(node_id)
            for node_id in state.sessions.connected_ids()
        },
    }
