"""Reading the JSON a small model *almost* got right (E3-01b).

A 12B model writing a ``finish`` with proposals often slips in small ways: a quote inside a string
that is not escaped (``"summary": "the "web" container is down"``), a raw line break inside a
string, a comma before a closing bracket, a code fence around the object. Strict ``json.loads``
rejects all of these, and the run then ends without a diagnosis or without proposals. This module
repairs exactly those slips and nothing else — no guessing of missing keys or values.
Pure functions, no I/O.
"""

import json
import re
from typing import Any

_NUMBER_THEN_END = re.compile(r"-?\d+(\.\d+)?\s*[,}\]]")
_LITERAL = re.compile(r"(true|false|null)\s*[,}\]]")


def _next_sig(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def _value_starts(text: str, i: int) -> bool:
    """Does a JSON value begin at ``i`` (after a comma)? Prose after an inner quote does not."""
    if i >= len(text):
        return True  # cut off: the quote was the last thing
    c = text[i]
    if c in '"{[':
        return True
    return bool(_NUMBER_THEN_END.match(text, i) or _LITERAL.match(text, i))


def repair_json_object(text: str) -> dict[str, Any] | None:
    """The first JSON object in ``text`` after repairing the usual slips of a small model, or
    None. Strict JSON passes through unchanged."""
    start = text.find("{")
    if start < 0:
        return None
    out: list[str] = []
    stack: list[str] = []  # "{" or "["
    in_string = False
    is_key = False
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if in_string:
            if c == "\\" and i + 1 < n:
                out.append(text[i : i + 2])
                i += 2
                continue
            if c == "\n":
                out.append("\\n")
            elif c == "\r":
                out.append("\\r")
            elif c == "\t":
                out.append("\\t")
            elif c == '"':
                j = _next_sig(text, i + 1)
                nxt = text[j] if j < n else ""
                if nxt in ("", "}", "]") or (nxt == ":" and is_key):
                    closes = True
                elif nxt == ",":
                    closes = _value_starts(text, _next_sig(text, j + 1))
                else:
                    closes = False
                if closes:
                    in_string = False
                    out.append(c)
                else:
                    out.append('\\"')
            else:
                out.append(c)
            i += 1
            continue
        if c == '"':
            in_string = True
            is_key = stack[-1:] == ["{"] and re.search(r"[{,]\s*$", "".join(out[-8:])) is not None
            out.append(c)
        elif c in "{[":
            stack.append(c)
            out.append(c)
        elif c in "}]":
            if not stack:
                break
            stack.pop()
            out.append(c)
            if not stack:
                break
        elif c == ",":
            j = _next_sig(text, i + 1)
            if j < n and text[j] in "}]":
                i += 1  # trailing comma
                continue
            out.append(c)
        else:
            out.append(c)
        i += 1
    try:
        value = json.loads("".join(out), strict=False)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def as_list_of_objects(value: Any) -> list[dict[str, Any]]:
    """``value`` as the list of objects a model meant: a list, one object, or a list written as a
    JSON string (a model that quotes it). Anything else is an empty list."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except ValueError:
            obj = repair_json_object(text)
            if obj is not None:
                return [obj]
            m = re.search(r"\[.*\]", text, re.DOTALL)
            try:
                value = json.loads(m.group(0)) if m else None
            except ValueError:
                value = None
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []
