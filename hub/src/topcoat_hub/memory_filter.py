"""Secret filter for the assistant's memory and, later, for knowledge chunks (D19).

Deterministic patterns for credentials, keys, tokens and account numbers. A text that matches
is **never stored**; the caller logs the *name of the rule* (never the value). The patterns
lean towards over-blocking: a note that is wrongly refused costs a sentence, a stored password
costs much more. The model check that complements this filter is part of the extraction and
consolidation prompts (:mod:`topcoat_hub.memory`).
"""

import re
from collections.abc import Callable

_UUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")

# "<secret word> [up to a few words] <is | = | : | set to> <value>". The word may be the end of a
# compound ("Routerpasswort", "client_secret"), a few words may stand between it and the verb ("das
# Passwort für den NAS ist …", "the password of the router is …"), and "was set to" counts as "is".
_CREDENTIAL_WORD = (
    r"(?:\w*(?:passwort|password|passwd|kennwort|passphrase|geheimzahl|zugangscode|secret|"
    r"geheimnis)|pwd|pin)"
)
_QUALIFIER = r"(?:[ \t]+[^\W\d_][\w\-./]*){0,4}?"  # words only: a comma or a colon ends it
_IS = (
    r"(?:(?:ist|is)[ \t]+(?:jetzt|nun|now)|wurde[ \t]+(?:auf|zu)|ist|lautet|is|heißt|heisst|=|:|->|"
    r"(?:gesetzt|geändert|geaendert|changed|set)[ \t]+(?:auf|to))"
)
_PASSWORD_CLAUSE = rf"\b{_CREDENTIAL_WORD}\b{_QUALIFIER}\W{{0,3}}{_IS}\W{{0,3}}[^\s]{{3,}}"

# name -> compiled pattern; the first that matches names the block.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern))
    for name, pattern in (
        ("private_key", r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY"),
        ("password", r"(?i)" + _PASSWORD_CLAUSE),
        ("api_key", r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"),
        ("api_key", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
        ("api_key", r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
        ("api_key", r"\bAKIA[0-9A-Z]{16}\b"),
        ("api_key", r"\bAIza[0-9A-Za-z_\-]{30,}"),
        ("api_key", r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
        ("api_key", r"\btc_[A-Za-z0-9_\-]{20,}"),  # this hub's own API keys
        ("token", r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
        ("token", r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{16,}=*"),
        ("url_credentials", r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:[^\s@/]+@"),
    )
)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")


def _iban_ok(candidate: str) -> bool:
    iban = candidate.replace(" ", "")
    if not 15 <= len(iban) <= 34:
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def _luhn_ok(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# "ohne Passwort", "without password", "kein Passwort": the sentence says there is none
_NEGATED = re.compile(r"(?i)\b(?:ohne|without|kein|keine|keinem|keinen|no)[ \t]*$")


_PASSWORD_VALUE = re.compile(
    rf"(?i)\b{_CREDENTIAL_WORD}\b{_QUALIFIER}(?P<pre>\W{{0,3}})(?P<verb>{_IS})(?P<post>\W{{0,3}})(?P<value>[^\s]{{3,}})"
)
# values after "Passwort:" / "password=" that say there is none, or where to look: not secrets
_NOT_A_VALUE = frozenset(
    (
        "siehe", "see", "none", "kein", "keins", "keine", "nicht", "nein", "no", "n/a", "tbd",
        "todo", "unbekannt", "unknown", "leer", "empty", "entfällt", "entfaellt", "deaktiviert",
        "disabled", "erforderlich", "required", "optional", "vorhanden", "nirgends", "ja", "yes",
        "true", "false", "an", "aus", "on", "off",
    )
)  # fmt: skip


def _looks_like_a_value(value: str) -> bool:
    """A word after a verbal "Passwort ist …" that could be the password itself rather than a
    word of the sentence: it has a digit or a symbol, a capital inside, or is quoted."""
    bare = value.strip(".,;:!?)(")
    if not bare:
        return False
    return (
        value[0] in "\"'`„“«"
        or any(c.isdigit() for c in bare)
        or any(not (c.isalnum() or c in "-_") for c in bare)
        or any(c.isupper() for c in bare[1:])
    )


def _password_in_prose(text: str) -> bool:
    """The ``password`` rule for text *about* passwords (documentation, runbooks) instead of text
    that *says* one: an assignment (``Passwort: x``, ``password=x``) is a secret unless what
    follows is plainly no value ("siehe", "keins"); a sentence ("Passwort ist …", "PIN lautet
    …") only if the next word looks like a value. "Anmeldung ohne Passwort", "Das Passwort ist
    abgelaufen" and "Die PIN ist dem Admin bekannt" mention the word and secure nothing."""
    for m in _PASSWORD_VALUE.finditer(text):
        value = m.group("value")
        fence = (m.group("post") + value).lstrip().startswith(("```", "~~~"))
        if fence and _NEGATED.search(text[max(0, m.start() - 24) : m.start()]):
            continue  # "ohne Passwort … angelegt:" and then a code block: no value
        # an assignment: "=", ":" or "->", or a quote right behind the verb ("ist `x`"); a quote
        # in front of the verb only closes a name ("`reset_password` ist …")
        if m.group("verb") in ("=", ":", "->") or any(
            sign in m.group("post") for sign in ("=", ":", "->", '"', "'", "`", "„", "“")
        ):
            if value.strip(".,;:!?)(`'\"*_").lower() not in _NOT_A_VALUE:
                return True
        elif _looks_like_a_value(value):
            return True
    return False


# "mit Kennwort Sommer2026", "Passwort Tr0ub4dor": the secret word with its value right behind it
# and no verb between. Only a value that looks like one counts (digit, symbol, capital inside),
# so "Passwort Manager" and "Passwort zurücksetzen" stay notes.
_PASSWORD_ADJACENT = re.compile(rf"(?i)\b{_CREDENTIAL_WORD}\b[ \t]+(?P<value>[^\s]{{4,}})")


def _password_adjacent(text: str) -> bool:
    for m in _PASSWORD_ADJACENT.finditer(text):
        value = m.group("value")
        if value.strip(".,;:!?)(").lower() in _NOT_A_VALUE:
            continue
        bare = value.strip(".,;:!?)(")
        if _looks_like_a_value(value) and (
            any(c.isdigit() for c in bare) or any(c.isupper() for c in bare[1:])
        ):
            return True
    return False


# A hash-like word: seven or more hex characters with both letters and digits (a commit).
_COMMIT = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
# The assistant's own acts and words are not what the user said or confirmed.
_ABOUT_ASSISTANT = re.compile(
    r"^\W*(?:der|die|das)?\s*(?:ki|assistent(?:in)?|assistant|ai)\b", re.I
)
# A state of the moment: something is running, failing or unstable right now.
_NOW = r"(?:aktuell\w*|gerade|derzeit|momentan|currently|right now|at the moment|already|bereits)"
_STATE = (
    r"(?:l(?:ä|ae)uft|laufen|instabil|beendet|fehlgeschlagen|abgebrochen|running|failed|unstable|"
    r"down|stopped|crashed|in arbeit|in progress|implementiert|implemented|umgesetzt)"
)
_MOMENT = re.compile(rf"\b{_NOW}\b.{{0,60}}\b{_STATE}\b", re.I)
_PROGRESS = re.compile(
    r"\btests?\b.{0,80}\b(?:bestanden|passed|failed|fehlgeschlagen|l(?:ä|ae)uft|laufen|running)\b"
    r"|\b\d+(?:[.,]\d+)?\s*(?:sekunden|seconds)\b",
    re.I,
)
_STATUS_REPORT = re.compile(r"^\W*(?:system|server|dienst)?status\b", re.I)
_BRANCH = re.compile(r"\b(?:branch|zweig)\b.{0,60}\b(?:commit|auf)\b", re.I)
# A project's own state: a numbered task or issue, its status, what was just created or waits for
# a click. The project holds that itself; as a note it is stale as soon as the task moves on (live
# 2026-11-21: "Aufgabe 1.2 hat den Status 'todo'" was read out in every chat for ten days).
_TASK_REF = re.compile(
    r"\b(?:aufgabe|aufgaben|unteraufgaben?|teilaufgaben?|task|tasks|subtasks?|issue|issues|knoten|"
    r"topic)"
    r"\s+['\"„“]?\d+(?:\.\d+)*\b",
    re.I,
)
_TASK_STATUS = re.compile(
    r"\bstatus\s+['\"„“]?(?:todo|done|doing|open|offen|erledigt|in_review|in review|blocked)\b",
    re.I,
)
_EVENT = re.compile(
    r"\b(?:wurde|wurden|ist|sind|was|were|has been|have been)\s+(?:\S+\s+){0,6}?"
    r"(?:angelegt|erstellt|vorbereitet|aktualisiert|created|updated|prepared)\b"
    r"|\bwarte[tn]?\s+auf\s+(?:\S+\s+){0,2}(?:bestätigung|freigabe|klick)"
    r"|\bwaits?\s+for\s+(?:\S+\s+){0,2}(?:confirmation|approval)",
    re.I,
)
# A snapshot: true at the moment it was said ("the newest commit", "in the last 24 hours").
_SNAPSHOT = re.compile(
    r"\b(?:neueste[nrs]?|letzte[nrs]?|latest|last)\s+commit\b"
    r"|\bin\s+den\s+letzten\s+\d+\s+(?:stunden|tagen|minuten)\b"
    r"|\bin\s+the\s+last\s+\d+\s+(?:hours|days|minutes)\b"
    r"|\b(?:zurzeit|zur\s+zeit)\b",
    re.I,
)
# A note about other notes: what the memory says of itself is not a fact about the person.
_ABOUT_NOTES = re.compile(
    r"\b(?:notiz|notizen|note|notes|angaben|eintrag|einträge)\b.{0,80}"
    r"\b(?:widerspr\w*|veraltet\w*|contradict\w*|outdated)",
    re.I,
)


def transient(text: str) -> str | None:
    """Name of the rule that says ``text`` is not a memory, or None: a state of the moment (what
    runs, fails or is at which commit right now, how far a task or a test run is), a report on
    system status, or something the assistant did or said. Such a line is true for hours; as a
    note it comes back in unrelated chats as if it were still so. Also what a project holds itself
    (a numbered task, its status, that something was just created or waits for a click), a
    snapshot ("the newest commit", "in the last 24 hours") and a note about other notes. Used for
    what is learned from a chat; never for what the person asks to be noted."""
    if _COMMIT.search(text):
        return "commit"
    if _TASK_REF.search(text) or _TASK_STATUS.search(text):
        return "project_state"
    if _EVENT.search(text):
        return "event"
    if _SNAPSHOT.search(text):
        return "snapshot"
    if _ABOUT_NOTES.search(text):
        return "about_notes"
    if _ABOUT_ASSISTANT.search(text):
        return "assistant"
    if _STATUS_REPORT.search(text):
        return "status_report"
    if _BRANCH.search(text):
        return "branch"
    if _PROGRESS.search(text):
        return "progress"
    if _MOMENT.search(text):
        return "moment"
    return None


def check(text: str, prose: bool = False) -> str | None:
    """Name of the rule that says ``text`` contains a secret, or None.

    ``prose=True`` is for text that talks *about* a system, such as the knowledge base: the
    ``password`` rule then fires only when a value stands behind the word (see
    :func:`_password_in_prose`), every other rule is as strict as ever. Notes, answers and
    anything that leaves the hub always use the strict form."""
    for name, pattern in _PATTERNS:
        if name == "password" and prose:
            if _password_in_prose(text):
                return name
            continue
        if pattern.search(text):
            return name
    if not prose and _password_adjacent(text):
        return "password"
    for m in _IBAN.finditer(text):
        if _iban_ok(m.group()):
            return "iban"
    for m in _CARD.finditer(text):
        if _luhn_ok(m.group()):
            return "card_number"
    for m in _LONG_TOKEN.finditer(text):
        token = m.group()
        if re.search(r"[A-Za-z]", token) and re.search(r"\d", token) and not _UUID.match(token):
            return "long_token"
    return None


# --- removal for text that leaves the hub (E3-02) ---------------------------------------------

REDACTED = "[REDACTED]"
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)"
)
_URL_CREDENTIALS = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*://)[^\s:/@]+:[^\s@/]+@")
# `NAME=value`, `name: value`, `"name": "value"` where the name says the value is a secret
_NAME_HINTS = (
    r"pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|private[_-]?key|credential|"
    r"auth(?:orization)?|access[_-]?key|session[_-]?id|cookie"
)
_KEY_VALUE = re.compile(
    r"(?i)(?P<key>[A-Za-z0-9_.\-]*(?:" + _NAME_HINTS + r")[A-Za-z0-9_.\-]*)"
    r"(?P<sep>[\"']?[ \t]*[=:][ \t]*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]]+)"
)
_CLI_FLAG = re.compile(r"(?i)(--?(?:pass(?:word|wd)?|pwd|token|secret|api-?key)[ \t]+)(?!-)(\S+)")


def scrub(text: str) -> tuple[str, list[str]]:
    """``text`` with everything that looks like a secret **removed** (not pseudonymised), and
    the names of the rules that fired (never the values). Used by the redaction layer and the
    knowledge rules, where text leaves its source; it over-blocks on purpose, like :func:`check`."""
    fired: list[str] = []

    def hit(name: str) -> None:
        if name not in fired:
            fired.append(name)

    def sub(pattern: re.Pattern[str], name: str, repl: str) -> None:
        nonlocal text
        text, n = pattern.subn(repl, text)
        if n:
            hit(name)

    sub(_PEM_BLOCK, "private_key", "[REDACTED:private_key]")
    sub(_URL_CREDENTIALS, "url_credentials", rf"\1{REDACTED}@")
    for name, pattern in _PATTERNS:
        if name not in ("private_key", "url_credentials"):
            sub(pattern, name, f"[REDACTED:{name}]")

    def key_value(m: re.Match[str]) -> str:
        if m.group("value").startswith("[REDACTED"):
            return m.group()
        hit("secret_value")
        return f"{m.group('key')}{m.group('sep')}{REDACTED}"

    text = _KEY_VALUE.sub(key_value, text)
    sub(_CLI_FLAG, "secret_value", rf"\1{REDACTED}")

    def only_if(ok: Callable[[str], bool], name: str) -> Callable[[re.Match[str]], str]:
        def repl(m: re.Match[str]) -> str:
            if not ok(m.group()):
                return m.group()
            hit(name)
            return f"[REDACTED:{name}]"

        return repl

    text = _IBAN.sub(only_if(_iban_ok, "iban"), text)
    text = _CARD.sub(only_if(_luhn_ok, "card_number"), text)
    text = _LONG_TOKEN.sub(
        only_if(
            lambda t: bool(
                re.search(r"[A-Za-z]", t) and re.search(r"\d", t) and not _UUID.match(t)
            ),
            "long_token",
        ),
        text,
    )
    return text, fired


def check_scrubbed(text: str) -> str | None:
    """Last line of defence: the rule that still finds a secret in text that went through
    :func:`scrub` (the removal markers themselves do not count)."""
    return check(re.sub(r"\[REDACTED[^\]]*\]", "", text))
