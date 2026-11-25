"""An employee's result in two forms: ``summary`` for the person, ``brief`` for the secretary
(ARCHITECTURE D112, issue #35).

The ``summary`` is what the employee wrote for the person. The ``brief`` is a few lines the
secretary works with: what kind of answer it is, its first sentence, how much it holds, what is
still open and where the details are. The brief is written by code from the payload
(:func:`brief_of`), so it needs no model and says nothing the payload does not.

Before either reaches the secretary's conversation it passes the private-data filter of D65
(:func:`topcoat_hub.redaction.private_kinds`): a text that holds a secret or an identifier of the
operator's network (a host, an address, a user name, an e-mail, an own domain) is replaced by a
pointer to the application where the details stay. Files, records (Akten) and branches are passed
by reference only (:func:`refs_of`): their kind and where they are, never their content. The
findings and their quotes never enter the secretary's text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from topcoat_hub import agent_contract
from topcoat_hub.redaction import private_kinds

BRIEF_CHARS = 600
SUMMARY_CHARS = 2000
REF_CHARS = 300
MAX_REFS = 20
RESULT_TYPES = ("research.result", "dev.result", "admin.result")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _first_sentence(text: str, limit: int = 240) -> str:
    text = " ".join(str(text or "").split())
    first = _SENTENCE.split(text, maxsplit=1)[0] if text else ""
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def _app_of(message_type: str) -> str:
    return message_type.partition(".")[0]


def brief_of(message_type: str, payload: Mapping[str, Any]) -> str:
    """The brief for the secretary, from the payload of a result (English, for the model): the
    kind of answer, its first sentence, its size, what is open, where the details are."""
    app = _app_of(message_type)
    parts = [f"{app} result: {_first_sentence(payload.get('summary', '')) or '(no summary)'}"]
    findings = payload.get("findings") or []
    if findings:
        parts.append(f"{len(findings)} finding(s) with sources")
    if message_type == "dev.result":
        if payload.get("branch"):
            parts.append(f"on branch {payload['branch']}")
        tests = payload.get("tests") or {}
        if tests.get("state"):
            parts.append(f"tests {tests['state']}")
        acceptance = payload.get("acceptance") or []
        if acceptance:
            met = sum(1 for a in acceptance if a.get("met"))
            parts.append(f"acceptance {met}/{len(acceptance)} met")
    coverage = payload.get("coverage") or {}
    if isinstance(coverage, Mapping):  # item -> {"findings": [...]} (message_result.coverage_of)
        bare = [
            k for k, v in coverage.items() if not (isinstance(v, Mapping) and v.get("findings"))
        ]
        if bare:
            parts.append(f"{len(bare)} required point(s) without a finding")
    open_points = payload.get("open_points") or []
    if open_points:
        parts.append(f"{len(open_points)} open point(s)")
    text = "; ".join(parts) + "."
    return text if len(text) <= BRIEF_CHARS else text[: BRIEF_CHARS - 1] + "…"


def with_brief(message_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The payload of a result with its ``brief`` (written by code when the employee gave none);
    other message types unchanged."""
    if message_type not in RESULT_TYPES:
        return payload
    brief = str(payload.get("brief") or "").strip()
    return {**payload, "brief": brief[:BRIEF_CHARS] or brief_of(message_type, payload)}


def refs_of(message_type: str, payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """Where the details of a result are, by reference only: the files its findings name, the
    branch of a Development result. Never a quote, never the content of a file."""
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, ref: Any) -> None:
        ref = str(ref or "").strip()[:REF_CHARS]
        if ref and (kind, ref) not in seen and len(out) < MAX_REFS:
            seen.add((kind, ref))
            out.append({"kind": kind, "ref": ref})

    for f in payload.get("findings") or []:
        if isinstance(f, Mapping) and f.get("path"):
            add("file", f["path"])
    if message_type == "dev.result" and payload.get("branch"):
        add("branch", payload["branch"])
    return out


@dataclass(frozen=True)
class ForSecretary:
    """What of one result may enter the secretary's conversation."""

    message_type: str
    summary: str
    brief: str
    refs: list[dict[str, str]]
    withheld: list[str] = field(default_factory=list)  # the private kinds that were found

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.message_type,
            "summary": self.summary,
            "brief": self.brief,
            "refs": self.refs,
            "withheld": self.withheld,
        }


def pointer(message_type: str, language: str = "en") -> str:
    """The text that stands in for one that holds private details."""
    contract = agent_contract.get(_app_of(message_type))
    title = contract.title(language) if contract else _app_of(message_type)
    if language == "de":
        return f"Das Ergebnis enthält private Angaben; sie bleiben in {title}. Dort öffnen."
    return f"The result holds private details; they stay in {title}. Open it there."


def for_secretary(
    message_type: str,
    payload: Mapping[str, Any],
    hosts: Iterable[str] = (),
    users: Iterable[str] = (),
    domains: Iterable[str] = (),
    language: str = "en",
) -> ForSecretary:
    """The two forms of a result as the secretary may see them, each through the private-data
    filter (D65): a text that holds private details becomes a :func:`pointer`, a reference whose
    path or name does is left out. ``hosts``/``users``/``domains`` are the operator's own names
    (:func:`topcoat_hub.web_access.private_names_of`)."""
    if message_type not in RESULT_TYPES:
        raise ValueError(f"{message_type} is not the result of an employee")
    hosts, users, domains = list(hosts), list(users), list(domains)
    withheld: list[str] = []

    def filtered(text: str, limit: int) -> str:
        text = str(text or "").strip()[:limit]
        kinds = private_kinds(text, hosts, users, domains) if text else []
        if kinds:
            withheld.extend(k for k in kinds if k not in withheld)
            return pointer(message_type, language)
        return text

    summary = filtered(str(payload.get("summary") or ""), SUMMARY_CHARS)
    brief_text = str(payload.get("brief") or "") or brief_of(message_type, payload)
    brief = filtered(brief_text, BRIEF_CHARS)
    refs = []
    for r in refs_of(message_type, payload):
        kinds = private_kinds(r["ref"], hosts, users, domains)
        if kinds:
            withheld.extend(k for k in kinds if k not in withheld)
            continue
        refs.append(r)
    return ForSecretary(message_type, summary, brief, refs, withheld)
