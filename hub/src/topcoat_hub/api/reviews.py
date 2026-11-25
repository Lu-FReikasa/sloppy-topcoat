"""What the second model found in recent chats. Every route is scoped to the caller (D20)."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.auth import CurrentUser, api_user
from topcoat_hub.db import get_db
from topcoat_hub.gateway import spawn
from topcoat_hub.models import ChatReview
from topcoat_hub.review_store import ReviewStore

router = APIRouter(prefix="/api")


def review_view(r: ChatReview) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "conversation_id": str(r.conversation_id),
        "message_id": r.message_id,
        "verdict": r.verdict,
        "findings": r.findings,
        "reviewer_model": r.reviewer_model,
        "reviewer_node": r.reviewer_node,
        # a check by the model that wrote the answer: a fresh context, not another opinion (D87)
        "same_model": bool(r.reviewer_model) and r.reviewer_model == r.author_model,
        "author_model": r.author_model,
        "author_node": r.author_node,
        "seen": r.seen,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/reviews")
async def unseen(
    db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """Findings the user has not looked at; ``errors`` have evidence, ``hints`` do not."""
    rows = [review_view(r) for r in await ReviewStore(db, user.uuid).unseen()]
    return {
        "errors": [r for r in rows if r["verdict"] == "error"],
        "hints": [r for r in rows if r["verdict"] == "hint"],
    }


@router.get("/conversations/{conversation_id}/reviews")
async def of_conversation(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(api_user),
) -> list[dict[str, Any]]:
    return [
        review_view(r) for r in await ReviewStore(db, user.uuid).for_conversation(conversation_id)
    ]


@router.get("/conversations/{conversation_id}/review-lights")
async def lights(
    conversation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(api_user),
) -> dict[str, Any]:
    """The traffic light of each answer of a conversation: the verdict of its second look by
    message id (``ok`` green, ``hint`` orange, ``error`` red) and the answers still waiting for
    it (yellow)."""
    verdicts = await ReviewStore(db, user.uuid).verdicts(conversation_id)
    reviewer = getattr(request.app.state, "reviewer", None)
    waiting = reviewer.waiting_in(conversation_id) if reviewer is not None else []
    return {
        "lights": {str(m): v for m, v in verdicts.items()},
        "waiting": [m for m in waiting if m not in verdicts],
    }


@router.post("/reviews/{review_id}/seen", status_code=204)
async def seen(
    review_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(api_user),
) -> None:
    if not await ReviewStore(db, user.uuid).mark_seen(review_id):
        raise HTTPException(404, detail={"code": "not_found", "message": "unknown review"})
    await db.commit()


@router.post("/messages/{message_id}/review", status_code=202)
async def review_now(
    message_id: int, request: Request, user: CurrentUser = Depends(api_user)
) -> dict[str, Any]:
    """A model reads this answer now (the person asked; nothing reads on its own any more). Runs in
    the background; the light of the answer is yellow until the result is there."""
    reviewer = getattr(request.app.state, "reviewer", None)
    if reviewer is None:
        raise HTTPException(503, detail={"code": "no_reviewer", "message": "reviews are off"})
    spawn(reviewer.review_now(user.uuid, message_id))
    return {"started": True}
