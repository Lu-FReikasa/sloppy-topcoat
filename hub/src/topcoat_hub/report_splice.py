"""A revision of a report changes the model's parts only (2026-11-13).

The block program ``research_report`` lets a model revise the whole document it was given, and the
revision was filed as it came back: the model had rewritten the title page too ("Erstellt
2024-11-13" for a report of 2026, "Tiefe: thorough" untranslated). The report's promise is that
everything about the run and the sources is written by the code (``report_doc.py``).

``splice`` keeps the code's parts of the version before (front matter, title, key facts, the box
of limits, the key claims, the sources, the origin) and takes from the revision only what the model
writes: the foreword and the numbered chapters. The table of contents follows the chapters.
"""

import re

from topcoat_hub.report_doc import LABELS, slug

_H2 = re.compile(r"^## (.+?)\s*$")
_H3 = re.compile(r"^### ")
_NUMBERED = re.compile(r"^\d+\.\s+(.+)$")


def _sections(text: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """The lines before the first chapter heading, and each ``## `` section with its lines."""
    head: list[str] = []
    out: list[tuple[str, list[str]]] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else _H2.match(line)
        if m:
            out.append((m.group(1).strip(), []))
        elif out:
            out[-1][1].append(line)
        else:
            head.append(line)
    return head, out


def _strip(lines: list[str]) -> str:
    return "\n".join(lines).strip()


def _until_h3(lines: list[str]) -> tuple[list[str], list[str]]:
    """A foreword's own text, and the code's box below it (``### …``)."""
    for i, line in enumerate(lines):
        if _H3.match(line):
            return lines[:i], lines[i:]
    return lines, []


def splice(original: str, revised: str, language: str, version: int | None = None) -> str | None:
    """The revised document with the code's parts of ``original``; None when the revision has no
    foreword and no chapter to take (the original stays)."""
    t = LABELS.get(language, LABELS["en"])
    head, old = _sections(original)
    _, new = _sections(revised)
    foreword_new = next((lines for title, lines in new if title == t["foreword"]), None)
    chapters_new = [
        (m.group(1).strip(), lines) for title, lines in new if (m := _NUMBERED.match(title))
    ]
    if foreword_new is None and not chapters_new:
        return None
    old_foreword = next((lines for title, lines in old if title == t["foreword"]), [])
    own_old, box = _until_h3(old_foreword)
    own_new = _until_h3(foreword_new)[0] if foreword_new is not None else own_old
    chapters = chapters_new or [
        (m.group(1).strip(), lines) for title, lines in old if (m := _NUMBERED.match(title))
    ]
    code_parts = [
        (title, lines)
        for title, lines in old
        if title not in (t["contents"], t["foreword"]) and not _NUMBERED.match(title)
    ]
    out = list(head)
    if version is not None:  # the facts name the version this text becomes
        row = f"| {t['status']} |"
        out = [
            re.sub(rf"{t['version']} \d+", f"{t['version']} {version}", line)
            if line.startswith(row)
            else line
            for line in out
        ]
    while out and not out[-1].strip():
        out.pop()
    out += ["", f"## {t['contents']}", "", f"- [{t['foreword']}](#{slug(t['foreword'])})"]
    for n, (title, _lines) in enumerate(chapters, 1):
        out.append(f"- [{n}. {title}](#{slug(f'{n}. {title}')})")
    for title, _lines in code_parts:
        out.append(f"- [{title}](#{slug(title)})")
    out += ["", f"## {t['foreword']}", "", _strip(own_new), ""]
    out += [line for line in box]
    for n, (title, lines) in enumerate(chapters, 1):
        out += ["", f"## {n}. {title}", "", _strip(lines)]
    for title, lines in code_parts:
        out += ["", f"## {title}", "", _strip(lines)]
    out.append("")
    return "\n".join(out)
