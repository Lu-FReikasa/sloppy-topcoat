"""The record of model requests (SCH-03 step 3): one row per request the gateway served or refused.

Never content: who asked (``user_id`` and the conversation or task, when the caller said so), what
for (the work label and the chain of functions), where it ran, how long it waited and ran, how it
ended. A failure to write is logged and swallowed: an answer must never fail because its record
could not be stored.
"""

import contextvars
import logging
import uuid
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.models import JobRecord

log = logging.getLogger(__name__)


# Work that must leave no trace: an incognito conversation is "never written" (PLATFORM.md 3.9),
# and a row that says who asked and when is a trace as well.
NO_RECORD_WORK = frozenset({"incognito"})


@dataclass(frozen=True)
class JobContext:
    """What the caller of a gateway request knows about its person and place. Set with
    :func:`set_job_context` before the request; all fields are optional."""

    user_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    message_id: int | None = None
    task_id: uuid.UUID | None = None
    application: str | None = None
    # the kind of work a model is needed for when the caller does not name one: "agent" (the
    # default), or "check" for a second look at another application's work (node_policy ROLES)
    role: str | None = None


_CONTEXT: contextvars.ContextVar[JobContext | None] = contextvars.ContextVar(
    "topcoat_job_context", default=None
)


def set_job_context(**kwargs: Any) -> contextvars.Token[JobContext | None]:
    """Name person and place of the gateway requests made from here on (in this task). A field
    left out keeps the value an outer caller set."""
    base = _CONTEXT.get() or JobContext()
    known = {f.name for f in fields(JobContext)}
    merged = {
        **{k: getattr(base, k) for k in known},
        **{k: v for k, v in kwargs.items() if k in known},
    }
    return _CONTEXT.set(JobContext(**merged))


def job_context() -> JobContext:
    return _CONTEXT.get() or JobContext()


def utcnow() -> datetime:
    return datetime.now(UTC)


class JobLog:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def write(self, **values: Any) -> None:
        """Store one row. Unknown keys are dropped; every failure is swallowed."""
        try:
            known = {c.name for c in JobRecord.__table__.columns}
            row = JobRecord(**{k: v for k, v in values.items() if k in known})
            async with self.sessionmaker() as db:
                db.add(row)
                await db.commit()
        except Exception:  # an answer does not fail because its record could not be written
            log.warning("job record not written", exc_info=True)
