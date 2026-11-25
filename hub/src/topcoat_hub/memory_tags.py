"""The tag tree of a person's memory (ARCHITECTURE D74).

Every note carries a few tags that say what it is about; the tags stand in one tree per person,
shared by all programs (the notes are not shared, the words for topics are). The top level is
fixed (:data:`ROOTS`); below it the person, and an assistant when no tag fits, add tags. A tag an
assistant made is marked unreviewed until the person has looked at it.

Three things are inherited down the tree, so they are set once on a branch:

* ``responsible`` — the program that decides when notes about this topic disagree
  (``infrastructure`` → Administration), or ``person`` (the person decides: a question);
* ``sensitive`` — the notes of the core with such a tag are hidden from every program …
* ``released`` — … except the programs listed here.

Personal data (D20): a :class:`TagTree` is made for one person, and every statement it runs filters
by that person's id."""

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import applications
from topcoat_hub.models import MemoryItem, MemoryItemTag, MemoryTag

PERSON = "person"  # the responsible "program" that is the person: a disagreement is a question
PROGRAMS = applications.keys()  # proto/ui/applications.json
RESPONSIBLE = (PERSON, *PROGRAMS)

# The fixed top level: name, responsible, description. Their names are keys (the page translates
# them); they cannot be renamed, moved or deleted.
ROOTS: tuple[tuple[str, str, str], ...] = (
    ("person", PERSON, "Who the person is: family, home, work, health, life"),
    ("preferences", PERSON, "How the person likes things and wants to be answered"),
    ("everyday", "chat", "Day-to-day matters, plans, errands, small talk"),
    ("knowledge", "research", "Topics, sources and what was found out"),
    ("development", "dev", "Repositories, code, tests, the workshop"),
    ("infrastructure", "admin", "Servers, devices, services, the network"),
    (
        "project",
        "project",
        "Projects being planned: description, the numbered tree, open questions",
    ),
)
# Seeded below the roots: the old default "hidden everywhere" tags, now a sensitive branch.
SEEDED: tuple[tuple[str, str, str], ...] = (
    ("health", "person", "Health, illness, medication"),
    ("finance", "person", "Money, accounts, contracts"),
    ("private", "person", "What the person wants kept to themselves"),
)
SENSITIVE_SEEDS = ("health", "finance", "private")
ROOT_NAMES = tuple(r[0] for r in ROOTS)
ROOT_OF_PROGRAM = {resp: name for name, resp, _ in ROOTS if resp != PERSON}

MAX_DEPTH = 4  # a root and three levels below it
MAX_TAGS_PER_NOTE = 6
MAX_NEW_PER_NOTE = 2
NAME_MAX = 30
DESCRIPTION_MAX = 200
NAME_RE = re.compile(r"^[^\W_][\w-]{0,29}$")
_FOLD = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "ß": "s", "-": "", "_": ""})
_PLURAL = ("es", "en", "s", "e", "n")


class TagError(ValueError):
    """A change the tree does not allow; ``code`` names it for the API."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def clean_name(value: Any) -> str | None:
    """A tag name as it is stored: lower case, a dash for a space, letters, digits, ``-`` and
    ``_``; None when nothing usable is left."""
    name = re.sub(r"\s+", "-", str(value or "").strip().lower())[:NAME_MAX].strip("-_")
    return name if NAME_RE.match(name) else None


def fold(name: str) -> str:
    """The comparison key of a tag: ``Container``, ``containers`` and ``container`` are one tag,
    and so are ``Geräte`` and ``geraet``."""
    key = name.casefold().replace("ae", "a").replace("oe", "o").replace("ue", "u")
    key = key.translate(_FOLD)
    for suffix in _PLURAL:
        if len(key) > len(suffix) + 3 and key.endswith(suffix):
            return key[: -len(suffix)]
    return key


def default_tag(kind: str, application: str | None) -> str:
    """The tag a note gets when nobody gave it one: the core by its kind, a program's note the
    root of that program's field."""
    if application is None:
        return "person" if kind == "person" else "preferences"
    return ROOT_OF_PROGRAM.get(application, "everyday")


@dataclass(frozen=True)
class Effective:
    responsible: str
    sensitive: bool
    released: tuple[str, ...]


class TagTree:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        self.db = db
        self.user_id = user_id
        self._tags: dict[uuid.UUID, MemoryTag] | None = None

    # --- reading ----------------------------------------------------------------------------

    async def ensure(self) -> list[MemoryTag]:
        """The person's tags; the fixed top level and the seeded branches on first use. A root
        added to ``ROOTS`` later is added for the people who already have the others."""
        tags = await self.all()
        roots = {t.name: t for t in tags if t.parent_id is None}
        missing = [r for r in ROOTS if r[0] not in roots]
        if not missing:
            return tags
        first = not roots
        for name, responsible, description in missing:
            tag = MemoryTag(
                user_id=self.user_id,
                name=name,
                responsible=responsible,
                description=description,
                created_by="seed",
                sensitive=False,
            )
            self.db.add(tag)
            roots[name] = tag
        await self.db.flush()
        if first:
            for name, parent, description in SEEDED:
                if await self.by_name(name) is None:
                    self.db.add(
                        MemoryTag(
                            user_id=self.user_id,
                            parent_id=roots[parent].id,
                            name=name,
                            description=description,
                            created_by="seed",
                            sensitive=name in SENSITIVE_SEEDS or None,
                        )
                    )
            await self.db.flush()
        self._tags = None
        return await self.all()

    async def all(self) -> list[MemoryTag]:
        if self._tags is None:
            rows = await self.db.scalars(
                select(MemoryTag)
                .where(MemoryTag.user_id == self.user_id)
                .order_by(MemoryTag.created_at, MemoryTag.name)
            )
            self._tags = {t.id: t for t in rows}
        return list(self._tags.values())

    async def get(self, tag_id: uuid.UUID) -> MemoryTag | None:
        await self.all()
        assert self._tags is not None  # noqa: S101
        return self._tags.get(tag_id)

    async def by_name(self, name: str) -> MemoryTag | None:
        wanted = clean_name(name)
        if wanted is None:
            return None
        for t in await self.all():
            if t.name == wanted:
                return t
        return None

    async def like(self, name: str) -> MemoryTag | None:
        """The tag that is this name, or means the same (its folded key)."""
        exact = await self.by_name(name)
        if exact is not None:
            return exact
        key = fold(name)
        return next((t for t in await self.all() if fold(t.name) == key), None)

    def _parent(self, tag: MemoryTag) -> MemoryTag | None:
        assert self._tags is not None  # noqa: S101
        return self._tags.get(tag.parent_id) if tag.parent_id else None

    async def path(self, tag: MemoryTag) -> list[MemoryTag]:
        """The tag and its ancestors, root first."""
        await self.all()
        out = [tag]
        seen = {tag.id}
        node = self._parent(tag)
        while node is not None and node.id not in seen:
            out.append(node)
            seen.add(node.id)
            node = self._parent(node)
        return list(reversed(out))

    async def depth(self, tag: MemoryTag) -> int:
        return len(await self.path(tag))

    async def effective(self, tag: MemoryTag) -> Effective:
        """What the tag inherits: the nearest value set on it or an ancestor."""
        responsible: str | None = None
        sensitive: bool | None = None
        released: list[str] | None = None
        for node in reversed(await self.path(tag)):
            responsible = responsible if responsible is not None else node.responsible
            sensitive = sensitive if sensitive is not None else node.sensitive
            released = released if released is not None else node.released
        return Effective(responsible or PERSON, bool(sensitive), tuple(released or ()))

    async def subtree(self, tag: MemoryTag) -> set[uuid.UUID]:
        """The tag and everything below it."""
        tags = await self.all()
        children: dict[uuid.UUID | None, list[MemoryTag]] = {}
        for t in tags:
            children.setdefault(t.parent_id, []).append(t)
        out: set[uuid.UUID] = set()
        todo = [tag.id]
        while todo:
            current = todo.pop()
            if current in out:
                continue
            out.add(current)
            todo.extend(c.id for c in children.get(current, []))
        return out

    async def hidden_for(self, application: str | None) -> set[uuid.UUID]:
        """The tags whose notes of the core this program must not see: sensitive and not released
        for it. None (no program: the person's own page, the care) hides nothing."""
        if application is None:
            return set()
        out: set[uuid.UUID] = set()
        for t in await self.all():
            eff = await self.effective(t)
            if eff.sensitive and application not in eff.released:
                out.add(t.id)
        return out

    async def responsible_for(self, tag_ids: Iterable[uuid.UUID]) -> str | None:
        """Who decides about a note with these tags: the deepest tag decides; tags of the same
        depth that name different programs leave it open (None). A sensitive tag always goes to
        the person."""
        best: list[tuple[int, str]] = []
        for tag_id in tag_ids:
            tag = await self.get(tag_id)
            if tag is None:
                continue
            eff = await self.effective(tag)
            if eff.sensitive:
                return PERSON
            best.append((await self.depth(tag), eff.responsible))
        if not best:
            return None
        deepest = max(d for d, _ in best)
        found = {r for d, r in best if d == deepest}
        return found.pop() if len(found) == 1 else None

    # --- what a note carries -------------------------------------------------------------------

    async def tags_of(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[MemoryTag]]:
        out: dict[uuid.UUID, list[MemoryTag]] = {i: [] for i in item_ids}
        if not item_ids:
            return out
        await self.all()
        assert self._tags is not None  # noqa: S101
        rows = await self.db.execute(
            select(MemoryItemTag.item_id, MemoryItemTag.tag_id).where(
                MemoryItemTag.user_id == self.user_id,
                MemoryItemTag.item_id.in_(list(item_ids)),
            )
        )
        for item_id, tag_id in rows.all():
            tag = self._tags.get(tag_id)
            if tag is not None:
                out.setdefault(item_id, []).append(tag)
        for tags in out.values():
            tags.sort(key=lambda t: t.name)
        return out

    async def set_item_tags(self, item_id: uuid.UUID, tag_ids: Sequence[uuid.UUID]) -> None:
        """Replace the tags of one of the person's notes."""
        owned = await self.db.scalar(
            select(MemoryItem.id).where(
                MemoryItem.id == item_id, MemoryItem.user_id == self.user_id
            )
        )
        if owned is None:
            return
        await self.db.execute(
            delete(MemoryItemTag).where(
                MemoryItemTag.item_id == item_id, MemoryItemTag.user_id == self.user_id
            )
        )
        await self.add_item_tags(item_id, tag_ids)

    async def add_item_tags(self, item_id: uuid.UUID, tag_ids: Sequence[uuid.UUID]) -> None:
        known = {t.id for t in await self.all()}
        rows = [
            {"item_id": item_id, "tag_id": t, "user_id": self.user_id}
            for t in dict.fromkeys(tag_ids)
            if t in known
        ][:MAX_TAGS_PER_NOTE]
        if rows:
            await self.db.execute(insert(MemoryItemTag).values(rows).on_conflict_do_nothing())

    async def resolve(
        self,
        names: Iterable[Any],
        *,
        by: str,
        allow_new: bool,
        fallback: str | None = None,
    ) -> list[uuid.UUID]:
        """Tag ids for the names an assistant or the person gave. A known name (or one that means
        the same) is used as it is. With ``allow_new`` a name written ``parent/name`` below a known
        tag is made (at most :data:`MAX_NEW_PER_NOTE`, never a new top level, never deeper than
        :data:`MAX_DEPTH`); anything else is dropped. ``fallback``: the tag when none is left."""
        await self.ensure()
        out: list[uuid.UUID] = []
        made = 0
        for raw in names:
            if not isinstance(raw, str):
                continue
            parent_name, _, leaf = raw.strip().rpartition("/")
            parent_name = parent_name.rsplit("/", 1)[-1] if parent_name else ""
            found = await self.like(leaf)
            if found is None and allow_new and parent_name and made < MAX_NEW_PER_NOTE:
                parent = await self.like(parent_name)
                if parent is not None and clean_name(leaf):
                    try:
                        found = await self.create(leaf, parent.id, by=by)
                        made += 1
                    except TagError:
                        found = None
            if found is not None and found.id not in out:
                out.append(found.id)
            if len(out) >= MAX_TAGS_PER_NOTE:
                break
        if not out and fallback:
            tag = await self.by_name(fallback)
            if tag is not None:
                out.append(tag.id)
        return out

    async def counts(self) -> dict[uuid.UUID, int]:
        """How many live notes carry each tag (directly)."""
        rows = await self.db.execute(
            select(MemoryItemTag.tag_id, func.count())
            .join(MemoryItem, MemoryItem.id == MemoryItemTag.item_id)
            .where(
                MemoryItemTag.user_id == self.user_id,
                MemoryItem.status.in_(("active", "disputed")),
            )
            .group_by(MemoryItemTag.tag_id)
        )
        return {tag_id: n for tag_id, n in rows.all()}

    async def listing(self, application: str | None, limit: int = 80) -> str:
        """The tree as an assistant sees it when it tags a note: one line per tag, ``a/b/c — what
        it is``. The most used tags first when there are many; a sensitive tag a person made that
        is not released for this program is left out (its name alone can say too much)."""
        tags = await self.all()
        counts = await self.counts()
        hidden = await self.hidden_for(application)
        shown = [
            t
            for t in tags
            if t.parent_id is None or not (t.id in hidden and t.created_by != "seed")
        ]
        shown.sort(key=lambda t: (t.parent_id is not None, -counts.get(t.id, 0), t.name))
        lines = []
        for t in shown[:limit]:
            path = "/".join(n.name for n in await self.path(t))
            lines.append(f"{path} — {t.description}" if t.description else path)
        return "\n".join(sorted(lines))

    # --- changing the tree ---------------------------------------------------------------------

    async def create(
        self,
        name: str,
        parent_id: uuid.UUID | None,
        *,
        by: str,
        description: str = "",
    ) -> MemoryTag:
        clean = clean_name(name)
        if clean is None:
            raise TagError("name", "a tag is a short word: letters, digits, - and _")
        if parent_id is None:
            raise TagError("top_level", "the top level is fixed; a tag needs a parent")
        parent = await self.get(parent_id)
        if parent is None:
            raise TagError("parent", "unknown parent tag")
        if await self.depth(parent) >= MAX_DEPTH:
            raise TagError("too_deep", f"the tree is at most {MAX_DEPTH} levels deep")
        same = await self.like(clean)
        if same is not None:
            raise TagError("exists", f"the tag {same.name!r} exists already")
        tag = MemoryTag(
            user_id=self.user_id,
            parent_id=parent.id,
            name=clean,
            description=" ".join(description.split())[:DESCRIPTION_MAX],
            created_by=by,
            reviewed=by != "agent",
        )
        self.db.add(tag)
        await self.db.flush()
        await self.db.refresh(tag)
        assert self._tags is not None  # noqa: S101 - filled by get() above
        self._tags[tag.id] = tag
        return tag

    async def update(self, tag_id: uuid.UUID, **changes: Any) -> MemoryTag:
        """Rename, move or describe a tag, or set what it hands down. A top-level tag keeps its
        name and place; ``responsible``, ``sensitive`` and ``released`` may be set anywhere."""
        tag = await self.get(tag_id)
        if tag is None:
            raise TagError("not_found", "unknown tag")
        root = tag.parent_id is None
        if "name" in changes:
            clean = clean_name(changes["name"])
            if clean is None:
                raise TagError("name", "a tag is a short word: letters, digits, - and _")
            if clean != tag.name:
                if root:
                    raise TagError("top_level", "the top level keeps its names")
                other = await self.like(clean)
                if other is not None and other.id != tag.id:
                    raise TagError("exists", f"the tag {other.name!r} exists already")
                tag.name = clean
        if "parent_id" in changes and changes["parent_id"] != tag.parent_id:
            if root:
                raise TagError("top_level", "the top level stays where it is")
            parent = await self.get(changes["parent_id"]) if changes["parent_id"] else None
            if parent is None:
                raise TagError("parent", "unknown parent tag")
            if parent.id in await self.subtree(tag):
                raise TagError("cycle", "a tag cannot move below itself")
            below_ids = await self.subtree(tag)
            deepest = max([await self.depth(t) for t in await self.all() if t.id in below_ids])
            below = deepest - await self.depth(tag)
            if await self.depth(parent) + 1 + below > MAX_DEPTH:
                raise TagError("too_deep", f"the tree is at most {MAX_DEPTH} levels deep")
            tag.parent_id = parent.id
        if "description" in changes:
            tag.description = " ".join(str(changes["description"] or "").split())[:DESCRIPTION_MAX]
        if "responsible" in changes:
            value = changes["responsible"]
            if value is not None and value not in RESPONSIBLE:
                raise TagError("responsible", "a program key or person")
            if value is None and root:
                raise TagError("responsible", "a top-level tag names who is responsible")
            tag.responsible = value
        if "sensitive" in changes:
            tag.sensitive = changes["sensitive"]
        if "released" in changes:
            value = changes["released"]
            tag.released = None if value is None else [p for p in PROGRAMS if p in set(value)]
        if "reviewed" in changes:
            tag.reviewed = bool(changes["reviewed"])
        await self.db.flush()
        return tag

    async def merge(self, tag_id: uuid.UUID, into_id: uuid.UUID) -> MemoryTag:
        """Two tags that mean the same: the notes and the children of ``tag_id`` go to
        ``into_id``, and ``tag_id`` is gone."""
        tag, into = await self.get(tag_id), await self.get(into_id)
        if tag is None or into is None or tag.id == into.id:
            raise TagError("not_found", "unknown tag")
        if tag.parent_id is None:
            raise TagError("top_level", "a top-level tag cannot be merged away")
        if into.id in await self.subtree(tag):
            raise TagError("cycle", "a tag cannot be merged into one below it")
        await self._move_contents(tag, into)
        return into

    async def remove(self, tag_id: uuid.UUID) -> None:
        """Delete a tag: its notes and children go to its parent."""
        tag = await self.get(tag_id)
        if tag is None:
            raise TagError("not_found", "unknown tag")
        parent = self._parent(tag)
        if parent is None:
            raise TagError("top_level", "the top level is fixed")
        await self._move_contents(tag, parent)

    async def _move_contents(self, tag: MemoryTag, into: MemoryTag) -> None:
        await self.db.execute(
            text(
                "INSERT INTO memory_item_tags (item_id, tag_id, user_id) "
                "SELECT item_id, :into, user_id FROM memory_item_tags "
                "WHERE tag_id = :tag AND user_id = :uid ON CONFLICT DO NOTHING"
            ),
            {"into": into.id, "tag": tag.id, "uid": self.user_id},
        )
        for child in [t for t in await self.all() if t.parent_id == tag.id]:
            child.parent_id = into.id
        await self.db.flush()
        await self.db.execute(
            delete(MemoryTag).where(MemoryTag.id == tag.id, MemoryTag.user_id == self.user_id)
        )
        await self.db.flush()
        self._tags = None
