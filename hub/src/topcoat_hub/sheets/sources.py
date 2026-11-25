"""What a chord page holds, read from its markup (``web.fetch`` with ``format: html``).

A page is untrusted data: nothing here follows a link or runs anything, it only reads text. Two
readers:

``ultimate_guitar``  the page embeds its data as JSON (``js-store``): the chord text with its
                     columns
                     (``[ch]G[/ch]`` marks a chord, ``[tab]`` a block), capo, key, tuning, votes,
                     rating, the kind of the version and the other versions of the song
``pre_blocks``       any page that prints the sheet in ``<pre>`` (most other chord sites): the text
                     of its pre-formatted blocks, columns kept

and ``lrclib`` for the reference lyrics (a JSON API), to tell whether a sheet is the same song.
Robots rules: the search page of Ultimate Guitar is closed to automated readers, its tab pages are
not; the research finds them through the web search (``research.py``).
"""

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

UG_HOSTS = ("tabs.ultimate-guitar.com", "www.ultimate-guitar.com", "ultimate-guitar.com")
_UG_STORE = re.compile(r'class="js-store"\s+data-content="([^"]*)"')
_PRE = re.compile(r"(?is)<pre\b[^>]*>(.*?)</pre>")
_TAGS = re.compile(r"(?s)<[^>]*>")
_TITLE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_EASY = re.compile(r"\b(easy|simplified|simple|beginner|einfach|leicht)\b", re.I)


@dataclass
class Found:
    """One source as read: the sheet as plain "chords over lyrics" text and what the page says."""

    url: str
    site: str
    text: str
    title: str = ""
    artist: str = ""
    kind: str = ""  # Chords, Tab, Ukulele, Bass … (Ultimate Guitar's type)
    version_note: str = ""  # what the page says about its version
    capo: int | None = None
    key: str = ""  # the key the page names (sounding)
    tuning: str = ""
    votes: int | None = None
    rating: float | None = None
    simplified: bool = False
    others: list[dict[str, Any]] = field(default_factory=list)  # other versions of the song

    def meta(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "site": self.site,
            "title": self.title,
            "artist": self.artist,
            "kind": self.kind,
            "version": self.version_note,
            "capo": self.capo,
            "page_key": self.key,
            "tuning": self.tuning,
            "votes": self.votes,
            "rating": self.rating,
            "simplified": self.simplified,
        }


def site_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_ultimate_guitar_tab(url: str) -> bool:
    u = urlparse(url)
    return (u.hostname or "").lower() in UG_HOSTS and u.path.startswith("/tab/")


def _ug_text(content: str) -> str:
    """The chord text of an Ultimate Guitar version as plain "chords over lyrics": the markers go,
    the columns stay (the markers take no columns on the page)."""
    t = content.replace("\r\n", "\n")
    t = re.sub(r"\[/?tab\]", "", t)
    t = re.sub(r"\[ch\](.*?)\[/ch\]", r"\1", t)
    # some tabbers typed entities into their text ("Huey Lewis &amp; the News")
    return html.unescape(t)


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ultimate_guitar(url: str, markup: str) -> Found | None:
    """An Ultimate Guitar tab page, or ``None`` if it carries no readable chord text (a Pro or
    Official version is not public text)."""
    m = _UG_STORE.search(markup)
    if not m:
        return None
    try:
        store = json.loads(html.unescape(m.group(1)))
        data = store["store"]["page"]["data"]
    except (ValueError, KeyError, TypeError):
        return None
    view = data.get("tab_view") or {}
    tab = data.get("tab") or {}
    content = ((view.get("wiki_tab") or {}).get("content")) or ""
    if not content.strip():
        return None
    meta = view.get("meta") or {}
    tuning = meta.get("tuning") or {}
    others = [
        {
            "url": v.get("tab_url"),
            "kind": v.get("type"),
            "version": v.get("version"),
            "votes": _int(v.get("votes")),
            "rating": _float(v.get("rating")),
            "key": v.get("tonality_name") or "",
        }
        for v in view.get("versions") or []
        if isinstance(v, dict) and v.get("tab_url")
    ]
    note = (tab.get("version_description") or "").strip()
    name = f"{tab.get('song_name') or ''} {note}"
    return Found(
        url=url,
        site="ultimate-guitar.com",
        text=_ug_text(content),
        title=str(tab.get("song_name") or ""),
        artist=str(tab.get("artist_name") or ""),
        kind=str(tab.get("type") or ""),
        version_note=note,
        capo=_int(meta.get("capo")),
        key=str(meta.get("tonality") or tab.get("tonality_name") or ""),
        tuning=str(tuning.get("value") or tuning.get("name") or "")
        if isinstance(tuning, dict)
        else "",
        votes=_int(tab.get("votes")),
        rating=_float(tab.get("rating")),
        simplified=bool(view.get("isSimplified")) or bool(_EASY.search(name)),
        others=others,
    )


def page_title(markup: str) -> str:
    m = _TITLE.search(markup)
    return " ".join(html.unescape(_TAGS.sub("", m.group(1))).split()) if m else ""


def pre_blocks(url: str, markup: str) -> Found | None:
    """A page that prints its sheet in ``<pre>``: the longest block that has chord lines."""
    from topcoat_hub.sheets.plaintext import chord_lines  # plaintext imports nothing from here

    best = ""
    for block in _PRE.findall(markup):
        text = html.unescape(_TAGS.sub("", block.replace("<br>", "\n").replace("<br/>", "\n")))
        if chord_lines(text) >= 2 and len(text) > len(best):
            best = text
    if not best:
        return None
    title = page_title(markup)
    return Found(
        url=url,
        site=site_of(url),
        text=best,
        title=title,
        version_note=title,
        simplified=bool(_EASY.search(title)),
    )


def read_page(url: str, markup: str) -> Found | None:
    """The reader that fits the page."""
    if is_ultimate_guitar_tab(url):
        return ultimate_guitar(url, markup)
    return pre_blocks(url, markup)


def lrclib(output: str) -> dict[str, Any] | None:
    """The reference lyrics from lrclib.net (``/api/get`` or the first hit of ``/api/search``)."""
    try:
        data = json.loads(output)
    except ValueError:
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict) or not data.get("plainLyrics"):
        return None
    return {
        "title": data.get("trackName") or "",
        "artist": data.get("artistName") or "",
        "album": data.get("albumName") or "",
        "duration": data.get("duration"),
        "lyrics": data.get("plainLyrics") or "",
    }
