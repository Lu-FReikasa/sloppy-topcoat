"""End-to-end: real hub process (``python -m topcoat_hub.serve``, TLS mode ``auto``) + two real
``topcoat-node run --fake`` processes paired with the hub CA fingerprint.

Needs a built node binary: ``TOPCOAT_NODE_BIN=/path/to/topcoat-node pytest -m integration``.
Skipped otherwise (CI runs it in its own job).
"""

import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import AUTH, DEV_TOKEN

NODE_BIN = os.environ.get("TOPCOAT_NODE_BIN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not NODE_BIN, reason="TOPCOAT_NODE_BIN not set"),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(check: Callable[[], Any], timeout: float = 20.0, what: str = "") -> Any:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (AssertionError, httpx.HTTPError, KeyError, StopIteration) as exc:
            last = exc
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}: {last!r}")


class Hub:
    def __init__(self, db_url: str, log_path: Path) -> None:
        self.port = _free_port()
        self.url = f"https://127.0.0.1:{self.port}"
        self.tls_dir = log_path.parent / "hub-tls"
        self.env = {
            **os.environ,
            "TOPCOAT_DATABASE_URL": db_url,
            "TOPCOAT_DEV_ADMIN_TOKEN": DEV_TOKEN,
            "TOPCOAT_WEB_DIST": str(log_path.parent / "no-web"),
            "TOPCOAT_TLS_MODE": "auto",
            "TOPCOAT_TLS_DIR": str(self.tls_dir),
            "TOPCOAT_TLS_HOSTNAMES": "hub.example.lan",
            "TOPCOAT_HOST": "127.0.0.1",
            "TOPCOAT_PORT": str(self.port),
        }
        self.log_path = log_path
        self.proc: subprocess.Popen[bytes] | None = None
        self.http: httpx.Client | None = None

    def start(self) -> None:
        log = self.log_path.open("ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "topcoat_hub.serve", "--no-migrate"],
            env=self.env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        ca = self.tls_dir / "ca.pem"
        wait_until(ca.exists, what="hub CA")
        # Every client call verifies the hub certificate against the hub CA.
        self.http = httpx.Client(verify=ssl.create_default_context(cafile=str(ca)), timeout=35)
        wait_until(lambda: self.http.get(f"{self.url}/api/health").status_code == 200, what="hub")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)
        if self.http:
            self.http.close()


@pytest.fixture
def hub(clean_db: str, tmp_path: Path) -> Iterator[Hub]:
    h = Hub(clean_db, tmp_path / "hub.log")
    h.start()
    yield h
    h.stop()
    print(h.log_path.read_text())


@pytest.fixture
def node_procs(tmp_path: Path) -> Iterator[list[subprocess.Popen[bytes]]]:
    procs: list[subprocess.Popen[bytes]] = []
    yield procs
    for p in procs:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
    for log in sorted(tmp_path.glob("node-*.log")):
        print(f"--- {log.name}\n{log.read_text()}")


def _pair_and_run(
    hub: Hub,
    name: str,
    tmp_path: Path,
    procs: list[subprocess.Popen[bytes]],
    run_args: Sequence[str] = (),
    role: str | None = None,
) -> None:
    assert NODE_BIN
    pairing = hub.http.post(f"{hub.url}/api/pairing-codes", headers=AUTH).json()
    code, fingerprint = pairing["code"], pairing["ca_fingerprint"]
    assert fingerprint.startswith("sha256:")
    data_dir = tmp_path / name
    subprocess.run(
        [
            NODE_BIN,
            "pair",
            "--hub",
            hub.url,
            "--code",
            code,
            "--name",
            name,
            "--data-dir",
            str(data_dir),
            "--ca-fingerprint",
            fingerprint,
            *(["--role", role] if role else []),
        ],
        check=True,
        timeout=30,
    )
    log = (tmp_path / f"node-{name}.log").open("ab")
    procs.append(
        subprocess.Popen(
            [NODE_BIN, "run", "--fake", "--data-dir", str(data_dir), *run_args],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    )


def _nodes(hub: Hub) -> dict[str, dict[str, Any]]:
    r = hub.http.get(f"{hub.url}/api/nodes", headers=AUTH)
    r.raise_for_status()
    return {n["name"]: n for n in r.json()}


def _event_count(hub: Hub, node_id: str, kind: str) -> int:
    r = hub.http.get(f"{hub.url}/api/nodes/{node_id}/events", params={"limit": 500}, headers=AUTH)
    r.raise_for_status()
    return sum(1 for e in r.json() if e["kind"] == kind)


def _action(hub: Hub, node_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    r = hub.http.post(
        f"{hub.url}/api/nodes/{node_id}/actions",
        headers=AUTH,
        json={"name": name, "args": args},
        timeout=35,
    )
    assert r.status_code == 200, r.text
    return r.json()


class StreamReader(threading.Thread):
    """Collects SSE events from /api/stream in the background."""

    def __init__(self, url: str, verify: str) -> None:
        super().__init__(daemon=True)
        self.url = url
        self.verify = verify
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.stop = False

    def run(self) -> None:
        try:
            with httpx.stream(
                "GET",
                f"{self.url}/api/stream",
                headers=AUTH,
                timeout=60,
                verify=ssl.create_default_context(cafile=self.verify),
            ) as r:
                event = ""
                for line in r.iter_lines():
                    if self.stop:
                        return
                    if line.startswith("event: "):
                        event = line.removeprefix("event: ")
                    elif line.startswith("data: "):
                        self.events.append((event, json.loads(line.removeprefix("data: "))))
        except httpx.HTTPError:
            return  # hub went away (restart part of the test)


def test_two_fake_nodes_load_unload_and_recover(
    hub: Hub, node_procs: list[subprocess.Popen[bytes]], tmp_path: Path
) -> None:
    stream = StreamReader(hub.url, str(hub.tls_dir / "ca.pem"))
    stream.start()

    _pair_and_run(hub, "fake-a", tmp_path, node_procs)
    _pair_and_run(hub, "fake-b", tmp_path, node_procs)

    def both_online() -> dict[str, dict[str, Any]]:
        nodes = _nodes(hub)
        assert set(nodes) == {"fake-a", "fake-b"}
        for n in nodes.values():
            assert n["connected"] and n["state"] == "available", n
            assert n["models"] and n["gpus"] and n["gpus"][0]["vram_used_mb"] is not None, n
            assert n["runner"]["type"] == "fake"
        return nodes

    nodes = wait_until(both_online, what="both fake nodes online")
    a = nodes["fake-a"]
    model = a["models"][0]["name"]
    vram_before = a["gpus"][0]["vram_used_mb"]

    result = _action(hub, a["id"], "models.load", {"model": model})
    assert result["ok"] is True, result

    def loaded_on_a() -> dict[str, Any]:
        n = _nodes(hub)["fake-a"]
        m = next(m for m in n["models"] if m["name"] == model)
        assert m["loaded"] is True
        assert n["gpus"][0]["vram_used_mb"] > vram_before
        return n

    wait_until(loaded_on_a, what="model loaded on fake-a")
    assert not any(m["loaded"] for m in _nodes(hub)["fake-b"]["models"])

    # The GUI stream saw the change.
    wait_until(
        lambda: any(
            ev == "node_updated" and d["name"] == "fake-a" and any(m["loaded"] for m in d["models"])
            for ev, d in stream.events
        ),
        what="node_updated with loaded model on the SSE stream",
    )

    # Node-side validation and whitelist.
    bad = _action(hub, a["id"], "models.load", {"model": model, "rm": "-rf"})
    assert bad == {
        "ok": False,
        "error": {"code": "invalid_args", "message": bad["error"]["message"]},
    }

    assert _action(hub, a["id"], "models.unload", {"model": model})["ok"] is True
    wait_until(
        lambda: not any(m["loaded"] for m in _nodes(hub)["fake-a"]["models"]),
        what="model unloaded on fake-a",
    )
    events = hub.http.get(f"{hub.url}/api/nodes/{a['id']}/events", headers=AUTH).json()
    kinds = [e["kind"] for e in events]
    assert "model_loaded" in kinds and "model_unloaded" in kinds and "action" in kinds

    # Hub restart: nodes reconnect on their own. Session state is in memory, so the only
    # reliable proof of a reconnect is a new "connected" event per node (the nodes retry
    # within ~1 s and may already be back by the time the restarted hub answers).
    def connects() -> dict[str, int]:
        return {name: _event_count(hub, n["id"], "connected") for name, n in _nodes(hub).items()}

    before = connects()
    stream.stop = True
    hub.stop()
    hub.start()

    def reconnected() -> bool:
        both_online()
        after = connects()
        assert all(after[name] > count for name, count in before.items()), (before, after)
        return True

    wait_until(reconnected, timeout=30, what="nodes reconnect after hub restart")


def _events(hub: Hub, node_id: str, kind: str) -> list[dict[str, Any]]:
    """Events of one kind, newest first."""
    r = hub.http.get(f"{hub.url}/api/nodes/{node_id}/events", params={"limit": 500}, headers=AUTH)
    r.raise_for_status()
    return [e for e in r.json() if e["kind"] == kind]


def test_resident_model_yields_vram_and_comes_back(
    hub: Hub, node_procs: list[subprocess.Popen[bytes]], tmp_path: Path
) -> None:
    """E1-06 end to end: another program takes the VRAM of the fake GPU, the node unloads its
    resident model and reports "yielded"; when the memory is free again the model returns."""
    model = "fake-llm-8b-q4"  # 6100 MB on the 16 GB fake GPU (800 MB used by the desktop)
    control = tmp_path / "control.json"
    control.write_text('{"gpus": []}')
    _pair_and_run(hub, "fake-p", tmp_path, node_procs, ["--fake-control", str(control)])

    def online() -> dict[str, Any]:
        n = _nodes(hub)["fake-p"]
        assert n["connected"] and n["state"] == "available", n
        return n

    node_id = wait_until(online, what="fake-p online")["id"]
    url = f"{hub.url}/api/nodes/{node_id}"
    config = hub.http.get(url, headers=AUTH).json()["config"]
    assert config["pressure"]["enabled"] is True  # default for new nodes
    fast = {
        "poll_seconds": 1,
        "sustain_seconds": 1,
        "reload_after_seconds": 1,
        "min_off_seconds": 0,
    }
    config["pressure"].update(fast)
    r = hub.http.put(f"{url}/config", headers=AUTH, json=config)
    assert r.status_code == 200, r.text
    r = hub.http.put(f"{url}/residents/{model}", headers=AUTH, json={"priority": 80})
    assert r.status_code == 200, r.text

    def loaded() -> bool:
        return any(m["name"] == model and m["loaded"] for m in _nodes(hub)["fake-p"]["models"])

    wait_until(loaded, what="resident loaded by the node itself")
    assert _events(hub, node_id, "model_loaded")[0]["data"]["reason"] == "resident"

    control.write_text(json.dumps({"gpus": [{"foreign_vram_mb": 9000, "foreign_util": 90}]}))

    def yielded() -> dict[str, Any]:
        n = _nodes(hub)["fake-p"]
        assert n["state"] == "yielded", n["state"]
        assert not any(m["loaded"] for m in n["models"])
        return n

    gpu = wait_until(yielded, what="node yielded")["gpus"][0]
    assert gpu["foreign_vram_used_mb"] >= 9000, gpu
    assert gpu["foreign_gpu_util"] == 90, gpu
    unloaded = _events(hub, node_id, "model_unloaded")[0]["data"]
    assert unloaded["model"] == model and unloaded["reason"] == "vram_pressure", unloaded

    control.write_text(json.dumps({"gpus": [{}]}))

    def back() -> bool:
        return loaded() and _nodes(hub)["fake-p"]["state"] == "available"

    wait_until(back, what="resident reloaded")
    assert _events(hub, node_id, "model_loaded")[0]["data"]["reason"] == "pressure_cleared"

    # A manual unload clears auto_reload: the node leaves the model unloaded.
    assert _action(hub, node_id, "models.unload", {"model": model})["ok"] is True
    residents = hub.http.get(url, headers=AUTH).json()["config"]["residents"]
    assert residents == [{"model": model, "priority": 80, "auto_reload": False}]
    time.sleep(3)  # reload_after_seconds is 1: a reload would have happened by now
    assert not loaded()


def test_gateway_streams_chat_answer_via_curl(
    hub: Hub, node_procs: list[subprocess.Popen[bytes]], tmp_path: Path
) -> None:
    """E1-05 definition of done: an OpenAI-style curl request against the hub gets a
    streamed answer from a fake node; the model is loaded on demand."""
    _pair_and_run(hub, "fake-g", tmp_path, node_procs)

    def online() -> dict[str, Any]:
        n = _nodes(hub)["fake-g"]
        assert n["connected"] and n["state"] == "available", n
        return n

    wait_until(online, what="fake-g online")
    key = hub.http.post(f"{hub.url}/api/keys", headers=AUTH, json={"name": "curl"}).json()["key"]
    body = json.dumps(
        {
            "model": "fake-llm-3b-q8",
            "stream": True,
            "messages": [{"role": "user", "content": "Hallo Gateway"}],
        }
    )
    out = subprocess.run(
        [
            "curl", "-sS", "-N", "--fail-with-body", "--max-time", "60",
            "--cacert", str(hub.tls_dir / "ca.pem"),
            "-H", f"Authorization: Bearer {key}",
            "-H", "Content-Type: application/json",
            "-D", "-",
            "-d", body,
            f"{hub.url}/v1/chat/completions",
        ],
        capture_output=True, text=True, timeout=90, check=False,
    )  # fmt: skip
    assert out.returncode == 0, out.stderr + out.stdout
    head, _, stream = out.stdout.partition("\n\n")  # text mode: \r\n became \n
    assert "x-topcoat-node: fake-g" in head.lower()
    events = [line[6:] for line in stream.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    text = "".join(json.loads(e)["choices"][0]["delta"].get("content", "") for e in events[:-1])
    assert text == "Fake-Antwort von fake-llm-3b-q8: Hallo Gateway"
    n = _nodes(hub)["fake-g"]
    assert any(m["name"] == "fake-llm-3b-q8" and m["loaded"] for m in n["models"])

    # Listed for clients; unknown models get an OpenAI-style 404.
    models = hub.http.get(f"{hub.url}/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert "fake-llm-3b-q8" in {m["id"] for m in models.json()["data"]}
    r = hub.http.post(
        f"{hub.url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "nope", "messages": []},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


def _sse_lines(response: httpx.Response) -> Iterator[tuple[str, dict[str, Any]]]:
    event = ""
    for line in response.iter_lines():
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            yield event, json.loads(line.removeprefix("data: "))


def test_chat_streams_and_stop_keeps_the_partial_answer(
    hub: Hub, node_procs: list[subprocess.Popen[bytes]], tmp_path: Path
) -> None:
    """E2-01 with real processes: a chat answer streams through hub and node; when the client
    closes the stream (the stop button) the hub cancels it on the node, keeps what was said
    (status "stopped") and the node is ready for the next question."""
    _pair_and_run(hub, "fake-c", tmp_path, node_procs)

    def online() -> dict[str, Any]:
        n = _nodes(hub)["fake-c"]
        assert n["connected"] and n["state"] == "available", n
        return n

    wait_until(online, what="fake-c online")
    model = "fake-llm-3b-q8"
    hub.http.put(
        f"{hub.url}/api/scheduling/applications",
        headers=AUTH,
        json={"rows": {"chat": {"models": [model]}}},
    )
    conv = hub.http.post(f"{hub.url}/api/conversations", headers=AUTH, json={}).json()["id"]
    url = f"{hub.url}/api/conversations/{conv}/messages"

    # A complete answer first (the model loads on demand).
    with hub.http.stream("POST", url, headers=AUTH, json={"content": "Hallo Chat"}) as r:
        assert r.status_code == 200
        events = list(_sse_lines(r))
    assert events[0][0] == "meta" and events[0][1]["node"] == "fake-c"
    assert events[-1][0] == "done"
    text = "".join(d["text"] for e, d in events if e == "delta")
    assert text == f"Fake-Antwort von {model}: Hallo Chat"
    done = events[-1][1]["message"]
    assert done["status"] == "complete" and done["completion_tokens"] == len(text.split())

    # A long answer (the fake node streams the question back word by word); the reader goes away
    # (a closed tab) -- since E3-07 the answer is finished on the hub regardless.
    words = " ".join(f"wort{i}" for i in range(150))
    with hub.http.stream("POST", url, headers=AUTH, json={"content": words}) as r:
        got = ""
        for event, data in _sse_lines(r):
            if event == "delta":
                got += data["text"]
            if len(got.split()) >= 6:
                break  # leaving the block closes the connection

    def finished() -> dict[str, Any]:
        msgs = hub.http.get(f"{hub.url}/api/conversations/{conv}", headers=AUTH).json()["messages"]
        last = [m for m in msgs if m["role"] == "assistant"][-1]
        assert last["status"] == "complete", last["status"]
        return last

    full = wait_until(finished, what="the answer to finish without a reader")
    assert len(full["content"].split()) > 150

    # Stopping is an explicit call: what was said stays (status "stopped"), the node is free again.
    with hub.http.stream("POST", url, headers=AUTH, json={"content": words}) as r:
        got = ""
        for event, data in _sse_lines(r):
            if event == "delta":
                got += data["text"]
            if len(got.split()) >= 6:
                break
    stop = hub.http.post(f"{hub.url}/api/conversations/{conv}/stop", headers=AUTH)
    assert stop.status_code == 200, stop.text

    def stopped() -> dict[str, Any]:
        msgs = hub.http.get(f"{hub.url}/api/conversations/{conv}", headers=AUTH).json()["messages"]
        return next(m for m in msgs if m["status"] == "stopped")

    partial = wait_until(stopped, what="the stopped answer to be stored")
    assert partial["role"] == "assistant" and partial["content"].startswith("Fake-Antwort")
    assert 6 <= len(partial["content"].split()) < 150

    # The gateway slot and the node are free again: the next question is answered in full.
    with hub.http.stream("POST", url, headers=AUTH, json={"content": "Noch da?"}) as r:
        events = list(_sse_lines(r))
    assert events[-1][0] == "done"
    statuses = [
        m["status"]
        for m in hub.http.get(f"{hub.url}/api/conversations/{conv}", headers=AUTH).json()[
            "messages"
        ]
        if m["role"] == "assistant"
    ]
    assert statuses == ["complete", "complete", "stopped", "complete"]


def test_a_tool_node_serves_read_only_capabilities_with_masked_output(
    hub: Hub, node_procs: list[subprocess.Popen[bytes]], tmp_path: Path
) -> None:
    """The real node binary paired with ``--role tools`` (fake Docker/journal behind it): the hub
    learns its capabilities from the inventory and runs them; secrets are masked on the node."""
    _pair_and_run(hub, "vm-fake", tmp_path, node_procs, role="tools")

    def online() -> dict[str, Any]:
        n = _nodes(hub)["vm-fake"]
        assert n["connected"], n
        return n

    node = wait_until(online, what="the tool node online")
    cfg = hub.http.get(f"{hub.url}/api/nodes/{node['id']}", headers=AUTH).json()["config"]
    assert cfg["roles"] == ["tools"] and cfg["runner"] == {"type": "none"}

    def offered() -> list[str]:
        r = hub.http.get(f"{hub.url}/api/nodes/{node['id']}/capabilities", headers=AUTH)
        r.raise_for_status()
        names = [c["name"] for c in r.json()["offered"]]
        assert names
        return names

    names = wait_until(offered, what="the capabilities in the inventory")
    assert {"docker.ps", "docker.logs", "compose.file", "journal.logs", "disk.usage"} <= set(names)

    def call(cap: str, body: dict[str, Any]) -> httpx.Response:
        return hub.http.post(
            f"{hub.url}/api/nodes/{node['id']}/capabilities/{cap}",
            headers=AUTH,
            json={"input": body},
        )

    ps = call("docker.ps", {})
    assert ps.status_code == 200 and ps.json()["ok"], ps.text
    by_name = {c["name"]: c for c in ps.json()["output"]["containers"]}
    assert by_name["site-worker-1"]["exit_code"] == 137 and by_name["site-worker-1"]["oom_killed"]

    logs = call("docker.logs", {"container": "site-worker-1", "lines": 20})
    assert logs.status_code == 200 and logs.json()["ok"], logs.text
    text = logs.json()["output"]["log"]
    assert "demo-password" not in text and "«masked:url_credentials»" in text

    env = call("compose.file", {"project": "site", "file": ".env"})
    assert env.status_code == 200 and env.json()["ok"], env.text
    assert "fake-db-password" not in env.json()["output"]["content"]
    assert "HOST=db" in env.json()["output"]["content"]

    # a connector (E2-04c): Uptime Kuma, answered by the fake node from memory; nothing of the key,
    # the monitors' tokens or the query strings reaches the hub
    assert "kuma.monitors" in names
    down = call("kuma.monitors", {"status": "down"})
    assert down.status_code == 200 and down.json()["ok"], down.text
    assert [m["name"] for m in down.json()["output"]["monitors"]] == ["NAS ping"]
    everything = call("kuma.monitors", {})
    assert everything.json()["ok"] and everything.json()["output"]["problems"] == 1
    web = next(m for m in everything.json()["output"]["monitors"] if m["name"] == "Web")
    assert web["status"] == "up" and web["cert_days_remaining"] == 9
    assert web["url"] == "https://web.example.lan/"
    assert call("kuma.monitors", {"url": "http://evil"}).status_code == 422  # no input takes a URL
    trail = hub.http.get(f"{hub.url}/api/tool-calls", headers=AUTH).text
    assert "uk1_fake" not in trail and "abcdefabcdef1234" not in trail
    assert "uk1_fake" not in down.text + everything.text

    # names that could steer a call somewhere else are refused by the schema at the hub already
    for bad in ("../etc", "a b", "web;id", "-x"):
        assert call("docker.logs", {"container": bad}).status_code == 422, bad
    assert call("service.status", {"unit": "--help"}).status_code == 422
    assert call("compose.file", {"project": "site", "file": "/etc/shadow"}).json()["ok"] is False

    audit = hub.http.get(f"{hub.url}/api/tool-calls", headers=AUTH).json()
    assert [c["capability"] for c in audit][:1] == ["compose.file"]
    assert any(c["capability"] == "docker.ps" and c["ok"] for c in audit)

    # the knowledge base (E2-03): the node releases one directory (the fake's notes folder), the
    # hub indexes it through docs.list / docs.read; what the exclude list names is never seen
    assert {"docs.list", "docs.read"} <= set(names)
    made = hub.http.post(
        f"{hub.url}/api/knowledge/sources",
        headers=AUTH,
        json={"name": "Notes", "node": "vm-fake", "visibility": "shared"},
    )
    assert made.status_code == 201, made.text

    def source_ready() -> dict[str, Any]:
        src = hub.http.get(f"{hub.url}/api/knowledge/sources/{made.json()['id']}", headers=AUTH)
        assert src.json()["status"] == "ready", src.text
        return src.json()

    src = wait_until(source_ready, what="the knowledge source indexed")
    assert src["files"] == 2 and src["excluded"] >= 2
    hits = hub.http.get(
        f"{hub.url}/api/knowledge/search", headers=AUTH, params={"q": "nightly backup database"}
    ).json()["hits"]
    assert hits and hits[0]["path"] == "README.md" and "03:00" in hits[0]["text"]
    for q in ("admin password fake-secret-value", "logins"):
        found = hub.http.get(f"{hub.url}/api/knowledge/search", headers=AUTH, params={"q": q})
        assert found.json()["hits"] == []  # (the answer echoes the query, so look at the hits)
    listing = call("docs.list", {})
    assert (
        listing.json()["ok"] and "SENSIBEL" not in listing.text and "password" not in listing.text
    )
    for path in ("SENSIBEL/logins.md", "servers/nas-password.md", "../../etc/passwd"):
        refused = call("docs.read", {"path": path})
        assert refused.json()["ok"] is False and "fake-secret-value" not in refused.text, path


def test_the_agent_reaches_its_model_through_the_gateway_with_a_tool_node_connected(
    hub: Hub, node_procs: list[subprocess.Popen[bytes]], tmp_path: Path
) -> None:
    """Real hub, real node binaries: a tool node (capabilities) and a fake model node. The fake
    model answers in prose, not in the agent's JSON — so the task must fail in an orderly way
    (``bad_format``) after three corrections, with the model's answers kept as steps. What this
    proves is the plumbing: the task starts, the hub reaches the model through the gateway (first
    with native ``tools``, then with ``response_format``), every answer is stored, and nothing
    else happens."""
    _pair_and_run(hub, "vm-fake", tmp_path, node_procs, role="tools")
    _pair_and_run(hub, "model-fake", tmp_path, node_procs)

    def ready() -> bool:
        nodes = _nodes(hub)
        assert {"vm-fake", "model-fake"} <= set(nodes)
        assert all(n["connected"] for n in nodes.values())
        r = hub.http.get(f"{hub.url}/api/nodes/{nodes['vm-fake']['id']}/capabilities", headers=AUTH)
        return bool(r.json()["offered"])

    wait_until(ready, what="both nodes online with the capabilities known")
    model = _nodes(hub)["model-fake"]["models"][0]["name"]

    r = hub.http.post(
        f"{hub.url}/api/agent/tasks",
        headers=AUTH,
        json={"prompt": "why is web restarting?", "model": model},
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    def finished() -> dict[str, Any]:
        t = hub.http.get(f"{hub.url}/api/agent/tasks/{task_id}", headers=AUTH).json()
        assert t["status"] in ("failed", "done"), t["status"]
        return t

    t = wait_until(finished, timeout=60, what="the task to end")
    assert t["status"] == "failed" and t["stop_reason"] == "bad_format", t
    kinds = [s["kind"] for s in t["steps"]]
    # the default tool mode is auto: two prose answers to native tool calls, then the JSON protocol
    # (three more corrections) — the model never does either
    assert kinds.count("assistant") == 5 and kinds.count("observation") == 5
    notes = [s["data"]["text"] for s in t["steps"] if s["kind"] == "note"]
    assert any("Switching to the prompt-based JSON protocol" in n for n in notes)
    assert all(s["data"]["content"] for s in t["steps"] if s["kind"] == "assistant")
    # the model node was asked (and loaded by the gateway); the tool node was never called
    assert _nodes(hub)["model-fake"]["models"][0]["loaded"]
    assert hub.http.get(f"{hub.url}/api/tool-calls", headers=AUTH).json() == []
