"""The mirror of the infrastructure profiles (E3-06, ARCHITECTURE D38): the notes in the folder
``steckbriefe/`` of a knowledge source — one file per host or service, a header line
``status · verified · aliases · kind`` and a line ``prüfen:`` — become ``operational`` notes with
source type ``kb``. **The file always wins**: a mirrored note is read-only for the assistant (the
store refuses its writes), changes only through :meth:`Mirror.apply`, is rebuilt when the file's
hash changed, and is archived when the file is gone. The assistant writes no file: a change it would
like is a *proposal* with Markdown to copy.

A ``prüfen:`` line (on its own line or at the end of a paragraph) is kept as a hint
(``source_ref.check_hint``), never turned into a ``verify_with``. The care run uses it as the
task of a live check when the profile's ``host`` has a tool node that is connected right now;
without such a node the note is simply left alone.
"""

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import knowledge_rules as rules
from topcoat_hub.memory import MemoryService
from topcoat_hub.memory_store import MemoryStore, normalise_text
from topcoat_hub.models import MemoryItem

log = logging.getLogger(__name__)

FOLDER = "steckbriefe"
PARSER_VERSION = 2  # a mirrored note written by an older parser is read again (known() hides it)
NOTE_MAX = 300
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})|(\d{1,2})\.(\d{1,2})\.(\d{4})")
_KEYED = re.compile(r"^\s*(?P<k>[A-Za-zÄÖÜäöüß_]+)\s*[:=]\s*(?P<v>.*)$")
_CHECK = re.compile(r"^\s*[-*]?\s*(?:prüfen|pruefen|check)\s*:\s*(?P<v>.+)$", re.IGNORECASE)
_CHECK_INLINE = re.compile(r"(?:^|\s)(?:prüfen|pruefen|check)\s*:\s*", re.IGNORECASE)
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_KEYS = {
    "status": "status",
    "verified": "verified",
    "geprüft": "verified",
    "geprueft": "verified",
    "aliases": "aliases",
    "alias": "aliases",
    "auch": "aliases",
    "host": "host",
    "kind": "kind",
    "art": "kind",
    "typ": "kind",
    "type": "kind",
}
_POSITIONAL = ("status", "verified", "aliases", "kind")


@dataclass
class Profile:
    path: str
    title: str
    status: str = ""
    verified: datetime | None = None
    aliases: list[str] = field(default_factory=list)
    kind: str = ""
    host: str = ""
    summary: str = ""
    check_hint: str = ""

    def note_text(self) -> str:
        """The mirrored note: who it is, its state, what it says — one short paragraph."""
        head = self.title
        facts = ", ".join(
            x for x in (self.kind, f"status {self.status}" if self.status else "") if x
        )
        text = f"{head} ({facts})" if facts else head
        if self.aliases:
            text += f", also called {', '.join(self.aliases[:4])}"
        if self.summary:
            text += f": {self.summary}"
        return normalise_text(text)[:NOTE_MAX].rstrip()


def parse_date(text: str) -> datetime | None:
    m = _DATE.search(text)
    if not m:
        return None
    try:
        if m.group(1):
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=UTC)
        return datetime(int(m.group(6)), int(m.group(5)), int(m.group(4)), tzinfo=UTC)
    except ValueError:
        return None


def parse(path: str, text: str) -> Profile | None:
    """The profile in a file, or ``None`` if there is nothing to mirror (no title, no header).

    The header is the first line with at least three `` · `` separated parts (after the title):
    positional (status, verified, aliases, kind) or labelled (``status: aktiv · verified: …``)."""
    title = ""
    header: list[str] | None = None
    summary_lines: list[str] = []
    check = ""
    in_fence = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        h = _HEADING.match(line)
        if h:
            if not title:
                title = h.group(2).strip()
                continue
            if header is not None and summary_lines:
                break  # the first section after the summary: that is enough
            continue
        c = _CHECK.match(line)
        if c:
            check = c.group("v").strip()
            continue
        inline = _CHECK_INLINE.split(line)
        if len(inline) > 1:  # "… text prüfen: `cmd`, `cmd`" — the hint ends the paragraph or line
            check = inline[-1].strip()
            line = inline[0].strip()
            if not line:
                continue
        if header is None and line.count("·") >= 2:
            header = [p.strip() for p in line.split("·")]
            continue
        if header is not None and line and not line.startswith(("<!--", "|")):
            summary_lines.append(line.lstrip("-*> ").strip())
            if sum(len(x) for x in summary_lines) > 260:
                break
    if not title or header is None:
        return None
    profile = Profile(path=path, title=title, check_hint=check[:200])
    for i, part in enumerate(header):
        m = _KEYED.match(part)
        key, value = (
            (_KEYS.get(m.group("k").lower()), m.group("v").strip())
            if m and _KEYS.get(m.group("k").lower())
            else (_POSITIONAL[i] if i < len(_POSITIONAL) else None, part)
        )
        if key == "status":
            profile.status = value
        elif key == "verified":
            profile.verified = parse_date(value)
        elif key == "aliases":
            profile.aliases = [a.strip() for a in re.split(r"[,;]", value) if a.strip()]
        elif key == "kind":
            profile.kind = value
        elif key == "host":
            profile.host = value.strip().lower()
    if not profile.host and profile.kind.lower().startswith("host"):
        profile.host = re.split(r"\s+[—-]\s+", profile.title, maxsplit=1)[0].strip().lower()
    profile.summary = " ".join(summary_lines)[:200].rstrip()
    return profile


class Mirror:
    """Brings the mirrored notes of one user in line with the files of a source."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], memory: MemoryService
    ) -> None:
        self.sessionmaker = sessionmaker
        self.memory = memory

    async def known(self, owner: uuid.UUID, source: str) -> dict[str, str]:
        """``path -> sha256`` of the files already mirrored for this source (active or archived)."""
        async with self.sessionmaker() as db:
            return {
                path: (item.source_ref or {}).get("sha256", "")
                if (item.source_ref or {}).get("pv") == PARSER_VERSION
                else ""  # written by an older parser: read the file again once
                for path, item in (await self._items(db, owner, source)).items()
            }

    async def _items(
        self, db: AsyncSession, owner: uuid.UUID, source: str
    ) -> dict[str, MemoryItem]:
        rows = await db.scalars(
            select(MemoryItem).where(
                MemoryItem.user_id == owner,
                MemoryItem.source_type == "kb",
                MemoryItem.source_ref["source"].astext == source,
            )
        )
        return {(i.source_ref or {}).get("path", ""): i for i in rows}

    async def apply(
        self,
        owner: uuid.UUID,
        source: str,
        files: dict[str, str],
        vanished: list[str],
    ) -> dict[str, int]:
        """``files``: path -> text of the profiles that are new or changed (read just now);
        ``vanished``: profiles whose file is gone. Idempotent: the same hash changes nothing, no
        duplicates; a vanished file archives its note; a file that comes back wakes it."""
        stats = {
            "created": 0,
            "updated": 0,
            "archived": 0,
            "restored": 0,
            "unchanged": 0,
            "blocked": 0,
        }
        async with self.sessionmaker() as db:
            store = MemoryStore(db, owner)
            mirrored = await self._items(db, owner, source)
            for path, body in files.items():
                profile = parse(path, body)
                if profile is None:
                    continue
                sha = hashlib.sha256(body.encode()).hexdigest()
                text, rule = rules.vet(profile.note_text())
                if text is None:
                    stats["blocked"] += 1
                    log.warning(
                        "mirror: %s not mirrored, the secret filter rule %r matched", path, rule
                    )
                    continue
                ref = {
                    "path": path,
                    "sha256": sha,
                    "source": source,
                    "verified": profile.verified.isoformat() if profile.verified else None,
                    "check_hint": profile.check_hint,
                    "host": profile.host,
                    "aliases": profile.aliases,
                    "pv": PARSER_VERSION,
                }
                item = mirrored.get(path)
                if (
                    item is not None
                    and (item.source_ref or {}).get("sha256") == sha
                    and (item.source_ref or {}).get("pv") == PARSER_VERSION
                ):
                    if item.status == "archived":
                        await store.change(
                            item.id,
                            reason="the note file is back",
                            actor="kb_sync",
                            status="active",
                        )
                        stats["restored"] += 1
                    else:
                        stats["unchanged"] += 1
                    continue
                vec, embedder = await self.memory.embed_one(text)
                if item is None:
                    created = await store.add(
                        text,
                        embedding=vec,
                        embedder=embedder,
                        evidence="doc",
                        confidence=0.9,
                        source_type="kb",
                        source_ref=ref,
                        reason=f"mirrored from {path}",
                        actor="kb_sync",
                        kind="operational",
                        last_verified_at=profile.verified,
                    )
                    created.verified_count = 0  # a file's own "verified" is no live check
                    stats["created"] += 1
                else:
                    changed = await store.change(
                        item.id,
                        reason=f"the note file {path} changed",
                        actor="kb_sync",
                        text_=text,
                        embedding=vec,
                        embedder=embedder,
                        status="active",
                    )
                    assert changed is not None  # noqa: S101
                    changed.source_ref = ref
                    changed.last_verified_at = profile.verified
                    changed.verified_count = 0
                    stats["updated"] += 1
            for path in vanished:
                item = mirrored.get(path)
                if item is not None and item.status != "archived":
                    await store.change(
                        item.id,
                        reason=f"the note file {path} is gone",
                        actor="kb_sync",
                        status="archived",
                    )
                    stats["archived"] += 1
            await db.commit()
        return stats
