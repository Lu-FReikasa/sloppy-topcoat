"""Does a cited result carry the sentence that cites it? A check without a model.

The second look by a second model (chat_review.py) was right mostly where a check needs no model
(evaluation of 2026-11-14, 45 findings read by hand: about 15 right, 20 wrong, 10 hair-splitting).
The right ones were mechanical: an action that never happened (answer_guard.py, claims.py already
catch those), and a sentence that cites a tool result the result does not support ("Das Menü
findest du unter 'Settings > Me' [E2]" while E2 held the state of a node). The wrong ones mostly
took notes of the memory for the truth, which earlier answers of the same assistant had written.

This module does the mechanical part at once, without loading anything: for each sentence that
cites ``[E2]`` (also ``[live E2]``, ``[E2, E5]``, ``(E1-E3)``) it takes what can be compared
literally (numbers with at least two digits, words in backticks or quotes, file names and paths)
and reports the sentence when none of them stands in the cited results, or when it cites a result
the check does not have. A sentence with nothing literal to compare is left alone: wording is not
checked here."""

import re
from collections.abc import Mapping
from typing import Any

MAX_FINDINGS = 4
# [E2] · [live E2] · [E2, E5] · [web E6] · (E1-E3) · E2/E4
_CITE = re.compile(
    r"[\[(][^\])\n]{0,20}?\bE\d+(?:\s*(?:[,/\u2013-]|und|and)\s*E?\d+)*[^\])\n]{0,5}[\])]"
)
_REF = re.compile(r"\bE(\d+)")
_RANGE = re.compile(r"\bE(\d+)\s*[\u2013-]\s*E?(\d+)")  # \u2013 is the dash of E1-E3
_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?](?=\s|$)|\n|$)")
_NUMBER = re.compile(r"(?<![\w.])\d[\d.,]*\d(?![\w])")
_QUOTED = re.compile(r"`([^`\n]{2,80})`|[„\"“']([^\"“”'„\n]{3,80})[\"”“']")
_PATH = re.compile(r"\b[\w-]+(?:/[\w.-]+)+|\b[\w-]+\.(?:py|ts|tsx|go|json|md|yml|yaml|toml)\b")


def _squash(text: str) -> str:
    return " ".join(text.lower().split())


def _digits(text: str) -> str:
    """A number as its digits only: 262.144, 262,144 and 262144 are the same number."""
    return re.sub(r"[.,]", "", text)


def cited(sentence: str) -> list[str]:
    """The results a sentence cites, in order (``E1``, ``E3``, a range spelled out)."""
    refs: list[str] = []
    for match in _CITE.finditer(sentence):
        part = match.group(0)
        for a, b in _RANGE.findall(part):
            lo, hi = int(a), int(b)
            if lo <= hi <= lo + 20:
                refs += [f"E{i}" for i in range(lo, hi + 1)]
        refs += [f"E{n}" for n in _REF.findall(part)]
    return list(dict.fromkeys(refs))


def literals(sentence: str) -> list[str]:
    """What of a sentence can be compared with a result word for word."""
    text = _CITE.sub(" ", sentence)
    out: list[str] = []
    for a, b in _QUOTED.findall(text):
        out.append((a or b).strip())
    out += _PATH.findall(text)
    out += [n for n in _NUMBER.findall(text) if len(_digits(n)) >= 2]
    return list(dict.fromkeys(x for x in out if x))


def _found(item: str, shown: str, digits: str) -> bool:
    if _NUMBER.fullmatch(item):
        return _digits(item) in digits
    return _squash(item) in shown


def unbacked(answer: str, results: Mapping[str, str]) -> list[dict[str, Any]]:
    """Findings in the form of chat_reviews: a sentence that cites a result it does not have, or
    whose literal parts stand in none of the results it cites. ``results`` maps ``E1`` to the text
    the check saw (its excerpt)."""
    findings: list[dict[str, Any]] = []
    for raw in _SENTENCE.findall(answer):
        sentence = " ".join(raw.split())
        refs = cited(sentence)
        if not refs:
            continue
        missing = [r for r in refs if r not in results]
        if missing:
            findings.append(
                {
                    "claim": sentence[:300],
                    "reason": f"cites {', '.join(missing)}, which this check does not have",
                    "evidence": None,
                    "severity": "minor",
                    "rule": "unknown_result",
                }
            )
        else:
            items = literals(sentence)
            if items:
                text = " ".join(results[r] for r in refs)
                shown, digits = _squash(text), _digits(text)
                if not any(_found(item, shown, digits) for item in items):
                    findings.append(
                        {
                            "claim": sentence[:300],
                            "reason": f"{', '.join(refs)} does not contain "
                            + ", ".join(f"„{i}“" for i in items[:3]),
                            "evidence": None,
                            "severity": "minor",
                            "rule": "not_in_result",
                        }
                    )
        if len(findings) >= MAX_FINDINGS:
            break
    return findings
