"""Messages between the applications in a project's tree (PPH-05, 05a): the store side.

A message is a node of kind ``report`` of the receiving project with the columns of a message
(``messages.py`` checks type, edge and payload). A chain (``ProjectChain``) is the conversation
about one project: the first request or order starts it, everything that follows carries its id,
and the hub resolves it to the project. Every message and every change of its state is a line of
the Akte (PPH-04, 04a).

The states of a message move only along :data:`TRANSITIONS`, and a chain that was stopped takes no
new message. Everything is scoped by the person and the application, like the rest of the store.
"""

import contextlib
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from topcoat_hub import messages, result_forms
from topcoat_hub.models import ProjectChain, ProjectNode
from topcoat_hub.project_base import (
    Invalid,
    NotFound,
    StoreBase,
    bump,
    get_node,
    get_project,
    owned,
)

ACTIVE_STATES = (
    "draft",
    "awaiting_click",
    "sent",
    "queued",
    "running",
    "waiting",
    "delivered",
    "reviewing",
)
TERMINAL = ("accepted", "revised", "rejected", "failed", "stopped")
# state -> the states it may go to
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("awaiting_click", "sent", "stopped"),
    "awaiting_click": ("sent", "rejected", "stopped", "accepted"),
    # back to awaiting_click: the mandate that allowed it ended before it was queued
    "sent": ("queued", "running", "waiting", "delivered", "failed", "stopped", "awaiting_click"),
    "queued": ("running", "waiting", "delivered", "failed", "stopped"),
    "running": ("waiting", "delivered", "failed", "stopped"),
    # asked something back: it ends when the person or a new round answers
    "waiting": ("accepted", "rejected", "failed", "stopped"),
    "delivered": ("reviewing", "accepted", "rejected", "failed", "stopped"),
    "reviewing": ("accepted", "revised", "rejected", "failed", "stopped"),
}


def chain_view(c: ProjectChain) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "project_id": str(c.project_id),
        "title": c.title,
        "status": c.status,
        "rounds": c.rounds,
        "flagged_at": c.flagged_at.isoformat() if c.flagged_at else None,
        "flag_reason": c.flag_reason,
        "stopped_at": c.stopped_at.isoformat() if c.stopped_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


class MessageMixin(StoreBase):
    # --- chains ---------------------------------------------------------------------------------

    async def start_chain(
        self, user_id: uuid.UUID, project_id: uuid.UUID, *, title: str = ""
    ) -> ProjectChain:
        async with self._sessionmaker() as db:
            project = await get_project(db, user_id, project_id, lock=True)
            chain = ProjectChain(
                id=uuid.uuid4(),
                user_id=user_id,
                application="project",
                project_id=project.id,
                title=title.strip()[:200],
                status="active",
                rounds=1,
            )
            db.add(chain)
            await db.flush()
            await self.with_actor(replace(self.actor, chain_id=chain.id))._record(
                db, user_id, project, "chain.started", payload={"title": chain.title}
            )
            await db.commit()
            return chain

    async def get_chain(self, user_id: uuid.UUID, chain_id: uuid.UUID) -> ProjectChain:
        async with self._sessionmaker() as db:
            return await _chain(db, user_id, chain_id)

    async def chains_of(
        self, user_id: uuid.UUID, project_id: uuid.UUID | None = None
    ) -> list[ProjectChain]:
        async with self._sessionmaker() as db:
            statement = select(ProjectChain).where(owned(ProjectChain, user_id))
            if project_id is not None:
                statement = statement.where(ProjectChain.project_id == project_id)
            return list(await db.scalars(statement.order_by(ProjectChain.created_at.desc())))

    async def flag_chain(self, user_id: uuid.UUID, chain_id: uuid.UUID, reason: str) -> bool:
        """Mark a chain that has run long or through many rounds. A hint, never a limit; the first
        reason stays. True when the chain was marked just now."""
        async with self._sessionmaker() as db:
            chain = await _chain(db, user_id, chain_id)
            if chain.flagged_at is not None:
                return False
            project = await get_project(db, user_id, chain.project_id, lock=True)
            chain.flagged_at = datetime.now(UTC)
            chain.flag_reason = reason[:40]
            await self.with_actor(replace(self.actor, chain_id=chain.id))._record(
                db, user_id, project, "chain.flagged", payload={"reason": reason}
            )
            await db.commit()
            return True

    async def stop_chain(
        self, user_id: uuid.UUID, chain_id: uuid.UUID
    ) -> list[tuple[ProjectNode, uuid.UUID | None]]:
        """Stop a chain: no new message is taken, and every message that is still on its way becomes
        ``stopped``. Returns those messages with the agent task each one had, for the caller to
        cancel. Stopping a stopped chain changes nothing."""
        async with self._sessionmaker() as db:
            chain = await _chain(db, user_id, chain_id)
            if chain.status == "stopped":
                return []
            project = await get_project(db, user_id, chain.project_id, lock=True)
            recorder = self.with_actor(replace(self.actor, chain_id=chain.id))
            rows = list(
                await db.scalars(
                    select(ProjectNode).where(
                        owned(ProjectNode, user_id),
                        ProjectNode.chain_id == chain.id,
                        ProjectNode.msg_state.in_(ACTIVE_STATES),
                    )
                )
            )
            stopped: list[tuple[ProjectNode, uuid.UUID | None]] = []
            for node in rows:
                before = node.msg_state
                stopped.append((node, node.task_id))
                node.msg_state = "stopped"
                bump(node)
                await recorder._record(
                    db,
                    user_id,
                    project,
                    "message.state_changed",
                    node=node,
                    payload={"from": before, "to": "stopped", "reason": "the chain was stopped"},
                )
            chain.status = "stopped"
            chain.stopped_at = datetime.now(UTC)
            chain.updated_at = chain.stopped_at
            await recorder._record(
                db, user_id, project, "chain.stopped", payload={"messages": len(stopped)}
            )
            await db.commit()
            return stopped

    async def finish_chain(self, user_id: uuid.UUID, chain_id: uuid.UUID) -> None:
        """A chain with nothing on its way any more is done (unless it was stopped)."""
        async with self._sessionmaker() as db:
            chain = await _chain(db, user_id, chain_id)
            if chain.status != "active":
                return
            open_ = await db.scalar(
                select(ProjectNode.id)
                .where(
                    owned(ProjectNode, user_id),
                    ProjectNode.chain_id == chain.id,
                    ProjectNode.msg_state.in_(ACTIVE_STATES),
                )
                .limit(1)
            )
            if open_ is None:
                project = await get_project(db, user_id, chain.project_id, lock=True)
                chain.status = "done"
                chain.updated_at = datetime.now(UTC)
                await self.with_actor(replace(self.actor, chain_id=chain.id))._record(
                    db, user_id, project, "chain.finished", payload={"rounds": chain.rounds}
                )
                await db.commit()

    # --- messages -------------------------------------------------------------------------------

    async def add_message(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        type: str,
        sender: str,
        receiver: str,
        payload: dict[str, Any],
        state: str = "sent",
        chain_id: uuid.UUID | None = None,
        in_reply_to: uuid.UUID | None = None,
        round: int = 1,
        task_id: uuid.UUID | None = None,
        parent_id: uuid.UUID | None = None,
        as_of: datetime | None = None,
        stats: dict[str, Any] | None = None,
        verification: dict[str, Any] | None = None,
        title: str = "",
    ) -> ProjectNode:
        """File one message in the project, after the registry checked its type, its edge and its
        payload. The node is a report with the next number of its parent. The result of an
        employee is filed with its ``brief`` for the secretary (D112, result_forms.py)."""
        # a payload of the wrong shape keeps no brief: the registry below names what is wrong
        with contextlib.suppress(AttributeError, TypeError, KeyError, ValueError):
            payload = result_forms.with_brief(type, payload)
        try:
            spec = messages.validate(type, sender, receiver, payload)
        except messages.MessageError as exc:
            raise Invalid(exc.message) from exc
        if state not in messages.load().states:
            raise Invalid(f"unknown message state: {state}")
        if round < 1:
            raise Invalid("a round counts from 1")
        async with self._sessionmaker() as db:
            project = await get_project(db, user_id, project_id, lock=True)
            chain = None
            if chain_id is not None:
                chain = await _chain(db, user_id, chain_id)
                if chain.project_id != project.id:
                    raise Invalid("the chain belongs to another project")
                if chain.status == "stopped":
                    raise Invalid("the chain was stopped: it takes no new message")
            if in_reply_to is not None:
                origin = await get_node(db, user_id, in_reply_to)
                if origin.project_id != project.id:
                    raise Invalid("the message it answers is in another project")
            fields = {
                "message_type": type,
                "source_app": sender,
                "target_app": receiver,
                "chain_id": chain_id,
                "in_reply_to": in_reply_to,
                "round": round,
                "msg_state": state,
                "task_id": task_id,
                "payload": payload,
                "schema_version": spec.schema_version,
                "trust": spec.trust,
                "stats": stats,
                "verification": verification,
                "as_of": as_of,
                "origin_app": sender,
            }
            actor = replace(self.actor, chain_id=chain_id) if chain_id else None
            node = await self._insert_node(  # type: ignore[attr-defined]
                db,
                user_id,
                project_id,
                kind="report",
                title=(title or messages.title_of(type, payload))[:200],
                body=messages.body_of(type, payload),
                parent_id=parent_id,
                report_kind=messages.report_kind_of(type),
                fields=fields,
                event_kind="message.sent" if sender == "project" else "message.received",
                event_payload={
                    "type": type,
                    "from": sender,
                    "to": receiver,
                    "round": round,
                    "state": state,
                    "trust": spec.trust,
                },
                actor=actor,
            )
            if chain is not None and round > chain.rounds:
                chain.rounds = round
                chain.updated_at = datetime.now(UTC)
            await db.commit()
            return node

    async def set_message_state(
        self,
        user_id: uuid.UUID,
        node_id: uuid.UUID,
        state: str,
        *,
        reason: str = "",
        task_id: uuid.UUID | None = None,
        stats: dict[str, Any] | None = None,
        verification: dict[str, Any] | None = None,
    ) -> ProjectNode:
        """Move a message to another state along :data:`TRANSITIONS`, and say why."""
        async with self._sessionmaker() as db:
            node = await get_node(db, user_id, node_id, lock=True)
            if node.message_type is None or node.msg_state is None:
                raise Invalid("this is not a message")
            if state not in TRANSITIONS.get(node.msg_state, ()):
                raise Invalid(f"a message that is {node.msg_state} cannot become {state}")
            project = await get_project(db, user_id, node.project_id, lock=True)
            before = node.msg_state
            node.msg_state = state
            if task_id is not None:
                node.task_id = task_id
            if stats is not None:
                node.stats = {**(node.stats or {}), **stats}
            if verification is not None:
                node.verification = verification
            bump(node)
            actor = replace(self.actor, chain_id=node.chain_id) if node.chain_id else None
            recorder = self if actor is None else self.with_actor(actor)
            await recorder._record(
                db,
                user_id,
                project,
                "message.state_changed",
                node=node,
                payload={"from": before, "to": state, "reason": reason},
            )
            await db.commit()
            return node

    async def messages_of(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        chain_id: uuid.UUID | None = None,
        type: str | None = None,
        state: str | None = None,
        limit: int = 200,
    ) -> list[ProjectNode]:
        async with self._sessionmaker() as db:
            await get_project(db, user_id, project_id)
            statement = select(ProjectNode).where(
                owned(ProjectNode, user_id),
                ProjectNode.project_id == project_id,
                ProjectNode.message_type.is_not(None),
                ProjectNode.trashed_at.is_(None),
            )
            if chain_id is not None:
                statement = statement.where(ProjectNode.chain_id == chain_id)
            if type is not None:
                statement = statement.where(ProjectNode.message_type == type)
            if state is not None:
                statement = statement.where(ProjectNode.msg_state == state)
            rows = await db.scalars(statement.order_by(ProjectNode.created_at).limit(limit))
            return list(rows)

    async def message_for_task(self, user_id: uuid.UUID, task_id: uuid.UUID) -> ProjectNode | None:
        """The message an agent task works on (a request, an order, a review), if any."""
        async with self._sessionmaker() as db:
            return await db.scalar(
                select(ProjectNode).where(
                    owned(ProjectNode, user_id),
                    ProjectNode.task_id == task_id,
                    ProjectNode.message_type.is_not(None),
                )
            )


async def _chain(db: Any, user_id: uuid.UUID, chain_id: uuid.UUID) -> ProjectChain:
    chain = await db.scalar(
        select(ProjectChain).where(ProjectChain.id == chain_id, owned(ProjectChain, user_id))
    )
    if chain is None:
        raise NotFound("chain")
    return chain
