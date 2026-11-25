"""The redaction layer: the only way text leaves the hub for an external model (E3-02, D33).

Two different treatments, because the two kinds of content need different things:

* **Secrets are removed** (:func:`topcoat_hub.memory_filter.scrub`): a password, key or token
  is never useful to an outside reader, and a stand-in for it would still be a hint.
* **Identifiers are pseudonymised, consistently**: host names, IP addresses (v4/v6), MAC
  addresses, e-mail addresses and user names get stand-ins that keep the *structure* — the
  same machine is always the same stand-in, two machines in one /24 stay in one /24 — so
  "host-1 cannot reach host-2" is still a readable diagnosis.

The mapping (real <-> stand-in) lives in the hub, per user and per scope (a task, a conversation),
is persisted so a second question about the same task uses the same stand-ins, and is **never
sent**. :meth:`Redactor.restore` translates the external answer back; a stand-in the model
invents that is not in the mapping stays as it is.

Everything is one pass of one regular expression over the text, so a stand-in is never itself
redacted again and two kinds of identifier cannot overlap.
"""

import ipaddress
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from topcoat_hub import memory_filter

# Top-level labels that only exist inside a home network: a name ending in one is a machine of
# the operator. (A public domain of the operator has to be listed in TOPCOAT_REDACT_DOMAINS.)
PRIVATE_TLDS = (
    "home.arpa",
    "fritz.box",
    "localdomain",
    "intranet",
    "internal",
    "private",
    "corp",
    "home",
    "lan",
    "local",
)

_MIN_NAME = 3  # shorter known names ("pi") would redact half of every log
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
_GUARD_BEFORE = r"(?<![\w.\-])"
_GUARD_AFTER = r"(?![\w\-])"

# addresses of the operator's own network (the documentation ranges are "public" here)
_LAN = tuple(
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10")
)

KINDS = ("host", "domain", "ip", "mac", "email", "user")
_ALIAS_GUARD = (r"(?<![\w\-])", r"(?![\w\-])")


class RedactionError(Exception):
    """The text cannot be made safe to send (a secret survived the removal)."""

    def __init__(self, rule: str) -> None:
        super().__init__(f"the text still contains something that looks like a secret ({rule})")
        self.rule = rule


@dataclass
class Redacted:
    text: str
    counts: dict[str, int] = field(default_factory=dict)  # kind -> distinct identifiers
    secrets: list[str] = field(default_factory=list)  # names of the removal rules that fired


def _alternation(names: Iterable[str]) -> str:
    return "|".join(re.escape(n) for n in sorted(set(names), key=lambda n: (-len(n), n)))


def _mac_key(value: str) -> str:
    return value.lower().replace("-", ":")


def _keep_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_loopback or ip.is_unspecified or ip == ipaddress.ip_address("255.255.255.255")


class Redactor:
    """Pseudonymises one scope. ``entries`` is the stored mapping of earlier calls."""

    def __init__(
        self,
        entries: Iterable[dict[str, str]] = (),
        hosts: Iterable[str] = (),
        users: Iterable[str] = (),
        domains: Iterable[str] = (),
    ) -> None:
        # (kind, key) -> (alias, real as first seen)
        self._by_key: dict[tuple[str, str], tuple[str, str]] = {}
        self._by_alias: dict[str, tuple[str, str]] = {}  # alias -> (kind, real)
        self._counters: dict[str, int] = dict.fromkeys(KINDS, 0)
        self._nets: dict[str, int] = {}  # private /24 (real) -> its number
        for e in entries:
            self._remember(e["kind"], e["key"], e["alias"], e["real"])
        hosts = [h for h in hosts if len(h) >= _MIN_NAME]
        users = [u for u in users if len(u) >= _MIN_NAME]
        domains = [d.strip().lower().strip(".") for d in domains if d.strip()]
        self._domains = sorted(set(domains), key=lambda d: (-len(d), d))
        parts = [
            ("email", r"[A-Za-z0-9._%+\-]+@(?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,}"),
            (
                "mac",
                r"(?<![0-9A-Fa-f:\-])(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:\-])",
            ),
            ("ip6", r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])"),
            ("ip4", r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)"),
        ]
        if self._domains:
            parts.append(
                (
                    "fqdn_own",
                    rf"{_GUARD_BEFORE}(?:{_LABEL}\.)*(?:{_alternation(self._domains)}){_GUARD_AFTER}",
                )
            )
        parts.append(
            (
                "fqdn_priv",
                rf"{_GUARD_BEFORE}(?:{_LABEL}\.)+(?:{_alternation(PRIVATE_TLDS)}){_GUARD_AFTER}",
            )
        )
        if hosts:
            parts.append(("host", rf"{_GUARD_BEFORE}(?:{_alternation(hosts)}){_GUARD_AFTER}"))
        if users:
            parts.append(("user", rf"{_GUARD_BEFORE}(?:{_alternation(users)}){_GUARD_AFTER}"))
        # the user component of a path: /home/<user>/, C:\Users\<user>\
        parts.append(("path_user", r"(?:(?<=/home/)|(?<=/Users/)|(?<=\\Users\\))[^/\\\s:\"']+"))
        self._pattern = re.compile(
            "|".join(f"(?P<{name}>{rx})" for name, rx in parts), re.IGNORECASE
        )
        self._text_lower = ""

    # --- the mapping ---------------------------------------------------------------------

    def entries(self) -> list[dict[str, str]]:
        """The mapping as stored (JSON-able); includes everything remembered so far."""
        return [
            {"kind": kind, "key": key, "alias": alias, "real": real}
            for (kind, key), (alias, real) in sorted(self._by_key.items(), key=lambda i: i[1][0])
        ]

    def _remember(self, kind: str, key: str, alias: str, real: str) -> None:
        self._by_key[(kind, key)] = (alias, real)
        self._by_alias[alias] = (kind, real)
        m = re.search(r"(\d+)$", alias)
        if m and kind in self._counters:
            self._counters[kind] = max(self._counters[kind], int(m.group(1)))
        if kind == "ip" and alias.startswith("10."):  # a stand-in for a non-public IPv4 address
            net = _net_of(key)
            if net:
                _, hi, lo, _ = alias.split(".")
                self._nets.setdefault(net, int(hi) * 256 + int(lo))

    def _taken(self, alias: str) -> bool:
        """An alias must not collide with a real token of the text it replaces in."""
        return alias in self._by_alias or alias.lower() in self._text_lower

    def _alias(self, kind: str, key: str, real: str, make: Callable[[int], str]) -> str:
        known = self._by_key.get((kind, key))
        if known:
            return known[0]
        while True:
            self._counters[kind] += 1
            alias = make(self._counters[kind])
            if not self._taken(alias):
                break
        self._remember(kind, key, alias, real)
        return alias

    # --- redact --------------------------------------------------------------------------

    def redact(self, text: str) -> Redacted:
        """Remove secrets, pseudonymise identifiers. Raises :class:`RedactionError` if a secret
        survives (the text is then not sent at all)."""
        text, fired = memory_filter.scrub(text)
        self._text_lower = text.lower()
        out = self._pattern.sub(self._replace, text)
        rule = memory_filter.check_scrubbed(out)
        if rule:
            raise RedactionError(rule)
        counts = dict.fromkeys(KINDS, 0)
        lowered = out.lower()
        for alias, (kind, _real) in self._by_alias.items():
            if alias.lower() in lowered:
                counts[kind] += 1
        return Redacted(out, {k: v for k, v in counts.items() if v}, fired)

    def _replace(self, m: re.Match[str]) -> str:
        kind = m.lastgroup or ""
        value = m.group()
        if kind == "email":
            return self._alias("email", value.lower(), value, lambda n: f"user-{n}@example.invalid")
        if kind == "mac":
            return self._alias("mac", _mac_key(value), value, lambda n: f"02:00:00:00:00:{n:02x}")
        if kind == "ip6":
            return self._ip(value, v6=True)
        if kind == "ip4":
            return self._ip(value, v6=False)
        if kind in ("fqdn_own", "fqdn_priv"):
            return self._fqdn(value)
        if kind == "host":
            return self._host(value)
        if kind in ("user", "path_user"):
            return self._alias("user", value.lower(), value, lambda n: f"user-{n}")
        return value

    def _host(self, name: str) -> str:
        return self._alias("host", name.lower(), name, lambda n: f"host-{n}")

    def _ip(self, value: str, *, v6: bool) -> str:
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            return value  # "12:30:45", "300.1.1.1": not an address
        if (ip.version == 6) != v6 or _keep_ip(ip):
            return value
        key = str(ip)
        known = self._by_key.get(("ip", key))
        if known:
            return known[0]
        if v6:
            return self._alias("ip", key, value, lambda n: f"fd00::{n:x}")
        if any(ip in net for net in _LAN):
            # 10.0.<network>.<last octet>: hosts of one /24 stay together, the last octet stays
            net = _net_of(key) or ""
            number = self._nets.get(net)
            if number is None:
                number = max(self._nets.values(), default=0) + 1
                self._nets[net] = number
            last = key.rsplit(".", 1)[1]
            alias = f"10.{number // 256}.{number % 256}.{last}"
            if self._taken(alias):
                # a real address of the text looks like the stand-in: take the next network
                number = max(self._nets.values()) + 1
                self._nets[net] = number
                alias = f"10.{number // 256}.{number % 256}.{last}"
            self._remember("ip", key, alias, value)
            return alias
        # public addresses: the benchmarking range (198.18.0.0/15) cannot route anywhere and the
        # last octet is not kept (it would leak a quarter of the address)
        return self._alias("ip", key, value, lambda n: f"198.18.{n // 256}.{n % 256}")

    def _fqdn(self, name: str) -> str:
        lowered = name.lower()
        domain = next((d for d in self._domains if lowered == d or lowered.endswith("." + d)), None)
        if domain is not None:
            tld = domain.rsplit(".", 1)[-1]
            rest = lowered[: -len(domain)].rstrip(".")
            alias_domain = self._domain(domain, tld)
            return f"{self._host(name[: len(rest)])}.{alias_domain}" if rest else alias_domain
        tld = next(t for t in PRIVATE_TLDS if lowered.endswith("." + t))
        labels = lowered[: -len(tld) - 1].split(".")
        original = name[: -len(tld) - 1].split(".")
        if len(labels) == 1:
            return f"{self._host(original[0])}.{name[-len(tld) :]}"
        domain_part = f"{labels[-1]}.{tld}"
        host = ".".join(original[:-1])
        return f"{self._host(host)}.{self._domain(domain_part, tld)}"

    def _domain(self, domain: str, tld: str) -> str:
        return self._alias("domain", domain, domain, lambda n: f"domain-{n}.{tld}")

    # --- restore -------------------------------------------------------------------------

    def restore(self, text: str) -> str:
        """Stand-ins back to the real names; unknown stand-ins stay as the model wrote them."""
        if not self._by_alias:
            return text
        aliases = sorted(self._by_alias, key=lambda a: (-len(a), a))
        before, after = _ALIAS_GUARD
        pattern = re.compile(
            before + "(?:" + "|".join(re.escape(a) for a in aliases) + ")" + after, re.IGNORECASE
        )
        lowered = {a.lower(): real for a, (_kind, real) in self._by_alias.items()}
        return pattern.sub(lambda m: lowered.get(m.group().lower(), m.group()), text)


def _net_of(ip: str) -> str | None:
    parts = ip.split(".")
    return ".".join(parts[:3]) if len(parts) == 4 else None


def private_kinds(
    text: str,
    hosts: Iterable[str] = (),
    users: Iterable[str] = (),
    domains: Iterable[str] = (),
) -> list[str]:
    """What in ``text`` is private: the kinds of identifier (``host``, ``ip``, ``mac``, ``email``,
    ``user``, ``domain``) and the names of the secret rules that fire; empty when nothing is.
    The same detection as :meth:`Redactor.redact`, used to refuse a web query instead of rewriting
    it (a search engine cannot use a stand-in)."""
    try:
        done = Redactor(hosts=hosts, users=users, domains=domains).redact(text)
    except RedactionError as exc:
        return [exc.rule]
    return [*done.counts, *done.secrets]
