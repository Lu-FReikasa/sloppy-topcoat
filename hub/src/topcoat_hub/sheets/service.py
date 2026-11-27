"""The research profile ``sheets`` as the hub runs it: the web through a node the person may use,
a model for the short judgement, the report into the Ablage and the sheet into the person's list.

A run is a job in memory (like a block program run): it is started by the person's click, belongs
to that person only, and is kept for an hour after it ends. What it produces (the report, the
sheet) is stored; the job is only its progress.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import web_access
from topcoat_hub.auth import CurrentUser
from topcoat_hub.models import User
from topcoat_hub.report_store import ReportStore
from topcoat_hub.sheets import chordpro, research
from topcoat_hub.sheets.research import Job, Request
from topcoat_hub.sheets.store import SheetError, SheetStore
from topcoat_hub.tools import Caller, ToolError, ToolService

KEEP = timedelta(hours=1)
MAX_RUNNING = 2  # per person


class NodeWeb:
    """``web.search`` and ``web.fetch`` of one node, as the person who started the run."""

    def __init__(self, tools: ToolService, caller: Caller, node_id: uuid.UUID) -> None:
        self.tools, self.caller, self.node_id = tools, caller, node_id

    async def _call(
        self, capability: str, tool_input: dict[str, Any], person_url: bool = False
    ) -> dict[str, Any]:
        try:
            res = await self.tools.invoke(
                self.caller,
                self.node_id,
                capability,
                tool_input,
                **({"person_url": True} if person_url else {}),
            )
        except ToolError as exc:
            raise RuntimeError(exc.message) from exc
        if not res.get("ok"):
            raise RuntimeError((res.get("error") or {}).get("message") or "the call failed")
        out = res.get("output")
        return out if isinstance(out, dict) else {}

    async def search(self, query: str) -> list[dict[str, Any]]:
        out = await self._call(web_access.SEARCH, {"query": query[:200], "limit": 10})
        return [r for r in out.get("results") or [] if isinstance(r, dict)]

    async def fetch(self, url: str, markup: bool, built: bool = False) -> dict[str, Any]:
        """One page. ``built``: the hub made the URL itself from the person's request (the lyrics
        lookup), it is no search hit; the node is told so (finding 03). Hits need nothing: the
        node knows its own search results."""
        tool_input: dict[str, Any] = {"url": url, "max_chars": 600000 if markup else 20000}
        if markup:
            tool_input["format"] = "html"
        return await self._call(web_access.FETCH, tool_input, person_url=built)


def reads_markup(tool: dict[str, Any]) -> bool:
    """Whether a node's ``web.fetch`` can return the markup (node 0.2.17 and later)."""
    props = (tool.get("input_schema") or {}).get("properties") or {}
    return "html" in ((props.get("format") or {}).get("enum") or [])


class SheetResearchService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        tools: ToolService,
        gateway: Any = None,
    ) -> None:
        self.sessionmaker, self.tools, self.gateway = sessionmaker, tools, gateway
        self.jobs: dict[uuid.UUID, Job] = {}
        self._tasks: set[Any] = set()

    def _prune(self) -> None:
        now = datetime.now(UTC)
        for jid, job in list(self.jobs.items()):
            if job.finished is not None and now - job.finished > KEEP:
                del self.jobs[jid]

    def jobs_of(self, user_id: uuid.UUID) -> list[Job]:
        self._prune()
        mine = [j for j in self.jobs.values() if j.user_id == user_id]
        return sorted(mine, key=lambda j: j.started, reverse=True)

    def job(self, user_id: uuid.UUID, job_id: uuid.UUID) -> Job:
        job = self.jobs.get(job_id)
        if job is None or job.user_id != user_id:
            raise SheetError(404, "not_found", "there is no such research")
        return job

    async def web_node(self, caller: Caller) -> uuid.UUID:
        usable = await self.tools.usable_tools(caller)
        search = {t["node_id"] for t in usable if t["capability"] == web_access.SEARCH}
        fetch = {
            t["node_id"] for t in usable if t["capability"] == web_access.FETCH and reads_markup(t)
        }
        both = sorted(search & fetch)
        if both:
            return uuid.UUID(both[0])
        if not search:
            raise SheetError(409, "no_web", "no node you may use offers a web search")
        raise SheetError(
            409,
            "no_markup",
            "no node you may use reads pages as markup yet (web.fetch format html, node 0.2.17)",
        )

    async def start(self, user: CurrentUser, req: Request) -> Job:
        if not req.song.strip():
            raise SheetError(422, "invalid", "name the song")
        running = [j for j in self.jobs_of(user.uuid) if j.status == "running"]
        if len(running) >= MAX_RUNNING:
            raise SheetError(429, "busy", f"at most {MAX_RUNNING} researches run at once")
        caller = Caller.of(user, "agent")
        node_id = await self.web_node(caller)
        job = Job(uuid.uuid4(), user.uuid, req)
        self.jobs[job.id] = job
        web = NodeWeb(self.tools, caller, node_id)
        research.run_in_background(self._run(job, web), self._tasks)
        return job

    async def _judge(self, prompt: str) -> str:
        if self.gateway is None:
            raise RuntimeError("no model")
        resolver = self.gateway.resolver
        choice = await resolver.resolve(
            "answer", application="research", fallback=await resolver.default_model()
        )
        payload = {
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1200,
            "temperature": 0.1,
        }
        data = await self.gateway.call(
            choice.requested, "/v1/chat/completions", payload, label="sheets research"
        )
        return str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")

    async def _language(self, user_id: uuid.UUID) -> str:
        async with self.sessionmaker() as db:
            user = await db.get(User, user_id)
        return (user.language if user and user.language in ("de", "en") else None) or "de"

    async def _run(self, job: Job, web: research.Web) -> None:
        try:
            judge = self._judge if self.gateway is not None else None
            out = await research.research(web, job.request, judge, job.steps.append)
            language = await self._language(job.user_id)
            await self.file(job, out, language)
            job.status = "done"
        except Exception as exc:  # the job says why; nothing half-made is left behind
            job.status = "failed"
            job.error = str(exc) or exc.__class__.__name__
        finally:
            job.finished = datetime.now(UTC)

    async def file(self, job: Job, out: research.Outcome, language: str) -> None:
        """The report into the Ablage, the chosen sheet with all sources into the list."""
        req = out.request
        body = research.report(out, language)
        async with self.sessionmaker() as db:
            rep = await ReportStore(db, job.user_id).create(
                title=f"Sheets: {req.song}" + (f" - {req.artist}" if req.artist else ""),
                question=f"{req.song} {req.artist} ({req.instrument}) {req.wish}".strip(),
                language=language,
                depth="default",
                body=body,
                meta={"profile": research.PROFILE, "steps": out.log[-60:]},
            )
            job.report_id = rep.id
            if out.main is not None:
                main = out.main
                stored = [s.stored(c) for s, c in zip(out.sources, out.comparisons, strict=True)]
                version = main.judgement.get("version") or main.found.version_note or ""
                row = await SheetStore(db, job.user_id).create(
                    chordpro.write(main.sheet),
                    req.instrument,
                    # the names as the source writes them ("Red Hot Chili Peppers"), not as typed
                    title=main.sheet.title or req.song,
                    artist=main.sheet.artist or req.artist,
                    version_note=str(version),
                    sources=stored,
                    report_id=rep.id,
                )
                job.sheet_id = row.id
            await db.commit()
