"""Authentication.

Dev only: a static bearer token (``TOPCOAT_DEV_ADMIN_TOKEN``) maps to the seeded dev admin.
Everything that needs "who is calling" depends on :func:`api_user`, so OIDC can replace
this module later without touching the endpoints.
"""

import hashlib
import hmac
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.db import get_db
from topcoat_hub.models import DEV_ADMIN_ID, ApiKey, User
from topcoat_hub.ratelimit import AddressLimiter

API_KEY_PREFIX = "tc_"

# The admin token is the whole admin login until OIDC: the hub does not start with a short one or
# with a value from an example file (serve.py).
MIN_DEV_TOKEN_LENGTH = 32
EXAMPLE_TOKENS = frozenset({"change-me", "change-me-too", "changeme", "secret", "admin"})

# Failed sign-ins per client address before the hub answers 429 for a while.
FAILED_SIGN_INS_PER_MINUTE = 30


def dev_token_problem(token: str) -> str | None:
    """Why the configured admin token may not be used, or None (empty is allowed: GUI locked)."""
    if not token:
        return None
    if token.strip().lower() in EXAMPLE_TOKENS:
        return "TOPCOAT_DEV_ADMIN_TOKEN still holds the value from the example file"
    if len(token) < MIN_DEV_TOKEN_LENGTH:
        return f"TOPCOAT_DEV_ADMIN_TOKEN is shorter than {MIN_DEV_TOKEN_LENGTH} characters"
    return None


def sign_in_limiter() -> AddressLimiter:
    return AddressLimiter(FAILED_SIGN_INS_PER_MINUTE, 60.0)


def client_address(client: object) -> str:
    """The caller's address for the per-address limits ("?" when the server does not know it)."""
    host = getattr(client, "host", None)
    return host if isinstance(host, str) and host else "?"


@dataclass(frozen=True)
class CurrentUser:
    id: str
    name: str
    role: str
    # True when the caller used an API key (a program acting for the user), not the dev token.
    via_key: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def uuid(self) -> uuid.UUID:
        return uuid.UUID(self.id)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail={"code": "unauthorized", "message": "missing or invalid token"},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def api_user(request: Request, db: AsyncSession = Depends(get_db)) -> CurrentUser:
    """Who is calling: the dev admin token (the seeded dev admin) or an API key (``tc_…``) of
    any user; revoked keys are rejected. Everything that needs "who is calling" depends on
    this, so OIDC can replace it later without touching the endpoints (stage 5). Until then
    other users exist for isolation (D20) and authenticate with API keys."""
    token = bearer_token(request.headers)
    if token is None:
        raise _unauthorized()
    limiter: AddressLimiter | None = getattr(request.app.state, "sign_ins", None)
    address = client_address(request.client)
    if limiter is not None and limiter.blocked(address):
        raise _too_many(limiter.retry_after(address))
    user = await user_of_token(db, token, request.app.state.settings.dev_admin_token)
    if user is None:
        if limiter is not None:
            limiter.record(address)
        raise _unauthorized()
    return user


def _too_many(retry_after: int) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "code": "too_many_attempts",
            "message": f"too many failed sign-ins from this address, try again in {retry_after} s",
        },
        headers={"Retry-After": str(retry_after)},
    )


async def user_of_token(db: AsyncSession, token: str, expected: str | None) -> CurrentUser | None:
    """The user a bearer token stands for, or ``None``. Shared by :func:`api_user` and the
    WebSockets a browser opens (it cannot set a header there: the token is their first frame)."""
    if token.startswith(API_KEY_PREFIX):
        row = (
            await db.execute(
                select(ApiKey, User)
                .join(User, User.id == ApiKey.user_id)
                .where(ApiKey.key_hash == hash_secret(token), ApiKey.revoked_at.is_(None))
            )
        ).first()
        if row is None:
            return None
        key, user = row
        key.last_used_at = func.now()
        await db.commit()
        return CurrentUser(id=str(user.id), name=user.name, role=user.role, via_key=True)
    if not expected or not hmac.compare_digest(token, expected):
        return None
    user = (await db.execute(select(User).where(User.id == DEV_ADMIN_ID))).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "dev admin missing, run migrations"
        )
    return CurrentUser(id=str(user.id), name=user.name, role=user.role)


# The OpenAI-compatible gateway accepts the same callers.
gateway_user = api_user


async def require_admin(user: CurrentUser = Depends(api_user)) -> CurrentUser:
    """Admin API (nodes, pairing, hub settings): the dev token only. An API key never opens
    it, not even the key of an admin: keys sit in editor configs and scripts."""
    if user.via_key or not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail={"code": "forbidden"})
    return user


async def person_user(user: CurrentUser = Depends(api_user)) -> CurrentUser:
    """Endpoints that stand for the person's own click (D15, D83): not an API key, which an agent
    or a script could hold."""
    if user.via_key:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": "person_only", "message": "only the signed-in person can do this"},
        )
    return user


def hash_secret(value: str) -> str:
    """SHA-256 is fine here: pairing codes are high-entropy random values."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def bearer_token(headers: Mapping[str, str]) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""
    header = headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()
