"""The web capabilities (``web.search``, ``web.fetch``) as the hub sees them: what is evidence
from the internet (the ``web`` tag, with the source URL) and the one rule about what may be put
into them — a search query or a URL leaves the network, so it never carries private data.

The node enforces the other half (no private addresses, only http/https, size limits)."""

import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from topcoat_hub.config import Settings
from topcoat_hub.models import Node, User
from topcoat_hub.redaction import private_kinds
from topcoat_hub.tools import ToolError

SEARCH = "web.search"
FETCH = "web.fetch"
WEB_CAPABILITIES = (SEARCH, FETCH)
# pictures for a report (2026-11-13): an image search the model may use, and the fetch of one
# picture that only the hub makes (research.report.image keeps it with the report; its bytes never
# go into a model's context)
IMAGES = "web.images"
IMAGE = "web.image"
GUARDED = (SEARCH, FETCH, IMAGES, IMAGE)  # every input of these is checked for private data
HUB_ONLY = frozenset({IMAGE})
MAX_IMAGE_SEARCHES = 3  # per run

# (host names, user names, own domains) that must not appear in anything sent to the internet
PrivateNames = Callable[[], Awaitable[tuple[Iterable[str], Iterable[str], Iterable[str]]]]


def private_names_of(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> PrivateNames:
    """The names that are the operator's: every node's name, every user's name, the own domains
    (``TOPCOAT_REDACT_DOMAINS``)."""

    async def names() -> tuple[Iterable[str], Iterable[str], Iterable[str]]:
        async with sessionmaker() as db:
            hosts = list(await db.scalars(select(Node.name)))
            users = list(await db.scalars(select(User.name)))
        return hosts, users, [d for d in settings.redact_domains.split(",") if d.strip()]

    return names


def is_web(capability: str) -> bool:
    return capability in WEB_CAPABILITIES


def outgoing_text(capability: str, tool_input: dict[str, Any]) -> str:
    """The part of an input that leaves the network: the query, or the URL."""
    key = "query" if capability in (SEARCH, IMAGES) else "url"
    return str(tool_input.get(key) or "")


async def guard(capability: str, tool_input: dict[str, Any], names: PrivateNames | None) -> None:
    """Refuse an input that would put private data on the internet (the same detection as the
    redaction layer of E3-02: secrets, IP and MAC addresses, e-mail addresses, the names of the
    operator's machines and users, private domain names). The refusal goes back to the model, which
    can ask again without them."""
    text = outgoing_text(capability, tool_input)
    if not text:
        return
    hosts, users, domains = await names() if names else ((), (), ())
    kinds = private_kinds(text, hosts, users, domains)
    if capability in (FETCH, IMAGE):
        # a public page address often carries a long id (product, article): that alone is not a
        # secret; labelled secrets (token=, key=, user:password@) still fire their own rules
        kinds = [k for k in kinds if k != "long_token"]
    if kinds:
        raise ToolError(
            422,
            "private_data",
            f"the {'query' if capability in (SEARCH, IMAGES) else 'URL'} contains private data "
            f"({', '.join(kinds)}) and would leave the network: ask again about the public thing "
            "only, without host names, addresses, user names or secrets",
        )


def make_guard(names: PrivateNames | None) -> Callable[[str, dict[str, Any]], Awaitable[None]]:
    """The guard as ``ToolService`` runs it for every call, whoever makes it (the agent, the chat
    check, the REST API): a web capability's input is checked, any other passes."""

    async def check(capability: str, tool_input: dict[str, Any]) -> None:
        if capability in GUARDED:
            await guard(capability, tool_input, names)

    return check


# a page with at least this much text is a usable source: enough to answer from, unless it is
# mostly footer, login or consent text (then it needs a lot more)
USABLE_PAGE_CHARS = 500
BOILERPLATE_FULL_CHARS = 3000
MAX_WEB_ATTEMPTS = 6  # calls per run, failed ones included, so a loop of failures still ends
_BOILERPLATE = re.compile(
    r"(?i)\b(cookies?|consent|sign in|log ?in|anmelden|einloggen|impressum|imprint|"
    r"privacy policy|datenschutz|all rights reserved|alle rechte vorbehalten|"
    r"terms of (?:use|service)|agb|help cent(?:er|re)|hilfe-?center|newsletter|subscribe)\b|©"
)


def usable_page(text: str) -> bool:
    """Is this page text a source to answer from? Enough text, and not just a footer, a login or
    a cookie banner: short text with several such words is boilerplate."""
    text = text.strip()
    if len(text) < USABLE_PAGE_CHARS:
        return False
    return len(text) >= BOILERPLATE_FULL_CHARS or len(_BOILERPLATE.findall(text)) < 3


@dataclass
class WebBudget:
    """What one run may still ask the internet (E3-04): a few searches, a few pages, and nothing
    more once a page that can answer the question has been read — a simple question took nine
    calls and 79 s. A refused call costs no time: it goes back to the model as a plain answer."""

    max_searches: int = 3
    max_fetches: int = 2
    # a research run reads on after a usable page and is not stopped by the attempt count; only
    # a server check answers from the first usable page (research_profile.py)
    stop_at_usable: bool = True
    max_attempts: int = MAX_WEB_ATTEMPTS
    searches: int = 0
    fetches: int = 0
    image_searches: int = 0
    attempts: int = 0
    usable: str | None = None  # the evidence reference of the first usable page
    # pages read with success: the same address again reads on in a long page, no new call
    read: set[str] = field(default_factory=set)
    # sites that refused automated reading (HTTP 401/403/429/451): their other pages would too
    refused_hosts: set[str] = field(default_factory=set)

    def refusal(self, capability: str, call_input: Any = None) -> str | None:
        """Why the call may not be made, or ``None``."""
        if capability == IMAGES and self.image_searches >= MAX_IMAGE_SEARCHES:
            return (
                f"you already made {self.image_searches} picture searches: place one of the hits "
                "you have (research.report.image) or go on without a picture"
            )
        if not is_web(capability):
            return None
        if (
            capability == FETCH
            and isinstance(call_input, dict)
            and call_input.get("format") == "html"
        ):
            # the markup is for the hub's own parsers (Sheets), not for a model's context
            return "read the page as text (leave out format): the markup is not for reading"
        url = _url_of(call_input)
        if capability == FETCH and url and url in self.read:
            return None  # the next part of a page already read: reading on, not a new call
        host = _host(url)
        if capability == FETCH and host and host in self.refused_hosts:
            return (
                f"{host} refused automated reading before; its other pages will too: fetch a "
                "hit from another site, or answer from what you have"
            )
        if self.attempts >= self.max_attempts:
            return (
                f"you already made {self.attempts} web calls for this question: answer without "
                "the web and say which pages could not be read"
            )
        if self.usable is not None and self.stop_at_usable:
            return (
                f"you already read a usable web source [{self.usable}]: answer from it now "
                "(cite its URL); do not search or fetch more"
            )
        if capability == SEARCH and self.searches >= self.max_searches:
            return (
                f"you already made {self.searches} web searches for this question: use the hits "
                "you have (fetch one page) or answer without the web; do not search again"
            )
        if capability == FETCH and self.fetches >= self.max_fetches:
            return (
                f"you already read {self.fetches} web pages for this question: answer from "
                "them or without the web; do not fetch more"
            )
        return None

    def closed(self, capability: str) -> bool:
        """Is every call of this capability refused from now on? Then it is not offered any more:
        a model that is offered a refused tool calls it again (live 2026-11-12: three refused
        searches in a row, and the run ended as "no progress" with the answer in hand)."""
        if not is_web(capability):
            return False
        if self.attempts >= self.max_attempts or (self.usable is not None and self.stop_at_usable):
            return not (capability == FETCH and self.read)  # reading on in a page stays open
        if capability == SEARCH:
            return self.searches >= self.max_searches
        return self.fetches >= self.max_fetches and not self.read

    def note_call(self, capability: str, call_input: Any = None) -> None:
        """A call is being made (counted whether it works or not: it costs time either way)."""
        if capability == IMAGES:
            self.image_searches += 1
        if capability == FETCH and _url_of(call_input) in self.read:
            return  # reading on in a page already read
        if is_web(capability):
            self.attempts += 1
        if capability == SEARCH:
            self.searches += 1
        elif capability == FETCH:
            self.fetches += 1

    def note_refused(self, call_input: Any, message: str) -> None:
        """A page that answered 401, 403, 429 or 451: its site reads no robots."""
        host = _host(_url_of(call_input))
        if host and re.search(r"\bHTTP (401|403|429|451)\b", message or ""):
            self.refused_hosts.add(host)

    def note_failure(self, capability: str) -> None:
        """A call gave nothing (an error, a refusal by the page or the node): it does not use up
        the budget of pages, so the next link may still be tried."""
        if capability == SEARCH and self.searches:
            self.searches -= 1
        elif capability == FETCH and self.fetches:
            self.fetches -= 1

    def note_result(self, capability: str, output: Any, ref: str, call_input: Any = None) -> None:
        """A call returned: a page with enough text is a usable source."""
        url = _url_of(call_input)
        if capability == FETCH and url and isinstance(output, dict):
            self.read.add(url)
        if capability == FETCH and self.usable is None and isinstance(output, dict):
            text = output.get("text")
            if isinstance(text, str):
                if usable_page(text):
                    self.usable = ref
                else:  # a footer or a login page is no source: the next link may be tried
                    self.note_failure(capability)


def _url_of(call_input: Any) -> str:
    if isinstance(call_input, dict) and isinstance(call_input.get("url"), str):
        return call_input["url"].strip()
    return ""


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def source_url(capability: str, output: Any) -> str | None:
    """The page a ``web.fetch`` result came from (after redirects): the evidence's source."""
    if capability == FETCH and isinstance(output, dict) and isinstance(output.get("url"), str):
        return output["url"]
    return None
