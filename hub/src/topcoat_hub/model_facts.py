"""What kind of work a model can do (PRG-00 R1): chat, embedding, speech, vision, rerank.

The kinds come from the model's name (``detect``) unless the admin set them by hand
(``NodePolicy.model_kinds``, source ``manual``), or by a passed probe (``model_probes``, source
``proven``), or by the header of the model file (the node reports it, source ``gguf``). Programs
filter their pool by kind."""

import re
from collections.abc import Mapping, Sequence
from typing import Any

KINDS = ("chat", "embedding", "rerank", "tts", "stt", "vision")

_MARKS: dict[str, tuple[str, ...]] = {
    "embedding": ("bge-", "bge_", "embed", "e5-", "minilm", "nomic", "gte-", "mxbai"),
    "rerank": ("rerank",),
    "tts": ("tts", "kokoro", "piper", "orpheus", "outetts"),
    "stt": ("whisper", "parakeet"),
    "vision": ("-vl", "_vl", "llava", "minicpm-v", "pixtral", "mmproj", "-vision"),
}
_NOT_CHAT = ("embedding", "rerank", "tts", "stt")


def detect(name: str) -> tuple[str, ...]:
    """The kinds the name suggests; a name that says nothing is a chat model."""
    low = name.lower()
    found = [kind for kind, marks in _MARKS.items() if any(m in low for m in marks)]
    if not any(k in _NOT_CHAT for k in found):
        found.insert(0, "chat")
    return tuple(found)


def from_gguf(facts: Mapping[str, Any] | None) -> tuple[str, ...] | None:
    """The kinds the header of a GGUF file states (R8), or None when it says nothing decisive: a
    pooling type is an embedding model (4: a reranker), the architecture ``clip`` a vision
    projector, a chat template a chat model. A file without any of these stays with the name."""
    if not facts:
        return None
    pooling = facts.get("pooling_type") or 0
    if pooling == 4:
        return ("rerank",)
    if pooling in (1, 2, 3):
        return ("embedding",)
    if facts.get("architecture") == "clip":
        return ("vision",)
    if facts.get("chat_template"):
        return ("chat",)
    return None


def kinds_of(
    name: str,
    manual: Mapping[str, Sequence[str]] | None = None,
    proven: Mapping[str, Sequence[str]] | None = None,
    gguf: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """The admin's setting wins over a passed probe, a probe over the header of the file, the
    header over the name."""
    if manual and name in manual:
        return tuple(manual[name])
    if proven and name in proven:
        return tuple(proven[name])
    if (stated := from_gguf(gguf)) is not None:
        # the header knows chat, embedding and rerank; that a chat model also looks at pictures is
        # not in the header of the language model (its projector is another file): the name says it
        if stated == ("chat",):
            return (*stated, *(k for k in detect(name) if k == "vision"))
        return stated
    return detect(name)


def source_of(
    name: str,
    manual: Mapping[str, Sequence[str]] | None = None,
    proven: Mapping[str, Sequence[str]] | None = None,
    gguf: Mapping[str, Any] | None = None,
) -> str:
    if manual and name in manual:
        return "manual"
    if proven and name in proven:
        return "proven"
    if from_gguf(gguf) is not None:
        return "gguf"
    return "detected"


# --- what probes proved (model_probes.py stores them; gateway.proofs keeps them) ----------------

PROBES = ("tools", "embedding")
PROOF_KEY = "model_proofs"  # hub_settings: {model: {probe: {"ok": bool, "node": str, "at": iso}}}


def proven_kinds(proofs: Mapping[str, Mapping[str, Any]]) -> dict[str, tuple[str, ...]]:
    """The model kinds a passed probe proves: an embedding answer makes an embedding model. A tool
    call is a capability, not a kind (see ``capabilities``)."""
    return {
        model: ("embedding",)
        for model, results in proofs.items()
        if (results.get("embedding") or {}).get("ok")
    }


def capabilities(proofs: Mapping[str, Mapping[str, Any]], model: str) -> dict[str, bool | None]:
    """What the probes say about one model: True (proven), False (probed, not proven), None (never
    probed)."""
    results = proofs.get(model, {})
    return {p: (bool(results[p]["ok"]) if p in results else None) for p in PROBES}


# --- tags: what a model is, read from its file name and header (2026-11-14) ---------------------

_SIZE = re.compile(r"(?<![a-z0-9])(\d+(?:\.\d+)?)b(?:-a(\d+(?:\.\d+)?)b)?(?![a-z0-9])", re.I)
_QUANT = re.compile(
    r"(?<![a-z0-9])((?:ud-)?(?:iq\d_[a-z]+|q\d_k_[a-z]+(?:_[a-z]+)?|q\d_k|q\d_\d|q\d_k_xl|"
    r"mxfp4|fp16|bf16|f16|q8_0))(?![a-z0-9])",
    re.I,
)
_MOE_ARCH = ("moe", "gpt-oss", "mixtral", "deepseek2")


def tags(name: str, gguf: Mapping[str, Any] | None = None, kinds: Sequence[str] = ()) -> list[str]:
    """Short labels to filter the model list by: the kinds, the family (the architecture in the
    header), the size in parameters, ``moe`` for a mixture of experts (with its active size), the
    quantisation and the context the file allows. All of it comes from what the node reports;
    nothing is set by hand here."""
    facts = gguf or {}
    out: list[str] = list(kinds)
    arch = str(facts.get("architecture") or "")
    if arch:
        out.append(arch)
    size = _SIZE.search(name.replace("_", "-"))
    if size:
        out.append(f"{size.group(1)}B")
        if size.group(2):
            out.append(f"A{size.group(2)}B")
    if (size and size.group(2)) or any(m in arch.lower() for m in _MOE_ARCH):
        out.append("moe")
    quant = _QUANT.search(name)
    if quant:
        out.append(quant.group(1).upper().replace("UD-", "UD "))
    context = facts.get("context_length")
    if isinstance(context, int) and context > 0:
        out.append(f"{context // 1024}k ctx" if context >= 1024 else f"{context} ctx")
    if size and not size.group(2):
        try:
            if float(size.group(1)) <= 4:
                out.append("small")
        except ValueError:
            pass
    return list(dict.fromkeys(out))
