"""How many steps a check usually takes (the maintainer, 2026-11-13).

The agent does not plan its steps ahead: it decides after every tool result whether to read more or
to answer. A count of "steps planned" therefore does not exist; what exists is experience. The card
of a check carries the usual number of calls of this person's earlier checks of the same
application (the 75th percentile of the last finished ones), so the browser can say "Schritt 4 ·
meist ~7, höchstens 16" and fill its bar towards the limit instead of a guess it outruns.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = logging.getLogger(__name__)

# fewer finished checks than this say nothing yet
MIN_SAMPLES = 5
SAMPLES = 40
DAYS = 30


def typical_of(calls: Sequence[int], cap: int | None = None) -> int | None:
    """The 75th percentile of the calls (nearest rank), at least 1, at most the run's limit; None
    without enough samples."""
    if len(calls) < MIN_SAMPLES:
        return None
    ordered = sorted(max(0, int(c)) for c in calls)
    rank = max(1, math.ceil(0.75 * len(ordered)))
    value = max(1, ordered[rank - 1])
    return min(value, cap) if cap else value


async def typical_calls(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    application: str | None,
    cap: int | None = None,
) -> int | None:
    """The usual calls of this person's finished chat checks of `application` (None: the checks
    that ran without one). A failure to read says nothing: the card goes without it."""
    try:
        async with sessionmaker() as db:
            rows = await db.execute(
                text(
                    "SELECT tool_calls FROM agent_tasks WHERE user_id = :u AND origin = 'chat' "
                    "AND status = 'done' AND application IS NOT DISTINCT FROM :a "
                    "AND created_at > now() - make_interval(days => :d) "
                    "ORDER BY created_at DESC LIMIT :n"
                ),
                {"u": user_id, "a": application, "d": DAYS, "n": SAMPLES},
            )
            calls = [int(r[0] or 0) for r in rows]
    except Exception:  # a hint for the progress bar, never a reason to fail
        log.debug("typical calls not read", exc_info=True)
        return None
    return typical_of(calls, cap)
