"""``<app>.message.send``: how the answering application hands its answer back (PPH-05, 05b).

The agent of Research (later Development and Administration) is offered one tool beside its own.
It says what it found, claim by claim, with the evidence marks (E1, E2 ...) that show each claim.
The hub builds the message from the stored evidence: quotes, sources, hosts, times and statistics
are not the model's to write (``message_result.py``). The tool is offered only to a run that was
started for a request; it answers that request and no other, once.
"""

import base64
import binascii
import copy
import json
import re
import uuid
from typing import Any
from urllib.parse import urlsplit

import jsonschema
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import message_check, tool_groups, web_access
from topcoat_hub.agent_store import AgentStore
from topcoat_hub.message_service import ANSWERING, MessageService, is_request
from topcoat_hub.project_base import Invalid, NotFound
from topcoat_hub.project_events import Actor
from topcoat_hub.project_store import ProjectStore
from topcoat_hub.research_profile import DEPTH_GUIDE
from topcoat_hub.research_profile import depth_in_prompt as research_depth_in_prompt
from topcoat_hub.tools import Caller, ToolError

MESSAGES_NODE = "messages"
MESSAGES_NODE_ID = uuid.UUID("00000000-0000-4000-8000-00000000a0d6")

_UUID = {
    "type": "string",
    "pattern": "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
}
_MARKS = {"type": "array", "items": {"type": "string", "pattern": "^E[0-9]{1,4}$"}, "maxItems": 10}

SEND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply_to": _UUID,
        "kind": {"enum": ["result", "failure"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 60000},
        "claims": {
            "type": "array",
            "maxItems": 100,
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "evidence": _MARKS,
                },
                "required": ["claim", "evidence"],
                "additionalProperties": False,
            },
        },
        "coverage": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "findings": {"type": "array", "items": {"type": "integer", "minimum": 0}},
                    "note": {"type": "string", "maxLength": 1000},
                },
                "additionalProperties": False,
            },
        },
        "open_points": {"type": "array", "items": {"type": "string", "maxLength": 500}},
        "reason": {
            "enum": ["node_away", "no_budget", "no_sources", "tool_errors", "refused", "other"]
        },
        "detail": {"type": "string", "maxLength": 4000},
    },
    "required": ["kind"],
    "additionalProperties": False,
}

DESCRIPTION = (
    "Answer the request you were started for. kind=result: your summary, and every claim with the "
    "evidence marks (E1, E2 ...) of the tool results that show it; list what the request asked to "
    "cover under coverage and what you could not settle under open_points. A claim without a "
    "mark that exists is not accepted as a finding. kind=failure: the reason and what happened. "
    "The quotes, sources and times are taken from the evidence by the hub, not from you."
)


_LINE = {"type": "string", "maxLength": 500}
DEV_SEND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply_to": _UUID,
        "kind": {"enum": ["result", "failure"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 6000},
        "branch": {"type": "string", "maxLength": 200},
        "head": {"type": "string", "maxLength": 64},
        "diff_stat": {
            "type": "object",
            "properties": {
                "files": {"type": "integer", "minimum": 0},
                "insertions": {"type": "integer", "minimum": 0},
                "deletions": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
        "tests": {
            "type": "object",
            "properties": {
                "state": {"enum": ["passed", "failed", "not_run"]},
                "summary": {"type": "string", "maxLength": 4000},
            },
            "additionalProperties": False,
        },
        "acceptance": {
            "type": "array",
            "maxItems": 50,
            "items": {
                "type": "object",
                "properties": {
                    "item": _LINE,
                    "met": {"type": "boolean"},
                    "evidence": {"type": "string", "maxLength": 2000},
                },
                "required": ["item", "met"],
                "additionalProperties": False,
            },
        },
        "findings": {
            "type": "array",
            "maxItems": 100,
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "path": _LINE,
                    "lines": {"type": "string", "maxLength": 40},
                    "quote": {"type": "string", "maxLength": 2000},
                },
                "required": ["claim"],
                "additionalProperties": False,
            },
        },
        "open_points": {"type": "array", "items": _LINE, "maxItems": 50},
        "reason": {
            "enum": ["node_away", "no_budget", "no_sources", "tool_errors", "refused", "other"]
        },
        "detail": {"type": "string", "maxLength": 4000},
    },
    "required": ["kind"],
    "additionalProperties": False,
}
DEV_DESCRIPTION = (
    "Answer the order or request you were started for. kind=result: what you did in your own "
    "words (summary), the branch, the head commit, the diff statistics, the test outcome and, for "
    "each point of the acceptance, whether it is met and what shows it. Say what you did not run. "
    "The hub checks the branch against the order and shows your numbers as your own report. "
    "kind=failure: the reason and what happened. This call sends nothing out of the workshop."
)
# A long report is written in sections (research_profile.py): one model answer is limited, a
# document is not. A section is stored as a note of the task, the final message is put together
# by the hub from the abstract (summary) and the sections in the order they were first written.
SECTION = "research.report.section"
SHOW = "research.report.show"
SECTION_CHARS = 30000
SECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "markdown": {"type": "string", "minLength": 1, "maxLength": SECTION_CHARS},
    },
    "required": ["title", "markdown"],
    "additionalProperties": False,
}
SECTION_DESCRIPTION = (
    "Write one section of your report (Markdown; cite the evidence marks [E1] [E2] in the text). "
    "Use it for a long report: one section per call instead of the whole report in the final "
    "answer. A section with the same title replaces the earlier one. When you send the result, "
    "the summary is the abstract and the sections follow it in the order you first wrote them."
)
SHOW_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
SHOW_DESCRIPTION = (
    "List the sections of your report written so far, with the size of each. Use it to see "
    "what is done before you write the next section or send the result."
)
# A picture for the report (2026-11-13): the hub fetches it through the web node (web.image), keeps
# it (report_images) and answers with the Markdown line that shows it from the hub. The model never
# sees the bytes; the reader's browser never asks the picture's server.
IMAGE = "research.report.image"
MAX_IMAGES = 6  # per report
IMAGE_CONTENT_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
IMAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "image_url": {"type": "string", "minLength": 8, "maxLength": 2000},
        "page_url": {"type": "string", "minLength": 8, "maxLength": 2000},
        "caption": {"type": "string", "minLength": 4, "maxLength": 200},
    },
    "required": ["image_url", "page_url", "caption"],
    "additionalProperties": False,
}
IMAGE_DESCRIPTION = (
    "Put a picture into the report: a picture from a page you read (web.fetch lists its pictures "
    "with their description) or from web.images. Only one that shows what the text says (an "
    "example output, the tool's interface, a diagram from the source), never a logo, a stock "
    "photo or an advertisement; judge it by its description, its title and its page. caption: "
    "what it shows, in the report's language. The hub keeps the picture and answers with a "
    "Markdown line: put that line into the section where the picture belongs, with the page as "
    f"its source. At most {MAX_IMAGES} pictures; a picture over 180 KB is refused (take the "
    "smaller copy web.images names)."
)
REVIEW = "project.review.submit"
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message_id": _UUID,
        "verdict": {"enum": ["accept", "revise", "reject"]},
        "criteria": {
            "type": "array",
            "maxItems": 50,
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string", "maxLength": 500},
                    "met": {"type": "boolean"},
                    "note": {"type": "string", "maxLength": 1000},
                },
                "required": ["item", "met"],
                "additionalProperties": False,
            },
        },
        "reasons": {"type": "string", "minLength": 1, "maxLength": 4000},
        "followups": {
            "type": "array",
            "maxItems": 10,
            "items": {"type": "string", "maxLength": 500},
        },
    },
    "required": ["message_id", "verdict", "reasons"],
    "additionalProperties": False,
}
REVIEW_DESCRIPTION = (
    "Give your verdict on the message you review: accept, revise (followups say what to ask next) "
    "or reject, with the reasons and, per point of the request, whether it is met. This is the "
    "only thing you can do; the hub acts on the verdict. Call it once."
)


_OBJECT = {"type": "object"}  # the registry checks the payload of the type in depth
KINDS = {
    "research": ("result", "failure", "clarification", "changelog", "proposal"),
    "dev": ("result", "failure", "clarification", "request", "changelog", "proposal"),
    "admin": ("result", "failure", "clarification", "request", "changelog", "proposal"),
}
ASKS = {"dev": ("research", "admin", "project"), "admin": ("research",)}
EXTRA_DESCRIPTION = (
    " Other kinds: clarification {questions, missing} when you cannot go on without more "
    "information (the request waits and the person is asked); request {to, payload} to ask "
    "another application something read-only inside this job (the answer comes to the project, "
    "not to you; to research the payload carries depth: quick, default or thorough, "
    + DEPTH_GUIDE
    + "); changelog {what, by, before, after, result} and proposal {title, why, steps} "
    "for what nobody asked for (a proposal waits for the person's click)."
)


def _coverage_entry(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        if isinstance(value.get("findings"), list):
            out["findings"] = [x for x in value["findings"] if isinstance(x, int) and x >= 0]
        note = value.get("note")
        if note is None:
            rest = {k: v for k, v in value.items() if k not in ("findings", "note")}
            note = "; ".join(f"{k}: {v}" for k, v in rest.items()) if rest else None
        if note:
            out["note"] = str(note)[:1000]
        return out
    if value is None or isinstance(value, bool):
        return {} if value is not False else {"note": "not covered"}
    if isinstance(value, list):
        return {"note": "; ".join(str(x) for x in value)[:1000]}
    return {"note": str(value)[:1000]}


def normalise_send(tool_input: dict[str, Any]) -> dict[str, Any]:
    """The answer of a run in the shape the schema wants, from the shapes small models write
    (live 2026-11-04 to 06: 45 refusals). ``coverage`` as ``true``, a text or a list per point, or
    the whole of it as a list of points; a claim as a bare string or with extra keys (``tag``);
    ``open_points`` as one text. Nothing is invented: a value that cannot be read stays as it is
    and the schema refuses it."""
    if not isinstance(tool_input, dict):
        return tool_input
    out = dict(tool_input)
    cov = out.get("coverage")
    if isinstance(cov, list):
        cov = {str(x): {} for x in cov if isinstance(x, (str, int))}
    if isinstance(cov, str):
        cov = {cov: {}}
    if isinstance(cov, dict):
        out["coverage"] = {str(k)[:200]: _coverage_entry(v) for k, v in cov.items()}
    claims = out.get("claims")
    if isinstance(claims, list):
        fixed = []
        for c in claims:
            if isinstance(c, str) and c.strip():
                c = {"claim": c, "evidence": []}
            if isinstance(c, dict) and isinstance(c.get("claim"), str):
                marks = c.get("evidence") or []
                if isinstance(marks, str):
                    marks = re.findall(r"E\d{1,4}", marks)
                c = {
                    "claim": c["claim"][:1000],
                    "evidence": [m for m in marks if isinstance(m, str)],
                }
            fixed.append(c)
        out["claims"] = fixed
    if isinstance(out.get("open_points"), str):
        out["open_points"] = [out["open_points"]]
    return out


def schema_for(app: str) -> dict[str, Any]:
    """The input schema of ``<app>.message.send``: the common fields of the application, the kinds
    it may send and, for a request, the applications it may ask."""
    base = DEV_SEND_SCHEMA if app == "dev" else SEND_SCHEMA
    schema = copy.deepcopy(base)
    props = schema["properties"]
    props["kind"] = {"enum": list(KINDS[app])}
    props["clarification"] = _OBJECT
    props["changelog"] = _OBJECT
    props["proposal"] = _OBJECT
    if app in ASKS:
        props["request"] = {
            "type": "object",
            "properties": {"to": {"enum": list(ASKS[app])}, "payload": _OBJECT},
            "required": ["to", "payload"],
            "additionalProperties": False,
        }
    return schema


def capability_of(app: str) -> str:
    return f"{app}.message.send"


REPORT_SECTION = "report_section"  # the key of a note that carries a section of a report


async def _sections(store: AgentStore, task_id: uuid.UUID) -> list[tuple[str, str]]:
    """The sections of the task's report as (title, text), in the order each title first
    appeared; a later section with the same title replaces the text."""
    found: dict[str, str] = {}
    for step in await store.steps(task_id):
        data = step.data if isinstance(step.data, dict) else {}
        item = data.get(REPORT_SECTION) if step.kind == "note" else None
        if isinstance(item, dict) and item.get("title") and item.get("text"):
            found[str(item["title"])] = str(item["text"])
    return list(found.items())


SECTION_SENT_BACK = "section_sent_back"  # key of the note that records a section sent back
SECTION_PAGES_SENT_BACK = "pages_sent_back"  # ... sent back for too few pages read
_EVIDENCE_MARK = re.compile(r"\[E\d{1,4}")
_FOREWORD_TITLE = re.compile(r"^\s*(vorwort|vorbemerkung|preface|foreword)\s*$", re.IGNORECASE)
MIN_WORDS_FOR_SOURCES = 40
# a thorough report rests on pages, not on search snippets (live 2026-11-13: six searches, no page
# read, a report whose "quotes" were glued snippets): its chapters wait for this many pages from
# this many sites; sent back at most this often per run, so a web that refuses cannot hold it
MIN_PAGES, MIN_HOSTS, PAGES_SEND_BACKS = 3, 2, 2
READ_CAPABILITIES = ("web.fetch", "knowledge.read", "docs.read", "wiki.read")
SENT_BACK = {
    "nothing_read": (
        "You have not read anything yet: no search, no page, no note. A chapter written now can "
        "only come from memory, and the report would look researched when it is not. Search and "
        "read sources first (web.search, web.fetch, knowledge.search), then write the chapter and "
        "mark each statement with the evidence it rests on, like [E3]."
    ),
    "few_pages": (
        f"A thorough report rests on pages you read, not on search snippets. Read at least "
        f"{MIN_PAGES} pages from {MIN_HOSTS} different sites first (web.fetch the best hits; a "
        "page that refuses is no loss, take the next), then write this chapter from them with "
        "their marks. Pictures on those pages are listed with each read."
    ),
    "no_marks": (
        "This section states facts but cites no source. Write it again and mark each statement "
        "with the evidence it rests on, like [E3] or [E3, E5] (the marks of the tool results you "
        "read). A statement you did not read anywhere does not belong in the section."
    ),
}


def _pages_read(read: list[Any]) -> tuple[int, int]:
    """Pages (and notes) read, and the sites the web pages came from."""
    pages, hosts = 0, set()
    for e in read:
        source = getattr(e, "source", None) or {}
        if source.get("capability") not in READ_CAPABILITIES:
            continue
        pages += 1
        given = source.get("input") if isinstance(source.get("input"), dict) else {}
        url = str(source.get("url") or given.get("url") or "")
        host = urlsplit(url).hostname if url.startswith("http") else None
        hosts.add(host or source.get("capability"))
    return pages, len(hosts)


async def _lacks_sources(
    store: AgentStore, task_id: uuid.UUID, title: str, text: str, thorough: bool = False
) -> str | None:
    """Why a chapter goes back to the model, or ``None`` when it is taken.

    * ``"nothing_read"``: the run has read nothing yet (no search, no page, no note), and a chapter
      with some length is written anyway: it can only come from the model's memory, and a report
      that looks researched but is not is worse than none. Refused every time.
    * ``"no_marks"``: the run has evidence, the chapter cites none of it. Sent back once; a second
      try with the same title is accepted (the hub does not hold a run hostage to its own rule).

    The foreword is a framing text and exempt, and so is a short chapter."""
    if _FOREWORD_TITLE.match(title) or len(text.split()) < MIN_WORDS_FOR_SOURCES:
        return None
    read = [
        e
        for e in await store.evidence(task_id)
        if (getattr(e, "source", None) or {}).get("node") != MESSAGES_NODE
    ]
    if not read:
        return "nothing_read"
    if thorough:
        pages, hosts = _pages_read(read)
        if pages < MIN_PAGES or hosts < MIN_HOSTS:
            sent = sum(
                1
                for step in await store.steps(task_id)
                if step.kind == "note"
                and isinstance(step.data, dict)
                and step.data.get(SECTION_PAGES_SENT_BACK)
            )
            if sent < PAGES_SEND_BACKS:
                return "few_pages"
    if _EVIDENCE_MARK.search(text):
        return None
    for step in await store.steps(task_id):
        data = step.data if isinstance(step.data, dict) else {}
        if step.kind == "note" and data.get(SECTION_SENT_BACK) == title:
            return None
    return "no_marks"


def _with_sections(summary: str, sections: list[tuple[str, str]]) -> str:
    """The abstract, then the sections as Markdown chapters; no sections, the summary as it is."""
    if not sections:
        return summary
    body = "\n\n".join(f"## {title}\n\n{text}" for title, text in sections)
    return f"{summary.strip()}\n\n{body}"


class MessageTools:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], hub_timezone: str = "UTC"
    ) -> None:
        self.sm = sessionmaker
        self.hub_timezone = hub_timezone
        self.agents: Any = None
        self.proposals: Any = None  # a push under a mandate (set by main)

    async def _answers_a_request(self, caller: Caller) -> bool:
        """Whether the run of this caller was started for a request of its own application (the
        same test as in :meth:`run_tool`)."""
        if (
            caller.task_id is None
        ):  # queued now, not yet run: the tool is offered (run_tool decides)
            return True
        store = ProjectStore(self.sm, Actor("app", app=caller.application), self.hub_timezone)
        request = await store.message_for_task(caller.user_id, caller.task_id)
        return (
            request is not None
            and is_request(request.message_type)
            and request.target_app == caller.application
        )

    async def offer(self, caller: Caller) -> list[dict[str, Any]]:
        if caller.application == "project":  # only a reviewer run, which has an allow-list
            if caller.allowed_capabilities is None or REVIEW not in caller.allowed_capabilities:
                return []
            return [
                {
                    "node_id": str(MESSAGES_NODE_ID),
                    "node": MESSAGES_NODE,
                    "capability": REVIEW,
                    "description": REVIEW_DESCRIPTION,
                    "input_schema": REVIEW_SCHEMA,
                }
            ]
        if caller.application not in ANSWERING:
            return []
        dev = caller.application == "dev"
        tools = []
        # a research run for the person's chat answers no request: the send tool would only be
        # refused ("no_request", live 2026-11-06, five times), so it is not offered to it
        if caller.application != "research" or await self._answers_a_request(caller):
            tools.append(
                {
                    "node_id": str(MESSAGES_NODE_ID),
                    "node": MESSAGES_NODE,
                    "capability": capability_of(caller.application),
                    "description": (DEV_DESCRIPTION if dev else DESCRIPTION) + EXTRA_DESCRIPTION,
                    "input_schema": schema_for(caller.application),
                }
            )
        if caller.application == "research" and caller.task_id is not None:
            # the picture tool too: whether a node fetches pictures is asked when it is called (the
            # list of tools cannot be asked from here: it includes this offer)
            for capability, description, schema in (
                (SECTION, SECTION_DESCRIPTION, SECTION_SCHEMA),
                (SHOW, SHOW_DESCRIPTION, SHOW_SCHEMA),
                (IMAGE, IMAGE_DESCRIPTION, IMAGE_SCHEMA),
            ):
                tools.append(
                    {
                        "node_id": str(MESSAGES_NODE_ID),
                        "node": MESSAGES_NODE,
                        "capability": capability,
                        "description": description,
                        "input_schema": schema,
                    }
                )
        return tools

    async def run_tool(
        self, caller: Caller, capability: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        app = caller.application
        if capability == REVIEW:
            return await self._review(caller, tool_input)
        if capability in (SECTION, SHOW):
            return await self._section(caller, capability, tool_input)
        if capability == IMAGE:
            return await self._image(caller, tool_input)
        if app not in ANSWERING or capability != capability_of(app) or caller.task_id is None:
            raise ToolError(404, "unknown_capability", f"no such capability: {capability}")
        if app != "dev":  # the dev answer has its own fields (DEV_SEND_SCHEMA)
            tool_input = normalise_send(tool_input)
        try:
            jsonschema.validate(tool_input, schema_for(app))
        except jsonschema.ValidationError as exc:
            raise ToolError(422, "invalid_input", exc.message) from exc
        user_id = caller.user_id
        kind = tool_input["kind"]
        store = ProjectStore(self.sm, Actor("app", app=app), self.hub_timezone)
        service = MessageService(store, self.agents, self.proposals)
        try:
            request = await store.message_for_task(user_id, caller.task_id)
            if request is not None and not is_request(request.message_type):
                request = None  # the run belongs to a review or a sorting, not to a request
            if request is not None and request.target_app != app:
                request = None
            if kind in ("changelog", "proposal") and request is None:
                # nobody asked: it goes to the Inbox, is received and not acted on (05f)
                if not isinstance(tool_input.get(kind), dict):
                    raise ToolError(422, "invalid_input", f"a {kind} needs its fields")
                node = await service.receive_unsolicited(
                    user_id, sender=app, kind=kind, payload=tool_input[kind]
                )
                return self._answer(node)
            if request is None:
                raise ToolError(409, "no_request", "this run was not started for a request")
            if tool_input.get("reply_to") and str(request.id) != tool_input["reply_to"]:
                raise ToolError(422, "invalid_input", "reply_to is not the request of this run")
            if kind in ("changelog", "proposal"):
                node = await self._in_chain(service, user_id, request, app, kind, tool_input)
                return self._answer(node)
            if request.msg_state not in ("queued", "running"):
                raise ToolError(
                    409, "already_answered", f"the request is {request.msg_state}: no second answer"
                )
            async with self.sm() as db:
                agent_store = AgentStore(db, user_id)
                task = await agent_store.get(caller.task_id)
                evidence = list(await agent_store.evidence(caller.task_id))
                if task is None:
                    raise ToolError(404, "not_found", "the task is gone")
                sections = await _sections(agent_store, caller.task_id)
                db.expunge_all()
            if kind == "failure":
                payload: dict[str, Any] = {
                    "reason": tool_input.get("reason", "other"),
                    "retryable": False,
                }
                if tool_input.get("detail"):
                    payload["detail"] = tool_input["detail"]
                node = await service.deliver_failure(user_id, request, payload, task)
            elif kind == "clarification":
                node = await service.deliver_clarification(
                    user_id, request, tool_input.get("clarification") or {}, task
                )
            elif kind == "request":
                asked = tool_input.get("request") or {}
                node, note = await service.send_agent_request(
                    user_id, caller.admin, request, sender=app, target=asked["to"],
                    payload=asked["payload"],
                )  # fmt: skip
                out = self._answer(node)
                out["output"]["queue_note"] = note
                return out
            elif not tool_input.get("summary"):
                raise ToolError(422, "invalid_input", "a result needs a summary")
            elif app == "dev":
                node = await service.deliver_dev_result(user_id, request, tool_input, task)
            else:
                node = await service.deliver_result(
                    user_id,
                    request,
                    summary=_with_sections(tool_input["summary"], sections),
                    claims=tool_input.get("claims", []),
                    task=task,
                    evidence=evidence,
                    open_points=tool_input.get("open_points"),
                    coverage=tool_input.get("coverage"),
                )
        except NotFound as exc:
            raise ToolError(404, "not_found", "there is no such item") from exc
        except Invalid as exc:
            raise ToolError(422, "invalid_input", str(exc)) from exc
        return self._answer(node)

    async def _section(
        self, caller: Caller, capability: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        """``research.report.section`` stores a section as a note of the task and
        ``research.report.show`` lists the sections. Only a research run of a task has them."""
        if caller.application != "research" or caller.task_id is None:
            raise ToolError(404, "unknown_capability", f"no such capability: {capability}")
        schema = SECTION_SCHEMA if capability == SECTION else SHOW_SCHEMA
        try:
            jsonschema.validate(tool_input, schema)
        except jsonschema.ValidationError as exc:
            raise ToolError(422, "invalid_input", exc.message) from exc
        async with self.sm() as db:
            store = AgentStore(db, caller.user_id)
            task = await store.get(caller.task_id)
            if task is None:
                raise ToolError(404, "not_found", "the task is gone")
            if capability == SECTION:
                title = tool_input["title"].strip()
                text = tool_input["markdown"].strip()
                thorough = research_depth_in_prompt(task.prompt) == "thorough"
                why_not = await _lacks_sources(store, caller.task_id, title, text, thorough)
                if why_not:
                    note: dict[str, Any] = {"text": f"Section sent back ({why_not}): {title}"}
                    if why_not == "few_pages":
                        note[SECTION_PAGES_SENT_BACK] = True
                    else:
                        note[SECTION_SENT_BACK] = title
                    await store.add_step(task, "note", note)
                    raise ToolError(422, "needs_sources", SENT_BACK[why_not])
                await store.add_step(
                    task,
                    "note",
                    {
                        "text": f"Section written: {title} ({len(text.split())} words)",
                        REPORT_SECTION: {"title": title, "text": text},
                    },
                )
            sections = await _sections(store, caller.task_id)
        output = {
            "sections": [{"title": t, "words": len(x.split())} for t, x in sections],
            "words": sum(len(x.split()) for _, x in sections),
        }
        return {"ok": True, "output": output, "at": None, "bytes": len(str(output))}

    async def _picture_node(self, caller: Caller) -> uuid.UUID | None:
        """The web node that fetches pictures for this run (the one the hub reads the web from),
        or None when no node offers web.image to this person."""
        tools = getattr(getattr(self, "agents", None), "tools", None)
        if tools is None:
            return None
        available = await tools.available(caller)
        web_node = tool_groups.pick_web_node(available)
        for t in available:
            if t.get("capability") == web_access.IMAGE and (
                web_node is None or t["node"] == web_node
            ):
                return uuid.UUID(str(t["node_id"]))
        return None

    async def _image(self, caller: Caller, tool_input: dict[str, Any]) -> dict[str, Any]:
        """``research.report.image``: fetch the picture through the web node, keep it, answer
        with the Markdown line that shows it from the hub."""
        from topcoat_hub.models import ReportImage

        if caller.application != "research" or caller.task_id is None:
            raise ToolError(404, "unknown_capability", f"no such capability: {IMAGE}")
        try:
            jsonschema.validate(tool_input, IMAGE_SCHEMA)
        except jsonschema.ValidationError as exc:
            raise ToolError(422, "invalid_input", exc.message) from exc
        caption = " ".join(tool_input["caption"].split())[:200]
        async with self.sm() as db:
            placed = await db.scalar(
                select(func.count())
                .select_from(ReportImage)
                .where(ReportImage.task_id == caller.task_id, ReportImage.user_id == caller.user_id)
            )
        if (placed or 0) >= MAX_IMAGES:
            raise ToolError(409, "enough_images", f"the report has {MAX_IMAGES} pictures already")
        node = await self._picture_node(caller)
        if node is None:
            raise ToolError(409, "no_web", "no node fetches pictures for this run")
        res = await self.agents.tools.invoke(
            caller, node, web_access.IMAGE, {"url": tool_input["image_url"]}
        )
        if not res.get("ok", True):
            err = res.get("error") or {}
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise ToolError(422, "image_failed", message or "the picture could not be fetched")
        out = res.get("output")
        if isinstance(out, str | bytes):
            try:
                out = json.loads(out)
            except ValueError:
                out = {}
        out = out if isinstance(out, dict) else {}
        ctype = str(out.get("content_type") or "")
        try:
            data = base64.b64decode(str(out.get("data") or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ToolError(422, "image_failed", "the node sent no picture") from exc
        if ctype not in IMAGE_CONTENT_TYPES or not data:
            raise ToolError(422, "image_failed", "the node sent no picture")
        async with self.sm() as db:
            image = ReportImage(
                user_id=caller.user_id,
                task_id=caller.task_id,
                content_type=ctype,
                data=data,
                image_url=str(out.get("url") or tool_input["image_url"])[:2000],
                page_url=tool_input["page_url"][:2000],
                caption=caption,
            )
            db.add(image)
            await db.commit()
            image_id = image.id
        line = f"![{caption}](/api/reports/images/{image_id})"
        output = {
            "markdown": line,
            "source": tool_input["page_url"],
            "note": "put the markdown line into the section where the picture belongs and name "
            "its page as the source",
        }
        return {"ok": True, "output": output, "at": None, "bytes": len(str(output))}

    @staticmethod
    def _answer(node: Any) -> dict[str, Any]:
        payload_out = node.payload or {}
        output = {
            "filed": True,
            "message_id": str(node.id),
            "type": node.message_type,
            "findings": len(payload_out.get("findings") or []),
            "open_points": payload_out.get("open_points") or [],
        }
        return {"ok": True, "output": output, "at": None, "bytes": len(str(output))}

    async def _in_chain(
        self,
        service: MessageService,
        user_id: uuid.UUID,
        request: Any,
        app: str,
        kind: str,
        tool_input: dict[str, Any],
    ) -> Any:
        """A changelog or a proposal that comes with a request: filed in the chain it belongs to
        (a proposal still waits for the person's click)."""
        if not isinstance(tool_input.get(kind), dict):
            raise ToolError(422, "invalid_input", f"a {kind} needs its fields")
        return await service.store.with_actor(Actor("app", app=app)).add_message(
            user_id, request.project_id, type=f"{app}.{kind}", sender=app, receiver="project",
            payload=message_check.clean(tool_input[kind]),
            state="awaiting_click" if kind == "proposal" else "accepted",
            chain_id=request.chain_id, in_reply_to=request.id, round=request.round or 1,
            parent_id=request.parent_id,
        )  # fmt: skip

    async def _review(self, caller: Caller, tool_input: dict[str, Any]) -> dict[str, Any]:
        if (
            caller.application != "project"
            or caller.task_id is None
            or caller.allowed_capabilities is None
            or REVIEW not in caller.allowed_capabilities
        ):
            raise ToolError(404, "unknown_capability", f"no such capability: {REVIEW}")
        try:
            jsonschema.validate(tool_input, REVIEW_SCHEMA)
        except jsonschema.ValidationError as exc:
            raise ToolError(422, "invalid_input", exc.message) from exc
        store = ProjectStore(
            self.sm, Actor("agent", agent_task_id=caller.task_id), self.hub_timezone
        )
        try:
            message = await store.message_for_task(caller.user_id, caller.task_id)
            if message is None or message.msg_state != "reviewing":
                raise ToolError(409, "no_review", "this run reviews no message that waits for it")
            if str(message.id) != tool_input["message_id"]:
                raise ToolError(422, "invalid_input", "message_id is not the message of this run")
            verdict = {k: v for k, v in tool_input.items() if k != "message_id"}
            await MessageService(store, self.agents, self.proposals).apply_verdict(
                caller.user_id, message, verdict
            )
        except NotFound as exc:
            raise ToolError(404, "not_found", "there is no such item") from exc
        except Invalid as exc:
            raise ToolError(422, "invalid_input", str(exc)) from exc
        output = {"recorded": True, "verdict": tool_input["verdict"]}
        return {"ok": True, "output": output, "at": None, "bytes": len(str(output))}
