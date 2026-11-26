"""The core (E3-06, ARCHITECTURE D38): the block at the very beginning of the system prompt,
before anything is searched — role, tone and style, rules of behaviour, a profile of the user, the
household's basic facts, the fixed system rules and what was discussed in the last chats (key
terms from their running summaries, :class:`RecentChat`).

Pure functions over notes (any object with the attributes of
:class:`topcoat_hub.models.MemoryItem`): what goes in under a hard token budget, in which order
it is cut when there is too much, and how the block is written. Nothing here reads a database, so
the budget and the cutting order are tested with plain objects. The block is *not* rebuilt for
every question (that would break the prompt cache): :mod:`topcoat_hub.core` builds it when a
conversation starts or when a core entry changed (:func:`stamp`) and keeps the text with the
conversation.
"""

import hashlib
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from topcoat_hub import memory_care

CHARS_PER_TOKEN = 3  # as the chat's estimate (chat.estimate_tokens): the hub has no tokenizer
OVERHEAD_TOKENS = 4
SMALL_CONTEXT = 8192  # a model context below this gets the small core
SMALL_TOKENS = 500

# how the budget is shared (system, style + rules, profile of the user, household)
SHARES = {"system": 0.20, "rules": 0.30, "profile": 0.25, "household": 0.25}
# what is cut first when the block is over its budget (the system rules never)
CUT_ORDER = ("profile", "household", "examples", "rules")

RULE_KINDS = ("style", "rule")
EXAMPLE_MAX_CHARS = 240  # "at most two short sentences"
STATEMENT_CHARS = 120  # one key term with its short explanation, as the block shows it
TITLE_CHARS = 40

HEADINGS = {
    "intro": "# Core — applies to every answer, before anything else",
    "system": "## System rules (fixed)",
    "frame": "## Frame of this program (it comes before the user's style, rules and voice where "
    "they differ)",
    "persona": "## Voice (persona)",
    "rules": "## How to answer (style and rules of this user)",
    "profile": "## About the user",
    "household": "## Household",
    "recent": "## Discussed last (key terms of the latest chats; background, bring it up only "
    "when the question fits)",
}


@dataclass(frozen=True)
class RecentChat:
    """What the block knows of a finished or running chat: the verified statements of its running
    summary, each written as ``key term: short explanation`` (chat_digest.py), oldest first."""

    id: str  # the conversation
    title: str
    day: str  # YYYY-MM-DD of the last message
    last_message_id: int
    statements: tuple[str, ...]


# A rule that relaxes a confirmation, an approval or a write right is never learned by the
# assistant on its own (D15, D48): only the user's own click makes one.
_LOOSENING = re.compile(
    r"without\s+(?:ask|confirm|approv|permission)|no\s+(?:confirmation|approval)"
    r"|(?:don'?t|never)\s+(?:ask|confirm)|automatic|on\s+my\s+behalf|self-?confirm"
    r"|ohne\s+(?:nachfrage|rückfrage|bestätigung|freigabe|fragen)"
    r"|nicht\s+(?:nachfragen|fragen|bestätigen)|nie\s+(?:nachfragen|fragen)"
    r"|freigabe|bestätigung|selbst\s+(?:ausführen|bestätigen|freigeben)"
    r"|(?:execut|run|ausführ|lösch|delet|restart|neustart).{0,40}(?:always|immer|alles)"
    r"|(?:always|immer|alles).{0,40}(?:execut|run|ausführ|lösch|delet|restart|neustart)",
    re.IGNORECASE,
)


def loosens_safety(text: str) -> bool:
    """True if a rule is about doing without asking, confirming or approving (either language)."""
    return bool(_LOOSENING.search(text or ""))


def tokens(text: str) -> int:
    """The estimate the chat uses for its budgets (3 characters per token, plus overhead)."""
    return math.ceil(len(text) / CHARS_PER_TOKEN) + OVERHEAD_TOKENS


def budget_for(context_length: int | None, configured: int) -> int:
    """The core's token budget: ``configured`` (``TOPCOAT_CORE_TOKENS``), at most 500 when the
    model's context is below 8k."""
    if context_length is not None and context_length < SMALL_CONTEXT:
        return min(configured, SMALL_TOKENS)
    return configured


def recent_budget(total: int, configured: int = 150) -> int:
    """Tokens for "discussed last": 150, scaled down with a small core (30 % of it at most)."""
    return max(min(configured, int(total * 0.3)), 40)


# --- which notes, in which order ----------------------------------------------------------------


def _score(item: Any, now: datetime, cfg: memory_care.CareConfig) -> float:
    """Reliability: how much a note deserves a place when there is not room for all. How often a
    note was recalled or used is no ranking input (D124: it measured the wording, not the note)."""
    return memory_care.reliability(item, now, cfg)


def ranked(items: Iterable[Any], now: datetime, cfg: memory_care.CareConfig) -> list[Any]:
    """Pinned first, then by reliability; a weak style hint after the others; ties by
    age then id, so the order is the same every time."""
    return sorted(
        items,
        key=lambda i: (
            not getattr(i, "core", False),
            bool(getattr(i, "weak", False)),
            -round(_score(i, now, cfg), 4),
            getattr(i, "created_at", None) or now,
            str(getattr(i, "id", "")),
        ),
    )


def clean_example(example: str | None) -> str | None:
    """An example of a style: one line, at most two short sentences / ``EXAMPLE_MAX_CHARS``."""
    if not example:
        return None
    text = " ".join(example.split())
    return text[:EXAMPLE_MAX_CHARS].rstrip() or None


_ADJECTIVES_ONLY_MAX_WORDS = 6


def is_weak_style(text: str, example: str | None) -> bool:
    """A style that is only adjectives ("freundlich, knapp", "friendly and short") says little: it
    is kept as a weak hint. A style with an example, or a real sentence, is not weak."""
    if example and example.strip():
        return False
    words = [w for w in text.replace(",", " ").replace(";", " ").split() if w]
    joiners = {"und", "and", "oder", "or", "aber", "but", "sehr", "very", "eher", "rather"}
    content = [w for w in words if w.lower() not in joiners]
    return len(content) <= _ADJECTIVES_ONLY_MAX_WORDS and not any(
        p in text for p in (".", ":", "!", "?", "\n")
    )


# --- the block ------------------------------------------------------------------------------------


@dataclass
class CoreBlock:
    text: str
    used: dict[str, int] = field(default_factory=dict)  # tokens per part
    budget: int = 0
    shares: dict[str, int] = field(default_factory=dict)  # tokens each part may take
    included: dict[str, list[str]] = field(default_factory=dict)  # ids per part
    cut: list[str] = field(default_factory=list)  # ids dropped for room, in the order they went
    examples_dropped: bool = False

    @property
    def total(self) -> int:
        return sum(self.used.values())


def _line(item: Any, with_example: bool) -> str:
    text = " ".join(str(item.text).split())
    kind = getattr(item, "kind", "")
    if kind == "style":
        weak = " (a hint only)" if getattr(item, "weak", False) else ""
        line = f"- Style{weak}: {text}"
        example = clean_example(getattr(item, "example", None))
        if with_example and example:
            line += f'  Example: "{example}"'
        return line
    return f"- {text}"


def _section(heading: str, lines: Sequence[str]) -> str:
    return heading + "\n" + "\n".join(lines) if lines else ""


def build(
    *,
    system: Sequence[str],
    rules: Sequence[Any],
    profile: Sequence[Any],
    household: Sequence[Any],
    recent: Sequence[RecentChat] = (),
    persona: str = "",
    frame: str = "",
    budget: int,
    recent_tokens: int = 150,
    now: datetime | None = None,
    cfg: memory_care.CareConfig = memory_care.DEFAULT,
) -> CoreBlock:
    """The core block for these notes under ``budget`` tokens (the system rules and the latest chats
    come on top of nothing: the system rules are the first claim on the budget, the latest chats
    have their own ``recent_tokens``).

    Each part takes at most its share of the budget (system 20 %, style + rules 30 %, profile 25 %,
    household 25 %), best notes first (pinned, then reliability x usefulness). If the whole block
    is still over the budget — the system rules alone may be longer than their share — it is cut in
    this order: profile, household, the examples of the styles, then style / rule notes; the system
    rules are never cut."""
    now = now or memory_care.utcnow()
    persona_section = _section(HEADINGS["persona"], [persona]) if persona.strip() else ""
    persona_tokens = tokens(persona_section) if persona_section else 0
    # the frame of the program (D74, short by its own limits) is never cut, like the system rules
    frame_section = _section(HEADINGS["frame"], [frame]) if frame.strip() else ""
    frame_tokens = tokens(frame_section) if frame_section else 0
    # the persona (already within its own cap, :mod:`personas`) and the frame take their room
    # first; the parts share what is left
    shares = {k: int(max(budget - persona_tokens - frame_tokens, 0) * v) for k, v in SHARES.items()}
    parts: dict[str, list[Any]] = {
        "rules": ranked(rules, now, cfg),
        "profile": ranked(profile, now, cfg),
        "household": ranked(household, now, cfg),
    }
    system_lines = [" ".join(s.split()) for s in system if s.strip()]
    keep: dict[str, list[Any]] = {k: [] for k in parts}
    for name, items in parts.items():
        used = tokens(HEADINGS[name])
        for item in items:
            cost = tokens(_line(item, True)) - OVERHEAD_TOKENS
            if used + cost > shares[name] and keep[name]:
                continue  # over its share (the first note of a part always gets in)
            keep[name].append(item)
            used += cost
    dropped_examples = False
    cut: list[str] = []

    def total() -> int:
        return (
            persona_tokens
            + frame_tokens
            + sum(part_tokens(k) for k in ("system", "rules", "profile", "household"))
        )

    def part_tokens(name: str) -> int:
        if name == "system":
            return tokens(_section(HEADINGS["system"], system_lines)) if system_lines else 0
        lines = [_line(i, not dropped_examples) for i in keep[name]]
        return tokens(_section(HEADINGS[name], lines)) if lines else 0

    # over the budget: profile, household, examples, rules — the system rules never
    for stage in CUT_ORDER:
        if total() <= budget:
            break
        if stage in ("profile", "household", "rules"):
            while keep[stage] and total() > budget:
                gone = keep[stage].pop()  # the least deserving first
                cut.append(str(getattr(gone, "id", "")))
        elif stage == "examples":
            dropped_examples = True
    lines = {k: [_line(i, not dropped_examples) for i in keep[k]] for k in keep}
    sections = [
        HEADINGS["intro"],
        _section(HEADINGS["system"], system_lines),
        frame_section,
        persona_section,
        _section(HEADINGS["rules"], lines["rules"]),
        _section(HEADINGS["profile"], lines["profile"]),
        _section(HEADINGS["household"], lines["household"]),
    ]
    used_tokens = {
        "system": part_tokens("system"),
        "rules": part_tokens("rules"),
        "profile": part_tokens("profile"),
        "household": part_tokens("household"),
        "persona": persona_tokens,
        "frame": frame_tokens,
    }
    recent_text, recent_ids, spent = _recent(recent, recent_tokens)
    if recent_text:
        sections.append(recent_text)
    used_tokens["recent"] = spent
    text = "\n\n".join(s for s in sections if s)
    return CoreBlock(
        text=text,
        used=used_tokens,
        budget=budget,
        shares={
            **shares,
            "recent": recent_tokens,
            "persona": persona_tokens,
            "frame": frame_tokens,
        },
        included={
            **{k: [str(getattr(i, "id", "")) for i in keep[k]] for k in keep},
            "recent": recent_ids,
        },
        cut=cut,
        examples_dropped=dropped_examples,
    )


def _statement(text: str) -> str:
    """One statement as a line part: whitespace collapsed, cut at ``STATEMENT_CHARS``."""
    text = " ".join(str(text).split())
    return text if len(text) <= STATEMENT_CHARS else text[: STATEMENT_CHARS - 1].rstrip() + "…"


def _recent(chats: Sequence[RecentChat], budget: int) -> tuple[str, list[str], int]:
    """The latest one or two chats as key terms within ``budget`` tokens. A chat is one line:
    its title and day, then the statements that fit, the newest first (where the chat ended is
    what a new chat needs most) and shown in the order they were made. A chat with nothing that
    fits is left out whole: a title alone says nothing."""
    lines: list[str] = []
    ids: list[str] = []
    spent = tokens(HEADINGS["recent"])
    shown = list(chats[:2])
    for index, chat in enumerate(shown):
        title = " ".join(chat.title.split())[:TITLE_CHARS]
        head = f"- {title} ({chat.day}): " if title else f"- ({chat.day}): "
        # each chat takes its share of what is left, so the newest cannot starve the one before
        share = (budget - spent) // (len(shown) - index)
        room = share * CHARS_PER_TOKEN - len(head)
        if room < 30:
            continue
        picked: list[str] = []
        for statement in reversed(chat.statements):
            part = _statement(statement)
            cost = len(part) + (2 if picked else 0)  # "; " between two
            if cost > room:
                break
            picked.insert(0, part)
            room -= cost
        if not picked:
            continue
        lines.append(head + "; ".join(picked))
        ids.append(chat.id)
        spent += tokens(lines[-1]) - OVERHEAD_TOKENS
    if not lines:
        return "", [], 0
    return _section(HEADINGS["recent"], lines), ids, spent


def stamp(
    *,
    budget: int,
    language: str,
    memory_enabled: bool,
    system: Iterable[tuple[str, int]],
    rules: Iterable[tuple[str, int, bool]],
    pinned: Iterable[tuple[str, int]],
    household: Iterable[tuple[str, int]],
    recent: Iterable[tuple[str, int, str]],
    persona: str = "",
    frame: str = "",
) -> str:
    """What the block was built for. The same stamp = the same block: it changes when a core entry
    changed (a version, a new or a removed one), the budget, the language or the memory switch —
    not when time passed or a fact that is not pinned was added (it joins the next conversation)."""
    h = hashlib.sha256()
    h.update(f"{budget}|{language}|{int(memory_enabled)}|{persona}|{frame}".encode())
    for group in (system, rules, pinned, household, recent):
        h.update(b"#")
        for entry in sorted(group, key=str):
            h.update(("|".join(str(x) for x in entry) + ";").encode())
    return h.hexdigest()[:40]
