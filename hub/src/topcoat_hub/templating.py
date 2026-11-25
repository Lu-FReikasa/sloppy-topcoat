"""Values in a flow: ``{{input.question}}``, ``{{plan.out.steps[0].title}}``, ``{{prev.out}}``.

A node's text parameters may contain such references. They are looked up in the data of the run
(the input, the output of every node that has finished, ``prev`` = the output of the node just
before). There is no expression language and nothing is evaluated: a reference is a path of names
and list positions, and a path that leads nowhere is an empty string. The result is bounded, so a
huge output cannot make a prompt that no model can read.
"""

import json
import re
from typing import Any

MAX_RENDERED = 60_000
MAX_DEPTH = 8

_TOKEN = re.compile(r"\{\{\s*([A-Za-z_][\w-]*(?:\.[\w-]+|\[\d+\])*)\s*\}\}")
_STEP = re.compile(r"\.?([\w-]+)|\[(\d+)\]")


def resolve(path: str, roots: dict[str, Any]) -> Any:
    """The value a path points to, or ``None`` when it leads nowhere."""
    steps = _STEP.findall(path)
    if not steps or len(steps) > MAX_DEPTH:
        return None
    name, _ = steps[0]
    value: Any = roots.get(name)
    for key, index in steps[1:]:
        if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            # an item of a model's JSON list is kept as its text: a field of it is read from the
            # JSON (live 2026-11-17: {{place.lines}} of an issue program's chosen places)
            try:
                value = json.loads(value)
            except ValueError:
                return None
        if index != "":
            if not isinstance(value, list) or int(index) >= len(value):
                return None
            value = value[int(index)]
        elif isinstance(value, dict):
            value = value.get(key)
        else:
            return None
    return value


def as_text(value: Any) -> str:
    """A value as it reads inside a prompt: text as it is, anything else as compact JSON."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def render(template: str, roots: dict[str, Any]) -> str:
    """The template with every reference replaced by its value as text."""
    out = _TOKEN.sub(lambda m: as_text(resolve(m.group(1), roots)), template or "")
    return out if len(out) <= MAX_RENDERED else out[:MAX_RENDERED] + "…"


def references(template: str) -> list[str]:
    """The first names of the references in a template (the nodes it reads)."""
    return [_STEP.findall(m.group(1))[0][0] for m in _TOKEN.finditer(template or "")]
