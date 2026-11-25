"""Tries of an issue's investigation in the workshop, under the person's probe mandate (2026-11-19).

Reading alone did not find the cause of issue 2.8 in three investigations: Development guessed (a
type mismatch that does not exist) and could not test its guess. One small test would have shown
it. A try is that test: the investigation writes ONE test file and the workshop runs only that
file, on a throw-away branch, and goes back to the live code with nothing kept.

Writing a file in the workshop is a change of a registered repository, which D99 confirms one by
one and D15 never lets an agent confirm. A try therefore runs only inside a **probe mandate**
(D83): the person's click on the issue's card ("Versuche erlauben") makes it, the card shows its
whole scope, and it is revocable. The scope is fixed by the hub, never by a model:

* repository :data:`issue_fixes.LIVE_REPO`, exactly the branch ``topcoat/probe-<issue>``;
* one new file ``hub/tests/_probe_<n>.py`` per try, the hub names it; at most :data:`MAX_BYTES`;
* the tests of that one file (``repo.test_start`` with ``path``), nothing else; no commit, no push;
* until the mandate expires (:data:`DAYS`) or is revoked.

The checks run on every try and no mandate waives them: an active probe mandate for exactly this
branch, the file name and size, no secret in the content (releases.mask_secrets), a clean workshop.
Every try is a line of the project's Akte (``mandate.used``)."""

import asyncio
import hashlib
import logging
import uuid
from typing import Any

from topcoat_hub.issue_fixes import LIVE_REPO, LIVE_TOPIC, WEB_MARK, FixError, topic_of
from topcoat_hub.tools import Caller, ToolError

log = logging.getLogger(__name__)

ACTION = "probe"
DAYS = 1
MAX_BYTES = 20_000
WAIT_SECONDS = 600  # a try that runs longer is left to the node's own limits
OUTPUT_CHARS = 4000


def probe_branch(number: str) -> str:
    return f"topcoat/probe-{topic_of(number)}"


def probe_scope(number: str) -> str:
    """What the card says the mandate covers, word for word what the hub enforces."""
    return (
        f"Die Untersuchung von Issue {number} darf in der Werkstatt ({LIVE_REPO}) auf dem Zweig "
        f"{probe_branch(number)} je Versuch eine neue Testdatei hub/tests/_probe_<n>.py schreiben "
        f"(höchstens {MAX_BYTES // 1000} kB, ohne Geheimnisse) und nur diese Datei testen lassen. "
        "Nach jedem Versuch geht die Werkstatt verworfen zurück auf topcoat/live. Kein Commit, "
        f"kein Push, keine andere Datei. Gilt {DAYS * 24} Stunden oder bis zum Widerruf."
    )


def hub_output(log_text: str) -> str:
    """The hub part of a workshop run's output (the web checks of the image follow it)."""
    hub = log_text.split(WEB_MARK, 1)[0]
    return hub.strip()[-OUTPUT_CHARS:]


def outcome_line(output: str) -> str:
    """What the Akte keeps of a try: its state and the line that decides it (pytest's first
    ``E   `` line, else the summary), not the start of the output (live 2026-11-19: three lines
    "ERROR collecting tests/_probe_1.py" and never why)."""
    lines = output.splitlines()
    state = lines[0] if lines else ""
    decisive = next((ln.strip() for ln in lines if ln.startswith("E ")), "")
    if not decisive:
        decisive = next(
            (ln.strip() for ln in reversed(lines[1:]) if " passed" in ln or " failed" in ln), ""
        )
    return f"{state} | {decisive}"[:300] if decisive else state[:300]


class ProbeRunner:
    """Runs tries for one person under their probe mandate; one workshop step at a time (it shares
    the lock of the fixes: the workshop has one scratch copy)."""

    def __init__(self, fixes: Any, store: Any) -> None:
        self.fixes = fixes
        self.store = store

    async def mandate(self, user_id: uuid.UUID, project_id: uuid.UUID, number: str) -> Any:
        return await self.store.find_mandate(
            user_id, project_id, LIVE_REPO, probe_branch(number), ACTION
        )

    async def run(
        self, user_id: uuid.UUID, project_id: uuid.UUID, number: str, n: int, content: str
    ) -> str:
        """One try: the output of its test, or why it did not run (a refusal is said, not
        raised: the investigation reads it like an answer)."""
        from topcoat_hub.releases import mask_secrets

        mandate = await self.mandate(user_id, project_id, number)
        if mandate is None:
            return "(kein Versuch: es gibt kein aktives Versuchs-Mandat für dieses Issue)"
        if not content.strip():
            return "(kein Versuch: die Testdatei ist leer)"
        if len(content.encode()) > MAX_BYTES:
            return f"(kein Versuch: die Testdatei ist größer als {MAX_BYTES} Bytes)"
        if mask_secrets(content) != content:
            return "(kein Versuch: die Testdatei enthält etwas, das wie ein Geheimnis aussieht)"
        path = f"hub/tests/_probe_{n}.py"
        try:
            async with self.fixes._lock:
                output = await self._steps(user_id, mandate, number, path, content)
        except (FixError, ToolError) as exc:
            output = f"(der Versuch scheiterte in der Werkstatt: {getattr(exc, 'message', exc)})"
        except Exception as exc:  # the investigation goes on; the person sees why in the report
            log.exception("probe of %s failed", number)
            output = f"(der Versuch scheiterte: {exc})"
        try:
            await self.store.use_mandate(
                user_id,
                mandate.id,
                what="probe",
                payload={"issue": number, "path": path, "outcome": outcome_line(output)},
            )
        except Exception:  # a mandate revoked during the try: the try already ended
            log.info("probe of %s: the use was not recorded", number, exc_info=True)
        return output

    async def _steps(
        self, user_id: uuid.UUID, mandate: Any, number: str, path: str, content: str
    ) -> str:
        fixes = self.fixes
        # the person who gave the mandate; the edit is the change the mandate covers (D83)
        caller = Caller(user_id, True, "gui")
        node = await fixes._dev_node(caller)
        repo = {"repo": LIVE_REPO}
        status = await fixes._read(caller, node, "repo.status", repo)
        if not status.get("clean", False):
            return "(kein Versuch: die Werkstatt hat ungesicherte Änderungen)"
        topic = probe_branch(number).removeprefix("topcoat/")
        await self._call(caller, node, "repo.start", {**repo, "topic": topic, "discard": True})
        try:
            digest = hashlib.sha256(f"{path}\n{content}".encode()).hexdigest()
            res = await fixes.tools.execute_confirmed(
                caller, node, "repo.write", {**repo, "path": path, "content": content},
                mandate.id, digest,
            )  # fmt: skip
            if not res.get("ok"):
                err = res.get("error") or {}
                return f"(die Testdatei wurde nicht geschrieben: {err.get('message', err)})"
            # the image runs pytest in hub/: the path is relative to it
            started = await self._call(
                caller, node, "repo.test_start", {**repo, "path": path.removeprefix("hub/")}
            )
            job = str((started or {}).get("job_id") or "")
        finally:  # back to the live code, the try's file dropped
            await self._call(
                caller, node, "repo.start", {**repo, "topic": LIVE_TOPIC, "discard": True}
            )
        out: dict[str, Any] = {}
        waited = 0
        while waited < WAIT_SECONDS:
            out = await fixes._read(
                caller,
                node,
                "repo.test_status",
                {**repo, "job_id": job, "wait_seconds": 15},
            )
            if not out.get("running"):
                break
            waited += 15
            await asyncio.sleep(0)
        state = str(out.get("state") or "unknown")
        return f"Testlauf: {state}\n{hub_output(str(out.get('log') or ''))}"

    async def _call(self, caller: Caller, node: uuid.UUID, cap: str, data: dict[str, Any]) -> Any:
        res = await self.fixes.tools.invoke(caller, node, cap, data)
        if not res.get("ok", True):
            err = res.get("error") or {}
            message = err.get("message") if isinstance(err, dict) else err
            raise FixError(502, "workshop", f"{cap}: {message}")
        return res.get("output", res)
