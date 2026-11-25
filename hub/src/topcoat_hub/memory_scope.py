"""Which notes a program may see (ARCHITECTURE D74).

A note is in **the core** (``application`` is null: who the person is and what they like, read by
every program) or in **one program's memory** (what Chat learned about the person's day, what
Development knows about a repository, what Administration knows about the machines). Recall in a
program reads the core and its own memory, never another program's.

Sensitive branches of the tag tree (:mod:`topcoat_hub.memory_tags`) narrow the core: a note of the
core with a tag below ``person/health`` is seen only where the person released that branch. The
tags of a note in a program's own memory never hide it from that program."""

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub.memory_tags import PROGRAMS, TagTree

# what is about the person and not about a place: the core by default
SHARED_KINDS = ("person", "preference", "style", "rule")
# what is always some program's own: a fact about a place, a day of work
PROGRAM_KINDS = ("operational",)
DEFAULT_APPLICATION = "chat"
APPLICATIONS = PROGRAMS
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


@dataclass(frozen=True)
class Scope:
    """Who is reading: a program, and the tags whose notes of the core are hidden from it."""

    application: str
    hidden: frozenset[uuid.UUID] = field(default_factory=frozenset)


def clean_application(value: Any) -> str | None:
    """An application key, or None (the core)."""
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    return text if KEY_RE.match(text) else None


async def scope_for(db: AsyncSession, user_id: uuid.UUID, application: str | None) -> Scope | None:
    """What a program reads; None (no limit) for work that is not a program's own, such as the
    consolidation of all notes or the person's own pages."""
    if not application:
        return None
    hidden = await TagTree(db, user_id).hidden_for(application)
    return Scope(application, frozenset(hidden))


# --- sorting what was learned before applications existed ---------------------------------------

_DEV = re.compile(
    r"\b(repo|repos|repository|repositories|branch|branches|commit|commits|pull request|pr|pytest|"
    r"unittest|github|workshop|migration|pipeline|linter|compile|compiler|merge|refactor\w*)\b",
    re.IGNORECASE,
)
_ADMIN = re.compile(
    r"\b(proxmox|vm|vms|docker|compose|container|containers|nextcloud|paperless|immich|keycloak|"
    r"router|server|dns|nas|backup|backups|homeassistant|home assistant|grafana|node|nodes|gpu|"
    r"llama\w*|uptime kuma|omada|vlan|firewall|ssh|certificate|ports?)\b",
    re.IGNORECASE,
)


# A sentence that names the person ("Der Nutzer bevorzugt …", "Mein Hund …") is about them; a
# sentence about a thing ("Themes werden über ein Manifest definiert") is not, whatever kind the
# model gave it.
_PERSON = re.compile(
    r"\b(?:nutzer\w*|benutzer\w*|user|users|operator\w*|person|maintainer|ich|mich|mir|mein\w*|"
    r"i|me|my)\b",
    re.IGNORECASE,
)


# A sentence with a subject of its own: it opens with an article or a preposition ("Die Taskleiste
# …", "Im Projekt …"). A note written the neutral way the extraction asks for ("Mag kurze
# Antworten", "Lebt in Hamburg") opens with its verb: its subject is the person.
_OWN_SUBJECT = re.compile(
    r"^\W*(?:der|die|das|den|dem|des|ein|eine|einen|einem|einer|es|im|in|auf|für|bei|zum|zur|"
    r"the|a|an|it|in|on|for)\b",
    re.IGNORECASE,
)


def about_the_person(text: str) -> bool:
    """Is the sentence about the person? It names them, or it has no subject of its own (the
    neutral form "Mag kurze Antworten"). Only those belong in the core when they were learned in
    a program's conversation."""
    return bool(_PERSON.search(text)) or not _OWN_SUBJECT.match(text)


def suggest_application(text: str) -> str | None:
    """Where a note about development or the machines probably belongs, from its words; None when
    nothing says. It is a suggestion the person confirms, never an assignment."""
    dev, admin = len(_DEV.findall(text)), len(_ADMIN.findall(text))
    if dev == 0 and admin == 0:
        return None
    return "dev" if dev >= admin and dev > 0 else "admin"
