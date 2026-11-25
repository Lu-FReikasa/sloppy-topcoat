"""The care run's look at the core (E3-06, ARCHITECTURE D38): the entries that stand first in every
prompt are the ones that must not be wrong. For each user the run

* asks the model whether two entries of the core contradict each other — the answer is a
  *proposal* (remove / reword), never a change;
* proposes to **unpin** a fact the assistant was given often but never used (it only costs
  budget), and to **pin** a fact that is used again and again and still reliable (it belongs in
  the core);
* does nothing new when the core is the same as at the last review (no model call).

Everything lands in the proposal queue of the user (ten open at most, a rejected one is not
proposed again for 90 days); the care run never writes an instruction itself."""

import hashlib
import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from topcoat_hub import memory_care
from topcoat_hub.gateway import Gateway, GatewayError, background_work
from topcoat_hub.memory import parse_json_object
from topcoat_hub.memory_store import MemoryStore, ProtectedWrite, SecretBlocked
from topcoat_hub.models import MemoryItem

log = logging.getLogger(__name__)

UNPIN_MIN_RECALLS = 8  # given to the model this often ...
UNPIN_MAX_USED = 0  # ... and never used
PIN_MIN_USES = 5  # a fact used this often, still reliable ...
PIN_MIN_USEFULNESS = 0.7
MAX_PINS_PER_RUN = 2
MAX_ENTRIES_REVIEWED = 40
ENTRY_CHARS = 280

REVIEW_SYSTEM = (
    "You review the standing instructions and basic facts that an assistant is given before every "
    "answer. Find entries that CONTRADICT each other (they cannot both be followed or both be "
    "true). Reply with ONE JSON object and nothing else: "
    '{"conflicts": [{"a": <number>, "b": <number>, "drop": "a" or "b" (the one that is older or '
    'less likely right), "reword": "<optional: one corrected sentence replacing the dropped '
    'entry>", "reason": "<one short sentence>"}]}. '
    'Report only real contradictions, not entries that merely differ in topic. {"conflicts": []} '
    "when there are none."
)


@dataclass
class Finding:
    action: str  # remove | change | pin | unpin
    item: MemoryItem
    reason: str
    text: str = ""
    other: MemoryItem | None = None


def core_entries(rules: Sequence[MemoryItem], pinned: Sequence[MemoryItem]) -> list[MemoryItem]:
    """The user's core: style and rule notes, plus the facts pinned into it (stable order)."""
    seen: set[uuid.UUID] = set()
    out: list[MemoryItem] = []
    for item in [*rules, *pinned]:
        if item.id not in seen and item.status == "active":
            seen.add(item.id)
            out.append(item)
    out.sort(key=lambda i: (i.created_at or datetime.min.replace(tzinfo=UTC), str(i.id)))
    return out[:MAX_ENTRIES_REVIEWED]


def fingerprint(entries: Sequence[MemoryItem]) -> str:
    """What the last review looked at: the same core, the same fingerprint, no new model call."""
    raw = ";".join(f"{i.id}:{i.version}" for i in entries)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def unpin_candidates(entries: Sequence[MemoryItem]) -> list[Finding]:
    """Pinned *facts* that were put into prompts often and never used (rules and styles steer, they
    are not "used" like a fact: they are never proposed for unpinning here)."""
    out = []
    for item in entries:
        if item.kind in ("style", "rule") or not item.core:
            continue
        if (item.recalled_count or 0) >= UNPIN_MIN_RECALLS and (
            item.used_count or 0
        ) <= UNPIN_MAX_USED:
            out.append(
                Finding(
                    "unpin",
                    item,
                    f"recalled {item.recalled_count} times, never used — it only takes room in "
                    "the core",
                )
            )
    return out


def pin_candidates(
    facts: Sequence[MemoryItem], now: datetime, cfg: memory_care.CareConfig
) -> list[Finding]:
    """Facts that are used again and again and still reliable: they belong in the core."""
    out = []
    for item in facts:
        if item.core or item.kind not in ("person", "preference") or item.status != "active":
            continue
        if item.expires_at is not None or (item.used_count or 0) < PIN_MIN_USES:
            continue
        if memory_care.usefulness(item, now) < PIN_MIN_USEFULNESS:
            continue
        if memory_care.reliability(item, now, cfg) < cfg.stage_sure:
            continue
        out.append(
            Finding("pin", item, f"used {item.used_count} times and still reliable — a core fact")
        )
    out.sort(key=lambda f: -(f.item.used_count or 0))
    return out[:MAX_PINS_PER_RUN]


def parse_conflicts(obj: dict[str, Any] | None, entries: Sequence[MemoryItem]) -> list[Finding]:
    """The model's answer as findings; indices out of range, a missing ``drop`` or two entries that
    are the same one are dropped (a verdict that cannot be applied counts for nothing)."""
    if not isinstance(obj, dict) or not isinstance(obj.get("conflicts"), list):
        return []
    out: list[Finding] = []
    for c in obj["conflicts"][:5]:
        if not isinstance(c, dict):
            continue
        try:
            a, b = int(c["a"]) - 1, int(c["b"]) - 1
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= a < len(entries) and 0 <= b < len(entries)) or a == b:
            continue
        drop = str(c.get("drop") or "").strip().lower()
        if drop not in ("a", "b"):
            continue
        gone, kept = (entries[a], entries[b]) if drop == "a" else (entries[b], entries[a])
        reason = " ".join(str(c.get("reason") or "").split())[:300] or "the two entries contradict"
        reword = " ".join(str(c.get("reword") or "").split())[:500]
        out.append(
            Finding(
                "change" if reword else "remove",
                gone,
                f"contradicts another core entry ({kept.text[:80]!r}): {reason}",
                text=reword,
                other=kept,
            )
        )
    return out


def render(entries: Sequence[MemoryItem]) -> str:
    return "\n".join(f"{n}. [{i.kind}] {i.text[:ENTRY_CHARS]}" for n, i in enumerate(entries, 1))


class CoreReview:
    def __init__(self, gateway: Gateway, cfg: memory_care.CareConfig) -> None:
        self.gateway = gateway
        self.cfg = cfg

    async def contradictions(self, entries: Sequence[MemoryItem], model: str) -> list[Finding]:
        if len(entries) < 2:
            return []
        payload = {
            "model": model,
            "stream": False,
            "temperature": 0,
            "max_tokens": 500,
            "messages": [
                {"role": "system", "content": REVIEW_SYSTEM},
                {"role": "user", "content": render(entries)},
            ],
        }
        try:
            data = await self.gateway.call(
                model, "/v1/chat/completions", payload, label="core_review"
            )
            text = data["choices"][0]["message"]["content"]
        except GatewayError:
            raise
        except (KeyError, IndexError, TypeError):
            return []
        return parse_conflicts(
            parse_json_object(text if isinstance(text, str) else json.dumps(text)), entries
        )

    async def due(self, store: MemoryStore, last_fingerprint: str | None) -> bool:
        """Is there something to look at: the core is not the one the last review saw, or a pinned
        fact costs room for nothing."""
        entries = core_entries(await store.instructions(), await store.pinned_facts())
        return fingerprint(entries) != last_fingerprint or bool(unpin_candidates(entries))

    @background_work
    async def review(
        self,
        store: MemoryStore,
        model: str | None,
        last_fingerprint: str | None,
        now: datetime,
    ) -> tuple[list[dict[str, Any]], str]:
        """Queue the proposals; returns ``(report lines, fingerprint)``. ``model=None`` skips the
        contradiction check (no model reachable) but still does the counters."""
        entries = core_entries(await store.instructions(), await store.pinned_facts())
        fp = fingerprint(entries)
        findings = unpin_candidates(entries)
        findings += pin_candidates(await store.profile(), now, self.cfg)
        if model is not None and fp != last_fingerprint:
            findings += await self.contradictions(entries, model)
        lines: list[dict[str, Any]] = []
        for f in findings:
            try:
                proposal, why = await store.propose(
                    action=f.action,
                    kind=f.item.kind,
                    text_=f.text,
                    example=f.item.example,
                    core=f.item.core if f.action != "pin" else True,
                    reason=f.reason,
                    evidence={
                        "review": True,
                        "other": str(f.other.id) if f.other is not None else None,
                    },
                    origin="care",
                    source_type="care",
                    target=f.item,
                    now=now,
                )
            except (ProtectedWrite, SecretBlocked) as exc:
                log.info("core review: proposal not queued: %s", exc)
                continue
            if proposal is not None and why == "ok":
                lines.append(
                    {
                        "action": "proposed",
                        "proposal_id": str(proposal.id),
                        "item_id": str(f.item.id),
                        "text": f.item.text,
                        "kind": f.item.kind,
                        "version": None,
                        "reason": f"core review: {f.action} — {f.reason}",
                        "undo": None,
                    }
                )
        return lines, fp
