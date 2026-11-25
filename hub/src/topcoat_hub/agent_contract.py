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
- the self-description: a short one (at most :data:`SHORT_CHARS`) in front of every chat turn, so
  "what can you do" is answered in words, and a long one for the chat's function
  ``about_abilities`` and the GUI (``/api/ui/workspaces/<key>/agent``). Neither names a function.
  Both are in the person's language (``de``/``en``, English by default).

The text for models (``intro``, ``rules``) is English (small models follow it better); the texts
for the person come in both languages (``*_de`` and ``*_en``, ``purpose.de``/``purpose.en``).
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

    def title(self, language: str = "en") -> str:
        return str(self.raw["title_de" if language == "de" else "title_en"])

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

    def purpose(self, language: str = "en") -> str:
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


# --- the self-description (in the person's language, deterministic, never a function name) --------

LANGUAGES = ("en", "de")

# the fixed words of the self-description, per language
_WORDS: dict[str, dict[str, str]] = {
    "en": {
        "and": " and ",
        "i_am": "I am {title}: {purpose}",
        "i_can": "I can {items}.",
        "i_cannot": "I cannot {items}.",
        "i_ask": "For that I ask {items}.",
        "tasks": "Typical tasks:",
        "task": "- {text} (for example: “{example}”)",
        "tools": "What I may use:",
        "no_tools": "- nothing but what I know",
        "never": "What I never do:",
        "clicks": "What needs your click:",
        "asks": "Whom I ask:",
    },
    "de": {
        "and": " und ",
        "i_am": "Ich bin {title}: {purpose}",
        "i_can": "Ich kann {items}.",
        "i_cannot": "Ich kann nicht {items}.",
        "i_ask": "Dafür frage ich {items}.",
        "tasks": "Typische Aufgaben:",
        "task": "- {text} (zum Beispiel: „{example}“)",
        "tools": "Was ich benutzen darf:",
        "no_tools": "- nichts außer meinem Wissen",
        "never": "Was ich nie tue:",
        "clicks": "Was deinen Klick braucht:",
        "asks": "Wen ich frage:",
    },
}


def language_of(language: str | None) -> str:
    """``de`` or ``en``: a language the contracts speak, English for anything else."""
    return language if language in LANGUAGES else "en"


def _join(items: list[str], language: str) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + _WORDS[language]["and"] + items[-1]


def _title_of(key: str, language: str) -> str:
    other = get(key)
    return other.title(language) if other else key


def short(application: str | None, language: str = "en") -> str:
    """ "I am …, I can …, I cannot …, for that I ask …" in at most :data:`SHORT_CHARS`
    characters, in ``language`` (``de``/``en``); "" without a contract."""
    c = get(application)
    if c is None:
        return ""
    lang = language_of(language)
    w = _WORDS[lang]
    can = [t[f"say_{lang}"] for t in c.raw["tools"]]
    cannot = list(c.raw["limits"][f"never_{lang}"])
    asks = [f"{_title_of(t['to'], lang)} ({t[f'for_{lang}']})" for t in c.raw["talks_to"]]
    while True:
        parts = [w["i_am"].format(title=c.title(lang), purpose=c.purpose(lang))]
        if can:
            parts.append(w["i_can"].format(items=_join(can, lang)))
        if cannot:
            parts.append(w["i_cannot"].format(items=_join(cannot, lang)))
        if asks:
            parts.append(w["i_ask"].format(items=_join(asks, lang)))
        text = " ".join(parts)
        if len(text) <= SHORT_CHARS:
            return text
        # too long: the longest list gives up its last item first
        longest = max((can, cannot, asks), key=len)
        if len(longest) <= 1:
            return text[: SHORT_CHARS - 1] + "…"
        longest.pop()


def long(application: str | None, language: str = "en") -> str:
    """Everything the contract says to the person, as text (Markdown lists) in ``language``."""
    c = get(application)
    if c is None:
        return ""
    lang = language_of(language)
    w = _WORDS[lang]
    lines = [f"**{c.title(lang)}**: {c.purpose(lang)}", "", w["tasks"]]
    lines += [w["task"].format(text=t[lang], example=t[f"example_{lang}"]) for t in c.raw["tasks"]]
    lines += ["", w["tools"]]
    lines += [f"- {t[f'say_{lang}']}" for t in c.raw["tools"]] or [w["no_tools"]]
    lines += ["", w["never"]] + [f"- {n}" for n in c.raw["limits"][f"never_{lang}"]]
    clicks = c.raw["limits"][f"needs_click_{lang}"]
    if clicks:
        lines += ["", w["clicks"]] + [f"- {n}" for n in clicks]
    if c.raw["talks_to"]:
        lines += ["", w["asks"]]
        lines += [f"- {_title_of(t['to'], lang)}: {t[f'for_{lang}']}" for t in c.raw["talks_to"]]
    return "\n".join(lines)


def view(application: str | None, language: str = "en") -> dict[str, Any] | None:
    """The contract for the GUI, in ``language`` (``de``/``en``)."""
    c = get(application)
    if c is None:
        return None
    lang = language_of(language)
    limits = c.raw["limits"]
    return {
        "key": c.key,
        "language": lang,
        "title": c.title(lang),
        "purpose": c.purpose(lang),
        "tasks": [{"text": t[lang], "example": t[f"example_{lang}"]} for t in c.raw["tasks"]],
        "tools": [t[f"say_{lang}"] for t in c.raw["tools"]],
        "never": list(limits[f"never_{lang}"]),
        "needs_click": list(limits[f"needs_click_{lang}"]),
        "talks_to": [
            {"to": _title_of(t["to"], lang), "for": t[f"for_{lang}"]} for t in c.raw["talks_to"]
        ],
    }


SELF_HINT = (
    "Who you are (say it in words when asked what you can do; never name a function, a tool "
    "group or a capability by its technical name):"
)


def prompt_block(application: str | None, language: str = "en") -> str:
    """The short self-description as a block in front of a chat turn ("" without a contract)."""
    text = short(application, language)
    return f"{SELF_HINT}\n{text}\n\n" if text else ""
