"""Deterministic scoring of one answer (PRG-00 R6): rules, never a model as judge.

The checks of a case (all that are given must hold):

``must``       strings, or lists of alternatives of which one must occur (case and spaces ignored)
``must_not``   strings that must not occur
``regex``      a pattern that has to match (case-insensitive, ``.`` matches a line break)
``final``      a pattern that has to match the last non-empty line (a model that works the sum out
               first is judged by where it ends up)
``max_words`` / ``min_words``
``json``       the answer, or the first JSON object in it, parses; ``{"keys": [...]}`` requires keys

The same rules as ``scripts/dev/param_sweep.py``, which stays a stand-alone script.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def first_json_object(text: str) -> Any | None:
    """The answer parsed as JSON, or the first JSON object inside it."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
            return obj
        except ValueError:
            start = text.find("{", start + 1)
    return None


@dataclass(frozen=True)
class CaseScore:
    passed: bool
    failed: list[str] = field(default_factory=list)


def score_case(answer: str, checks: dict[str, Any]) -> CaseScore:
    """Which checks hold for ``answer``; ``passed`` when all do."""
    failed: list[str] = []
    text = _norm(answer)
    for item in checks.get("must", []):
        options = [item] if isinstance(item, str) else list(item)
        if not any(_norm(o) in text for o in options):
            failed.append(f"must:{options[0]}")
    for item in checks.get("must_not", []):
        if _norm(item) in text:
            failed.append(f"must_not:{item}")
    pattern = checks.get("regex")
    if pattern and not re.search(pattern, answer, re.IGNORECASE | re.DOTALL):
        failed.append(f"regex:{pattern}")
    final = checks.get("final")
    if final:
        lines = [line for line in answer.splitlines() if line.strip()]
        if not lines or not re.search(final, lines[-1], re.IGNORECASE):
            failed.append(f"final:{final}")
    words = len(answer.split())
    if "max_words" in checks and words > checks["max_words"]:
        failed.append(f"max_words:{words}>{checks['max_words']}")
    if "min_words" in checks and words < checks["min_words"]:
        failed.append(f"min_words:{words}<{checks['min_words']}")
    if "json" in checks:
        obj = first_json_object(answer)
        keys = checks["json"].get("keys", []) if isinstance(checks["json"], dict) else []
        if not isinstance(obj, dict):
            failed.append("json:not an object")
        else:
            failed.extend(f"json:key {k}" for k in keys if k not in obj)
    if not answer.strip():
        failed.append("empty")
    return CaseScore(not failed, failed)
