"""How the chat answers, as a setting an admin edits (docs/PLATFORM.md, 3.4 and 3.6).

The policy is one document in ``hub_settings`` (``chat_policy``): the limits of the server check,
the second look, recall, writing notes, the Nachlese, reactions and placement. Every value may be
an *effort table* (``{"effort": {"fast": ..., "balanced": ..., "thorough": ...}}``), so the answer
reads it at the effort of the conversation. Whatever the document does not say is what the hub did
before the policy existed: a policy that cannot be read never changes an answer, it only fails to
change it. (Until the flows were taken out the same values were read from the policy nodes of a
flow; the document has the same keys.)"""

import logging
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import quality
from topcoat_hub.agent_loop import Limits
from topcoat_hub.chat_store import get_hub_setting, set_hub_setting

log = logging.getLogger(__name__)

SETTING_KEY = "chat_policy"
LEVELS = ("fast", "balanced", "thorough")
DEFAULT_LEVEL = "balanced"
TABLE_KEY = "effort"

# key -> (low, high) of a number
NUMBERS: dict[str, tuple[int, int]] = {
    "max_turns": (1, 100),
    "max_tool_calls": (1, 100),
    "time_budget": (10, 1800),
}
BOOLEANS = ("second_look", "recall", "write_memory", "review", "reactions")
KEYS = frozenset({*NUMBERS, *BOOLEANS, "node"})


class PolicyError(ValueError):
    """The submitted policy is not valid."""


@dataclass(frozen=True)
class ChatPolicy:
    """What the policy says at one effort level; ``None`` = the hub's own rule applies."""

    max_turns: int | None = None
    max_tool_calls: int | None = None
    time_budget: float | None = None
    second_look: bool | None = None
    recall: bool = True
    write_memory: bool = True
    node: str = ""
    # None: the hub's setting (``review_enabled``, ``reactions_enabled``) holds
    review: bool | None = None
    reactions: bool | None = None

    def apply_limits(self, base: Limits) -> Limits:
        """The limits of a check with the policy's numbers over the level's (never a number the
        policy does not give)."""
        changes: dict[str, Any] = {}
        if self.max_turns is not None:
            changes["max_turns"] = self.max_turns
        if self.max_tool_calls is not None:
            changes["max_tool_calls"] = self.max_tool_calls
        if self.time_budget is not None:
            changes["time_budget_seconds"] = float(self.time_budget)
        return replace(base, **changes) if changes else base

    def wants_review(self, hub_setting: bool) -> bool:
        return hub_setting if self.review is None else self.review

    def wants_reactions(self, hub_setting: bool) -> bool:
        return hub_setting if self.reactions is None else self.reactions

    def wants_second_look(self, level: str) -> bool:
        return self.second_look if self.second_look is not None else level == "thorough"


DEFAULT = ChatPolicy()


def is_table(value: Any) -> bool:
    return (
        isinstance(value, dict) and set(value) == {TABLE_KEY} and isinstance(value[TABLE_KEY], dict)
    )


def at_level(value: Any, level: str) -> Any:
    """The value at ``level``: a table gives its entry (the balanced one when the level is
    missing), anything else stays as it is."""
    if is_table(value):
        levels = value[TABLE_KEY]
        return levels[level] if level in levels else levels.get(DEFAULT_LEVEL)
    return value


def _one(key: str, value: Any, where: str) -> Any:
    if key in NUMBERS:
        low, high = NUMBERS[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not low <= value <= high
        ):
            raise PolicyError(f"{where}: a number from {low} to {high}")
        return value
    if key in BOOLEANS:
        if not isinstance(value, bool):
            raise PolicyError(f"{where}: true or false")
        return value
    if not isinstance(value, str) or len(value.strip()) > 63 or "@" in value:
        raise PolicyError(f"{where}: the name of a node (or empty)")
    return value.strip()


def check(doc: Any) -> dict[str, Any]:
    """The document as a clean dict, or a :class:`PolicyError` that names the key."""
    if doc is None:
        return {}
    if not isinstance(doc, dict) or set(doc) - KEYS:
        raise PolicyError(f"the policy has {', '.join(sorted(KEYS))}")
    out: dict[str, Any] = {}
    for key, value in doc.items():
        if is_table(value):
            levels = value[TABLE_KEY]
            if not levels or set(levels) - set(LEVELS):
                raise PolicyError(f"{key}: a table has the levels {', '.join(LEVELS)}")
            out[key] = {TABLE_KEY: {lv: _one(key, v, f"{key}.{lv}") for lv, v in levels.items()}}
        else:
            out[key] = _one(key, value, key)
    return out


def from_doc(doc: dict[str, Any], effort: str) -> ChatPolicy:
    """The policy the document states at ``effort``."""
    level = effort if effort in LEVELS else DEFAULT_LEVEL

    def param(key: str, default: Any = None) -> Any:
        if key not in doc:
            return default
        value = at_level(doc[key], level)
        return default if value is None else value

    node = param("node", "")
    return ChatPolicy(
        max_turns=param("max_turns"),
        max_tool_calls=param("max_tool_calls"),
        time_budget=param("time_budget"),
        second_look=param("second_look"),
        recall=bool(param("recall", True)),
        write_memory=bool(param("write_memory", True)),
        node=str(node or "").strip(),
        review=param("review"),
        reactions=param("reactions"),
    )


async def stored(db: AsyncSession) -> dict[str, Any]:
    """The stored document (checked); an unusable one counts as none."""
    try:
        return check(await get_hub_setting(db, SETTING_KEY))
    except PolicyError:
        log.warning("chat policy: the stored document is not valid, the hub's own rules apply")
        return {}


async def save(db: AsyncSession, doc: dict[str, Any]) -> None:
    await set_hub_setting(db, SETTING_KEY, doc or None)


async def load(sessionmaker: async_sessionmaker[AsyncSession], effort: str) -> ChatPolicy:
    """The policy at ``effort``; the hub's own rules when nothing is set or it cannot be read."""
    try:
        async with sessionmaker() as db:
            doc = await stored(db)
        return from_doc(doc, quality.level_of(effort))
    except Exception:
        log.exception("chat policy: it could not be read, the hub's own rules apply")
        return DEFAULT
