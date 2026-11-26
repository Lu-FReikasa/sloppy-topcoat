"""How the self-maintaining memory tells what is true (D19 extended, D22).

Pure logic without I/O:

* **Values and statements** — what a note claims (:func:`entities`), what the user's own message
  states (:func:`statements`, :func:`supports`; questions state nothing).
* **Disputes** — notes the extraction or the consolidation found to contradict each other are
  linked (``disputed_with``); :func:`conflict_groups` returns those groups. There are no slots
  and no cue words any more (D124): what contradicts is said by the model that read the notes,
  never guessed from single words of the person's text.
* **Evidence and judgement** — evidence with a time is collected per note by pluggable
  :class:`EvidenceSource` objects (today: the conversations the notes came from; later
  knowledge, web and tools, E2-03/E2-04, can name the winning note as *decisive*), and
  :func:`judge` decides only when the matter is clear: a decisive check, or the user saying it
  again after the conflict; :func:`auto_verdict` settles the rest by the stronger evidence, then
  the latest word of the person. Anything else stays in doubt and becomes a question.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from topcoat_hub.embedding import _fold


def _matches(value: str, words: set[str]) -> bool:
    """``value`` is among ``words`` (also as a stem: "hund" / "hundes")."""
    return value in words or any(
        min(len(w), len(value)) >= 4 and (w.startswith(value) or value.startswith(w)) for w in words
    )


_QUESTION_START = frozenset(
    {"wo", "was", "wer", "wie", "wann", "warum", "wieso", "weshalb", "welche", "welcher"}
    | {"welches", "wohin", "woher", "kannst", "kann", "könntest", "weißt", "weisst", "ist"}
    | {"sind", "hast", "hat", "what", "where", "who", "how", "when", "why", "which", "can"}
    | {"could", "do", "does", "is", "are"}
)


def statements(text: str) -> str:
    """The sentences of ``text`` that are not questions: "Wo wohne ich, in Hamburg oder in
    Berlin?" names two cities and states nothing."""
    kept: list[str] = []
    for sentence in re.findall(r"[^.!?\n]+[.!?]?", text):
        words = _words(sentence)
        if sentence.rstrip().endswith("?") or (
            words
            and not sentence.rstrip().endswith((".", "!"))
            and _fold(words[0]) in _QUESTION_START
        ):
            continue
        kept.append(sentence)
    return " ".join(kept)


def supports(user_text: str, note: str) -> bool:
    """Does the user's own message state ``note``? Its values (cities, names, numbers) must
    appear in the message; a note without values needs most of its words there. Used so that
    only what the user *said* is confirmed — not what the answer or a note in the prompt
    said (a question like "Was weißt du über mich?" confirms nothing)."""
    said = {_fold(w) for w in _words(statements(user_text))}
    values = entities(note, [statements(user_text)])
    if values:
        return all(_matches(v, said) for v in values)
    words = [_fold(w) for w in _words(note) if len(w) >= 4]
    return bool(words) and sum(_matches(w, said) for w in words) >= 0.6 * len(words)


_NOT_ENTITIES = frozenset(
    {"der", "die", "das", "ein", "eine", "ich", "er", "sie", "es", "wir", "the", "a", "an"}
    | {"i", "he", "she", "it", "we", "user", "nutzer", "nutzerin", "benutzer"}
)


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text)


def entities(text: str, others: Sequence[str] = ()) -> set[str]:
    """The *values* a note states: numbers and capitalised words after the first word (cities,
    names, brands, nouns). The first word (usually a verb: "Lebt", "Wohnt") only counts if it
    also appears in one of the ``others`` it is compared with ("Hund heißt Bello" / "Mein
    Hund heißt Bello"). Two notes that say the same share their values; a merge that would
    lose one is not a merge."""
    seen = {_fold(w) for other in others if other != text for w in _words(other)}
    found: set[str] = set()
    for i, word in enumerate(_words(text)):
        folded = _fold(word)
        is_value = word[:1].isupper() and folded not in _NOT_ENTITIES and (i > 0 or folded in seen)
        if is_value or any(c.isdigit() for c in word):
            found.add(folded)
    return found


# --- disputes ---------------------------------------------------------------------------


def values_differ(a: str, b: str) -> bool:
    """The notes state values that exclude each other. A note that only adds to the other
    ("Lebt in Hamburg an der Elbe" after "Wohnt in Hamburg") refines it: no conflict."""
    ea, eb = entities(a, [b]), entities(b, [a])
    return not (ea <= eb or eb <= ea)


def conflict_groups(notes: Sequence[Any]) -> list[list[Any]]:
    """Groups of live notes in a dispute: connected by their ``disputed_with`` links (set where the
    extraction or the consolidation found a contradiction), oldest first."""
    parent: dict[uuid.UUID, uuid.UUID] = {}

    def find(x: uuid.UUID) -> uuid.UUID:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_id = {n.id: n for n in notes}
    for n in notes:
        other = getattr(n, "disputed_with", None)
        if other is not None and other in by_id and other != n.id:
            parent[find(n.id)] = find(other)
    groups: dict[uuid.UUID, list[Any]] = {}
    for x in list(parent):
        groups.setdefault(find(x), []).append(by_id[x])
    return [sorted(g, key=lambda n: n.created_at) for g in groups.values() if len(g) > 1]


# --- evidence and judgement --------------------------------------------------------------


@dataclass
class Evidence:
    note_id: uuid.UUID
    kind: str  # "statement" | "correction" | "confirmation" | a checker's own kind
    at: datetime
    detail: str = ""  # the user's own words (a quote) or what a check found
    decisive_for: uuid.UUID | None = None  # a check that settles it names the note that is true
    message: str = ""  # the whole message the note came from (for the model to read)


class EvidenceSource(Protocol):
    """A way to find out what is true about a doubtful note. Today only the conversations the
    notes came from are used; the knowledge base, the web and tools (E2-03, E2-04) plug in
    here and may return decisive evidence."""

    name: str

    async def gather(self, notes: Sequence[Any]) -> list[Evidence]: ...


class ConversationEvidence:
    """What the user said, and when: the statement a note came from (read from the message itself
    where it can be loaded) and every time they said it again. Whether a message *corrects* is
    no longer read from cue words (D124)."""

    name = "conversation"

    async def message_of(self, note: Any) -> str:
        """The user's message a note was written from ("" if it is not known). The memory
        service overrides this to read the stored conversation of the note's own user."""
        return ""

    async def gather(self, notes: Sequence[Any]) -> list[Evidence]:
        out: list[Evidence] = []
        for n in notes:
            if n.source_type == "chat" and n.evidence != "assumption":
                text = await self.message_of(n)
                out.append(Evidence(n.id, "statement", n.created_at, "", message=text))
            if n.last_confirmed_at is not None:
                out.append(Evidence(n.id, "confirmation", n.last_confirmed_at))
        return out


@dataclass
class Verdict:
    winner: Any
    losers: list[Any]
    reason: str  # with the evidence, for the version history


def _when(at: datetime) -> str:
    return f"{at:%Y-%m-%d %H:%M}"


def _latest(by_id: dict[uuid.UUID, Any], evidence: Sequence[Evidence]) -> dict[uuid.UUID, datetime]:
    latest: dict[uuid.UUID, datetime] = {}
    for e in evidence:
        if e.note_id in by_id and (e.note_id not in latest or e.at > latest[e.note_id]):
            latest[e.note_id] = e.at
    return latest


def last_word(notes: Sequence[Any], evidence: Sequence[Evidence]) -> Any | None:
    """The note whose latest evidence is newer than the latest evidence of every other note,
    or None (nobody has the last word)."""
    by_id = {n.id: n for n in notes}
    latest = _latest(by_id, evidence)
    if not latest:
        return None
    newest_id = max(latest, key=lambda i: latest[i])
    if any(i in latest and latest[i] >= latest[newest_id] for i in by_id if i != newest_id):
        return None
    return by_id[newest_id]


def judge(notes: Sequence[Any], evidence: Sequence[Evidence]) -> Verdict | None:
    """Decide which of the conflicting notes is true — only if that is clear. None: not
    clear, the user is asked. Clear is: a check that names the winner; or the user said one note
    again after every other's latest evidence."""
    by_id = {n.id: n for n in notes}
    decisive = {e.decisive_for for e in evidence if e.decisive_for in by_id}
    if len(decisive) == 1:
        win = by_id[next(iter(decisive))]
        why = next(e for e in evidence if e.decisive_for == win.id)
        return Verdict(
            win,
            [n for n in notes if n is not win],
            f"checked ({why.kind}): “{win.text}” is confirmed"
            + (f": {why.detail}" if why.detail else ""),
        )
    if len(decisive) > 1:
        return None  # the checks contradict each other
    newest = last_word(notes, evidence)
    if newest is None:
        return None  # no note has the last word
    latest = _latest(by_id, evidence)
    proof = next(
        (
            e
            for e in evidence
            if e.note_id == newest.id and e.at == latest[newest.id] and e.kind != "statement"
        ),
        None,
    )
    if proof is None:
        return None  # merely the newer statement of a personal fact: ask
    how = "corrected it" if proof.kind == "correction" else "said it again"
    others = [n for n in notes if n is not newest]
    older = "; ".join(
        f"“{n.text}” ({_when(latest[n.id]) if n.id in latest else 'no evidence'})" for n in others
    )
    quote = f": “{proof.detail}”" if proof.detail and proof.kind == "correction" else ""
    return Verdict(
        newest,
        others,
        f"the user {how} on {_when(latest[newest.id])} after the earlier statements{quote} — "
        f"“{newest.text}” replaces {older}",
    )


_EVIDENCE_RANK = {"live": 3, "doc": 2, "assumption": 1}


def _touched(note: Any) -> datetime:
    """When a note was last said, confirmed or checked."""
    times = [
        t
        for t in (
            getattr(note, "last_verified_at", None),
            getattr(note, "last_confirmed_at", None),
            getattr(note, "created_at", None),
        )
        if t is not None
    ]
    return max(times)


def auto_verdict(notes: Sequence[Any]) -> Verdict | None:
    """The hub's own decision when the evidence does not settle a conflict (setting
    ``memory_auto_settle``: the person wants no questions). The note with the stronger evidence
    wins (live > doc > assumption); among statements of the person the one said, confirmed or
    checked last. The losers are superseded with this reason, so the decision can be undone from
    the history. None for fewer than two notes, a tie in both, and when the best of them are only
    assumptions: which guess came last says nothing about which is true (live 2026-11-21 the
    newer of two guesses was the wrong one twice) — see :func:`unproven`."""
    if len(notes) < 2:
        return None

    def rank(n: Any) -> tuple[int, datetime]:
        return (_EVIDENCE_RANK.get(getattr(n, "evidence", ""), 0), _touched(n))

    ordered = sorted(notes, key=rank, reverse=True)
    win, runner_up = ordered[0], ordered[1]
    if rank(win) == rank(runner_up):
        return None
    if win.evidence == "assumption" and runner_up.evidence == "assumption":
        return None
    strength = _EVIDENCE_RANK.get(win.evidence, 0) > _EVIDENCE_RANK.get(runner_up.evidence, 0)
    how = (
        f"stronger evidence ({win.evidence})"
        if strength
        else f"the latest word ({_when(_touched(win))})"
    )
    others = [n for n in notes if n is not win]
    return Verdict(
        win,
        others,
        f"decided by the hub without asking, {how}: “{win.text}” replaces "
        + "; ".join(f"“{n.text}”" for n in others),
    )


def unproven(notes: Sequence[Any]) -> bool:
    """Contradicting notes that are all assumptions: none of them is kept (archived, restorable),
    nobody is asked; the next statement of the person or a live check teaches it again."""
    return len(notes) >= 2 and all(getattr(n, "evidence", "") == "assumption" for n in notes)


UNPROVEN_REASON = (
    "contradicting assumptions, none of them stated or checked: archived by the hub without "
    "asking (restorable)"
)


def question_context(notes: Sequence[Any], question: str = "") -> dict[str, Any]:
    """What the GUI needs to word the question: the notes in doubt with their dates, and the
    concrete point of the disagreement when the model could name it."""
    return {
        **({"question": question, "compared": True} if question else {}),
        "candidates": [
            {
                "id": str(n.id),
                "text": n.text,
                "at": n.created_at.isoformat() if n.created_at else None,
                "confirmed_count": n.confirmed_count,
                "last_confirmed_at": n.last_confirmed_at.isoformat()
                if n.last_confirmed_at
                else None,
            }
            for n in notes
        ],
    }
