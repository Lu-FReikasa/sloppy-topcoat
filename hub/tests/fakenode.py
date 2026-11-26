"""A minimal protocol-level fake node for hub tests (the real one is the Go binary)."""

import base64
import contextlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from topcoat_hub.nodeauth import signed_message
from topcoat_hub.protocol import PROTOCOL_VERSION

PUBLIC_KEY = "A" * 43 + "="  # well-formed, for tests that never open a session


def msg(
    msg_type: str, body: dict[str, Any], re: str | None = None, v: int = PROTOCOL_VERSION
) -> dict[str, Any]:
    m: dict[str, Any] = {
        "v": v,
        "type": msg_type,
        "id": str(uuid.uuid4()),
        "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "body": body,
    }
    if re:
        m["re"] = re
    return m


def public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def pair(
    client: TestClient, name: str, os_name: str = "linux", role: str | None = None
) -> tuple[str, Ed25519PrivateKey]:
    """Pair a node; returns its id and private key (what the real node keeps in node.key)."""
    key = Ed25519PrivateKey.generate()
    code = client.post("/api/pairing-codes", headers=AUTH).json()["code"]
    body: dict[str, Any] = {"code": code, "name": name, "os": os_name, "arch": "amd64"}
    if role:
        body["role"] = role
    r = client.post("/api/node/pair", json={**body, "public_key": public_key_b64(key)})
    assert r.status_code == 200, r.text
    return r.json()["node_id"], key


def auth_header(client: TestClient, node_id: str, key: Ed25519PrivateKey) -> dict[str, str]:
    """Fetch a nonce and sign it, like the node does before every WebSocket connect."""
    r = client.post("/api/node/challenge", json={"node_id": node_id})
    assert r.status_code == 200, r.text
    nonce = r.json()["nonce"]
    sig = base64.urlsafe_b64encode(key.sign(signed_message(node_id, nonce))).rstrip(b"=")
    return {"Authorization": f"Topcoat-Ed25519 {node_id}.{nonce}.{sig.decode()}"}


INVENTORY = {
    "hardware": {
        "cpu": {"model": "Fake CPU", "cores": 8},
        "ram_total_mb": 32768,
        "gpus": [{"index": 0, "name": "Fake GPU", "vendor": "amd", "vram_total_mb": 16000}],
        "network": [
            {"name": "eth0", "mac": "02:00:00:00:00:01", "kind": "ethernet"},
            {"name": "wlan0", "mac": "02:00:00:00:00:02", "kind": "wifi"},
        ],
    },
    "runner": {"type": "fake", "available": True},
    "models": [
        {"name": "small", "size_bytes": 1000, "format": "gguf"},
        {"name": "big", "size_bytes": 9000, "format": "gguf"},
    ],
    "loaded": [],
}


def eventually(check: Callable[[], Any], timeout: float = 5.0) -> Any:
    """Retry ``check`` until it stops raising AssertionError (WS handling is asynchronous)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return check()
        except AssertionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)


class FakeNode:
    """Speaks the protocol over TestClient's websocket; can answer actions in a thread."""

    def __init__(
        self,
        client: TestClient,
        node_id: str,
        key: Ed25519PrivateKey,
        version: int = PROTOCOL_VERSION,
    ) -> None:
        self.node_id = node_id
        self.version = version  # protocol version this fake node speaks
        self._cm = client.websocket_connect(
            "/api/node/ws", headers=auth_header(client, node_id, key)
        )
        self.ws = self._cm.__enter__()
        self.loaded: list[dict[str, Any]] = []
        self.received: list[dict[str, Any]] = []
        self.configs: list[dict[str, Any]] = []
        self._thread: threading.Thread | None = None
        self._closed = False
        self._send_lock = threading.Lock()
        # Gateway (E1-05): what serve() saw and how it answers.
        self.proxy_requests: list[dict[str, Any]] = []
        self.cancels: list[str] = []
        self.loads = 0
        self.load_args: list[dict[str, Any]] = []  # the arguments of every models.load
        self.legacy = False  # True: refuses min_context_length like a node from before step 2
        self.auto_context = 0  # what the node picks for min_context_length (at least the minimum)
        self.unloaded: list[str] = []
        self.unload_error: str | None = None  # the node refuses to unload with this message
        self.load_delay = 0.0
        self.chunk_delay = 0.0
        self.state = "available"
        self.not_loaded_once: set[str] = set()  # answer not_loaded once for these models
        self.drop_stream_after: int | None = None  # end streams after N chunks, no proxy_end
        self.drop_error_code = (
            "runner_error"  # the error of that proxy_end ("interrupted": unloaded)
        )
        # E2-05 B: after N chunks the node says "paused" (reasons) for pause_seconds, then "running"
        self.pause_after: int | None = None
        self.pause_seconds = 0.0
        self.pause_reasons = ["input"]
        self.finish_reason = "stop"  # "length": the model ran out of context
        # llama-server answering with an error: (HTTP status, message) before any answer, or an
        # error event in the stream after ``stream_error_after`` chunks.
        self.load_error: tuple[str, str] | None = None  # models.load fails: (code, message)
        self.model_error: tuple[int, str] | None = None
        self.stream_error: str | None = None
        self.stream_error_after = 2
        self.first_chunk_delay = 0.0  # prompt processing before the first byte
        self.answer_padding = ""  # appended to every answer (long answers)
        # (request body) -> answer text: lets a test play the memory extraction / consolidation
        # model; return None to fall back to the default echo answer.
        # text, or a whole message (a dict with tool_calls) for a non-streamed answer
        self.answer_fn: Callable[[dict[str, Any]], str | dict[str, Any] | None] | None = None
        # (request body) -> the text of a *streamed* answer (the chat's own answer); None = echo
        self.stream_text_fn: Callable[[dict[str, Any]], str | None] | None = None
        # E7-01: reasoning the streamed answer sends first, as ``reasoning_content`` deltas
        self.stream_reasoning: str | None = None
        # a real llama-server counts the thinking in ``completion_tokens``
        self.usage_counts_reasoning = False
        # E3-04: the model of the chat is offered a function: (request body) -> the arguments it
        # calls it with (a dict) or None to answer in text; refuse_tools: a server without --jinja
        self.tool_call_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
        self.refuse_tools = False
        self.tool_requests: list[dict[str, Any]] = []
        self.embedding_dim = 0  # > 0: /v1/embeddings answers with vectors of this size
        # E2-04: the inventory this node sends (None = INVENTORY) and how it answers
        # capability.invoke: capability -> output (dict) or an {"error": {...}} to fail with
        self.inventory: dict[str, Any] | None = None
        self.capability_answers: dict[str, Any] = {}
        self.capability_calls: list[dict[str, Any]] = []
        self.execute_calls: list[dict[str, Any]] = []  # capability.execute (E3-01b)
        self.build_calls: list[dict[str, Any]] = []  # build.manage (NS-01)
        self.hub_calls: list[dict[str, Any]] = []  # hub.manage (NS-01, phase 5)
        self.manage_calls: list[dict[str, Any]] = []  # workshop.manage (D76)

    def send(self, message: dict[str, Any]) -> None:
        with self._send_lock:
            self.ws.send_text(json.dumps({**message, "v": self.version}))

    def recv(self) -> dict[str, Any]:
        return json.loads(self.ws.receive_text())

    def handshake(self) -> dict[str, Any]:
        self.send(
            msg(
                "hello",
                {
                    "node_id": self.node_id,
                    "agent_version": "test",
                    "protocol_version": self.version,
                    "config_hash": None,
                },
            )
        )
        config = self.recv()
        assert config["type"] == "config"
        inventory = self.inventory or INVENTORY
        if self.version < 1:  # the frozen v0 schema does not know hardware.network (E2-05 E)
            hardware = {k: v for k, v in INVENTORY["hardware"].items() if k != "network"}
            inventory = {**INVENTORY, "hardware": hardware}
        self.send(msg("inventory", inventory))
        self.heartbeat()
        return config

    gpu_util: float | None = None  # reported as gpu_util in the heartbeat when set
    cpu_util: float | None = None  # reported as cpu_util in the heartbeat when set

    def heartbeat(self, state: str | None = None) -> None:
        state = state or self.state
        used = sum(m["vram_used_mb"] for m in self.loaded)
        self.send(
            msg(
                "heartbeat",
                {
                    "state": state,
                    "ram_used_mb": 1000,
                    **({"cpu_util": self.cpu_util} if self.cpu_util is not None else {}),
                    "gpus": [
                        {
                            "index": 0,
                            "vram_used_mb": 500 + used,
                            **({"gpu_util": self.gpu_util} if self.gpu_util is not None else {}),
                        }
                    ],
                    "loaded": self.loaded,
                },
            )
        )

    def serve_actions(self, count: int = 1, answer: bool = True) -> None:
        """Answer ``count`` actions in a background thread (loads/unloads like the fake runner).

        Config pushes that arrive meanwhile are collected in ``configs``.
        """

        def run() -> None:
            handled = 0
            while handled < count:
                action = self.recv()
                if action["type"] == "config":
                    self.configs.append(action)
                    continue
                handled += 1
                self.received.append(action)
                if not answer:
                    continue
                name, args = action["body"]["name"], action["body"]["args"]
                if name == "models.load":
                    self.loaded.append({"name": args["model"], "vram_used_mb": 4000})
                    body: dict[str, Any] = {"ok": True, "result": {"model": args["model"]}}
                    self.send(msg("action_result", body, re=action["id"]))
                    self.send(msg("event", {"kind": "model_loaded", "data": args}))
                    self.heartbeat()
                elif name == "models.unload" and ("model" in args or args.get("all")):
                    names = [
                        m["name"]
                        for m in self.loaded
                        if args.get("all") or m["name"] == args["model"]
                    ]
                    self.loaded = [m for m in self.loaded if m["name"] not in names]
                    body = {"ok": True, "result": {"unloaded": names}}
                    self.send(msg("action_result", body, re=action["id"]))
                    self.heartbeat()
                elif name == "models.unload":
                    err = {"code": "invalid_args", "message": "model or all is required"}
                    self.send(msg("action_result", {"ok": False, "error": err}, re=action["id"]))
                else:
                    err = {"code": "not_allowed", "message": name}
                    self.send(msg("action_result", {"ok": False, "error": err}, re=action["id"]))

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def serve(self) -> None:
        """Answer actions and gateway requests in the background until the node closes."""

        def run() -> None:
            while True:
                try:
                    m = self.recv()
                except Exception:  # socket closed
                    return
                if m["type"] == "config":
                    self.configs.append(m)
                elif m["type"] == "action":
                    self.received.append(m)
                    threading.Thread(target=self._answer_action, args=(m,), daemon=True).start()
                elif m["type"] == "proxy_request":
                    self.proxy_requests.append(m)
                    threading.Thread(
                        target=self._answer_proxy_quietly, args=(m,), daemon=True
                    ).start()
                elif m["type"] == "proxy_cancel":
                    self.cancels.append(m["body"]["stream_id"])

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def _answer_action(self, action: dict[str, Any]) -> None:
        name, args = action["body"]["name"], action["body"]["args"]
        if name in (
            "capability.invoke",
            "capability.execute",
            "workshop.manage",
            "build.manage",
            "hub.manage",
        ):
            # a confirmed, state-changing run (E3-01b) and the person's own workshop steps (D76)
            # are recorded apart from the reads
            {
                "capability.execute": self.execute_calls,
                "workshop.manage": self.manage_calls,
                "build.manage": self.build_calls,
                "hub.manage": self.hub_calls,
            }.get(name, self.capability_calls).append(args)
            answer = self.capability_answers.get(args["capability"], {})
            if callable(answer):  # a function of the call's input (paging, reading a file)
                answer = answer(args.get("input") or {})
            if "error" in answer:
                self.send(
                    msg("action_result", {"ok": False, "error": answer["error"]}, re=action["id"])
                )
                return
            result = {
                "capability": args["capability"],
                "output": answer,
                "bytes": len(json.dumps(answer)),
                "duration_ms": 5,
                "at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
            self.send(msg("action_result", {"ok": True, "result": result}, re=action["id"]))
            return
        if name == "models.unload" and "model" in args and self.unload_error:
            if "not loaded" in self.unload_error:  # it was gone already (the node's idle unload)
                self.loaded = [m for m in self.loaded if m["name"] != args["model"]]
            err = {"code": "runner_error", "message": self.unload_error}
            self.send(msg("action_result", {"ok": False, "error": err}, re=action["id"]))
            self.heartbeat()
            return
        if name == "models.unload" and "model" in args:
            self.unloaded.append(args["model"])
            self.loaded = [m for m in self.loaded if m["name"] != args["model"]]
            self.send(
                msg(
                    "action_result",
                    {"ok": True, "result": {"unloaded": [args["model"]]}},
                    re=action["id"],
                )
            )
            self.heartbeat()
            return
        if name != "models.load":
            err = {"code": "not_allowed", "message": name}
            self.send(msg("action_result", {"ok": False, "error": err}, re=action["id"]))
            return
        if self.load_delay:
            time.sleep(self.load_delay)
        if self.load_error is not None:
            code, message = self.load_error
            err = {"code": code, "message": message}
            self.send(msg("action_result", {"ok": False, "error": err}, re=action["id"]))
            return
        self.load_args.append(args)
        if self.legacy and "min_context_length" in args:  # a node from before step 2
            err = {"code": "invalid_args", "message": "unknown argument min_context_length"}
            self.send(msg("action_result", {"ok": False, "error": err}, re=action["id"]))
            return
        if not any(m["name"] == args["model"] for m in self.loaded):
            self.loads += 1
            ctx = args.get("context_length")
            if not ctx and args.get("min_context_length"):
                ctx = max(args["min_context_length"], self.auto_context)  # the node sizes it
            self.loaded.append(
                {
                    "name": args["model"],
                    "vram_used_mb": 4000,
                    **({"context_length": ctx} if ctx else {}),
                }
            )
        self.send(msg("action_result", {"ok": True, "result": {}}, re=action["id"]))
        self.heartbeat()

    def _answer_proxy_quietly(self, request: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):  # the node may be closed mid-stream (tests)
            self._answer_proxy(request)

    def _answer_proxy(self, request: dict[str, Any]) -> None:
        body = request["body"]
        sid, model = body["stream_id"], body["model"]

        def end(status: int, error: dict[str, str] | None = None) -> None:
            b: dict[str, Any] = {"stream_id": sid, "status": status}
            if error:
                b["error"] = error
            self.send(msg("proxy_end", b))

        if self.state != "available":
            end(503, {"code": "busy", "message": "busy"})
            return
        if model in self.not_loaded_once or not any(m["name"] == model for m in self.loaded):
            self.not_loaded_once.discard(model)
            end(409, {"code": "not_loaded", "message": f"{model} is not loaded"})
            return
        req = json.loads(base64.b64decode(body["body"]))
        if body.get("path", "").endswith("/v1/embeddings"):
            self._answer_json(sid, self._embeddings(req), end)
            return
        if req.get("tools") and req.get("stream"):
            self.tool_requests.append(req)
            if self.refuse_tools:
                body = json.dumps({"error": {"code": 400, "message": "tools are not supported"}})
                chunk = {
                    "stream_id": sid,
                    "seq": 0,
                    "data": base64.b64encode(body.encode()).decode(),
                    "status": 400,
                    "content_type": "application/json",
                }
                self.send(msg("proxy_chunk", chunk))
                end(400)
                return
            call = self.tool_call_fn(req) if self.tool_call_fn else None
            if call is not None:
                # ``__name`` picks one of the offered functions; else the first is called
                call = dict(call)
                name = call.pop("__name", None) or req["tools"][0]["function"]["name"]
                events = [
                    {"choices": [{"delta": {"content": "Ich schaue nach. "}}]},
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_1",
                                            "function": {
                                                "name": name,
                                                "arguments": json.dumps(call),
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                ]
                for seq, ev in enumerate(events):
                    data = ("data: " + json.dumps(ev) + "\n\n").encode()
                    chunk = {"stream_id": sid, "seq": seq, "data": base64.b64encode(data).decode()}
                    if seq == 0:
                        chunk.update(status=200, content_type="text/event-stream")
                    self.send(msg("proxy_chunk", chunk))
                self.send(
                    msg(
                        "proxy_chunk",
                        {
                            "stream_id": sid,
                            "seq": len(events),
                            "data": base64.b64encode(b"data: [DONE]\n\n").decode(),
                        },
                    )
                )
                end(200)
                return
        custom = self.answer_fn(req) if self.answer_fn else None
        if custom is not None and not req.get("stream"):
            self._answer_json(
                sid,
                {
                    "object": "chat.completion",
                    "model": model,
                    "choices": [
                        {
                            "message": custom
                            if isinstance(custom, dict)
                            else {"role": "assistant", "content": custom}
                        }
                    ],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 20},
                },
                end,
            )
            return
        if self.model_error is not None:
            status, message = self.model_error
            body = json.dumps({"error": {"code": status, "message": message}})
            chunk = {
                "stream_id": sid,
                "seq": 0,
                "data": base64.b64encode(body.encode()).decode(),
                "status": status,
                "content_type": "application/json",
            }
            self.send(msg("proxy_chunk", chunk))
            end(status)
            return
        last = req["messages"][-1]["content"]
        if isinstance(last, list):  # content parts (an image, D125): the text of the turn
            last = "".join(p.get("text", "") for p in last if p.get("type") == "text")
        text = f"Antwort von {self.node_id[:8]}: " + last
        text += self.answer_padding
        if self.stream_text_fn is not None and req.get("stream"):
            got = self.stream_text_fn(req)
            text = text if got is None else got  # "" is a stream without a word
        if not req.get("stream"):
            data = json.dumps(
                {
                    "object": "chat.completion",
                    "model": model,
                    "choices": [{"message": {"role": "assistant", "content": text}}],
                }
            )
            chunk = {
                "stream_id": sid,
                "seq": 0,
                "data": base64.b64encode(data.encode()).decode(),
                "status": 200,
                "content_type": "application/json",
            }
            self.send(msg("proxy_chunk", chunk))
            end(200)
            return
        words = text.split()
        completion_tokens = len(words) + (
            len((self.stream_reasoning or "").split()) if self.usage_counts_reasoning else 0
        )
        prompt_tokens = sum(len(str(m["content"]).split()) for m in req["messages"])
        finish = json.dumps({"choices": [{"delta": {}, "finish_reason": self.finish_reason}]})
        usage = json.dumps(
            {
                "choices": [],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            }
        )
        lines = [
            "data: "
            + json.dumps({"choices": [{"delta": {"content": w if i == 0 else " " + w}}]})
            + "\n\n"
            for i, w in enumerate(words)
        ]
        if self.stream_reasoning:
            thought = self.stream_reasoning.split()
            lines = [
                "data: "
                + json.dumps(
                    {"choices": [{"delta": {"reasoning_content": w if i == 0 else " " + w}}]}
                )
                + "\n\n"
                for i, w in enumerate(thought)
            ] + lines
        if self.stream_error is not None:
            err = "data: " + json.dumps({"error": {"message": self.stream_error}}) + "\n\n"
            lines = [*lines[: self.stream_error_after], err]
        else:
            lines += [f"data: {finish}\n\n", f"data: {usage}\n\n", "data: [DONE]\n\n"]
        time.sleep(self.first_chunk_delay)
        for seq, line in enumerate(lines):
            if sid in self.cancels:
                return
            chunk = {"stream_id": sid, "seq": seq, "data": base64.b64encode(line.encode()).decode()}
            if seq == 0:
                chunk.update(status=200, content_type="text/event-stream")
            self.send(msg("proxy_chunk", chunk))
            if self.drop_stream_after is not None and seq + 1 >= self.drop_stream_after:
                end(200, {"code": self.drop_error_code, "message": "model process died"})
                return
            if self.pause_after is not None and seq + 1 == self.pause_after:
                status = {"stream_id": sid, "state": "paused", "reasons": self.pause_reasons}
                self.send(msg("proxy_status", status))
                time.sleep(self.pause_seconds)
                self.send(msg("proxy_status", {"stream_id": sid, "state": "running"}))
            if self.chunk_delay:
                time.sleep(self.chunk_delay)
        end(200)

    def _embeddings(self, req: dict[str, Any]) -> dict[str, Any]:
        texts = req["input"] if isinstance(req["input"], list) else [req["input"]]
        dim = self.embedding_dim or 8
        data = []
        for i, t in enumerate(texts):
            vec = [0.0] * dim
            for ch in str(t):
                vec[ord(ch) % dim] += 1.0
            data.append({"object": "embedding", "index": i, "embedding": vec})
        return {"object": "list", "data": data}

    def _answer_json(self, sid: str, payload: dict[str, Any], end: Callable[..., None]) -> None:
        chunk = {
            "stream_id": sid,
            "seq": 0,
            "data": base64.b64encode(json.dumps(payload).encode()).decode(),
            "status": 200,
            "content_type": "application/json",
        }
        self.send(msg("proxy_chunk", chunk))
        end(200)

    def join(self, timeout: float = 5) -> None:
        if self._thread:
            self._thread.join(timeout)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            with contextlib.suppress(Exception):
                self._cm.__exit__(None, None, None)


def online_node(
    client: TestClient,
    connect: Any,
    name: str,
    loaded: list[str] | None = None,
    context_length: int | None = None,
) -> FakeNode:
    node_id, key = pair(client, name)
    node = connect(node_id, key)
    node.handshake()
    node.loaded = [
        {
            "name": m,
            "vram_used_mb": 4000,
            **({"context_length": context_length} if context_length else {}),
        }
        for m in loaded or []
    ]
    node.serve()
    node.heartbeat()

    def ready() -> None:
        r = client.get(f"/api/nodes/{node_id}", headers=AUTH).json()
        assert r["state"] == node.state
        assert [m["name"] for m in r["loaded"]] == (loaded or [])

    eventually(ready)
    return node


def _schema(required: list[str], **props: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": props}
    if required:
        out["required"] = required
    return out


def tools_inventory(
    *,
    with_inventory: bool = False,
    stacks: bool = False,
    with_shell: bool = False,
    extra: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The inventory of a tool node (E2-04): read-only capabilities and one that is not."""
    caps = [
        ("system.info", True, _schema([])),
        ("docker.ps", True, _schema([], all={"type": "boolean"}, name={"type": "string"})),
        (
            "docker.logs",
            True,
            _schema(
                ["container"],
                container={"type": "string", "maxLength": 128},
                lines={"type": "integer", "minimum": 1, "maximum": 500},
            ),
        ),
        ("docker.restart", False, _schema(["container"], container={"type": "string"})),
    ]
    if stacks:  # compose projects as one thing (logs, restart)
        project = {"type": "string", "maxLength": 128}
        caps += [
            ("compose.projects", True, _schema([])),
            (
                "compose.logs",
                True,
                _schema(["project"], project=project, lines={"type": "integer"}),
            ),
            ("compose.restart", False, _schema(["project"], project=project)),
        ]
    caps += [(name, True, _schema([])) for name in extra]  # read-only, no input
    if with_inventory:
        caps.append(("inventory.snapshot", True, _schema([])))
    if with_shell:  # the read-only shell: offered by a node only where tools.shell_socket is set
        caps.append(("shell.read", True, _schema(["command"], command={"type": "string"})))
    return {
        "hardware": {"cpu": {"model": "Fake VM CPU", "cores": 4}, "ram_total_mb": 8192, "gpus": []},
        "runner": {"type": "none", "available": True},
        "models": [],
        "loaded": [],
        "capabilities": [
            {"name": n, "description": f"{n} (test)", "read_only": ro, "input_schema": schema}
            for n, ro, schema in caps
        ],
    }
