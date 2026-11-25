"""Node authentication by signature (ARCHITECTURE D12, docs/PROTOCOL.md "Session").

The node asks for a single-use nonce (``POST /api/node/challenge``), signs
``topcoat-node-auth-v1\\n<node_id>\\n<nonce>`` with its ed25519 key and sends
``Authorization: Topcoat-Ed25519 <node_id>.<nonce>.<signature>`` on the WebSocket upgrade.
The hub stores only the public key from pairing.
"""

import base64
import binascii
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

AUTH_SCHEME = "topcoat-ed25519"
SIGNED_PREFIX = b"topcoat-node-auth-v1\n"
NONCE_TTL_SECONDS = 60
MAX_PENDING = 10_000


def signed_message(node_id: str, nonce: str) -> bytes:
    return SIGNED_PREFIX + node_id.encode() + b"\n" + nonce.encode()


@dataclass(frozen=True)
class NodeAuth:
    node_id: uuid.UUID
    nonce: str
    signature: bytes


def parse_authorization(headers: Mapping[str, str]) -> NodeAuth | None:
    """Parse ``Authorization: Topcoat-Ed25519 <node_id>.<nonce>.<signature>``."""
    scheme, _, value = headers.get("authorization", "").partition(" ")
    if scheme.lower() != AUTH_SCHEME:
        return None
    parts = value.strip().split(".")
    if len(parts) != 3:
        return None
    raw_id, nonce, raw_sig = parts
    try:
        node_id = uuid.UUID(raw_id)
        signature = base64.urlsafe_b64decode(raw_sig + "=" * (-len(raw_sig) % 4))
    except (ValueError, binascii.Error):
        return None
    if not nonce or len(signature) != 64:
        return None
    return NodeAuth(node_id=node_id, nonce=nonce, signature=signature)


def verify_signature(public_key_b64: str, message: bytes, signature: bytes) -> bool:
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        key.verify(signature, message)
    except (ValueError, binascii.Error, InvalidSignature):
        return False
    return True


class ChallengeStore:
    """Pending nonces, in memory (the hub is a single process, D6).

    Nonces are handed out for any node id, known or not, so the endpoint does not reveal which
    nodes exist. Each nonce can be consumed once, successful or not.
    """

    def __init__(
        self,
        ttl_seconds: float = NONCE_TTL_SECONDS,
        max_pending: int = MAX_PENDING,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._max = max_pending
        self._clock = clock
        # nonce -> (node_id, expires_at); insertion order == expiry order.
        self._pending: OrderedDict[str, tuple[uuid.UUID, float]] = OrderedDict()

    def _expire(self) -> None:
        now = self._clock()
        while self._pending:
            nonce, (_, expires_at) = next(iter(self._pending.items()))
            if expires_at > now:
                break
            del self._pending[nonce]

    def issue(self, node_id: uuid.UUID) -> str:
        self._expire()
        while len(self._pending) >= self._max:
            self._pending.popitem(last=False)  # drop the oldest instead of growing without bound
        nonce = secrets.token_urlsafe(32)
        self._pending[nonce] = (node_id, self._clock() + self.ttl_seconds)
        return nonce

    def consume(self, node_id: uuid.UUID, nonce: str) -> bool:
        self._expire()
        entry = self._pending.pop(nonce, None)
        return entry is not None and entry[0] == node_id

    def __len__(self) -> int:
        return len(self._pending)
