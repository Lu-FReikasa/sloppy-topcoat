# ruff: noqa: E501  (realistic log lines are long)
"""The redaction layer (E3-02): secrets removed, identifiers pseudonymised consistently,
back-translation, isolation between scopes."""

import re

import pytest

from topcoat_hub import memory_filter
from topcoat_hub.redaction import RedactionError, Redactor

HOSTS = ["node-amd", "node-laptop", "vm-docker1"]
USERS = ["Alice Example", "alice", "bob"]

COMPOSE = """\
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: fake-db-password
      TZ: Europe/Berlin
  app:
    image: registry.example.lan:5000/app:2
    environment:
      DATABASE_URL: postgres://app:demo-password@db.example.lan/app
      API_TOKEN: "tok_live_abcdefghijklmnop123456"
    volumes:
      - /home/alice/site/data:/data
"""

LOG = """\
2026-09-30T07:59:00Z node-amd.example.lan sshd[412]: Accepted publickey for alice from 192.168.10.23 port 50122
2026-09-30T07:59:01Z vm-docker1 kernel: eth0: link up, mac 52:54:00:AB:CD:01, gw 192.168.10.1
2026-09-30T07:59:02Z vm-docker1 app[7]: upstream 192.168.20.5:8080 unreachable, retry via 203.0.113.9
2026-09-30T07:59:03Z vm-docker1 app[7]: notify alice@example.org via fd12:3456:789a::5 failed
2026-09-30T07:59:04Z node-laptop C:\\Users\\Alice\\AppData\\Local\\app.log: Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345
"""

SECRETS = [
    "fake-db-password",
    "demo-password",
    "tok_live_abcdefghijklmnop123456",
    "abcdefghijklmnopqrstuvwxyz012345",
]


def redactor(**kw: object) -> Redactor:
    return Redactor(hosts=HOSTS, users=USERS, domains=kw.pop("domains", ()), **kw)  # type: ignore[arg-type]


def test_round_trip_keeps_structure_and_removes_secrets() -> None:
    r = redactor()
    red = r.redact(COMPOSE + LOG)
    text = red.text
    for secret in SECRETS:
        assert secret not in text
    for real in ("node-amd", "node-laptop", "vm-docker1", "alice", "192.168.10.23", "203.0.113.9"):
        assert real not in text.lower()
    assert "52:54:00:ab:cd:01" not in text.lower()
    assert "example.lan" not in text
    assert "/home/user-" in text  # the path keeps its shape
    # "same /24 stays one /24": .23 and .1 share the network, .5 of the other does not
    m = re.findall(r"\b10\.0\.(\d+)\.(23|1|5)\b", text)
    nets = {last: net for net, last in m}
    assert nets["23"] == nets["1"] != nets["5"]
    assert red.counts["ip"] >= 4 and red.counts["host"] >= 3 and red.counts["mac"] == 1
    assert {"url_credentials", "secret_value"} <= set(red.secrets)
    # everything that came back is translated to the real names again
    restored = r.restore(text)
    for real in ("node-amd", "vm-docker1", "192.168.10.23", "192.168.20.5", "203.0.113.9"):
        assert real in restored
    assert "52:54:00:AB:CD:01" in restored
    assert "alice@example.org" in restored
    assert "fd12:3456:789a::5" in restored
    assert "\\users\\alice\\" in restored.lower()
    assert restored.count("[REDACTED") == text.count("[REDACTED")  # removed stays removed


def test_consistent_within_a_scope_and_across_calls() -> None:
    r = redactor()
    a = r.redact("node-amd talks to node-laptop").text
    b = r.redact("then node-laptop answers node-amd").text
    (x, y) = re.findall(r"host-\d+", a)
    assert re.findall(r"host-\d+", b) == [y, x]
    # the mapping survives a restart: a new Redactor with the stored entries gives the same names
    again = redactor(entries=r.entries())
    assert again.redact("node-laptop").text == y
    assert again.redact("vm-docker1").text not in (x, y)


def test_scopes_do_not_share_stand_ins() -> None:
    one, two = redactor(), redactor()
    one.redact("node-amd and node-laptop")  # host-1, host-2 in scope one
    text = two.redact("only node-laptop here").text
    assert text == "only host-1 here"  # scope two numbers from 1 again
    assert one.restore("host-1") == "node-amd"
    assert two.restore("host-1") == "node-laptop"


def test_fqdn_and_short_name_are_the_same_machine() -> None:
    r = redactor()
    text = r.redact("node-amd.example.lan and node-amd; x.example.lan").text
    m = re.match(r"(host-\d+)\.(domain-\d+)\.lan and (host-\d+); (host-\d+)\.\2\.lan$", text)
    assert m and m.group(1) == m.group(3) != m.group(4)
    assert r.restore(text) == "node-amd.example.lan and node-amd; x.example.lan"


def test_own_public_domain_from_configuration() -> None:
    r = redactor(domains=["mydomain.test"])
    text = r.redact("https://grafana.mydomain.test/d/1 and mydomain.test, docker.io").text
    assert "mydomain" not in text and "grafana" not in text and "docker.io" in text
    assert r.restore(text) == "https://grafana.mydomain.test/d/1 and mydomain.test, docker.io"


def test_public_addresses_do_not_keep_the_last_octet() -> None:
    r = redactor()
    text = r.redact("peer 203.0.113.77 and 8.8.8.8").text
    assert re.fullmatch(r"peer 198\.18\.0\.\d+ and 198\.18\.0\.\d+", text)
    assert "77" not in text.split(" and ")[0].rsplit(".", 1)[-1]


def test_non_identifying_addresses_stay() -> None:
    text = (
        redactor()
        .redact("listen 127.0.0.1:8080 and [::1] on 0.0.0.0, at 07:59:00, std::string")
        .text
    )
    assert text == "listen 127.0.0.1:8080 and [::1] on 0.0.0.0, at 07:59:00, std::string"


def test_versions_and_hashes_are_not_addresses() -> None:
    text = redactor().redact("postgres 16.4 build v1.2.3.4 abc@1.2.3.4-x").text
    assert "v1.2.3.4" in text  # preceded by a word character


def test_unknown_stand_ins_from_the_model_stay() -> None:
    r = redactor()
    r.redact("node-amd")
    assert r.restore("check host-1 and host-7, 10.0.9.9") == "check node-amd and host-7, 10.0.9.9"


def test_stand_in_is_not_a_prefix_match() -> None:
    r = redactor()
    r.redact("node-amd")
    assert r.restore("host-10 host-1.") == "host-10 node-amd."


def test_a_name_in_the_text_that_looks_like_a_stand_in_is_not_reused() -> None:
    r = redactor()
    text = r.redact("container host-1 runs on node-amd").text
    assert r.restore(text) == "container host-1 runs on node-amd"
    assert text.count("host-1") == 1 or "host-2" in text


def test_private_key_block_is_removed_entirely() -> None:
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----"
    text = redactor().redact(f"key follows\n{pem}\nnext line").text
    assert "b3Blbn" not in text and "next line" in text and "[REDACTED:private_key]" in text


def test_short_known_names_are_ignored() -> None:
    r = Redactor(hosts=["pi", "node-amd"])
    assert r.redact("pi and node-amd").text == "pi and host-1"


@pytest.mark.parametrize(
    "line",
    [
        "password=hunter2hunter2",
        "DB_PASSWORD: 'correct horse battery'",
        '{"client_secret": "0123456789abcdefghij"}',
        "curl --password s3cr3tpass https://x",
        "ssh://root:toor12345@node-amd/x",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAABCDEFGHIJKLMNOP",
        "IBAN DE89 3704 0044 0532 0130 00",
    ],
)
def test_secrets_never_survive(line: str) -> None:
    r = redactor()
    text = r.redact(f"before {line} after").text
    assert memory_filter.check_scrubbed(text) is None
    for needle in (
        "hunter2",
        "correct horse",
        "0123456789abcdefghij",
        "s3cr3t",
        "toor12345",
        "abcdefghijklmnopqrstuvwxyz0123",
        "ghp_",
        "AKIA",
        "3704",
    ):
        assert needle not in text
    assert text.startswith("before ") and text.endswith(" after")


def test_property_no_secret_of_the_samples_survives_any_chunking() -> None:
    whole = COMPOSE + LOG
    lines = whole.splitlines()
    for start in range(0, len(lines)):
        for end in range(start + 1, min(len(lines), start + 4) + 1):
            chunk = "\n".join(lines[start:end])
            out = redactor().redact(chunk).text
            for secret in SECRETS:
                assert secret not in out


def test_a_surviving_secret_refuses_the_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory_filter, "scrub", lambda t: (t, []))
    with pytest.raises(RedactionError) as exc:
        redactor().redact("password is hunter2hunter2")
    assert exc.value.rule == "password"
