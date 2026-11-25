"""Text embeddings for the assistant's memory (E2-02) and, later, the knowledge base (E2-03).

Two implementations behind one interface:

* :class:`GatewayEmbedder` calls ``/v1/embeddings`` through the model gateway with the model
  named in ``TOPCOAT_EMBEDDING_MODEL`` (``bge-m3`` on ``node-nvidia`` once E2-03 has set it up).
* :class:`HashingEmbedder` is the **transitional** embedder that needs no model at all: words
  and character trigrams hashed into a fixed-size vector (the "hashing trick"). It finds notes
  that share words or word stems, not notes that only share a meaning, so recall is close to
  keyword search. Every stored vector records which embedder made it; when the gateway
  embedder is configured later, the consolidation job re-embeds the old notes.
"""

import asyncio
import hashlib
import logging
import math
import re
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Protocol

from topcoat_hub.gateway import Gateway, GatewayError

log = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """The embedder could not produce vectors (model unavailable, bad answer)."""


class Embedder(Protocol):
    id: str  # stored with every vector; vectors of different ids are never compared
    dup_threshold: float  # cosine similarity from which two notes count as near-duplicates
    recall_min: float  # cosine similarity below which a note is not recalled
    cluster_threshold: float  # similarity from which consolidation looks at two notes together

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _fold(text: str) -> str:
    """Lower case without accents, so "Käse" and "kase" hash alike."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


class HashingEmbedder:
    """Transitional embedder without a model (see the module docstring)."""

    dup_threshold = 0.92
    recall_min = 0.18
    cluster_threshold = 0.35

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim
        self.id = f"hashing-v1-{dim}"

    def _add(self, vec: list[float], feature: str, weight: float) -> None:
        digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % self.dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[index] += sign * weight

    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in _WORD.findall(_fold(text)):
            self._add(vec, "w:" + word, 1.0)
            padded = f"^{word}$"
            if len(padded) > 3:
                for i in range(len(padded) - 2):
                    self._add(vec, "t:" + padded[i : i + 3], 0.4)
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm else vec

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


class GatewayEmbedder:
    """Embeddings from a model served through the gateway (``/v1/embeddings``)."""

    dup_threshold = 0.93
    recall_min = 0.35
    cluster_threshold = 0.6

    def __init__(self, gateway: Gateway, model: str, batch: int = 32) -> None:
        self.gateway = gateway
        self.model = model
        self.batch = batch
        self.id = f"gateway:{model}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch):
            chunk = texts[start : start + self.batch]
            try:
                data = await self.gateway.call(
                    self.model, "/v1/embeddings", {"input": chunk}, label="embedding"
                )
            except GatewayError as exc:
                raise EmbeddingError(f"embedding model unavailable: {exc.message}") from exc
            rows = data.get("data")
            if not isinstance(rows, list) or len(rows) != len(chunk):
                raise EmbeddingError("the embedding model answered with an unexpected shape")
            try:
                ordered = sorted(rows, key=lambda r: r.get("index", 0))
                vectors = [[float(x) for x in r["embedding"]] for r in ordered]
            except (KeyError, TypeError, ValueError) as exc:
                raise EmbeddingError("the embedding model answered with bad vectors") from exc
            out.extend(_normalise(v) for v in vectors)
        return out


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


class AutoEmbedder:
    """The transitional hashing embedder until the preferred embedding model (``bge-m3``) is served
    through the gateway, then that model (E3-04). The switch happens only in :meth:`refresh`
    (called by a periodic task, never in the middle of an ``embed``), so ``id`` and the vectors
    that go with it always belong together; vectors made by the earlier embedder are refreshed by
    the existing maintenance (knowledge re-index, memory consolidation) because they carry its id.
    It never switches back: a node that is offline for a while answers with an
    :class:`EmbeddingError`, which every caller already handles (keywords only)."""

    def __init__(self, gateway: Gateway, preferred: str = "bge-m3") -> None:
        self.gateway = gateway
        self.preferred = preferred.strip().lower()
        self._current: Embedder = HashingEmbedder()
        self.switched_to: str | None = None
        # called after a switch (the knowledge base re-embeds at once instead of at the next run)
        self.on_switch: Callable[[str], Awaitable[object]] | None = None

    @property
    def id(self) -> str:
        return self._current.id

    @property
    def dup_threshold(self) -> float:
        return self._current.dup_threshold

    @property
    def recall_min(self) -> float:
        return self._current.recall_min

    @property
    def cluster_threshold(self) -> float:
        return self._current.cluster_threshold

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self._current.embed(texts)

    def _wanted(self) -> str:
        """The embedding model the admin chose on the Responsibilities page, else ''."""
        return self.gateway.policy.embedding_model.strip()

    async def _follow_choice(self) -> None:
        """The admin's choice wins over the first served ``bge-m3``: switch to it as soon as a
        node serves it (and again when it changes). The vectors made by the earlier model carry
        its id and are refreshed by the maintenance, like after the first switch."""
        wanted = self._wanted()
        if not wanted or self.switched_to == wanted:
            return
        try:
            served = {str(m.get("id", "")) for m in await self.gateway.list_models()}
        except Exception:
            return
        if wanted not in served:
            return
        self._current = GatewayEmbedder(self.gateway, wanted)
        self.switched_to = wanted
        log.info("embeddings: using %s, as chosen on the Responsibilities page", wanted)
        if self.on_switch is not None:
            try:
                await self.on_switch(wanted)
            except Exception:
                log.exception("embeddings: the follow-up of the switch failed")

    async def refresh(self) -> str | None:
        """Switch to the preferred model if a node serves it now; returns the model name when this
        call switched, else ``None``."""
        if self._wanted():
            before = self.switched_to
            await self._follow_choice()
            return self.switched_to if self.switched_to != before else None
        if self.switched_to is not None or not self.preferred:
            return None
        try:
            models = await self.gateway.list_models()
        except Exception:
            return None
        for m in models:
            name = str(m.get("id", ""))
            if self.preferred in name.lower():
                self._current = GatewayEmbedder(self.gateway, name)
                self.switched_to = name
                log.info(
                    "embeddings: %s is served now, switching from the transitional embedder; "
                    "vectors are refreshed by the next re-index / consolidation",
                    name,
                )
                if self.on_switch is not None:
                    try:
                        await self.on_switch(name)
                    except Exception:
                        log.exception("embeddings: the follow-up of the switch failed")
                return name
        return None

    async def watch(self, interval: float = 120.0) -> None:
        """Look for the preferred model now and then (an asyncio task of the hub)."""
        while True:
            await self.refresh()
            if self.switched_to is not None and not self._wanted():
                return  # the first served bge-m3 is in use and nothing is chosen
            await asyncio.sleep(interval)


def make_embedder(gateway: Gateway, embedding_model: str, auto: bool = True) -> Embedder:
    """The gateway embedder when a model is configured; else the one that becomes bge-m3 as soon
    as it is served (``auto``), else the transitional one."""
    if embedding_model.strip():
        return GatewayEmbedder(gateway, embedding_model.strip())
    if auto:
        return AutoEmbedder(gateway)
    return HashingEmbedder()
