"""The contract of each employee (plan 2026-11-24, B1, step 1.9).

One file per application in ``proto/agents/<key>.json`` (assistant, research, dev, admin, project),
checked against ``proto/agents/contract.schema.json``: what it is for, its typical tasks, the tool
groups it may use (each said in a sentence), what it never does and what needs the person's click,
the answer forms it writes, whom it gives work to, its model role. It was taken from what the
prompts and the registry said before, not made up anew.

Who uses it:
- the agent prompt (agent_prompts.for_application): the role sentence and its own rules in place of
  the shared bullets about machines (Development and Projekte; the others keep the shared prompt);
- the registry's responsibility of an application (messages.responsibility);
- the self-description: a short German one (at most :data:`SHORT_CHARS`) in front of every chat
  turn, so "what can you do" is answered in words, and a long one for the chat's function
  ``about_abilities`` and the GUI (``/api/ui/workspaces/<key>/agent``). Neither names a function.

The text for models is English (small models follow it better), the ``*_de`` fields are for the
person.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema

from topcoat_hub.config import REPO_ROOT

KEYS = ("assistant", "research", "dev", "admin", "project")
SHORT_CHARS = 600
# applications whose conversations the assistant answers (the general chat has no key of its own)
_ALIASES = {"chat": "assistant", "": "assistant"}
# the image sets TOPCOAT_PROTO_ROOT; main.py sets the configured root again
_PROTO: list[Path] = [Path(os.environ.get("TOPCOAT_PROTO_ROOT") or REPO_ROOT / "proto")]


class ContractError(Exception):
    pass


@dataclass(frozen=True)
class Contract:
    key: str
    raw: dict[str, Any]

    @property
    def title(self) -> str:
        return str(self.raw["title_de"])

    @property
    def intro(self) -> str | None:
        return self.raw.get("intro")

    @property
    def rules(self) -> list[str]:
        return list(self.raw.get("rules") or [])

    @property
    def drops(self) -> tuple[str, ...]:
        return tuple(self.raw.get("drops") or [])

    @property
    def groups(self) -> list[str]:
        """Its tool groups, without the internet (that is a switch of its own)."""
        return [t["group"] for t in self.raw["tools"] if t["group"] != "internet"]

    def purpose(self, language: str = "de") -> str:
        texts = self.raw["purpose"]
        return str(texts.get(language) or texts["en"])


def set_proto_root(root: Path) -> None:
    _PROTO[0] = Path(root)
    _load_all.cache_clear()


@cache
def _load_all() -> dict[str, Contract]:
    folder = _PROTO[0] / "agents"
    schema = json.loads((folder / "contract.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    out: dict[str, Contract] = {}
    for key in KEYS:
        path = folder / f"{key}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.absolute_path))
        if errors:
            where = "/".join(str(p) for p in errors[0].absolute_path)
            raise ContractError(f"{path.name}: {errors[0].message} ({where})")
        if raw["key"] != key:
            raise ContractError(f"{path.name}: its key is {raw['key']!r}")
        out[key] = Contract(key, raw)
    return out


def get(application: str | None) -> Contract | None:
    """The contract of an application (the general chat is the assistant's), ``None`` for an
    application without one (Files, Wiki, Sheets: no employee of their own)."""
    key = _ALIASES.get(application or "", application or "")
    try:
        return _load_all().get(key)
    except (OSError, ValueError, ContractError):
        return None  # a broken file costs no answer; the tests see it


def all_contracts() -> dict[str, Contract]:
    return dict(_load_all())


# --- the self-description (German, deterministic, never a function name) --------------------------


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " und " + items[-1]


def short(application: str | None) -> str:
    """ "Ich bin …, ich kann …, ich kann nicht …, dafür frage ich …" in at most
    :data:`SHORT_CHARS` characters; "" without a contract."""
    c = get(application)
    if c is None:
        return ""
    can = [t["say_de"] for t in c.raw["tools"]]
    cannot = list(c.raw["limits"]["never_de"])
    asks = [f"{get(t['to']).title if get(t['to']) else t['to']} ({t['for_de']})"
            for t in c.raw["talks_to"]]  # fmt: skip
    while True:
        parts = [f"Ich bin {c.title}: {c.purpose('de')}"]
        if can:
            parts.append(f"Ich kann {_join(can)}.")
        if cannot:
            parts.append(f"Ich kann nicht {_join(cannot)}.")
        if asks:
            parts.append(f"Dafür frage ich {_join(asks)}.")
        text = " ".join(parts)
        if len(text) <= SHORT_CHARS:
            return text
        # too long: the longest list gives up its last item first
        longest = max((can, cannot, asks), key=len)
        if len(longest) <= 1:
            return text[: SHORT_CHARS - 1] + "…"
        longest.pop()


def long(application: str | None) -> str:
    """Everything the contract says to the person, as German text (Markdown lists)."""
    c = get(application)
    if c is None:
        return ""
    lines = [f"**{c.title}**: {c.purpose('de')}", "", "Typische Aufgaben:"]
    lines += [f"- {t['de']} (zum Beispiel: „{t['example_de']}“)" for t in c.raw["tasks"]]
    lines += ["", "Was ich benutzen darf:"]
    lines += [f"- {t['say_de']}" for t in c.raw["tools"]] or ["- nichts außer meinem Wissen"]
    lines += ["", "Was ich nie tue:"] + [f"- {n}" for n in c.raw["limits"]["never_de"]]
    clicks = c.raw["limits"]["needs_click_de"]
    if clicks:
        lines += ["", "Was deinen Klick braucht:"] + [f"- {n}" for n in clicks]
    if c.raw["talks_to"]:
        lines += ["", "Wen ich frage:"]
        for t in c.raw["talks_to"]:
            other = get(t["to"])
            lines.append(f"- {other.title if other else t['to']}: {t['for_de']}")
    return "\n".join(lines)


def view(application: str | None) -> dict[str, Any] | None:
    """The contract for the GUI (German fields only)."""
    c = get(application)
    if c is None:
        return None
    return {
        "key": c.key,
        "title": c.title,
        "purpose": c.purpose("de"),
        "tasks": [{"text": t["de"], "example": t["example_de"]} for t in c.raw["tasks"]],
        "tools": [t["say_de"] for t in c.raw["tools"]],
        "never": list(c.raw["limits"]["never_de"]),
        "needs_click": list(c.raw["limits"]["needs_click_de"]),
        "talks_to": [
            {"to": (get(t["to"]).title if get(t["to"]) else t["to"]), "for": t["for_de"]}
            for t in c.raw["talks_to"]
        ],
    }


SELF_HINT = (
    "Who you are (say it in words when asked what you can do; never name a function, a tool "
    "group or a capability by its technical name):"
)


def prompt_block(application: str | None) -> str:
    """The short self-description as a block in front of a chat turn ("" without a contract)."""
    text = short(application)
    return f"{SELF_HINT}\n{text}\n\n" if text else ""
