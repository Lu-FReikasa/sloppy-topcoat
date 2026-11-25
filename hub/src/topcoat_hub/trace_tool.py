"""How the hub handled one answer, for the assistant of the development program (read only).

The person wants to understand how an old request went through Topcoat and have the development
assistant walk through it with the code. Two tools make that possible and nothing more:

* ``trace.read`` reads the **structure** of one answer: which steps ran in which order, how long
  they took, whether they worked, and for each step where in the code it happens. It never returns
  what was said (neither the question, the answer nor what a tool brought back) and it never
  names a machine or a model: those stay in the person's own interface.
* ``repo.show`` asks the person's file reader to open a file at some lines. It changes nothing but
  what the person sees.

The assistant gets to see an answer only if the **person handed it over**: the button "Look at
this in Development" asks the hub for a **reference** (``trace:<chat>:<message>:<tag>``), and the
tag is a keyed hash of the person and the message, so a reference cannot be made up by a model, only
copied from what the person gave it. Without a reference the tool reads nothing: there is no
blanket access to the history.
"""

import re
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.chat_store import ChatStore
from topcoat_hub.devrepos import DevRepoStore
from topcoat_hub.secretbox import SecretBox
from topcoat_hub.tools import Caller, ToolError

TRACE_CAPABILITY = "trace.read"
SHOW_CAPABILITY = "repo.show"
NODE = "topcoat"
NODE_ID = uuid.UUID(
    "00000000-0000-4000-8000-00000000a0d3"
)  # the hub's own view, like topcoat.state

REF = re.compile(r"^trace:([0-9a-f]{8}):(\d{1,12}):([0-9a-f]{10})$")
_TAG = 10
MAX_PATH = 300
_NODE_AT = re.compile(r"@\s*[\w.\-]+")
_CODE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")

# what an activity item may say about itself: names of things, never content
_DATA_KEYS = ("seq", "second_look", "flow", "node", "type", "step", "calls", "evidence", "code")

# Where in the code each kind of step happens. ``find`` is a text to search in the file (a line
# number would be wrong after the next change); the assistant opens it with repo.show or searches
# for it with repo.search.
_POINTERS: dict[str, list[dict[str, str]]] = {
    "thinking": [{"path": "hub/src/topcoat_hub/activity.py", "find": "class ThinkingTrace"}],
    "lead_in": [{"path": "hub/src/topcoat_hub/api/chat.py", "find": 'activity.add("lead_in"'}],
    "plan": [{"path": "hub/src/topcoat_hub/activity.py", "find": "class CheckTrace"}],
    "tool": [
        {"path": "hub/src/topcoat_hub/activity.py", "find": "class CheckTrace"},
        {"path": "hub/src/topcoat_hub/chat_tools.py", "find": '"tools_start"'},
    ],
    "check": [
        {"path": "hub/src/topcoat_hub/chat_tools.py", "find": '"tools_start"'},
        {"path": "hub/src/topcoat_hub/api/chat.py", "find": "async def _verify_answer"},
    ],
    "retry": [{"path": "hub/src/topcoat_hub/api/chat.py", "find": "async def _answer"}],
    "steer": [{"path": "hub/src/topcoat_hub/api/chat.py", "find": "async def steer_check"}],
    "note": [{"path": "hub/src/topcoat_hub/activity.py", "find": "class CheckTrace"}],
    "announce": [{"path": "hub/src/topcoat_hub/api/chat.py", "find": 'activity.add("announce"'}],
}
_SECOND_LOOK = [{"path": "hub/src/topcoat_hub/api/chat.py", "find": "async def _verify_answer"}]

# The way of every answer through the hub, in order, for the assistant to walk along.
PIPELINE: list[dict[str, str]] = [
    {
        "step": "the request arrives and is stored",
        "path": "hub/src/topcoat_hub/api/chat.py",
        "find": "async def send_message",
    },
    {
        "step": "scope, model, node and the effort are settled; the prompt is assembled "
        "(rules, notes of the memory, hand-over of an earlier section)",
        "path": "hub/src/topcoat_hub/api/chat.py",
        "find": "async def _prepare",
    },
    {
        "step": "the hand-over from the running summary of the chat",
        "path": "hub/src/topcoat_hub/segments.py",
        "find": "class SegmentService",
    },
    {
        "step": "the notes the memory recalls for this question",
        "path": "hub/src/topcoat_hub/memory.py",
        "find": "async def recall",
    },
    {
        "step": "a server check runs first when the question is about the machines",
        "path": "hub/src/topcoat_hub/chat_tools.py",
        "find": '"tools_start"',
    },
    {
        "step": "the answer streams from the chosen model (placement by the node policy)",
        "path": "hub/src/topcoat_hub/api/chat.py",
        "find": "async def _stream_turn",
    },
    {
        "step": "the model is reached through the gateway",
        "path": "hub/src/topcoat_hub/gateway.py",
        "find": "async def call_streamed",
    },
    {
        "step": "afterwards, in the background: running summary, second look, review",
        "path": "hub/src/topcoat_hub/chat_digest.py",
        "find": "def after_answer",
    },
]

TRACE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reference"],
    "properties": {
        "reference": {
            "type": "string",
            "maxLength": 60,
            "description": "the reference the person gave you, exactly as written "
            "(trace:<chat>:<message>:<tag>); you cannot make one up",
        }
    },
}
TRACE_DESCRIPTION = (
    "How Topcoat handled one earlier answer of the person's, read only: the steps in order "
    "(kind, how long, whether it worked), where in the code each one happens (path and a text to "
    "search for), and the usual way of an answer through the hub. It never returns what was said "
    "and never a machine or model name. It reads only an answer whose reference the person gave "
    "you; open the places with repo.show and explain them from the code."
)
SHOW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["repo", "path"],
    "properties": {
        "repo": {"type": "string", "maxLength": 100, "description": "the registered repository"},
        "path": {
            "type": "string",
            "maxLength": MAX_PATH,
            "description": "a file or a folder inside the repository, relative, with /",
        },
        "from": {"type": "integer", "minimum": 1, "description": "first line to mark"},
        "to": {"type": "integer", "minimum": 1, "description": "last line to mark"},
        "find": {
            "type": "string",
            "maxLength": 120,
            "description": "alternatively a text to jump to (the first place it occurs)",
        },
    },
}
SHOW_DESCRIPTION = (
    "Points the PERSON's file reader at a file or a folder of a registered repository, marked at "
    "some lines. It does NOT return the file's text to you: to read a file yourself call repo.read "
    "of the development node (repo, path, start_line, end_line). It changes nothing; it only moves "
    "what the person looks at. The reader may not be open: say where you point to in your answer "
    "too."
)


def trace_tool() -> dict[str, Any]:
    return {
        "node_id": str(NODE_ID),
        "node": NODE,
        "capability": TRACE_CAPABILITY,
        "description": TRACE_DESCRIPTION,
        "input_schema": TRACE_SCHEMA,
    }


def show_tool() -> dict[str, Any]:
    return {
        "node_id": str(NODE_ID),
        "node": NODE,
        "capability": SHOW_CAPABILITY,
        "description": SHOW_DESCRIPTION,
        "input_schema": SHOW_SCHEMA,
    }


def clean_path(raw: str) -> str:
    """A path inside a repository: relative, with /, no ``..``, no drive, nothing absolute."""
    path = raw.strip().replace("\\", "/").strip("/")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if (
        not parts
        or len(path) > MAX_PATH
        or any(p == ".." or ":" in p or "\x00" in p for p in parts)
        or any(p == ".git" for p in parts)
    ):
        raise ToolError(422, "invalid_input", "path must be a relative path inside the repository")
    return "/".join(parts)


def mask_title(title: str) -> str:
    """A step's technical name without the machine (``docker.ps @ node-a`` -> ``docker.ps``)."""
    return _NODE_AT.sub("", title).strip()


class TraceService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        box: SecretBox,
        repos: DevRepoStore | None,
    ) -> None:
        self.sm = sessionmaker
        self.box = box
        self.repos = repos

    # --- the person's side: hand an answer over ----------------------------------------------

    def _tag(self, user_id: uuid.UUID, conversation_id: uuid.UUID, message_id: int) -> str:
        return self.box.sign(f"trace|{user_id}|{conversation_id}|{message_id}", _TAG)

    async def reference(
        self, user_id: uuid.UUID, conversation_id: uuid.UUID, message_id: int
    ) -> str:
        """The reference for one message of the person's own chat (never for anybody's else)."""
        async with self.sm() as db:
            store = ChatStore(db, user_id)
            if await store.get_conversation(conversation_id) is None:
                raise ToolError(404, "not_found", "no such conversation of yours")
            messages = await store.messages(conversation_id)
        if not any(m.id == message_id and m.kind == "say" for m in messages):
            raise ToolError(404, "not_found", "no such message in this conversation")
        tag = self._tag(user_id, conversation_id, message_id)
        return f"trace:{str(conversation_id)[:8]}:{message_id}:{tag}"

    # --- the assistant's side -----------------------------------------------------------------

    async def _resolve(self, db: AsyncSession, caller: Caller, reference: str) -> tuple[Any, int]:
        match = REF.match(reference.strip())
        if match is None:
            raise ToolError(422, "invalid_input", "that is not a reference the person gave you")
        short, message_id, tag = match.group(1), int(match.group(2)), match.group(3)
        rows = (
            await db.execute(
                text(
                    "SELECT id FROM conversations WHERE user_id = :u AND id::text LIKE :p LIMIT 2"
                ),
                {"u": caller.user_id, "p": f"{short}%"},
            )
        ).all()
        # one answer for one person: the tag binds the person, the chat and the message
        if len(rows) != 1:
            raise ToolError(404, "not_found", "that reference does not lead to an answer of yours")
        conversation_id = rows[0][0]
        if not _same(tag, self._tag(caller.user_id, conversation_id, message_id)):
            raise ToolError(403, "not_handed_over", "the person did not hand this answer over")
        conv = await ChatStore(db, caller.user_id).get_conversation(conversation_id)
        if conv is None:
            raise ToolError(404, "not_found", "no such conversation of yours")
        return conv, message_id

    async def read(self, caller: Caller, tool_input: dict[str, Any]) -> dict[str, Any]:
        reference = tool_input.get("reference")
        if not isinstance(reference, str):
            raise ToolError(422, "invalid_input", "reference must be a text")
        async with self.sm() as db:
            conv, message_id = await self._resolve(db, caller, reference)
            messages = [m for m in await ChatStore(db, caller.user_id).messages(conv.id)]
        say = [m for m in messages if m.kind == "say"]
        at = next((i for i, m in enumerate(say) if m.id == message_id), None)
        if at is None:
            raise ToolError(404, "not_found", "no such message in this conversation")
        answer = say[at]
        if answer.role != "assistant":  # the person handed over a question: its answer follows
            answer = next((m for m in say[at + 1 :] if m.role == "assistant"), None)  # type: ignore[assignment]
            if answer is None:
                raise ToolError(404, "not_found", "that question has no answer yet")
        question = next((m for m in reversed(say[: say.index(answer)]) if m.role == "user"), None)
        return summarise(conv, question, answer)

    # --- repo.show ----------------------------------------------------------------------------

    async def show(self, caller: Caller, tool_input: dict[str, Any]) -> dict[str, Any]:
        unknown = set(tool_input) - set(SHOW_SCHEMA["properties"])
        if unknown:
            raise ToolError(422, "invalid_input", f"unknown input: {', '.join(sorted(unknown))}")
        repo = tool_input.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            raise ToolError(
                422, "invalid_input", "repo must be the name of a registered repository"
            )
        path = clean_path(str(tool_input.get("path", "")))
        shown: dict[str, Any] = {"repo": repo.strip(), "path": path}
        for key in ("from", "to"):
            value = tool_input.get(key)
            if value is not None:
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise ToolError(422, "invalid_input", f"{key} must be a line number")
                shown[key] = value
        if "from" in shown and "to" in shown and shown["to"] < shown["from"]:
            raise ToolError(422, "invalid_input", "to must not be before from")
        find = tool_input.get("find")
        if find is not None:
            if not isinstance(find, str) or not find.strip() or len(find) > 120:
                raise ToolError(422, "invalid_input", "find must be a short text")
            shown["find"] = find
        if self.repos is not None:
            names = {r.name.lower() for r in await self.repos.list(caller.user_id)}
            if shown["repo"].lower() not in names:
                raise ToolError(404, "unknown_repo", "no such registered repository")
        return {
            "shown": True,
            **shown,
            "note": "this only points the person's file reader at the place; you got no text "
            "of the file. To read it yourself call repo.read (repo, path, start_line, end_line) "
            "on the development node. Say where you point to in your answer.",
        }


def _same(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a, b)


def pointers_for(item: dict[str, Any]) -> list[dict[str, str]]:
    data = item.get("data") or {}
    if item.get("kind") == "check" and data.get("second_look"):
        return list(_SECOND_LOOK)
    return list(_POINTERS.get(str(item.get("kind")), []))


def summarise(conv: Any, question: Any, answer: Any) -> dict[str, Any]:
    """The structure of one answer. Nothing in it is something that was said."""
    activity = answer.activity or []
    steps = []
    for item in activity:
        data = item.get("data") or {}
        steps.append(
            {
                "id": item.get("id"),
                "kind": item.get("kind"),
                "status": item.get("status"),
                "ms": item.get("ms"),
                # only a call has a name worth giving; a check is titled by the model that looks,
                # a flow node by text of the person's
                "what": mask_title(str(item.get("title") or ""))
                if item.get("kind") == "tool"
                else "",
                "detail_chars": len(item.get("detail") or ""),
                "facts": {k: data[k] for k in _DATA_KEYS if k in data and _plain(data[k])},
                "code": pointers_for(item),
            }
        )
    error = answer.error if isinstance(answer.error, str) and _CODE.match(answer.error) else None
    groups = (conv.tool_scope or {}).get("groups") if isinstance(conv.tool_scope, dict) else None
    verification = answer.verification if isinstance(answer.verification, dict) else {}
    return {
        "application": conv.application,
        "effort": conv.quality,
        "tool_groups": groups,
        "question_chars": len(question.content) if question is not None else None,
        "answer_chars": len(answer.content or ""),
        "answered_at": answer.created_at.isoformat() if answer.created_at else None,
        "outcome": {
            "status": answer.status,
            "error_code": error,
            "duration_ms": answer.duration_ms,
            "first_token_ms": answer.first_token_ms,
            "prompt_tokens": answer.prompt_tokens,
            "completion_tokens": answer.completion_tokens,
            "context_trimmed": answer.context_trimmed,
            "node_changed": bool(answer.fallback_from),
        },
        "memory_notes_recalled": len(answer.memory_refs or []),
        "server_check_ran": answer.agent_task_id is not None,
        "second_look": {
            k: v for k, v in verification.items() if k in ("verdict", "status") and _plain(v)
        },
        "steps": steps,
        "pipeline": PIPELINE,
        "note": "structure only: nothing that was said, no machine and no model names; open the "
        "places with repo.show (path, find) and explain them from the code",
    }


def _plain(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) and (
        not isinstance(value, str) or len(value) <= 80
    )


__all__ = [
    "NODE_ID",
    "PIPELINE",
    "SHOW_CAPABILITY",
    "TRACE_CAPABILITY",
    "TraceService",
    "clean_path",
    "mask_title",
    "pointers_for",
    "show_tool",
    "summarise",
    "trace_tool",
]
