"""What the code checks about a message before any agent reads it (PPH-05, 05c).

A pure function of the message, the request it answers and the evidence the task stored: no model,
no network. It cleans every text (markup, hidden characters, control characters), then checks that
every quote occurs literally in the stored evidence of its source, that the number of distinct
sources and hosts reaches what the request asked for, and that every ``must_cover`` item has a
finding or an open point. The outcome is attached to the message (``verification``) and shown to the
reviewer, who may not take its word for the facts. URLs are data: nothing here, and nothing in the
Project application, fetches one.
"""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

_TAGS = re.compile(r"<[^>\n]{0,300}>")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# zero-width and direction-changing characters hide text from a reader
_HIDDEN = re.compile("[​-‏‪-‮⁠-⁤﻿]")
_SPACE = re.compile(r"\s+")


def clean_text(text: str) -> str:
    """The text without markup tags, control characters and hidden characters (NFC)."""
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL.sub("", _HIDDEN.sub("", text))
    return _TAGS.sub("", text)


def clean(value: Any) -> Any:
    """Every string of a payload cleaned, structure kept."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    return value


def _fold(text: str) -> str:
    return _SPACE.sub(" ", clean_text(text)).strip().lower()


def verify(
    payload: Mapping[str, Any],
    request: Mapping[str, Any],
    evidence: Sequence[Any],
) -> dict[str, Any]:
    """The checks of a result. ``ok`` is false when a quote is not in the evidence, the request's
    minimum of sources or hosts is not reached, or a ``must_cover`` item is neither covered nor
    named as an open point."""
    issues: list[str] = []
    excerpts = [_fold(e.excerpt or "") for e in evidence]
    findings = list(payload.get("findings") or [])

    missing = []
    for i, f in enumerate(findings):
        quote = _fold(str(f.get("quote") or ""))
        if not quote or not any(quote in ex for ex in excerpts):
            missing.append(i)
    if missing:
        issues.append(f"quote not found in the evidence: finding {', '.join(map(str, missing))}")

    sources = {f.get("source_url") for f in findings if f.get("source_url")}
    hosts = {f.get("source_host") for f in findings if f.get("source_host")}
    want_sources = int(request.get("min_sources") or 0)
    want_hosts = int(request.get("min_hosts") or 0)
    if len(sources) < want_sources:
        issues.append(f"{len(sources)} sources, {want_sources} wanted")
    if len(hosts) < want_hosts:
        issues.append(f"{len(hosts)} hosts, {want_hosts} wanted")

    open_text = " ".join(str(p) for p in payload.get("open_points") or []).lower()
    coverage = payload.get("coverage") or {}
    covered: dict[str, bool] = {}
    for item in request.get("must_cover") or []:
        has_finding = bool((coverage.get(item) or {}).get("findings"))
        covered[item] = has_finding or str(item).lower() in open_text
        if not covered[item]:
            issues.append(f"not covered and not named as open: {item}")

    return {
        "ok": not issues,
        "issues": issues,
        "quotes": {"checked": len(findings), "not_found": len(missing)},
        "sources": len(sources),
        "hosts": len(hosts),
        "covered": covered,
        "wanted": {"min_sources": want_sources, "min_hosts": want_hosts},
    }


def verify_dev(payload: Mapping[str, Any], order: Mapping[str, Any]) -> dict[str, Any]:
    """The checks of a Development result that the hub can make itself: the branch is the ordered
    one, the tests did not fail, every acceptance item is met. The diff statistics, the head and the
    test summary are what the run reports (``self_reported``); the push re-reads the real diff.
    UNVERIFIED on real hardware: how the workshop reports them."""
    issues: list[str] = []
    branch = payload.get("branch")
    if branch and branch != order.get("branch"):
        issues.append(f"branch {branch} is not the ordered branch {order.get('branch')}")
    tests = payload.get("tests") or {}
    if tests.get("state") == "failed":
        issues.append("the tests failed")
    unmet = [a.get("item") for a in payload.get("acceptance") or [] if not a.get("met")]
    if unmet:
        issues.append(f"acceptance not met: {', '.join(map(str, unmet))}")
    reported = [k for k in ("head", "diff_stat", "tests") if payload.get(k)]
    return {"ok": not issues, "issues": issues, "self_reported": reported}
