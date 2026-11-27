"""An offer to change something that no action backs (issue #223; live 2026-11-27: the assistant
said it can only read the Omada controller, asked "Shall I enable the setting for you?", and after
"yes" said it cannot).

The assistant may offer a change only when a state-changing action for it is on a node's whitelist
for this user (the actions the hub assigns, D15/D83: proposed, then run after the person's click).
The prompt says so; this is the hub's check of the answer, without a model: an answer that ends on
an offer to change something (enable, restart, delete, ...) keeps it only when an available action
does that kind of change. Otherwise the offer is cut, like the hub's other notes on an answer
(``chat.source_notes``). An offer to explain or to look is never touched: those need no action.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any

from topcoat_hub import chat_loop

# the kinds of change an offer can ask for: (kind, the words of an offer, the words of an action's
# name or description that do it). The offer words are the forms an offer uses ("Shall I enable",
# "Soll ich ... aktivieren", "dass ich es aktiviere"), never a participle ("whether it is
# enabled", "ob es aktiviert ist" asks to look). Creating, writing or fixing are left out on
# purpose: "Shall I write you a script?" or "Soll ich einen Bericht erstellen?" are no changes.
_KINDS: tuple[tuple[str, re.Pattern[str], frozenset[str]], ...] = (
    (
        "restart",
        re.compile(
            r"\b(?:restart|reboot|neu\s*(?:starten|starte|booten)|neustarten|rebooten)\b", re.I
        ),
        frozenset({"restart", "reboot"}),
    ),
    (
        "switch",
        re.compile(
            r"\b(?:enable|disable|activate|deactivate|turn\s+(?:\w+\s+)?(?:on|off)|"
            r"switch\s+(?:\w+\s+)?(?:on|off)|toggle|(?:de)?aktivieren|(?:de)?aktiviere|"
            r"(?:ein|aus|an|ab|frei)schalten?|"
            r"schalte\b[^.?!]{0,40}\b(?:ein|aus|an|ab))\b",
            re.I,
        ),
        frozenset({"enable", "disable", "activate", "deactivate", "switch", "toggle"}),
    ),
    (
        "stop",
        re.compile(
            r"\b(?:stop|shut\s+down|stoppen|stoppe|anhalten|herunterfahren|beenden)\b", re.I
        ),
        frozenset({"stop", "shutdown"}),
    ),
    (
        "install",
        re.compile(
            r"\b(?:install|uninstall|upgrade|installieren|installiere|deinstallieren|"
            r"aktualisieren|aktualisiere|upgraden)\b",
            re.I,
        ),
        frozenset({"install", "uninstall", "upgrade", "update"}),
    ),
    (
        "delete",
        re.compile(r"\b(?:delete|remove|löschen|lösche|entfernen|entferne)\b", re.I),
        frozenset({"delete", "remove"}),
    ),
    (
        "configure",
        re.compile(
            r"\b(?:change|set(?!\s+up)|configure|reset|apply|rename|ändern|ändere|setzen|setze|"
            r"einstellen|umstellen|stelle\b[^.?!]{0,40}\b(?:ein|um)|konfigurieren|konfiguriere|"
            r"zurücksetzen|anwenden|umbenennen)\b",
            re.I,
        ),
        frozenset(
            {"change", "set", "configure", "reset", "apply", "patch", "edit", "write", "rename"}
        ),
    ),
    (
        "block",
        re.compile(
            r"\b(?:block|unblock|kick|disconnect|sperren|sperre|entsperren|blockieren|trennen)\b",
            re.I,
        ),
        frozenset({"block", "unblock", "kick", "disconnect"}),
    ),
)

# "Shall I explain how to enable it?", "Soll ich dir zeigen, wie du es aktivierst?": the person
# makes the change, the assistant only explains it
_EXPLAIN = re.compile(
    r"\b(?:erkl[äa]r\w*|beschreib\w*|zeig\w*(?:\s+dir)?\s*,?\s*wie|anleitung\w*|"
    r"schritt\s+f[üu]r\s+schritt|explain\w*|describe|show\s+you\s+how|walk\s+you\s+through|"
    r"tell\s+you\s+how|sagen,?\s+wie)\b",
    re.I,
)
_WORD = re.compile(r"[a-zäöüß]+", re.I)


def change_offer(text: str) -> tuple[str, set[str]]:
    """The offer to change something the answer ends with and the kinds of change it asks for;
    ``("", set())`` when it ends on no offer, or on one to explain or to look."""
    offer = chat_loop.closing_offer(text)
    if not offer or _EXPLAIN.search(offer):
        return "", set()
    kinds = {kind for kind, words, _ in _KINDS if words.search(offer)}
    return (offer, kinds) if kinds else ("", set())


def _action_words(action: Mapping[str, Any]) -> set[str]:
    """The words of an action's name (``docker.restart``: docker, restart) and description."""
    name = str(action.get("capability") or "")
    text = f"{name.replace('.', ' ').replace('_', ' ')} {action.get('description') or ''}"
    words = {w.lower() for w in _WORD.findall(text)}
    # "restarted", "enabled", "deletes": the form without the ending counts too
    stems = {w[:-1] for w in words if w.endswith(("s", "d"))}
    stems |= {w[:-2] for w in words if w.endswith(("ed", "es"))}
    return words | stems


def backed(kinds: set[str], actions: Iterable[Mapping[str, Any]]) -> bool:
    """Does one of the available actions do one of these kinds of change?"""
    does = {kind: verbs for kind, _, verbs in _KINDS}
    return any(_action_words(a) & does[k] for a in actions for k in kinds)


def cut_note(language: str) -> str:
    """What stands in place of an offer that was the whole answer."""
    if language == "de":
        return "Das kann ich hier nicht selbst ändern: dafür steht mir keine Aktion zur Verfügung."
    return "I cannot change this myself here: no action for it is available to me."


def without_unbacked_offer(
    text: str, actions: Iterable[Mapping[str, Any]], language: str, keep: bool = False
) -> str:
    """The answer without its closing offer to change something when no available action does
    that kind of change; else the answer as it is. ``keep``: the conversation has its own way to
    make changes (a project's change sets, a change that already waits for the click)."""
    if keep:
        return text
    offer, kinds = change_offer(text)
    if not offer or backed(kinds, actions):
        return text
    cut = text.rstrip()
    rest = cut[: len(cut) - len(offer)].rstrip()
    return rest if rest else cut_note(language)


def labels(actions: Iterable[Mapping[str, Any]], limit: int = 8) -> tuple[str, ...]:
    """The actions in words for the system prompt: the first sentence of each description (its
    machine), without technical names, at most ``limit``."""
    out: list[str] = []
    for a in actions:
        desc = str(a.get("description") or a.get("capability") or "").strip()
        first = re.split(r"(?<=[.:;])\s|\s\(", desc, maxsplit=1)[0].rstrip(".:;")[:100]
        label = f"{first} ({a['node']})" if a.get("node") else first
        if first and label not in out:
            out.append(label)
    return tuple(out[:limit])
