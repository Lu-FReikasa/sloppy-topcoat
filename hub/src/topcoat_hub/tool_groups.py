"""The capabilities of the user's tool nodes in groups the user can read: what the chat tells the
assistant it can do, what the model is offered as one decision tool, what the user can switch on
and off per conversation (docker, journal, network, monitors, notes, web …)."""

import re
from collections.abc import Iterable
from typing import Any

# (key, label for the assistant and the GUI's English fallback, capability name prefixes)
GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("docker", "Docker containers, their logs and compose projects", ("docker.", "compose.")),
    ("journal", "systemd services and the journal", ("journal.", "service.")),
    (
        "host",
        "host health, disks, load, what runs where (inventory), a log search over everything and, "
        "where the operator released them, the files of the host and the Windows read checks",
        ("system.", "disk.", "health.", "logs.", "inventory.", "files.", "windows."),
    ),
    ("network", "network devices, clients and alerts (Omada)", ("omada.",)),
    ("monitors", "monitors and certificates (Uptime Kuma)", ("kuma.",)),
    ("reach", "DNS, ping, ports and TLS certificates", ("certs.", "net.")),
    ("notes", "the user's notes (knowledge base)", ("docs.", "knowledge.")),
    (
        "identity",
        "who is in which group and which group opens which app (Keycloak)",
        ("keycloak.", "identity."),
    ),
    (
        "topcoat",
        "Topcoat itself: what it can do, the user's own chats and settings, the connected "
        "servers and nodes with their installed and loaded models",
        ("topcoat.",),
    ),
    (
        "github",
        "the project's GitHub repository: source code, pull requests and issues (read only)",
        ("github.",),
    ),
    (
        "shell",
        "a read-only shell on the server (an OS user that can only read)",
        ("shell.",),
    ),
    (
        "workshop",
        "the dev workshop: a registered repository's code in a scratch copy (read, search, change, "
        "run its tests, build the node, look at its GUI in a headless browser); nothing leaves "
        "the workshop without the user's click",
        ("repo.", "browser."),
    ),
    (
        "trace",
        "how Topcoat handled an earlier answer the user handed over (steps, timings, where in "
        "the code), read only",
        ("trace.",),
    ),
    (
        "project",
        "the user's projects (Projekte): projects, topics, tasks, reports and requests",
        ("project.",),
    ),
    (
        "wiki",
        "Topcoat's own wiki: its pages, checked against the code (read only)",
        ("wiki.",),
    ),
    ("web", "web search and reading public pages", ("web.",)),
)
KEYS = tuple(k for k, _, _ in GROUPS)
# what the user can switch per conversation next to the servers: the web is NOT among them — it has
# its own switch ("Internet") and does not depend on which servers are chosen (E3-04)
WEB = "web"
# nodes that are the hub's own view of something, not machines the user can pick; a choice of
# servers never takes them away (the group switch and the incognito mode do; incognito takes every
# tool away but the web)
HUB_VIEWS = ("topcoat", "github", "knowledge", "trace", "messages", "wiki")
SELECTABLE = tuple(k for k in KEYS if k != WEB)


def group_of(capability: str) -> str | None:
    for key, _label, prefixes in GROUPS:
        if capability.startswith(prefixes):
            return key
    return None


def present(available: Iterable[dict[str, Any]]) -> list[str]:
    """The group keys that at least one of the capabilities belongs to, in the fixed order."""
    have = {group_of(t["capability"]) for t in available}
    return [k for k in KEYS if k in have]


def labels(keys: Iterable[str]) -> tuple[str, ...]:
    want = set(keys)
    return tuple(label for key, label, _ in GROUPS if key in want)


def only(available: list[dict[str, Any]], keys: Iterable[str] | None) -> list[dict[str, Any]]:
    """The capabilities in the chosen groups (``None`` = all); a capability that is in no group is
    kept only when everything is chosen."""
    if keys is None:
        return available
    want = set(keys)
    return [t for t in available if group_of(t["capability"]) in want]


def clean_scope(value: Any) -> dict[str, Any] | None:
    """A stored or submitted scope as ``{"nodes": [...]|None, "groups": [...]|None, "internet":
    bool|None}``; ``None`` when it restricts nothing and leaves the internet to the user's default
    (so "everything" has one form). Unknown group keys, and ``web`` (it is the internet switch),
    are dropped."""
    if not isinstance(value, dict):
        return None
    nodes = value.get("nodes")
    groups = value.get("groups")
    internet = value.get("internet")
    memory = value.get("memory")
    out: dict[str, Any] = {
        "nodes": sorted({n for n in nodes if isinstance(n, str) and n})
        if isinstance(nodes, list)
        else None,
        "groups": [k for k in SELECTABLE if k in set(groups)] if isinstance(groups, list) else None,
        "internet": internet if isinstance(internet, bool) else None,
    }
    if all(v is None for v in out.values()) and not isinstance(memory, bool):
        return None
    if isinstance(memory, bool):  # only when set: the key is absent while the user's default holds
        out["memory"] = memory
    return out


def memory_on(scope: dict[str, Any] | None, user_default: bool) -> bool:
    """Does the assistant use and keep notes about the user in this conversation? The user's
    setting, switched off (never on) by an incognito conversation."""
    scope = clean_scope(scope)
    return user_default and not (scope is not None and scope.get("memory") is False)


def resolve_scope(scope: dict[str, Any] | None, internet_default: bool) -> dict[str, Any]:
    """The scope a turn runs with: the conversation's own choice, and the internet switch settled
    (its own value or, when the conversation has none, the user's default). Always a dict."""
    cleaned = clean_scope(scope) or {"nodes": None, "groups": None, "internet": None}
    internet = cleaned["internet"]
    return {**cleaned, "internet": internet_default if internet is None else internet}


def restricts_servers(scope: dict[str, Any] | None) -> bool:
    """Does the scope narrow the servers or tool groups (not the internet)?"""
    scope = clean_scope(scope)
    return scope is not None and (scope["nodes"] is not None or scope["groups"] is not None)


# What a question can name that justifies handing the agent a shorter tool list. Only unmistakable
# words; a question that names none keeps the whole list (nothing is ever taken away on a guess).
_TOPIC_GROUPS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("docker", re.compile(r"\b(?:docker\w*|container\w*|compose|image\w*)\b", re.I)),
    ("journal", re.compile(r"\b(?:journalctl|systemd|systemctl|journal)\b", re.I)),
    ("network", re.compile(r"\b(?:omada|wlan|wifi|access[ -]?point\w*)\b", re.I)),
    ("monitors", re.compile(r"\b(?:uptime[ -]?kuma|kuma|monitor\w*)\b", re.I)),
    (
        "reach",
        re.compile(r"\b(?:dns|ping\w*|ports?|zertifikat\w*|certificates?|certs?|tls|ssl)\b", re.I),
    ),
    ("identity", re.compile(r"\b(?:keycloak|sso|single sign[ -]?on|anmeldegruppe\w*)\b", re.I)),
    (
        "github",
        re.compile(
            r"\b(?:github|commits?|pull[ -]?requests?|issues?|repos?|repositor(?:y|ies|ium))\b",
            re.I,
        ),
    ),
    # the registered repositories live in the workshop: a question about "the repo" needs its tools
    (
        "workshop",
        re.compile(r"\b(?:werkstatt|workshop|repos?|repositor(?:y|ies|ium)|pytest)\b", re.I),
    ),
)
# the groups every question may need next to its topic: Topcoat itself, the notes, the host facts
ALWAYS_GROUPS = ("topcoat", "notes", "host", "wiki")
# a read-only shell can stand in for these (docker ps, journalctl)
SHELL_STAND_IN = ("docker", "journal")


_WORKSHOP_WORDS = re.compile(
    r"\b(?:werkstatt|workshop|pytest|repo|repos|repositor\w+|datei\w*|files?|commit\w*)\b", re.I
)


def may_use_workshop(prompt: str, scope: dict[str, Any] | None) -> bool:
    """Does this question look like work in a registered repository, with the workshop group
    not switched off? (Such a check needs longer limits.)"""
    groups = (clean_scope(scope) or {}).get("groups")
    return (groups is None or "workshop" in groups) and bool(_WORKSHOP_WORDS.search(prompt))


def topic_groups(prompt: str) -> list[str]:
    """The tool groups a question names (in the fixed order); empty when it names none."""
    return [key for key, pattern in _TOPIC_GROUPS if pattern.search(prompt)]


def auto_scope(
    prompt: str, scope: dict[str, Any] | None, available: Iterable[dict[str, Any]]
) -> tuple[dict[str, Any] | None, list[str]]:
    """The scope a chat check starts with: when the user has not picked groups and the question
    names a topic, only that topic's groups (plus ``ALWAYS_GROUPS``, and the shell where it can
    stand in) are offered, which keeps the tool definitions the model has to read short. Returns
    the scope and the groups it was narrowed to (empty = not narrowed). A choice of groups by the
    user is never touched, and a question without a clear topic keeps everything."""
    cleaned = clean_scope(scope)
    if cleaned is not None and cleaned["groups"] is not None:
        return scope, []
    topics = topic_groups(prompt)
    if not topics:
        return scope, []
    have = present(available)
    want = {*topics, *ALWAYS_GROUPS}
    if any(t in SHELL_STAND_IN for t in topics):
        want.add("shell")
    keys = [k for k in KEYS if k in want and k in have and k != WEB]
    if not any(k in have for k in topics) or len(keys) >= len([k for k in have if k != WEB]):
        return scope, []  # nothing to look at live for the topic, or nothing would be left out
    base = cleaned or {"nodes": None, "groups": None, "internet": None}
    return {**base, "groups": keys}, keys


def pick_web_node(available: Iterable[dict[str, Any]]) -> str | None:
    """The node web search comes from: the hub's choice among the nodes that offer the web
    capabilities to this user (the one with most of them, then by name), so it never depends on
    the servers the user picked and the model is not asked to choose between identical tools."""
    count: dict[str, int] = {}
    for t in available:
        if group_of(t["capability"]) == WEB:
            count[t["node"]] = count.get(t["node"], 0) + 1
    if not count:
        return None
    return min(count, key=lambda n: (-count[n], n))


def internet_on(scope: dict[str, Any] | None) -> bool:
    """The internet switch of a scope; without one the internet is on (as before E3-04)."""
    scope = clean_scope(scope)
    return True if scope is None or scope["internet"] is None else scope["internet"]


def apply_scope(
    available: list[dict[str, Any]], scope: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """What the assistant may use in a conversation: the capabilities the user may run (their
    grants, D20) narrowed to the chosen nodes and groups — plus, when the internet switch is on,
    the web capabilities of one node the hub picks, whatever servers were chosen. A scope only ever
    removes: it adds nothing the grants do not already give."""
    scope = clean_scope(scope)
    servers = [t for t in available if group_of(t["capability"]) != WEB]
    if scope is not None and scope.get("memory") is False:
        servers = []  # incognito: no server, no notes - only the internet switch is left
    elif scope is not None:
        if scope["nodes"] is not None:
            nodes = set(scope["nodes"])
            # Topcoat itself and GitHub are the hub's own views, not servers the picker lists: a
            # node choice never takes them away (the group switch still does)
            servers = [t for t in servers if t["node"] in nodes or t["node"] in HUB_VIEWS]
        # the hub's own message and report tools belong to no group (a group is a kind of
        # machine); they exist only for the run or application they were made for, so a choice of
        # groups never takes them away - a report run without its section tool cannot write
        own = [t for t in servers if t["node"] == "messages"]
        servers = only(servers, scope["groups"])
        servers += [t for t in own if t not in servers]
    if not internet_on(scope):
        return servers
    web_node = pick_web_node(available)
    web = [
        t
        for t in available
        if group_of(t["capability"]) == WEB
        and t["node"] == web_node
        # web.image is the hub's own (research.report.image): its bytes never reach a model
        and t["capability"] != "web.image"
    ]
    # keep the order of ``available`` (stable listings for the model)
    keep = {id(t) for t in servers} | {id(t) for t in web}
    return [t for t in available if id(t) in keep]


# What a question needs before the agent starts thinking: a topic the question names, the group
# that looks at it live, and the capabilities that can stand in (the hub's inventory and the
# monitors know about containers too, the notes about everything). Only unmistakable topics.
_NEEDS: tuple[tuple[str, re.Pattern[str], tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "docker",
        re.compile(r"\b(?:docker\w*|container\w*|compose)\b", re.I),
        ("docker.", "compose.", "shell."),  # a shell can run docker ps
        ("inventory.", "kuma.", "knowledge."),
    ),
    (
        "journal",
        re.compile(r"\b(?:journalctl|systemd|systemctl|journal)\b", re.I),
        ("journal.", "service.", "shell."),  # ... and systemctl, journalctl
        ("logs.", "knowledge."),
    ),
    ("network", re.compile(r"\bomada\b", re.I), ("omada.",), ("knowledge.",)),
    ("monitors", re.compile(r"\buptime[ -]?kuma\b", re.I), ("kuma.",), ("knowledge.",)),
)


def _topics(prompt: str, available: Iterable[dict[str, Any]], stand_in: bool) -> list[str]:
    caps = [str(t["capability"]) for t in available]
    return [
        key
        for key, pattern, live, other in _NEEDS
        if pattern.search(prompt)
        and not any(c.startswith(live + other if stand_in else live) for c in caps)
    ]


def missing_tools(prompt: str, available: Iterable[dict[str, Any]]) -> list[str]:
    """The topics the prompt names that nothing among ``available`` can answer — not even by a
    stand-in. Empty when every named topic has something to look at (or the prompt names none):
    the agent can start. Checked before the first model turn, so a question the agent has no tool
    for ends at once instead of after minutes of thinking."""
    return _topics(prompt, available, True)


def no_live_tool(prompt: str, available: Iterable[dict[str, Any]]) -> list[str]:
    """The topics the prompt names that no tool looks at live (a stand-in may know something,
    but only as old as its last look): the agent must say so instead of guessing."""
    return [
        t for t in _topics(prompt, available, False) if t not in missing_tools(prompt, available)
    ]


def tools_summary(available: Iterable[dict[str, Any]]) -> str:
    """What the agent can look at, in plain words ("Docker containers …; the user's notes …")."""
    keys = present(available)
    return "; ".join(label for key, label, _ in GROUPS if key in keys) or "nothing"
