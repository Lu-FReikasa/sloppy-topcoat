"""The style sheet of a theme as parts (2026-11-17): bare declarations (the variables at the top),
rules (``selector { … }``) and at-rules with a block (``@keyframes name { … }``, ``@media …``).

Two users: the lint looks at what a rule animates, and the theme workshop changes a theme by a
patch (a variable, a rule, a keyframes block) instead of having a model write the whole sheet
again. The split is tolerant: it never refuses, comments stay with the part that follows them, and
``join(split(css))`` keeps every declaration and block (white space between parts is normalised)."""

import re
from dataclasses import dataclass

_COMMENT = re.compile(r"/\*.*?\*/", re.S)


@dataclass
class Part:
    kind: str  # "decl" (a bare declaration), "rule", "at" (an at-rule with a block)
    head: str  # the declaration itself, the selector, or the at-rule's prelude ("@keyframes x")
    body: str = ""  # what stands between the braces (rules and at-rules)

    @property
    def name(self) -> str:
        """What a patch names the part by: the variable, the selector, the at-rule's prelude."""
        if self.kind == "decl":
            return self.head.split(":", 1)[0].strip()
        return norm_selector(self.head)


def norm_selector(text: str) -> str:
    """A selector or prelude in one spelling: single spaces, ``a, b`` lists, ``a > b``."""
    s = re.sub(r"\s+", " ", _COMMENT.sub("", text)).strip()
    s = re.sub(r"\s*,\s*", ", ", s)
    s = re.sub(r"\s*([>+~])\s*", r" \1 ", s)
    return s


def split(css: str) -> list[Part]:
    """The parts of a style sheet in their order."""
    parts: list[Part] = []
    i, n = 0, len(css)
    start = 0
    while i < n:
        c = css[i]
        if css.startswith("/*", i):
            end = css.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if c in "\"'":  # a string may hold braces and semicolons
            end = css.find(c, i + 1)
            i = n if end < 0 else end + 1
            continue
        if c == ";":
            text = _COMMENT.sub("", css[start:i]).strip()
            if text:
                parts.append(Part("decl", text + ";"))
            i += 1
            start = i
            continue
        if c == "{":
            head = _COMMENT.sub("", css[start:i]).strip()
            depth, j = 1, i + 1
            while j < n and depth:
                if css.startswith("/*", j):
                    end = css.find("*/", j + 2)
                    j = n if end < 0 else end + 2
                    continue
                if css[j] in "\"'":
                    end = css.find(css[j], j + 1)
                    j = n if end < 0 else end + 1
                    continue
                depth += {"{": 1, "}": -1}.get(css[j], 0)
                j += 1
            body = css[i + 1 : j - 1 if depth == 0 else j].strip()
            if head:
                parts.append(Part("at" if head.startswith("@") else "rule", head, body))
            i = j
            start = i
            continue
        if c == "}":  # a stray closing brace: dropped
            i += 1
            start = i
            continue
        i += 1
    tail = _COMMENT.sub("", css[start:]).strip()
    if tail:  # a last declaration without ';'
        parts.append(Part("decl", tail.rstrip(";") + ";"))
    return parts


def _indent(body: str) -> str:
    lines = [line.strip() for line in body.strip().split("\n")]
    return "\n".join(f"  {line}" if line else "" for line in lines)


def join(parts: list[Part]) -> str:
    """The style sheet of the parts: the declarations first in their order, then the blocks."""
    decls = [p.head for p in parts if p.kind == "decl"]
    blocks = [f"{p.head} {{\n{_indent(p.body)}\n}}" for p in parts if p.kind != "decl"]
    out = "\n".join(decls)
    if blocks:
        out += ("\n\n" if out else "") + "\n\n".join(blocks)
    return out


def declarations(body: str) -> list[tuple[str, str]]:
    """``(property, value)`` of a rule's body, lower-case properties, in order."""
    out: list[tuple[str, str]] = []
    for raw in _COMMENT.sub("", body).split(";"):
        if ":" not in raw:
            continue
        prop, value = raw.split(":", 1)
        prop = prop.strip().lower()
        if prop and "{" not in prop:
            out.append((prop, value.strip()))
    return out


def rules(parts: list[Part]) -> list[tuple[str, str]]:
    """``(selector, body)`` of every style rule, also those inside ``@media`` / ``@supports``."""
    out: list[tuple[str, str]] = []
    for p in parts:
        if p.kind == "rule":
            out.append((p.head, p.body))
        elif p.kind == "at" and p.head.lower().startswith(("@media", "@supports")):
            out.extend(rules(split(p.body)))
    return out
