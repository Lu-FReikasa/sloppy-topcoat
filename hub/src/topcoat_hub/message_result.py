"""A result message, written by code from stored evidence (PPH-05, 05b).

The agent that did the work says *what it found* (a claim, and which evidence marks show it) and
what it could not settle. Everything that can be checked is made here: the quote is a passage of the
stored excerpt, as it stands there; the source, its host and the time come from the evidence row;
the statistics come from the task. A mark that names no evidence row of the task is dropped and the
claim becomes an open point, so a result never cites a source the task did not see. Pure functions,
no model, no network.
"""

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from topcoat_hub import stop_reasons
from topcoat_hub.handover_file import best_quote

QUOTE_CHARS = 2000
SUMMARY_CHARS = 60000  # a thorough report is a document (research_profile.py)
CLAIM_CHARS = 1000


# why a task that ended badly did, for ``<app>.failure``
def _literal(claim: str, excerpt: str) -> str:
    """A passage of the excerpt for the claim, never shortened with an ellipsis (a quote that is
    cut stays a literal start of the line, so the check can find it again)."""
    found = best_quote(claim, excerpt, QUOTE_CHARS + 1)
    return found.removesuffix("…").strip()[:QUOTE_CHARS]


def _source(row: Any) -> dict[str, Any]:
    source: Mapping[str, Any] = row.source or {}
    url = source.get("url")
    if not isinstance(url, str) or not url:
        url = f"{source.get('capability', 'tool')}@{source.get('node', 'node')}#{row.ref}"
        host = ""
    else:
        host = urlsplit(url).netloc
    out = {"source_url": url[:2000], "retrieved_at": str(source.get("at") or "")[:40]}
    if host:
        out["source_host"] = host[:300]
    return out


def findings_of(
    claims: Sequence[Mapping[str, Any]], evidence: Sequence[Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """The findings for the claims, and the claims that could not be backed (as open points).

    ``claims`` are ``{"claim": str, "evidence": ["E1", ...]}``; the first mark that names a row of
    the task with an excerpt carries the claim."""
    rows = {r.ref: r for r in evidence}
    findings: list[dict[str, Any]] = []
    open_points: list[str] = []
    for item in claims:
        claim = str(item.get("claim") or "").strip()[:CLAIM_CHARS]
        if not claim:
            continue
        backing = next(
            (
                rows[ref]
                for ref in item.get("evidence") or []
                if ref in rows and (rows[ref].excerpt or "").strip()
            ),
            None,
        )
        quote = _literal(claim, backing.excerpt) if backing is not None else ""
        if backing is None or not quote:
            open_points.append(f"Not backed by evidence: {claim}")
            continue
        findings.append({"claim": claim, "quote": quote, **_source(backing)})
    return findings, open_points


def stats_of(task: Any, findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What the work cost, from the task and the findings (observation only, no limit)."""
    sources = {f["source_url"] for f in findings}
    hosts = {f["source_host"] for f in findings if f.get("source_host")}
    out: dict[str, Any] = {
        "sources": len(sources),
        "hosts": len(hosts),
        "steps": task.step_count,
        "tool_calls": task.tool_calls,
        "model": task.model,
    }
    for key, value in (
        ("queued_at", task.created_at),
        ("started_at", task.started_at),
        ("finished_at", task.finished_at),
    ):
        out[key] = value.isoformat() if value is not None else None
    return out


def coverage_of(
    must_cover: Sequence[str], told: Mapping[str, Any] | None, n_findings: int
) -> dict[str, Any]:
    """The coverage of the ``must_cover`` items as the agent says it, cut to what exists: an index
    that is no finding is dropped, and an item the agent did not mention is not invented."""
    out: dict[str, Any] = {}
    for item in must_cover:
        entry = (told or {}).get(item)
        if not isinstance(entry, Mapping):
            continue
        idx = [i for i in entry.get("findings") or [] if isinstance(i, int) and 0 <= i < n_findings]
        note = str(entry.get("note") or "")[:1000]
        out[item] = {"findings": idx, **({"note": note} if note else {})}
    return out


def result_payload(
    *,
    summary: str,
    claims: Sequence[Mapping[str, Any]],
    evidence: Sequence[Any],
    open_points: Sequence[str] = (),
    coverage: Mapping[str, Any] | None = None,
    must_cover: Sequence[str] = (),
    as_of: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The payload of a ``research.result`` and its findings."""
    findings, unbacked = findings_of(claims, evidence)
    payload: dict[str, Any] = {
        "summary": summary.strip()[:SUMMARY_CHARS] or "(no summary)",
        "findings": findings,
    }
    points = [str(p)[:500] for p in [*open_points, *unbacked]][:50]
    if points:
        payload["open_points"] = points
    covered = coverage_of(must_cover, coverage, len(findings))
    if covered:
        payload["coverage"] = covered
    if as_of:
        payload["as_of"] = as_of[:40]
    return payload, findings


def claims_of_diagnosis(task: Any) -> list[dict[str, Any]]:
    """The claims a finished task wrote itself (its diagnosis), for a result nobody sent."""
    out: list[dict[str, Any]] = []
    for f in (task.diagnosis or {}).get("findings") or []:
        if isinstance(f, dict) and f.get("claim"):
            out.append({"claim": f["claim"], "evidence": [r for r in f.get("evidence") or [] if r]})
    return out


def failure_payload(task: Any) -> dict[str, Any]:
    reason = stop_reasons.category(task.stop_reason, bool(task.error))
    detail = (task.error or task.summary or "")[:4000]
    return {
        "reason": reason,
        "retryable": reason in ("node_away", "tool_errors"),
        **({"detail": detail} if detail else {}),
    }
