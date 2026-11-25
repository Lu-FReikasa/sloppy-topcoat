"""Personas (E3-06, point 11; ARCHITECTURE D38): the tone of voice the assistant speaks with.

A persona is **built from blocks**, not generated: a role, a form of address, humor (0 off, 1 dry,
2 playful — always off for errors and risk), length, transparency, how to ask back, emojis and one
fixed example sentence. The blocks and the shipped templates (Butler, Freund, Co-Pilot, Sachlich)
are data in ``persona_data/personas.json``; the first-start questionnaire is data in
``persona_data/questionnaire.json``. Same answers, same text — nothing here asks a model.

The rendered text stands in the core (:mod:`topcoat_hub.core_rules`) and may take 400 tokens of its
budget at most (:data:`MAX_TOKENS`): when it is longer the example goes first, then the smaller
blocks; the role is cut last, a free text at a sentence.

Ownership: a template belongs to the household pseudo user (every user reads it, an admin changes
it), a personal persona to its user (D20). Only a person writes either — no model, no web page, no
note file has a way here (the AI proposes style / rule entries to fine-tune, :mod:`memory_store`).
A change is a new ``version``; the text it produced stays in the prompt snapshots."""

import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from topcoat_hub import core_rules, memory_filter
from topcoat_hub.models import HOUSEHOLD_ID, Message, Persona, UserSettings

DATA = Path(__file__).parent / "persona_data"
MAX_TOKENS = 400  # of the core's budget
NAME_MAX = 80
ROLE_MAX_CHARS = 700
FREE_TEXT_MAX = 4000
LEGACY_KEY = "legacy"  # the user's old system prompt, taken over as "Eigener Text"

HUMORS = (0, 1, 2)
LENGTHS = ("short", "long")
ASKS = ("ask_first", "read_first")
ADDRESSES = ("du", "sie")
# what goes when the text is over its tokens: the last first, the role never
DROP_ORDER = ("example", "emojis", "ask", "transparency", "evidence", "length", "humor", "address")


class PersonaError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@lru_cache(maxsize=1)
def library() -> dict[str, Any]:
    return json.loads((DATA / "personas.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def questionnaire() -> dict[str, Any]:
    return json.loads((DATA / "questionnaire.json").read_text(encoding="utf-8"))


def lang_of(language: str | None) -> str:
    return "de" if (language or "").lower().startswith("de") else "en"


# --- rendering --------------------------------------------------------------------------------


@dataclass
class Rendered:
    text: str
    tokens: int
    truncated: bool  # something was left out to stay within the tokens


def _sentence_cut(text: str, max_tokens: int) -> str:
    room = max((max_tokens - core_rules.OVERHEAD_TOKENS) * core_rules.CHARS_PER_TOKEN, 1)
    if len(text) <= room:
        return text
    cut = text[:room]
    ends = [m.end() for m in re.finditer(r"[.!?…](?:\s|$)", cut)]
    return (cut[: ends[-1]] if ends else cut).rstrip()


def parts_of(components: dict[str, Any], example: str, lang: str) -> dict[str, str]:
    """The text parts of a persona in the user's language, by block name (empty ones left out)."""
    blocks = library()["blocks"]
    role = components.get("role") or {}
    values = {
        "role": str(role.get(lang) or role.get("en") or role.get("de") or "").strip(),
        "address": blocks["address"].get(str(components.get("address") or ""), {}).get(lang, ""),
        "humor": blocks["humor"].get(str(components.get("humor", "")), {}).get(lang, ""),
        "length": blocks["length"].get(str(components.get("length") or ""), {}).get(lang, ""),
        "transparency": blocks["transparency"]
        .get(str(bool(components.get("transparency"))).lower(), {})
        .get(lang, ""),
        "ask": blocks["ask"].get(str(components.get("ask") or ""), {}).get(lang, ""),
        "evidence": blocks["evidence"][lang] if components.get("evidence", True) else "",
        "emojis": blocks["emojis"]
        .get(str(bool(components.get("emojis"))).lower(), {})
        .get(lang, ""),
        "example": blocks["example"][lang].format(text=example) if example else "",
    }
    return {k: v for k, v in values.items() if v}


def render(
    components: dict[str, Any],
    example: dict[str, str] | str | None,
    language: str | None,
    max_tokens: int = MAX_TOKENS,
) -> Rendered:
    """The persona's text for one language, within ``max_tokens``. Deterministic: the same blocks
    give the same text, byte for byte."""
    lang = lang_of(language)
    free = str(components.get("free_text") or "").strip()
    if free:
        text = " ".join(free.split()) if "\n" not in free else free
        cut = _sentence_cut(text, max_tokens)
        return Rendered(cut, core_rules.tokens(cut), cut != text)
    ex = example.get(lang) or example.get("en") or "" if isinstance(example, dict) else example
    parts = parts_of(components, ex or "", lang)
    order = ["role", "address", "humor", "length", "transparency", "ask", "evidence", "emojis"]
    order.append("example")

    def join() -> str:
        return " ".join(parts[k] for k in order if k in parts)

    truncated = False
    for drop in DROP_ORDER:
        if core_rules.tokens(join()) <= max_tokens:
            break
        if drop in parts:
            del parts[drop]
            truncated = True
    text = join()
    if core_rules.tokens(text) > max_tokens:
        text, truncated = _sentence_cut(text, max_tokens), True
    return Rendered(text, core_rules.tokens(text), truncated)


# --- validation -----------------------------------------------------------------------------


def _text(value: Any, limit: int, field: str) -> str:
    if not isinstance(value, str):
        raise PersonaError(422, "invalid_persona", f"{field} must be text")
    text = value.strip()
    if len(text) > limit:
        raise PersonaError(422, "invalid_persona", f"{field} is longer than {limit} characters")
    if rule := memory_filter.check(text):
        raise PersonaError(422, "secret_blocked", f"{field} looks like a secret ({rule})")
    return text


def clean_components(raw: dict[str, Any]) -> dict[str, Any]:
    """The blocks of a persona, checked: only known keys and values, texts through the secret
    filter. ``free_text`` (a persona that is just a text) excludes the other blocks."""
    if not isinstance(raw, dict):
        raise PersonaError(422, "invalid_persona", "components must be an object")
    out: dict[str, Any] = {}
    if raw.get("free_text"):
        out["free_text"] = _text(raw["free_text"], FREE_TEXT_MAX, "free_text")
        return out
    role = raw.get("role")
    if not isinstance(role, dict) or not any(role.get(k) for k in ("de", "en")):
        raise PersonaError(422, "invalid_persona", "a persona needs a role text (de or en)")
    out["role"] = {
        k: _text(role[k], ROLE_MAX_CHARS, f"role.{k}") for k in ("de", "en") if role.get(k)
    }
    if (address := raw.get("address", "du")) not in ADDRESSES:
        raise PersonaError(422, "invalid_persona", "address must be du or sie")
    out["address"] = address
    humor = raw.get("humor", 1)
    if isinstance(humor, str) and humor.isdigit():
        humor = int(humor)
    if humor not in HUMORS:
        raise PersonaError(422, "invalid_persona", "humor must be 0, 1 or 2")
    out["humor"] = humor
    if (length := raw.get("length", "short")) not in LENGTHS:
        raise PersonaError(422, "invalid_persona", "length must be short or long")
    out["length"] = length
    if (ask := raw.get("ask", "ask_first")) not in ASKS:
        raise PersonaError(422, "invalid_persona", "ask must be ask_first or read_first")
    out["ask"] = ask
    for flag, default in (("transparency", True), ("emojis", False), ("evidence", True)):
        value = raw.get(flag, default)
        if isinstance(value, str):
            value = value.lower() == "true"
        out[flag] = bool(value)
    return out


def clean_example(raw: Any) -> dict[str, str]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, str):
        raw = {"de": raw, "en": raw}
    if not isinstance(raw, dict):
        raise PersonaError(422, "invalid_persona", "example must be text or {de, en}")
    return {
        k: _text(raw[k], 300, f"example.{k}")
        for k in ("de", "en")
        if isinstance(raw.get(k), str) and raw[k].strip()
    }


# --- the questionnaire --------------------------------------------------------------------------


def _template_data(key: str) -> dict[str, Any]:
    for t in library()["templates"]:
        if t["key"] == key:
            return t
    raise PersonaError(404, "unknown_template", key)


def role_of(scenes: dict[str, str]) -> str:
    """The role the scene choices stand for: the most-voted, ties by the order of the roles; every
    scene "none" (or no scene answered) is the neutral one."""
    q = questionnaire()
    valid = {s["id"]: {a["id"]: a["role"] for a in s["answers"]} for s in q["scenes"]}
    votes: Counter[str] = Counter()
    for scene_id, answer in scenes.items():
        role = valid.get(scene_id, {}).get(answer)
        if role:
            votes[role] += 1
    if not votes:
        return q["none_role"]
    best = max(votes.values())
    return next(r for r in q["roles"] if votes.get(r) == best)


def answers_to_persona(answers: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], str]:
    """``(components, example, role)`` from the answers of the questionnaire — the same answers give
    the same persona. Unknown scene / question ids or values are an error; a question not answered
    has its default."""
    q = questionnaire()
    scenes = answers.get("scenes") or {}
    known = {s["id"]: {a["id"] for a in s["answers"]} | {"none"} for s in q["scenes"]}
    for scene_id, answer in scenes.items():
        if scene_id not in known or answer not in known[scene_id]:
            raise PersonaError(422, "invalid_answers", f"unknown scene answer {scene_id}={answer}")
    role = role_of(scenes)
    base = _template_data(role)
    chosen: dict[str, str] = {}
    for question in q["questions"]:
        value = str(answers.get(question["id"], question["default"]))
        if value not in {o["value"] for o in question["options"]}:
            raise PersonaError(422, "invalid_answers", f"unknown value {question['id']}={value}")
        chosen[question["id"]] = value
    components = clean_components(
        {
            "role": base["components"]["role"],
            "address": chosen["address"],
            "humor": int(chosen["humor"]),
            "length": chosen["length"],
            "transparency": chosen["transparency"] == "true",
            "ask": chosen["ask"],
            "emojis": chosen["emojis"] == "true",
            "evidence": True,
        }
    )
    return components, dict(base["example"]), role


def samples_for(answers: dict[str, Any], language: str | None) -> list[str]:
    """The two sample sentences "this is how I would sound" of the preview."""
    components, _example, role = answers_to_persona(answers)
    key = "neutral" if components["humor"] == 0 and role != "neutral" else role
    return list(questionnaire()["samples"][key][lang_of(language)])


# --- the store ----------------------------------------------------------------------------------


def view(p: Persona, language: str | None) -> dict[str, Any]:
    rendered = render(p.components, p.example, language)
    return {
        "id": str(p.id),
        "key": p.key,
        "name": p.name,
        "version": p.version,
        "scope": p.scope,
        "status": p.status,
        "components": p.components,
        "example": p.example,
        "defaults": {k: v for k, v in (p.defaults or {}).items() if not k.startswith("_")},
        "text": rendered.text,
        "tokens": rendered.tokens,
        "truncated": rendered.truncated,
        "ref": ref_of(p),
    }


def ref_of(p: Persona) -> str:
    return f"{p.id}@{p.version}"


class PersonaStore:
    """The personas one user can see (the household's templates and their own) and write (their own;
    the templates only as an admin). Every query is scoped by ``user_id`` or the household."""

    def __init__(self, db: AsyncSession, user_id: uuid.UUID, acting_admin: bool = False) -> None:
        self.db = db
        self.user_id = user_id
        self.acting_admin = acting_admin

    async def templates(self, include_archived: bool = False) -> list[Persona]:
        stmt = select(Persona).where(Persona.user_id == HOUSEHOLD_ID, Persona.scope == "household")
        if not include_archived:
            stmt = stmt.where(Persona.status == "active")
        return list(await self.db.scalars(stmt.order_by(Persona.created_at, Persona.name)))

    async def own(self, include_archived: bool = False) -> list[Persona]:
        stmt = select(Persona).where(Persona.user_id == self.user_id, Persona.scope == "user")
        if not include_archived:
            stmt = stmt.where(Persona.status == "active")
        return list(await self.db.scalars(stmt.order_by(Persona.created_at, Persona.name)))

    async def get(self, persona_id: uuid.UUID) -> Persona | None:
        """One persona the user may see: a template or their own."""
        return await self.db.scalar(
            select(Persona).where(
                Persona.id == persona_id,
                ((Persona.user_id == self.user_id) & (Persona.scope == "user"))
                | ((Persona.user_id == HOUSEHOLD_ID) & (Persona.scope == "household")),
            )
        )

    async def by_key(self, key: str) -> Persona | None:
        return await self.db.scalar(
            select(Persona).where(
                Persona.key == key,
                ((Persona.user_id == self.user_id) & (Persona.scope == "user"))
                | ((Persona.user_id == HOUSEHOLD_ID) & (Persona.scope == "household")),
            )
        )

    def _may_write(self, p: Persona) -> None:
        if p.scope == "household":
            if not self.acting_admin:
                raise PersonaError(403, "admin_only", "only an admin changes the templates")
        elif p.user_id != self.user_id:
            raise PersonaError(404, "not_found", "unknown persona")

    async def create(
        self,
        name: str,
        components: dict[str, Any],
        example: Any = None,
        *,
        scope: str = "user",
        key: str | None = None,
        defaults: dict[str, Any] | None = None,
    ) -> Persona:
        if scope == "household" and not self.acting_admin:
            raise PersonaError(403, "admin_only", "only an admin creates templates")
        name = _text(name, NAME_MAX, "name")
        if not name:
            raise PersonaError(422, "invalid_persona", "a persona needs a name")
        p = Persona(
            user_id=HOUSEHOLD_ID if scope == "household" else self.user_id,
            scope=scope,
            key=key,
            name=name,
            version=1,
            components=clean_components(components),
            example=clean_example(example),
            defaults=defaults or dict(library()["defaults"]),
        )
        self.db.add(p)
        await self.db.flush()
        await self.db.refresh(p)
        return p

    async def update(
        self,
        persona_id: uuid.UUID,
        *,
        name: str | None = None,
        components: dict[str, Any] | None = None,
        example: Any = ...,
    ) -> Persona:
        p = await self.get(persona_id)
        if p is None:
            raise PersonaError(404, "not_found", "unknown persona")
        self._may_write(p)
        changed = False
        if name is not None and (new := _text(name, NAME_MAX, "name")) != p.name and new:
            p.name, changed = new, True
        if components is not None and (new_c := clean_components(components)) != p.components:
            p.components, changed = new_c, True
        if example is not ... and (new_e := clean_example(example)) != p.example:
            p.example, changed = new_e, True
        if changed:
            p.version += 1
            if p.scope == "household":
                p.defaults = {**(p.defaults or {}), "_edited": True}
            p.updated_at = func.now()
            await self.db.flush()
            await self.db.refresh(p)
        return p

    async def archive(self, persona_id: uuid.UUID) -> Persona:
        p = await self.get(persona_id)
        if p is None:
            raise PersonaError(404, "not_found", "unknown persona")
        self._may_write(p)
        p.status = "archived"
        p.version += 1
        p.updated_at = func.now()
        await self.db.flush()
        return p

    # --- the user's own settings ---------------------------------------------------------------

    async def settings(self) -> UserSettings | None:
        return await self.db.scalar(
            select(UserSettings).where(UserSettings.user_id == self.user_id)
        )

    async def default(self) -> Persona | None:
        s = await self.settings()
        if s is None or s.persona_id is None:
            return None
        p = await self.get(s.persona_id)
        return p if p is not None and p.status == "active" else None

    async def select_default(self, persona_id: uuid.UUID | None) -> None:
        """The persona new conversations start with (``None``: no persona). Old conversations keep
        theirs: they were pinned when they were created."""
        if persona_id is not None:
            p = await self.get(persona_id)
            if p is None or p.status != "active":
                raise PersonaError(404, "not_found", "unknown persona")
        s = await self.settings()
        if s is None:
            s = UserSettings(user_id=self.user_id)
            self.db.add(s)
        s.persona_id = persona_id
        await self.db.flush()

    async def set_wizard(self, state: str | None) -> None:
        s = await self.settings()
        if s is None:
            s = UserSettings(user_id=self.user_id)
            self.db.add(s)
        s.persona_wizard = state
        await self.db.flush()

    async def stats(self) -> list[dict[str, Any]]:
        """Per persona (this user's answers only, D20): answers and the user's verdicts."""
        base = Message.persona_ref
        pid = func.split_part(base, "@", 1)
        rows = await self.db.execute(
            select(
                pid.label("persona"),
                func.count().label("answers"),
                func.count().filter(Message.feedback == 1).label("helped"),
                func.count().filter(Message.feedback == -1).label("unfit"),
            )
            .where(Message.user_id == self.user_id, Message.role == "assistant", base.is_not(None))
            .group_by(pid)
        )
        names = {str(p.id): p for p in [*await self.templates(True), *await self.own(True)]}
        out = []
        for r in rows:
            p = names.get(r.persona)
            out.append(
                {
                    "persona_id": r.persona,
                    "name": p.name if p else None,
                    "key": p.key if p else None,
                    "answers": r.answers,
                    "helped": r.helped,
                    "unfit": r.unfit,
                }
            )
        return sorted(out, key=lambda x: -x["answers"])
