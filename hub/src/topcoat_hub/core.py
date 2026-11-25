"""The core service (E3-06, ARCHITECTURE D38): builds the block that opens every system prompt,
keeps it byte-identical for the length of a conversation, takes over what the old chat settings
said, and guards the two actors that own what belongs to nobody in particular (household and
system).

* :meth:`CoreService.block` — the block of one conversation: built when the conversation starts or
  when a core entry changed (its *stamp*), otherwise the text stored with the conversation is
  returned unchanged, so the prompt prefix stays the same and the model's cache keeps working (D27).
* :meth:`CoreService.ensure_actors` — at startup: the household and system pseudo users and the
  system rules (:mod:`topcoat_hub.core_seed`), versioned.
* :meth:`CoreService.ensure_user` — the user's form of address and language rule as ``style`` /
  ``rule`` notes (source ``user``); the user's system prompt is the personal persona "Eigener
  Text" (:mod:`topcoat_hub.personas`): nothing the settings said is lost, and the settings API
  keeps working (it writes through, :meth:`sync_settings`).
* The persona of a conversation (frozen from the user's default when it was created, changed only by
  the user) stands in the core within its own token cap; the core's *stamp* includes it, so a
  change of persona gives the conversation a new block — and a new prompt snapshot.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import agent_contract, core_rules, core_seed, memory_care, personas
from topcoat_hub.config import Settings
from topcoat_hub.digest_store import DigestStore
from topcoat_hub.memory_scope import DEFAULT_APPLICATION, Scope, scope_for
from topcoat_hub.memory_store import MemoryStore
from topcoat_hub.models import (
    HOUSEHOLD_ID,
    SYSTEM_ID,
    Conversation,
    Persona,
    User,
    UserSettings,
)
from topcoat_hub.ui.access import clean_agent, frame_text
from topcoat_hub.ui.store import TemplateStore

log = logging.getLogger(__name__)


def _room_for_self(context_length: int | None) -> bool:
    """Whether the self-description of the contract (about 170 tokens) goes into the core: not
    below the small context of core_rules (a 1024-token model needs its room for the answer)."""
    return context_length is None or context_length >= core_rules.SMALL_CONTEXT


@dataclass
class CoreResult:
    text: str
    block: core_rules.CoreBlock | None
    rebuilt: bool
    stamp: str
    persona_ref: str | None = None


class CoreService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.care_config = memory_care.CareConfig.of(settings)

    # --- the two actors and the system rules -------------------------------------------------

    async def ensure_actors(self) -> None:
        """The household and system pseudo users exist, and the system rules are seeded (one note
        per language) or brought to the current seed when nobody edited them."""
        async with self.sessionmaker() as db:
            for ident, name in ((HOUSEHOLD_ID, "household"), (SYSTEM_ID, "system-rules")):
                if await db.get(User, ident) is None:
                    db.add(User(id=ident, name=name, role="user"))
            await db.flush()
            store = MemoryStore(db, SYSTEM_ID)
            for lang, text in core_seed.SYSTEM_RULES.items():
                rows = await store.system_rules(lang)
                if not rows:
                    await store.add(
                        text,
                        embedding=None,
                        embedder=None,
                        evidence="doc",
                        confidence=1.0,
                        source_type="seed",
                        source_ref={"lang": lang, "seed": core_seed.SEED_VERSION},
                        reason=f"system rules, seed v{core_seed.SEED_VERSION}",
                        actor="seed",
                        kind="rule",
                        core=True,
                    )
                    continue
                item = rows[0]
                versions = await store.versions(item.id)
                seeded_by_us = bool(versions) and versions[0].actor == "seed"
                if seeded_by_us and (item.source_ref or {}).get("seed", 0) < core_seed.SEED_VERSION:
                    await store.change(
                        item.id,
                        reason=f"system rules, seed v{core_seed.SEED_VERSION}",
                        actor="seed",
                        text_=text,
                    )
            await self._seed_templates(db)
            await db.commit()

    async def _seed_templates(self, db: AsyncSession) -> None:
        """The shipped persona templates (``persona_data/personas.json``) in the household: new
        ones are created, one nobody edited follows the file when the file's version grew."""
        lib = personas.library()
        existing = {p.key: p for p in await personas.PersonaStore(db, HOUSEHOLD_ID).templates(True)}
        now = datetime.now(UTC)
        for n, t in enumerate(lib["templates"]):
            defaults = {**lib["defaults"], "_seed": lib["version"], "_edited": False}
            row = existing.get(t["key"])
            if row is None:
                db.add(
                    Persona(
                        user_id=HOUSEHOLD_ID,
                        scope="household",
                        key=t["key"],
                        name=t["name"],
                        version=1,
                        components=t["components"],
                        example=t["example"],
                        defaults=defaults,
                        # the order of the file (one transaction has one "now")
                        created_at=now + timedelta(milliseconds=n),
                    )
                )
            elif (
                not (row.defaults or {}).get("_edited")
                and (row.defaults or {}).get("_seed", 0) < lib["version"]
            ):
                row.name, row.components, row.example = t["name"], t["components"], t["example"]
                row.defaults = defaults
                row.version += 1

    # --- what the old chat settings said ---------------------------------------------------------

    async def ensure_user(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        """The user's language rule, form of address and system prompt as notes of their own (once;
        a note the user deleted stays deleted)."""
        store = MemoryStore(db, user_id)
        row = await db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
        address = (row.address if row else None) or "du"
        wanted = [
            ("language", "rule", core_seed.LANGUAGE_RULE),
            ("address", "style", core_seed.ADDRESS.get(address, core_seed.ADDRESS["du"])),
        ]
        for key, kind, text in wanted:
            if await store.legacy_item(key) is None:
                await store.add(
                    text,
                    embedding=None,
                    embedder=None,
                    evidence="doc",
                    confidence=1.0,
                    source_type="user",
                    source_ref={"legacy": key},
                    reason="taken over from the chat settings",
                    actor="user",
                    kind=kind,
                    core=True,
                )

    async def sync_settings(
        self, user_id: uuid.UUID, system_prompt: str | None, address: str | None
    ) -> None:
        """The chat settings were saved (the old API): the form of address note follows, the system
        prompt is the user's personal persona "Eigener Text" (a new version when it changed) and, as
        the old setting always did, the persona new conversations start with. A text that looks
        like a secret is refused (:class:`personas.PersonaError`)."""
        async with self.sessionmaker() as db:
            store = MemoryStore(db, user_id)
            await self.ensure_user(db, user_id)
            text = core_seed.ADDRESS.get(address or "du", "")
            item = await store.legacy_item("address")
            if text and item is not None and (item.text != text or item.status != "active"):
                await store.change(
                    item.id,
                    reason="chat settings changed",
                    actor="user",
                    text_=text,
                    status="active",
                )
            await self._sync_legacy_persona(db, user_id, (system_prompt or "").strip())
            await db.commit()

    async def _sync_legacy_persona(self, db: AsyncSession, user_id: uuid.UUID, text: str) -> None:
        pstore = personas.PersonaStore(db, user_id)
        legacy = next((p for p in await pstore.own(True) if p.key == personas.LEGACY_KEY), None)
        current = await pstore.default()
        if text:
            if legacy is None:
                legacy = await pstore.create(
                    "Eigener Text", {"free_text": text}, key=personas.LEGACY_KEY
                )
            elif legacy.components.get("free_text") != text or legacy.status != "active":
                legacy.status = "active"
                await pstore.update(legacy.id, components={"free_text": text})
            if current is None or current.id == legacy.id or current.key == personas.LEGACY_KEY:
                await pstore.select_default(legacy.id)
        elif legacy is not None and legacy.status == "active":
            if current is not None and current.id == legacy.id:
                await pstore.select_default(None)
            await pstore.archive(legacy.id)

    # --- the block --------------------------------------------------------------------

    def budget(self, context_length: int | None) -> int:
        return core_rules.budget_for(context_length, self.settings.core_tokens)

    async def persona_of(
        self, db: AsyncSession, user_id: uuid.UUID, conv: Conversation | None
    ) -> Persona | None:
        """The persona a conversation speaks with: its own (pinned when it was created); with no
        conversation (the preview) the user's default. An archived or unknown one is no persona."""
        pstore = personas.PersonaStore(db, user_id)
        if conv is None:
            return await pstore.default()
        if not conv.persona_ref:
            return None
        try:
            found = await pstore.get(uuid.UUID(conv.persona_ref))
        except ValueError:
            return None
        return found if found is not None and found.status == "active" else None

    async def frame_of(
        self, db: AsyncSession, conv: Conversation | None, with_self: bool = True
    ) -> str:
        """The frame of the conversation's program (D74): its tone and rules, as an admin set them
        on the program; nothing for a conversation without one."""
        if conv is None:
            return ""
        application = conv.application or DEFAULT_APPLICATION
        template = await TemplateStore(db).get(application)
        frame = frame_text(clean_agent(template.agent)["frame"]) if template is not None else ""
        # who answers, from the program's contract (1.9): "what can you do" is answered in words;
        # in the system prompt, so the prefix stays the same for every turn of the conversation
        # (not with a small context: its room is for the conversation)
        own = agent_contract.short(application) if with_self else ""
        if own:
            frame = f"{frame}\n- {agent_contract.SELF_HINT} {own}".strip("\n")
        return frame

    async def _material(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        language: str,
        memory_enabled: bool,
        persona: Persona | None = None,
        scope: Scope | None = None,
        conv: Conversation | None = None,
    ) -> dict[str, Any]:
        store = MemoryStore(db, user_id, scope=scope)
        await self.ensure_user(db, user_id)
        system_rows = await store.system_rules(language) or await store.system_rules("en")
        rules = await store.instructions()
        if persona is not None and not persona.components.get("free_text"):
            # the persona carries the form of address: the note of the settings would say it twice
            rules = [i for i in rules if (i.source_ref or {}).get("legacy") != "address"]
        pinned = await store.pinned_facts()
        profile = [*pinned]
        recent: list[core_rules.RecentChat] = []
        if memory_enabled:
            seen = {i.id for i in pinned}
            profile += [i for i in await store.profile() if i.id not in seen]
            recent = await DigestStore(db, user_id).recent(
                (conv.application if conv is not None else None) or DEFAULT_APPLICATION,
                conv.id if conv is not None else None,
                self.settings.core_recent_chats,
                self.settings.core_recent_days,
                DEFAULT_APPLICATION,
            )
        household = await store.household()
        return {
            "system": system_rows,
            "rules": rules,
            "profile": profile,
            "pinned": pinned,
            "household": household,
            "recent": recent,
            "persona": persona,
        }

    def _persona_text(self, m: dict[str, Any], language: str, budget: int) -> str:
        p: Persona | None = m.get("persona")
        if p is None:
            return ""
        cap = min(personas.MAX_TOKENS, int(budget * 0.5))
        return personas.render(p.components, p.example, language, cap).text

    def _stamp(self, m: dict[str, Any], budget: int, language: str, memory_enabled: bool) -> str:
        return core_rules.stamp(
            budget=budget,
            language=language,
            memory_enabled=memory_enabled,
            system=[(str(i.id), i.version) for i in m["system"]],
            rules=[(str(i.id), i.version, i.core) for i in m["rules"]],
            pinned=[(str(i.id), i.version) for i in m["pinned"]],
            household=[(str(i.id), i.version) for i in m["household"]],
            recent=[(c.id, c.last_message_id, c.day) for c in m["recent"]],
            persona=personas.ref_of(m["persona"]) if m.get("persona") is not None else "",
            frame=m.get("frame", ""),
        )

    def _build(self, m: dict[str, Any], budget: int, language: str = "en") -> core_rules.CoreBlock:
        return core_rules.build(
            persona=self._persona_text(m, language, budget),
            frame=m.get("frame", ""),
            system=[i.text for i in m["system"]],
            rules=m["rules"],
            profile=m["profile"],
            household=m["household"],
            recent=m["recent"],
            budget=budget,
            recent_tokens=core_rules.recent_budget(budget, self.settings.core_recent_tokens),
            cfg=self.care_config,
        )

    async def block(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        context_length: int | None,
        memory_enabled: bool = True,
    ) -> CoreResult:
        """The core block for this conversation: the one stored with it while nothing it was built
        from has changed (the same text, byte for byte), else a new one (stored)."""
        budget = self.budget(context_length)
        async with self.sessionmaker() as db:
            user = await db.get(User, user_id)
            language = (user.language if user else None) or "en"
            conv = None
            if conversation_id is not None:
                conv = await db.scalar(
                    select(Conversation).where(
                        Conversation.id == conversation_id, Conversation.user_id == user_id
                    )
                )
            persona = await self.persona_of(db, user_id, conv)
            persona_ref = personas.ref_of(persona) if persona is not None else None
            scope = await scope_for(
                db, user_id, (conv.application or DEFAULT_APPLICATION) if conv is not None else None
            )
            m = await self._material(db, user_id, language, memory_enabled, persona, scope, conv)
            m["frame"] = await self.frame_of(db, conv, _room_for_self(context_length))
            stamp = self._stamp(m, budget, language, memory_enabled)
            if conv is not None and conv.core_stamp == stamp and conv.core_text:
                await db.commit()  # (the seeding of a new user, if there was any)
                return CoreResult(conv.core_text, None, False, stamp, persona_ref)
            block = self._build(m, budget, language)
            if conv is not None:
                conv.core_stamp, conv.core_text = stamp, block.text
            await db.commit()
            return CoreResult(block.text, block, True, stamp, persona_ref)

    async def preview(
        self,
        user_id: uuid.UUID,
        context_length: int | None,
        memory_enabled: bool = True,
        conversation_id: uuid.UUID | None = None,
    ) -> core_rules.CoreBlock:
        """ "What the assistant sees now": the block as a new conversation would get it (nothing
        stored) — or, with a conversation, as that one would get it."""
        budget = self.budget(context_length)
        async with self.sessionmaker() as db:
            user = await db.get(User, user_id)
            language = (user.language if user else None) or "en"
            conv = None
            if conversation_id is not None:
                conv = await db.scalar(
                    select(Conversation).where(
                        Conversation.id == conversation_id, Conversation.user_id == user_id
                    )
                )
            persona = await self.persona_of(db, user_id, conv)
            scope = await scope_for(
                db, user_id, (conv.application or DEFAULT_APPLICATION) if conv is not None else None
            )
            m = await self._material(db, user_id, language, memory_enabled, persona, scope, conv)
            m["frame"] = await self.frame_of(db, conv, _room_for_self(context_length))
            block = self._build(m, budget, language)
            await db.commit()
            return block
