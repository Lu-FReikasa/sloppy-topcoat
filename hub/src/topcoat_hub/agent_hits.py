"""What a run found by searching, and the read calls that point at it (Phase 1.1, 2026-11-11).

A small model that read a list of search hits asks for one of them in its own words: the evidence
mark of the search (``"path": "E2"``), the title with a folder in front of it, or the name cut at
``~…`` (live 2026-11-04..11: 39 of 59 ``knowledge.read`` failed, 80 of 247 ``web.fetch``). Each
failure costs a model turn and stays in the prompt. Here the run keeps the hits of its searches,
and a read call whose target is no hit is mapped to the one hit it clearly means. Nothing is
guessed: a target that fits none or several hits stays as it is, and the failure of the read then
lists the hits the run has.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# the search tools and the key their hits carry; the read tools and the key of their target
SEARCHES: dict[str, str] = {
    "knowledge.search": "path",
    "web.search": "url",
    "repo.search": "path",
    "repo.list": "path",
    "repo.outline": "path",
}
READS: dict[str, str] = {"knowledge.read": "path", "web.fetch": "url", "repo.read": "path"}
# which search feeds which read (a web page is never a file, a note is never a URL)
FEEDS: dict[str, tuple[str, ...]] = {
    "knowledge.read": ("knowledge.search",),
    "web.fetch": ("web.search",),
    "repo.read": ("repo.search", "repo.list", "repo.outline"),
}
MAX_HITS = 200
_REF = re.compile(r"^\s*\[?(E\d{1,4})\]?\s*$")
_HIT = re.compile(r"^\s*(?:hit|treffer)?\s*#?\s*(\d{1,3})\s*$", re.I)


def _strings(output: Any, key: str) -> list[str]:
    """Every string under ``key`` in the output, in order."""
    found: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                if k == key and isinstance(v, str) and v:
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            return []
    walk(output)
    return found


def _base(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1]


@dataclass
class Hits:
    """The hits of one run: per search tool in order, and per evidence mark of the search."""

    by_tool: dict[str, list[str]] = field(default_factory=dict)
    by_ref: dict[str, list[str]] = field(default_factory=dict)

    def note(self, capability: str, output: Any, ref: str) -> None:
        key = SEARCHES.get(capability)
        if key is None:
            return
        got = _strings(output, key)
        if not got:
            return
        self.by_ref[ref] = got
        have = self.by_tool.setdefault(capability, [])
        for v in got:
            if v not in have and len(have) < MAX_HITS:
                have.append(v)

    def known(self, capability: str) -> list[str]:
        out: list[str] = []
        for search in FEEDS.get(capability, ()):
            out += [v for v in self.by_tool.get(search, []) if v not in out]
        return out

    def resolve(self, capability: str, call_input: Any) -> tuple[dict[str, Any], str] | None:
        """The input with its target mapped to the one hit it means, and a note saying so; None
        when nothing is to be done (the target is a hit, or fits none or several)."""
        key = READS.get(capability)
        if key is None or not isinstance(call_input, dict):
            return None
        known = self.known(capability)
        hit_no = call_input.get("hit")
        target = call_input.get(key)
        if isinstance(hit_no, int) and not target:
            if 1 <= hit_no <= len(known):
                rest = {k: v for k, v in call_input.items() if k != "hit"}
                return {**rest, key: known[hit_no - 1]}, f"hit {hit_no} is {known[hit_no - 1]}"
            return None
        if not isinstance(target, str) or not target.strip() or not known or target in known:
            return None
        pick = self._pick(target, known)
        if pick is None or pick == target:
            return None
        return {
            **call_input,
            key: pick,
        }, f"{key} {target!r} read as {pick!r} (a hit of your search)"

    def _pick(self, target: str, known: list[str]) -> str | None:
        ref = _REF.match(target)
        if ref:  # the mark of a search: its only hit, or its first one when it named a single
            hits = self.by_ref.get(ref.group(1)) or []
            return hits[0] if len(hits) == 1 else None
        number = _HIT.match(target)
        if number:
            n = int(number.group(1))
            return known[n - 1] if 1 <= n <= len(known) else None
        low = target.strip().casefold()
        same = [h for h in known if h.casefold() == low]
        if len(same) == 1:
            return same[0]
        stem = re.sub(r"(~|\.\.\.|…).*$", "", _base(target.strip())).strip().casefold()
        if len(stem) < 4:
            return None
        fits = [h for h in known if _base(h).casefold().startswith(stem)]
        if len(fits) == 1:
            return fits[0]
        if not fits and "/" not in target and not target.startswith("http"):
            inside = [h for h in known if stem in h.casefold()]
            return inside[0] if len(inside) == 1 else None
        return None

    def listing(self, capability: str, limit: int = 8) -> str:
        """The hits a failed read could have meant, numbered, for the failure text."""
        known = self.known(capability)
        if not known:
            return ""
        lines = "; ".join(f"{i}: {v}" for i, v in enumerate(known[:limit], 1))
        more = f" (and {len(known) - limit} more)" if len(known) > limit else ""
        return (
            f' The hits of your searches are — {lines}{more}. Use one of them exactly, or "hit": N.'
        )
