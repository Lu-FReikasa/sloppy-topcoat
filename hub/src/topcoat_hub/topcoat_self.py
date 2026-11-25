"""Topcoat as a tool of the assistant: it can read Topcoat itself to guide the user through it.

Two virtual tools of the hub (no machine behind them, like the notes and the inventory). Unlike
those they are offered with no node connected too: Topcoat can explain itself on its own.

- ``topcoat.guide`` — what Topcoat can do and where it is in the GUI. Text that ships with the
  hub build (evidence tag ``doc``).
- ``topcoat.state`` — what is set up right now (evidence tag ``live``): the user's own chats, the
  user's own settings, the connected servers and tools the user may run, and — for an admin only —
  the hub's configuration.

Read-only by construction: nothing here writes. Every read is scoped to the caller (``user_id``);
an admin does not see other people's chats through it. Anything that looks like a secret is
blackened in what the assistant gets. The user switches the tool per conversation with the tool
group ``topcoat``; an operator can switch it off for the whole hub (``self_tool_enabled``).
"""

import re
import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub import __version__, node_rename, tool_groups
from topcoat_hub.chat_store import ChatStore
from topcoat_hub.config import Settings
from topcoat_hub.models import (
    AgentTask,
    KnowledgeSource,
    MemoryItem,
    Node,
    NodeInventory,
    User,
    UserSettings,
)
from topcoat_hub.tools import Caller, ToolError, ToolService
from topcoat_hub.views import node_summary

TOPCOAT_NODE = "topcoat"
TOPCOAT_NODE_ID = uuid.UUID("00000000-0000-4000-8000-00000000a0d3")
GUIDE_CAPABILITY = "topcoat.guide"
STATE_CAPABILITY = "topcoat.state"

MAX_CHATS = 30
MAX_MESSAGES = 40
MAX_NOTES = 40
MESSAGE_CHARS = 700
HIDDEN = "[hidden]"

# a value that names a secret, or a long token-like string: never shown
_SECRET_KEY = re.compile(r"(token|secret|passw|passphrase|api[_-]?key|private|credential)", re.I)
_SECRET_TEXT = re.compile(
    r"(?i)\b(token|secret|passw\w*|api[_-]?key|authorization|bearer)\b(\s*[:=]\s*|\s+)\S+"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"
    r"|\b(?:sk|ghp|gho|xox[bap]|AKIA)[-_A-Za-z0-9]{16,}\b",
    re.S,
)
# hub settings that say where things live on the host or how to reach other systems
_CONFIG_HIDDEN = {
    "database_url",
    "dev_admin_token",
    "tls_dir",
    "tls_cert_file",
    "tls_key_file",
    "tls_ca_file",
    "tls_hostnames",
    "public_url",
    "proto_root",
    "web_dist",
    "wol_broadcast",
    "host",
}


def scrub(text: str) -> str:
    """The text with everything that looks like a secret blackened."""
    return _SECRET_TEXT.sub(HIDDEN, text)


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


# --- what Topcoat can do, as the assistant explains it ----------------------------------------
# One entry per area: where it is on the desktop (program > view, docs/DESKTOP.md), what it does,
# what to know. "Program" is the word of the interface for what the code calls a workspace.
FEATURES: dict[str, dict[str, str]] = {
    "desktop": {
        "title": "The desktop and its programs",
        "where": "the whole page; the taskbar at the bottom",
        "text": (
            "Topcoat is one desktop with programs: Inbox, Chat, Research, Development, Incognito, "
            "Administration (admins) and Settings. A program opens, is minimised to the taskbar, "
            "maximised or closed; open programs stand side by side. Each program has its views on "
            "the left. Ctrl+K opens the command palette to find any program, view or action by "
            "name; Ctrl+Enter opens the conversation of the program in front, Alt+1 to 9 the "
            "programs of the taskbar, Escape minimises. The colour and the look (Terminal or "
            "Leitstand, the control room) are set in the person's menu at the bottom right."
        ),
    },
    "inbox": {
        "title": "Inbox, board and hand-overs",
        "where": "Inbox > Inbox, Board, Hand-overs, Tasks",
        "text": (
            "What happened and wants to be read: answers of hand-overs, finished background tasks,"
            " proposals waiting for a click, failures. Reading an item files it in the corner of "
            "its program unless it is pinned (the board) or urgent and not settled. Comments, "
            "Post-Its, stickers and highlights can be stuck on an item; a pinned answer is checked"
            " again every day and marked still correct or outdated."
        ),
    },
    "chat": {
        "title": "Conversations",
        "where": "Chat > Conversation (also Research and Development)",
        "text": (
            "Conversations with a local model, in the program they were started in. Per "
            "conversation: the effort (quick, balanced, careful: how much trouble the answer "
            "takes), the way of answering (a flow) and the tools, within what the program allows. "
            "When a question needs a look at the user's systems the chat runs a read-only check "
            "with evidence tags and answers from what it found. 'Details' shows the list of "
            "conversations, models and timings."
        ),
    },
    "tools": {
        "title": "What each program may use",
        "where": "Chat, Research, Development > Settings",
        "text": (
            "Every program has a ceiling of tool groups set by the admin: Chat and Research the "
            "notes and the web, Development the workshop and GitHub, Administration the servers "
            "(Docker, systemd and journal, host health, network, monitors, DNS and certificates, "
            "identities, Topcoat itself). A conversation may switch tools off, never on beyond the"
            " ceiling, and grants per user and node still apply. All tools are read-only."
        ),
    },
    "handover": {
        "title": "Hand-over between programs",
        "where": "the link 'Hand over' in a conversation; Inbox > Hand-overs",
        "text": (
            "What a program cannot do it hands to another: the assistant writes a dossier (the "
            "request, a few quotes, what is asked, what was kept back), the person reads it and "
            "sends it; the assistant may propose one itself but never sends it. The answer comes "
            "back as a short summary in the inbox; details that are private stay in the other "
            "program."
        ),
    },
    "incognito": {
        "title": "Incognito",
        "where": "the program Incognito",
        "text": (
            "Conversations that are never written: no memory, no notes, no tools but the web "
            "(switchable per tab), no row in the database. A tab stays until it is burnt or the "
            "hub restarts. A web search still leaves the hub."
        ),
    },
    "tasks": {
        "title": "Tasks (the agent)",
        "where": "Inbox > Tasks",
        "text": (
            "A background run of the agent: it chooses read-only tools, collects evidence and "
            "writes a diagnosis with findings; can be cancelled and continued with fresh limits. A"
            " big question can be split into one check per machine. Changing actions exist only as"
            " proposals that run after one click by the owner, one concrete action at a time."
        ),
    },
    "memory": {
        "title": "Memory",
        "where": "Memory in every program; Settings > Memory for the core and the tags",
        "text": (
            "Every program keeps its own memory; no program reads another's. The core (who the "
            "person is, what they like, how they want answers) is read by all programs. Every "
            "note carries tags from one tag tree; a branch names the program responsible for its "
            "topic and may be sensitive (hidden until the person releases it for a program). The "
            "care run compares the memories: a contradiction goes to the responsible program, "
            "which keeps, corrects or retires the notes and tells the other program what it needs "
            "to know; a topic of the person becomes a question. The person sees, edits, confirms "
            "and deletes every note."
        ),
    },
    "knowledge": {
        "title": "Knowledge base",
        "where": "Research > Knowledge",
        "text": (
            "Folders of Markdown files are indexed read-only and searched (hybrid search). Files "
            "that look sensitive are never read. Hits count as doc evidence and can be out of "
            "date, so a live check wins over a note."
        ),
    },
    "machines": {
        "title": "Machines, models and inventory",
        "where": "Administration > Nodes, Models",
        "text": (
            "A node is a small agent on one machine of the lab. It connects out to the hub (nodes "
            "open no port), reports GPU, memory and models, and loads, unloads and downloads "
            "models on request; it yields its GPU when a game needs it. Machines lists every node "
            "live with what runs on it (Inventory tab) and has the button to add a node with a "
            "pairing code; Models shows the models and which machine does which work."
        ),
    },
    "identity": {
        "title": "Identity",
        "where": "Administration > Access & rights > Identity",
        "text": (
            "Who is in which Keycloak group and which group opens which app, read through a tool "
            "node. Only role mappings are visible; composite roles are not expanded."
        ),
    },
    "chat_policy": {
        "title": "How the chat answers (chat policy)",
        "where": "Administration > Models > Parameters",
        "text": (
            "The chat policy, set per effort (fast, balanced, thorough): how far a check of the "
            "servers may go (turns, tool calls, seconds), whether a second model reads each "
            "answer, whether notes about the person are looked up and written, the Nachlese "
            "(finished answers read again in the background), emoji answers to a plain thanks, "
            "and the machine that makes the answers. An empty field is the hub's own rule. "
            "(The earlier flow editor was removed.)"
        ),
    },
    "names": {
        "title": "Names of nodes",
        "where": "Administration > Nodes > a node's page > Rename",
        "text": (
            "A node gets its name when it is paired (without a name it uses its host name); an "
            "admin can rename it later on the node's page. Use the machine's host name in small "
            "letters, so that the assistant and the person mean the same computer. The open chats, "
            "server choices, pools and model choices (model@node) follow the new name; the history "
            "(messages, reports, tool calls) keeps the old one, and the old name still reaches the "
            "node. topcoat.state lists earlier names under 'formerly'."
        ),
    },
    "settings": {
        "title": "Settings",
        "where": "Settings > Me, Instructions, Memory; Administration > Models > Parameters",
        "text": (
            "Per person (Settings > Me): form of address, default model, the personal instruction "
            "text, temperature, effort, memory on or off, the Internet default, the external "
            "model. Language and colour are in the person's menu. Admins set the hub's models, the"
            " technical parameters (Administration > Models > Parameters) and the access of people "
            "(Administration > Access & rights)."
        ),
    },
    "access": {
        "title": "API access (gateway)",
        "where": "Administration > Access & rights > API keys",
        "text": (
            "The hub speaks the OpenAI-compatible /v1 API (models and chat completions) and routes"
            " a request to a node that has the model. Access works with API keys created there; a "
            "key is shown once. A key never confirms an action."
        ),
    },
    "instructions": {
        "title": "Instructions",
        "where": "Settings > Instructions",
        "text": (
            "The assistant's standing rules about the user and the lab, built from the user's "
            "answers and proposals the assistant makes; proposals wait for the user's decision."
        ),
    },
    "security": {
        "title": "Safety rules",
        "where": "everywhere",
        "text": (
            "Nodes run only named, schema-checked actions from a list the hub assigns; no remote "
            "shell. All traffic is TLS. Every personal row is tied to its user. The assistant "
            "reads only; a change needs the user's click on one concrete action. Anything sent to "
            "an external model goes through redaction first and, by default, asks first."
        ),
    },
}

GUIDE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "topic": {
            "type": "string",
            "maxLength": 40,
            "description": "one of: "
            + ", ".join(FEATURES)
            + "; empty = the list of all topics; all = every topic in full",
        },
    },
}
GUIDE_DESCRIPTION = (
    "What Topcoat itself can do and where each function is in the GUI (menu entries, switches, "
    "limits). Use it to explain Topcoat to the user, to say where to click, and before claiming "
    "that Topcoat cannot do something. Without a topic it lists every topic with one line. It is "
    "the documentation that ships with this hub version, so for what is set up right now use "
    "topcoat.state. It changes nothing."
)

STATE_WHATS = (
    "overview",
    "chats",
    "chat",
    "settings",
    "nodes",
    "memory",
    "knowledge",
    "tasks",
    "task",
    "config",
)
STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["what"],
    "properties": {
        "what": {"type": "string", "enum": list(STATE_WHATS)},
        "query": {
            "type": "string",
            "maxLength": 100,
            "description": "what=chats, memory or tasks: only entries containing this text",
        },
        "id": {
            "type": "string",
            "maxLength": 40,
            "description": (
                "what=chat or task: the id from the list (what=chats or tasks); "
                "what=nodes: a machine's name, for all details of that one machine"
            ),
        },
    },
}
STATE_DESCRIPTION = (
    "What is set up in Topcoat right now, read-only and only what this user may see: "
    "what=overview (counts and the hub version), chats (the user's own conversations, optionally "
    "filtered by a title text), chat with id (the messages of one of them, shortened), settings "
    "(the user's own settings), nodes (the connected servers and which tools the user may run on "
    "them), memory (the user's own memory notes, with how reliable they are), knowledge (the "
    "knowledge sources the user may search: name, state, size; no paths), tasks (the user's own "
    "agent tasks) and task with id (one task's result), config (the hub's configuration "
    "without secrets; admins only). Secrets are hidden. "
    "Use it to look at how the user's Topcoat is configured instead of guessing; it changes "
    "nothing."
)


def guide_tool() -> dict[str, Any]:
    return {
        "node_id": str(TOPCOAT_NODE_ID),
        "node": TOPCOAT_NODE,
        "capability": GUIDE_CAPABILITY,
        "description": GUIDE_DESCRIPTION,
        "input_schema": GUIDE_SCHEMA,
    }


def state_tool() -> dict[str, Any]:
    return {
        "node_id": str(TOPCOAT_NODE_ID),
        "node": TOPCOAT_NODE,
        "capability": STATE_CAPABILITY,
        "description": STATE_DESCRIPTION,
        "input_schema": STATE_SCHEMA,
    }


def _machine_row(node: Any, view: dict[str, Any], tools: int, *, full: bool) -> dict[str, Any]:
    """One machine as the agent sees it. The short form (all machines at once) is a few readable
    values per machine, so ten machines stay inside the tool's excerpt (an excerpt that is cut made
    the agent repeat the call and then state numbers it never read); with ``id`` the machine comes
    back in full."""
    if full:
        return {
            "node": node.name,
            "state": view["state"],
            "os": node.os,
            "roles": view["roles"],
            "last_seen": view["last_seen"],
            "cpu": view["cpu"],
            "cpu_util_percent": _round(view["cpu_util"]),
            "ram_total_mb": view["ram_total_mb"],
            "ram_used_mb": view["ram_used_mb"],
            "gpus": [
                {
                    k: g.get(k)
                    for k in ("name", "vram_total_mb", "vram_used_mb", "gpu_util")
                    if g.get(k) is not None
                }
                for g in view["gpus"]
            ],
            "loaded_models": [
                {
                    k: m.get(k)
                    for k in ("name", "vram_used_mb", "context_length", "n_parallel")
                    if m.get(k) is not None
                }
                for m in view["loaded"]
            ],
            "loading": [m if isinstance(m, str) else m.get("name") for m in view["loading"]],
            "models": [m.get("name") for m in view.get("models") or []],
            "tools_you_may_run": tools,
        }
    row: dict[str, Any] = {"node": node.name, "state": view["state"]}
    if view["state"] != "available":
        row["last_seen"] = view["last_seen"]
    row["cpu_percent"] = _round(view["cpu_util"])
    if view["ram_total_mb"] is not None:
        row["ram_mb"] = f"{view['ram_used_mb']}/{view['ram_total_mb']}"
    gpus = [
        f"{g.get('name')} {g.get('vram_used_mb')}/{g.get('vram_total_mb')} MB"
        for g in view["gpus"]
        if g.get("name")
    ]
    if gpus:
        row["gpu_vram"] = gpus
    if view["loaded"]:
        row["loaded"] = [m.get("name") for m in view["loaded"]]
    if view["loading"]:
        row["loading"] = [m if isinstance(m, str) else m.get("name") for m in view["loading"]]
    if tools:
        row["tools"] = tools
    if view.get("models"):
        row["models"] = [m.get("name") for m in view["models"]]
    return row


def _round(value: Any) -> Any:
    return round(value, 1) if isinstance(value, float) else value


def _formerly(name: str) -> dict[str, Any]:
    """The names a node had before it was renamed: old notes and chats may still use them."""
    earlier = node_rename.earlier_names(name)
    return {"formerly": earlier} if earlier else {}


def _ok(output: Any) -> dict[str, Any]:
    return {"ok": True, "output": output, "at": None, "bytes": len(str(output))}


def guide(topic: str) -> dict[str, Any]:
    topic = topic.strip().lower()
    if not topic:
        return {
            "version": __version__,
            "topics": [
                {"topic": k, "title": v["title"], "where": v["where"]} for k, v in FEATURES.items()
            ],
        }
    if topic == "all":  # every topic in full (a check of the documentation reads them at once)
        return {"version": __version__, "topics": [{"topic": k, **v} for k, v in FEATURES.items()]}
    entry = FEATURES.get(topic)
    if entry is None:
        raise ToolError(
            422, "invalid_input", f"unknown topic; use one of: {', '.join(FEATURES)}, or all"
        )
    return {"version": __version__, "topic": topic, **entry}


class TopcoatService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        tools: ToolService,
        settings: Settings,
    ) -> None:
        self.sm = sessionmaker
        self.tools = tools
        self.settings = settings
        # how an answer was handled and the file reader (trace_tool.py), set by the app
        self.trace: Any = None

    async def offer(self, caller: Caller) -> list[dict[str, Any]]:
        """The two tools, unless an operator switched Topcoat-as-a-tool off for the whole hub;
        next to them the two for the development program (their group limits who gets them)."""
        if not self.settings.self_tool_enabled:
            return []
        out = [guide_tool(), state_tool()]
        if self.trace is not None:
            from topcoat_hub.trace_tool import show_tool, trace_tool

            out += [trace_tool(), show_tool()]
        return out

    async def run_tool(
        self, caller: Caller, capability: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        if not self.settings.self_tool_enabled:
            raise ToolError(403, "disabled", "Topcoat as a tool is switched off on this hub")
        if self.trace is not None and capability == "trace.read":
            return _ok(await self.trace.read(caller, tool_input))
        if self.trace is not None and capability == "repo.show":
            return _ok(await self.trace.show(caller, tool_input))
        schema = GUIDE_SCHEMA if capability == GUIDE_CAPABILITY else STATE_SCHEMA
        unknown = set(tool_input) - set(schema["properties"])
        if unknown:
            raise ToolError(422, "invalid_input", f"unknown input: {', '.join(sorted(unknown))}")
        for key, val in tool_input.items():
            if not isinstance(val, str) or len(val) > 100:
                raise ToolError(422, "invalid_input", f"{key} must be a short text")
        if capability == GUIDE_CAPABILITY:
            return _ok(guide(tool_input.get("topic", "")))
        if capability != STATE_CAPABILITY:
            raise ToolError(404, "unknown_capability", f"no such capability: {capability}")
        what = tool_input.get("what", "")
        if what not in STATE_WHATS:
            raise ToolError(422, "invalid_input", f"what must be one of: {', '.join(STATE_WHATS)}")
        if what == "overview":
            out = await self._overview(caller)
        elif what == "chats":
            out = await self._chats(caller, tool_input.get("query", ""))
        elif what == "chat":
            out = await self._chat(caller, tool_input.get("id", ""))
        elif what == "settings":
            out = await self._settings(caller)
        elif what == "nodes":
            out = await self._nodes(caller, tool_input.get("id", ""))
        elif what == "memory":
            out = await self._memory(caller, tool_input.get("query", ""))
        elif what == "knowledge":
            out = await self._knowledge(caller)
        elif what == "tasks":
            out = await self._tasks(caller, tool_input.get("query", ""))
        elif what == "task":
            out = await self._task(caller, tool_input.get("id", ""))
        else:
            out = self._config(caller)
        return _ok(out)

    # --- the reads -----------------------------------------------------------------------------

    async def _overview(self, caller: Caller) -> dict[str, Any]:
        async with self.sm() as db:
            chats = await ChatStore(db, caller.user_id).list_conversations(limit=1000)
        usable = await self.tools.usable_tools(caller)
        return {
            "version": __version__,
            "you_are_admin": caller.admin,
            "chats": len(chats),
            "servers": len({t["node"] for t in usable}),
            "tools_you_may_run": len(usable),
            "agent_enabled": self.settings.agent_enabled,
            "proposals_enabled": self.settings.proposals_enabled,
            "coordinator_enabled": self.settings.coordinator_enabled,
        }

    async def _chats(self, caller: Caller, query: str) -> dict[str, Any]:
        async with self.sm() as db:
            rows = await ChatStore(db, caller.user_id).list_conversations(
                query or None, limit=MAX_CHATS
            )
        return {
            "chats": [
                {
                    "id": str(c.id),
                    "title": scrub(c.title or ""),
                    "messages": n,
                    "updated": c.updated_at.isoformat() if c.updated_at else None,
                    "model": c.model,
                }
                for c, n in rows
            ],
            "note": f"your own conversations only, newest first, at most {MAX_CHATS}",
        }

    async def _chat(self, caller: Caller, chat_id: str) -> dict[str, Any]:
        try:
            cid = uuid.UUID(chat_id)
        except ValueError:
            raise ToolError(422, "invalid_input", "id must be a conversation id") from None
        async with self.sm() as db:
            store = ChatStore(db, caller.user_id)
            conv = await store.get_conversation(cid)  # scoped to the caller: None for anyone's else
            if conv is None:
                raise ToolError(404, "not_found", "no such conversation of yours")
            messages = [m for m in await store.messages(cid) if m.kind == "say"]
        shown = messages[-MAX_MESSAGES:]
        return {
            "id": str(conv.id),
            "title": scrub(conv.title or ""),
            "messages": [
                {"role": m.role, "text": _cut(scrub(m.content), MESSAGE_CHARS), "status": m.status}
                for m in shown
            ],
            "earlier_messages_not_shown": len(messages) - len(shown),
        }

    async def _settings(self, caller: Caller) -> dict[str, Any]:
        async with self.sm() as db:
            user = await db.scalar(select(User).where(User.id == caller.user_id))
            row = await db.scalar(
                select(UserSettings).where(UserSettings.user_id == caller.user_id)
            )
        return {
            "language": user.language if user else None,
            "role": user.role if user else None,
            "address": (row.address if row else None) or "du",
            "temperature": row.temperature if row else None,
            "quality": row.chat_quality if row else "balanced",
            "memory_enabled": row.memory_enabled if row else True,
            "internet_default": row.internet if row else True,
            "tool_scope_last_choice": row.tool_scope if row else None,
            "own_instruction_text": scrub(row.system_prompt) if row and row.system_prompt else None,
        }

    async def _nodes(self, caller: Caller, node_name: str = "") -> dict[str, Any]:
        everything = await self.tools.usable_tools(caller)
        # the chat's scope decides what this chat's run may use; the machines it leaves out are
        # named apart, so the answer does not claim a tool that this chat cannot run (T2.3)
        usable = tool_groups.apply_scope(everything, caller.scope) if caller.scope else everything
        per_node: dict[str, list[str]] = {}
        for t in usable:
            per_node.setdefault(t["node"], []).append(t["capability"])
        out: dict[str, Any] = {
            "connected_servers_with_tools_you_may_run": [
                {"node": n, "capabilities": sorted(c), **_formerly(n)}
                for n, c in sorted(per_node.items())
            ],
            "note": "only servers that are connected now and capabilities granted to you",
        }
        if caller.scope:
            outside = {t["node"] for t in everything} - set(per_node)
            out["outside_scope"] = {
                "nodes": sorted(outside),
                "note": "these servers exist and you may use them, but this chat's server choice "
                "leaves them out; the person can add them there",
            }
        if not caller.admin:
            return out
        # An admin also sees every machine of the hub, with or without tools: a model node has
        # no tools, so it is absent from the list above although it is connected and working.
        async with self.sm() as db:
            rows = (
                await db.execute(
                    select(Node, NodeInventory).outerjoin(
                        NodeInventory, NodeInventory.node_id == Node.id
                    )
                )
            ).all()
        # an earlier name of a renamed node still finds it (an old note says node-x)
        wanted = node_rename.current_name((node_name or "").strip())
        machines = []
        for node, inv in sorted(rows, key=lambda r: r[0].name):
            if wanted and node.name != wanted:
                continue
            connected = self.tools.sessions.get(node.id) is not None
            view = node_summary(node, inv, connected)
            machines.append(
                {
                    **_machine_row(node, view, len(per_node.get(node.name, [])), full=bool(wanted)),
                    **_formerly(node.name),
                }
            )
        if wanted and not machines:
            raise ToolError(404, "not_found", f"no machine named {wanted!r}")
        out["all_machines"] = machines
        out["note"] = (
            "all_machines describes every node of the hub from what the hub itself knows: state "
            "('available' means connected and working), memory, load, GPUs and the loaded models. "
            "A machine without a tool node (a model node) is described here and cannot be asked "
            "for logs or containers. connected_servers_with_tools_you_may_run lists only servers "
            "that offer tools. Without an id the rows are short so that all machines fit "
            "(cpu_percent, ram_mb used/total, gpu_vram used/total, loaded models); with id set to "
            "a machine's name that machine comes back in full (CPU model, last seen)."
        )
        return out

    async def _memory(self, caller: Caller, query: str) -> dict[str, Any]:
        stmt = (
            select(MemoryItem)
            .where(
                MemoryItem.user_id == caller.user_id, MemoryItem.status.in_(("active", "disputed"))
            )
            .order_by(MemoryItem.updated_at.desc())
            .limit(MAX_NOTES)
        )
        if query.strip():
            stmt = stmt.where(MemoryItem.text.ilike(f"%{_like(query.strip())}%", escape="\\"))
        async with self.sm() as db:
            rows = (await db.scalars(stmt)).all()
        return {
            "notes": [
                {
                    "text": _cut(scrub(m.text), MESSAGE_CHARS),
                    "kind": m.kind,
                    "status": m.status,
                    "evidence": m.evidence,
                    "confidence": round(m.confidence, 2),
                    "updated": m.updated_at.isoformat() if m.updated_at else None,
                }
                for m in rows
            ],
            "note": f"your own notes only, newest first, at most {MAX_NOTES}",
        }

    async def _knowledge(self, caller: Caller) -> dict[str, Any]:
        stmt = (
            select(KnowledgeSource)
            .where(
                or_(
                    KnowledgeSource.visibility == "shared",
                    KnowledgeSource.user_id == caller.user_id,
                )
            )
            .order_by(KnowledgeSource.name)
        )
        async with self.sm() as db:
            rows = (await db.scalars(stmt)).all()
        return {
            "sources": [
                {
                    "name": k.name,
                    "visibility": k.visibility,
                    "kind": k.kind,
                    "status": k.status,
                    "files": k.file_count,
                    "chunks": k.chunk_count,
                    "kept_out_by_the_exclude_list": k.excluded_count,
                    "held_by_the_secret_filter": k.blocked_count,
                    "indexed": k.indexed_at.isoformat() if k.indexed_at else None,
                }
                for k in rows
            ],
            "note": "folder paths are not shown",
        }

    async def _tasks(self, caller: Caller, query: str) -> dict[str, Any]:
        stmt = (
            select(AgentTask)
            .where(AgentTask.user_id == caller.user_id, AgentTask.origin == "tasks")
            .order_by(AgentTask.created_at.desc())
            .limit(MAX_CHATS)
        )
        if query.strip():
            stmt = stmt.where(AgentTask.title.ilike(f"%{_like(query.strip())}%", escape="\\"))
        async with self.sm() as db:
            rows = (await db.scalars(stmt)).all()
        return {
            "tasks": [
                {
                    "id": str(t.id),
                    "title": scrub(t.title),
                    "status": t.status,
                    "stop_reason": t.stop_reason,
                    "created": t.created_at.isoformat() if t.created_at else None,
                }
                for t in rows
            ],
            "note": f"your own tasks only, newest first, at most {MAX_CHATS}",
        }

    async def _task(self, caller: Caller, task_id: str) -> dict[str, Any]:
        try:
            tid = uuid.UUID(task_id)
        except ValueError:
            raise ToolError(422, "invalid_input", "id must be a task id") from None
        async with self.sm() as db:
            t = await db.scalar(
                select(AgentTask).where(AgentTask.id == tid, AgentTask.user_id == caller.user_id)
            )
        if t is None:
            raise ToolError(404, "not_found", "no such task of yours")
        return {
            "id": str(t.id),
            "title": scrub(t.title),
            "question": _cut(scrub(t.prompt), MESSAGE_CHARS),
            "status": t.status,
            "stop_reason": t.stop_reason,
            "error": scrub(t.error) if t.error else None,
            "summary": _cut(scrub(t.summary), 2000) if t.summary else None,
            "steps": t.step_count,
            "tool_calls": t.tool_calls,
        }

    def _config(self, caller: Caller) -> dict[str, Any]:
        if not caller.admin:
            raise ToolError(403, "forbidden", "the hub configuration is for admins only")
        out: dict[str, Any] = {}
        for name in type(self.settings).model_fields:
            if name in _CONFIG_HIDDEN or _SECRET_KEY.search(name):
                continue
            value = getattr(self.settings, name)
            out[name] = (
                scrub(value)
                if isinstance(value, str)
                else str(value)
                if not isinstance(value, (int, float, bool, list, dict, type(None)))
                else value
            )
        return {"hub_version": __version__, "config": out, "hidden": "secrets, paths and addresses"}
