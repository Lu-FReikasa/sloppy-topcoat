"""The class of a report: public, sensitive or critical (docs plan of 2026-11-09).

- ``public``: freely available information (the internet, documentation, general knowledge). A
  report of this class may pass between applications without a further approval.
- ``sensitive``: concrete facts about the operator's infrastructure: host names, addresses, the
  apps in use. It may pass between applications too, but only on purpose: it is marked, and the
  handover says so.
- ``critical``: secrets (passwords, keys, tokens, credentials). It passes only with an explicit
  approval, each time.

The class is decided here from the text, never by the model alone: a model may suggest a class,
and :func:`settle` keeps the higher of the two. A secret is never stored: :func:`hide_secrets`
replaces it with the marker, so the report keeps the place and not the value.
"""

import ipaddress
import re
from collections.abc import Iterable

from topcoat_hub.topcoat_self import _SECRET_TEXT, HIDDEN

PUBLIC, SENSITIVE, CRITICAL = "public", "sensitive", "critical"
ORDER = (PUBLIC, SENSITIVE, CRITICAL)

# a home-network name ends in one of these labels (redaction.PRIVATE_TLDS)
_PRIVATE_NAME = re.compile(
    r"\b[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9][a-z0-9\-]*)*\.(?:home\.arpa|fritz\.box|localdomain|"
    r"intranet|internal|private|corp|home|lan|local)\b",
    re.I,
)
_IPV4 = re.compile(
    r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\d|\.\d)"
)  # a full stop may end a sentence


def _has_address(text: str) -> bool:
    """Any IPv4 address is the operator's infrastructure: a sensitive fact, public or not."""
    for found in _IPV4.findall(text):
        try:
            ipaddress.IPv4Address(found)
        except ValueError:
            continue
        return True
    return False


def hide_secrets(text: str) -> str:
    """The text with every secret replaced by the marker; the value itself is not kept."""
    return _SECRET_TEXT.sub(HIDDEN, text)


def classify(text: str, known_names: Iterable[str] = ()) -> str:
    """The class of a text. ``known_names`` are the operator's machines and apps (the inventory),
    which make a plain word sensitive too."""
    if _SECRET_TEXT.search(text):
        return CRITICAL
    lowered = text.casefold()
    if _PRIVATE_NAME.search(text) or _has_address(text):
        return SENSITIVE
    for name in known_names:
        if len(name) < 3:
            continue  # a short name ("pi") would match half of any text
        pattern = rf"(?<![\w\-]){re.escape(name.casefold())}(?![\w\-])"
        if re.search(pattern, lowered):
            return SENSITIVE
    return PUBLIC


def settle(suggested: str, found: str) -> str:
    """The class the report keeps: the higher of the model's suggestion and what the text shows.
    A suggestion can raise a class, never lower it."""
    if suggested not in ORDER:
        suggested = PUBLIC
    return max(suggested, found, key=ORDER.index)


def may_pass(cls: str, approved: bool) -> bool:
    """Whether a report of this class may pass to another application now. Critical reports pass
    only with an approval given for that handover."""
    if cls == CRITICAL:
        return approved
    return cls in ORDER
