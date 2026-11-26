"""Mandates: the person's written release of one branch (PPH-05, 05d), the store side.

A mandate is made only by the person's click (``api/mandates.py``); nothing in this module is
reachable from an agent tool. It names one repository, one branch ``topcoat/*``, the actions it
covers, whether the hub may push that branch, and an expiry. An order that matches it exactly
(project, repository, branch, action, not expired, not revoked) is sent without a further click;
anything else waits for one. Every creation, use and revocation is a line of the Akte.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from topcoat_hub import clock
from topcoat_hub.models import ProjectMandate
from topcoat_hub.project_base import Invalid, NotFound, StoreBase, get_project, owned

# what an order's mandate covers (the release of an order gives exactly these)
ORDER_ACTIONS = ("create_prototype", "fix")
# probe: the tries of an issue's investigation, a mandate of its own (issue_probes.py)
# workshop: the drafts of a large task's sub-tasks on its branch topcoat/v-<nr> (D110, vorhaben.py);
# a mandate of its own, never with another action and never with the push flag
WORKSHOP = "workshop"
ACTIONS = (*ORDER_ACTIONS, "probe", WORKSHOP)
DEFAULT_DAYS = 14
MAX_DAYS = 90
BRANCH_PREFIX = "topcoat/"


def mandate_view(m: ProjectMandate, now: datetime | None = None) -> dict[str, Any]:
    now = now or clock.hub_now()
    state = "revoked" if m.revoked_at else "expired" if m.expires_at <= now else "active"
    return {
        "id": str(m.id),
        "project_id": str(m.project_id),
        "chain_id": str(m.chain_id) if m.chain_id else None,
        "target": m.target,
        "actions": list(m.actions),
        "repo": m.repo,
        "branch": m.branch,
        "push": bool(m.push),
        "text": m.text,
        "expires_at": m.expires_at.isoformat(),
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "revoked_at": m.revoked_at.isoformat() if m.revoked_at else None,
        "uses": m.uses,
        "last_used_at": m.last_used_at.isoformat() if m.last_used_at else None,
        "state": state,
        # what the card says a mandate does not cover, so the page never has to guess
        "not_covered": [
            "other branches",
            "other repositories",
            "merging",
            "force pushes",
            "deploying",
        ],
    }


def active(m: ProjectMandate, now: datetime | None = None) -> bool:
    return m.revoked_at is None and m.expires_at > (now or clock.hub_now())


def matches(m: ProjectMandate, repo: str, branch: str, action: str) -> bool:
    """Exactly this repository, exactly this branch, an action the mandate covers."""
    return m.repo == repo and m.branch == branch and action in (m.actions or [])


class MandateMixin(StoreBase):
    async def create_mandate(
        self,
        user_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        repo: str,
        branch: str,
        actions: list[str],
        push: bool = True,
        text: str = "",
        days: int = DEFAULT_DAYS,
        chain_id: uuid.UUID | None = None,
    ) -> ProjectMandate:
        repo, branch = repo.strip(), branch.strip()
        if not repo or len(repo) > 200:
            raise Invalid("a mandate names one repository")
        if not branch.startswith(BRANCH_PREFIX) or len(branch) > 200 or "*" in branch:
            raise Invalid("a mandate names one exact branch below topcoat/")
        if branch.endswith("/") or ".." in branch or any(c.isspace() for c in branch):
            raise Invalid("that is not a branch name")
        if not actions or not set(actions) <= set(ACTIONS):
            raise Invalid(f"a mandate covers {' and '.join(ACTIONS)}, nothing else")
        if WORKSHOP in actions and (push or len(set(actions)) > 1):
            raise Invalid("a workshop scope covers only the workshop and never pushes (D110)")
        if not 1 <= days <= MAX_DAYS:
            raise Invalid(f"a mandate runs 1 to {MAX_DAYS} days")
        async with self._sessionmaker() as db:
            project = await get_project(db, user_id, project_id, lock=True)
            now = clock.hub_now()
            mandate = ProjectMandate(
                id=uuid.uuid4(),
                user_id=user_id,
                application="project",
                project_id=project.id,
                chain_id=chain_id,
                target="dev",
                actions=sorted(set(actions)),
                repo=repo,
                branch=branch,
                push=push,
                text=text.strip()[:2000],
                expires_at=now + timedelta(days=days),
            )
            db.add(mandate)
            await db.flush()
            await self._record(
                db,
                user_id,
                project,
                "mandate.created",
                payload={
                    "mandate": str(mandate.id),
                    "repo": repo,
                    "branch": branch,
                    "actions": mandate.actions,
                    "push": push,
                    "expires_at": mandate.expires_at.isoformat(),
                },
            )
            await db.commit()
            await db.refresh(mandate)
            return mandate

    async def get_mandate(self, user_id: uuid.UUID, mandate_id: uuid.UUID) -> ProjectMandate:
        async with self._sessionmaker() as db:
            return await _mandate(db, user_id, mandate_id)

    async def mandates_of(self, user_id: uuid.UUID, project_id: uuid.UUID) -> list[ProjectMandate]:
        async with self._sessionmaker() as db:
            await get_project(db, user_id, project_id)
            rows = await db.scalars(
                select(ProjectMandate)
                .where(
                    ProjectMandate.user_id == user_id,
                    ProjectMandate.application == "project",
                    ProjectMandate.project_id == project_id,
                )
                .order_by(ProjectMandate.created_at.desc())
            )
            return list(rows)

    async def find_mandate(
        self, user_id: uuid.UUID, project_id: uuid.UUID, repo: str, branch: str, action: str
    ) -> ProjectMandate | None:
        """The active mandate this order matches exactly, if there is one."""
        async with self._sessionmaker() as db:
            rows = await db.scalars(
                select(ProjectMandate)
                .where(
                    ProjectMandate.user_id == user_id,
                    ProjectMandate.application == "project",
                    ProjectMandate.project_id == project_id,
                    ProjectMandate.repo == repo,
                    ProjectMandate.branch == branch,
                )
                .order_by(ProjectMandate.created_at.desc())
            )
            now = clock.hub_now()
            return next(
                (m for m in rows if active(m, now) and matches(m, repo, branch, action)), None
            )

    async def revoke_mandate(self, user_id: uuid.UUID, mandate_id: uuid.UUID) -> ProjectMandate:
        async with self._sessionmaker() as db:
            mandate = await _mandate(db, user_id, mandate_id)
            if mandate.revoked_at is not None:
                return mandate
            project = await get_project(db, user_id, mandate.project_id, lock=True)
            mandate.revoked_at = clock.hub_now()
            await self._record(
                db, user_id, project, "mandate.revoked", payload={"mandate": str(mandate.id)}
            )
            await db.commit()
            return mandate

    async def note_mandate(
        self, user_id: uuid.UUID, mandate_id: uuid.UUID, kind: str, payload: dict[str, Any]
    ) -> None:
        """A line about a mandate that is not a use (a push that was stopped, and why)."""
        async with self._sessionmaker() as db:
            mandate = await _mandate(db, user_id, mandate_id)
            project = await get_project(db, user_id, mandate.project_id, lock=True)
            await self._record(
                db, user_id, project, kind, payload={"mandate": str(mandate.id), **payload}
            )
            await db.commit()

    async def use_mandate(
        self,
        user_id: uuid.UUID,
        mandate_id: uuid.UUID,
        *,
        what: str,
        payload: dict[str, Any] | None = None,
    ) -> ProjectMandate:
        """Count one use and write the line (``mandate.used``: an order sent, a branch pushed).
        Refuses a mandate that is not active any more."""
        async with self._sessionmaker() as db:
            mandate = await _mandate(db, user_id, mandate_id, lock=True)
            if not active(mandate):
                raise Invalid("the mandate is revoked or expired")
            project = await get_project(db, user_id, mandate.project_id, lock=True)
            mandate.uses += 1
            mandate.last_used_at = clock.hub_now()
            await self._record(
                db,
                user_id,
                project,
                "mandate.used",
                payload={
                    "mandate": str(mandate.id),
                    "what": what,
                    "branch": mandate.branch,
                    "uses": mandate.uses,
                    **(payload or {}),
                },
            )
            await db.commit()
            return mandate


async def _mandate(
    db: Any, user_id: uuid.UUID, mandate_id: uuid.UUID, *, lock: bool = False
) -> ProjectMandate:
    statement = select(ProjectMandate).where(
        ProjectMandate.id == mandate_id, owned(ProjectMandate, user_id)
    )
    if lock:
        statement = statement.with_for_update()
    mandate = await db.scalar(statement)
    if mandate is None:
        raise NotFound("mandate")
    return mandate
