"""nodes: state 'yielded'; add config.residents and config.pressure defaults (E1-06, D14)

GPU VRAM pressure handling is enabled on nodes whose latest inventory shows a discrete GPU
(or that never reported one yet) and disabled on CPU-only / iGPU-only nodes.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-28
"""

from collections.abc import Sequence
from typing import Any

from alembic import op

from topcoat_hub.configmigrate import add_config_defaults
from topcoat_hub.protocol import default_pressure

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATES = ("available", "busy_gaming", "degraded", "offline")


def _state_check(states: Sequence[str]) -> None:
    op.drop_constraint("nodes_state_check", "nodes", type_="check")
    values = ", ".join(f"'{s}'" for s in states)
    op.create_check_constraint("nodes_state_check", "nodes", f"state IN ({values})")


def has_discrete_gpu(inventory: dict[str, Any] | None) -> bool:
    """True if the inventory lists a discrete GPU, or if there is no inventory yet."""
    if not inventory:
        return True
    gpus = (inventory.get("hardware") or {}).get("gpus") or []
    return any(not g.get("integrated") for g in gpus)


def upgrade() -> None:
    _state_check((*STATES, "yielded"))
    add_config_defaults(
        op.get_bind(),
        lambda inv: {"residents": [], "pressure": default_pressure(has_discrete_gpu(inv))},
        source="migration 0005",
    )


def downgrade() -> None:
    # The config keys are optional in the schema; older hubs ignore them.
    op.execute("UPDATE nodes SET state = 'available' WHERE state = 'yielded'")
    _state_check(STATES)
