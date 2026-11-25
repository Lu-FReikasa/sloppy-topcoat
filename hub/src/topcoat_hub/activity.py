"""The activity of one answer: what the hub did, in order, shown live and kept (E7-01, D56).

An answer used to show a transient progress line while a check ran and a report card at the end;
what the model thought, which call failed and why, and what the hub decided in between was gone by
the time anybody looked. This module is the one record of it. An :class:`ActivityLog` holds the
**items** of one answer (a thought, a plan line, a tool call, a check, a retry, a note from the
person) in the order they happened. Every change is also a chunk of the answer's SSE stream
(``event: activity``) so the browser draws the chain while it grows and a tab that attaches later
replays it; the finished log is stored with the answer (``messages.activity``).

An item is plain data, no sentence: the browser words it (the language is the user's). ``kind``
says what it is, ``title`` is the technical name (``docker.ps @ node-a``), ``detail`` is text the
model or a tool produced, ``data`` holds the structured rest. The log is bounded: a run that does
more than :data:`MAX_ITEMS` things keeps the first ones and counts the rest in ``dropped``.
"""

import json
import time
from datetime import UTC, datetime
from typing import Any

KINDS = frozenset(
    {
        "thinking",  # what the model reasoned (reasoning_content or a <think> block)
        "lead_in",  # text the model said before it called a function (it used to be thrown away)
        "plan",  # why the next call is made
        "tool",  # one call of a capability, with its result
        "check",  # a server check as a whole, or the second look at an answer
        "retry",  # the hub tried again (a cut-off or invalid reply, a fallback)
        "steer",  # a note the person sent into a running check
        "note",  # anything else the hub wants to say
        "step_check",  # the second node's note on a tool step (Gründlich, PLAN 7)
        "announce",  # the model announced an action and then did not do it (D57)
        "flow",  # a node of a workflow run (E7-05)
    }
)
STATUSES = frozenset({"running", "ok", "failed", "info", "stopped"})

MAX_ITEMS = 400
MAX_TITLE = 200
MAX_DETAIL = 6000
MAX_DATA_JSON = 2000


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _clip(text: str, limit: int, keep_tail: bool = False) -> str:
    if len(text) <= limit:
        return text
    return "…" + text[-(limit - 1) :] if keep_tail else text[: limit - 1] + "…"


def _safe_data(data: dict[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {}
    try:
        text = json.dumps(data, default=str)
    except (TypeError, ValueError):
        return {"unreadable": True}
    if len(text) > MAX_DATA_JSON:
        return {"truncated": True}
    return json.loads(text)  # type: ignore[no-any-return]


class ActivityLog:
    """The ordered items of one answer. Methods that change it return the SSE chunk to send (an
    empty string when nothing changed), so a caller can ``yield`` it."""

    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []
        self._index: dict[int, dict[str, Any]] = {}
        self._clock: dict[int, float] = {}
        self._next = 1
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._items)

    def add(
        self,
        kind: str,
        title: str = "",
        *,
        status: str = "running",
        detail: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> tuple[int, str]:
        """Append an item. Returns ``(id, chunk)``; the id is 0 (and the chunk empty) when the log
        is full."""
        if len(self._items) >= MAX_ITEMS:
            self.dropped += 1
            return 0, ""
        item = {
            "id": self._next,
            "kind": kind if kind in KINDS else "note",
            "status": status if status in STATUSES else "info",
            "title": _clip(title, MAX_TITLE),
            "detail": _clip(detail, MAX_DETAIL, keep_tail=True) if detail else None,
            "data": _safe_data(data),
            "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "ms": None,
        }
        self._next += 1
        self._items.append(item)
        self._index[item["id"]] = item
        self._clock[item["id"]] = time.monotonic()
        if item["status"] != "running":
            item["ms"] = 0
        return item["id"], _sse("activity", {"op": "add", "item": item})

    def update(
        self,
        item_id: int,
        *,
        status: str | None = None,
        title: str | None = None,
        detail: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> str:
        """Change an item (a thought that grows, a call that has its result). A finished status
        (anything but ``running``) also fixes how long it took."""
        item = self._index.get(item_id)
        if item is None:
            return ""
        if status is not None and status in STATUSES:
            item["status"] = status
            if status != "running" and item["ms"] is None:
                item["ms"] = int((time.monotonic() - self._clock[item_id]) * 1000)
        if title is not None:
            item["title"] = _clip(title, MAX_TITLE)
        if detail is not None:
            item["detail"] = _clip(detail, MAX_DETAIL, keep_tail=True)
        if data:
            item["data"] = {**item["data"], **_safe_data(data)}
        return _sse("activity", {"op": "update", "item": item})

    def finish(self, item_id: int, status: str = "ok", detail: str | None = None) -> str:
        return self.update(item_id, status=status, detail=detail)

    def settle(self) -> None:
        """The answer is over: whatever still runs did not finish (stop, error, restart)."""
        for item in self._items:
            if item["status"] == "running":
                item["status"] = "stopped"
                item["ms"] = int((time.monotonic() - self._clock[item["id"]]) * 1000)

    def snapshot(self) -> list[dict[str, Any]]:
        """A copy that can be stored (JSON-safe)."""
        return json.loads(json.dumps(self._items))

    def failed(self) -> int:
        return sum(1 for i in self._items if i["status"] == "failed")


class CheckTrace:
    """Turns the events of a server check (``tools_start``, ``tool_step``, ``tools_done``) into
    items. The model's plan for a call opens a ``tool`` item that *runs* (the person sees which
    call is made now and why); the call's result closes it, as ``ok`` or ``failed`` with the
    reason, so a failing call is visible at once, not in the report at the end."""

    def __init__(self, log: ActivityLog, extra: dict[str, Any] | None = None) -> None:
        self.log = log
        self._extra = extra or {}  # added to every item (a flow marks the node they belong to)
        self._check = 0
        self._open: list[tuple[int, str, str]] = []  # (item id, capability, node) awaiting a result

    def _chunks(self, *chunks: str) -> list[str]:
        return [c for c in chunks if c]

    def _add(
        self, kind: str, title: str = "", *, data: dict[str, Any] | None = None, **kw: Any
    ) -> tuple[int, str]:
        return self.log.add(kind, title, data={**(data or {}), **self._extra}, **kw)

    def event(self, name: str, data: dict[str, Any]) -> list[str]:
        if name == "tools_start":
            self._check, chunk = self._add("check", status="running", data=_task(data))
            return self._chunks(chunk)
        if name == "tools_done":
            return self._done(data)
        if name != "tool_step":
            return []
        kind = data.get("kind")
        if kind == "plan":
            capability, node = str(data.get("capability") or ""), str(data.get("node") or "")
            text = str(data.get("text") or "")
            if not capability:
                _, chunk = self._add("plan", status="info", detail=text)
                return self._chunks(chunk)
            item_id, chunk = self._add(
                "tool", _name(capability, node), detail=text or None, data={"seq": data.get("seq")}
            )
            self._open.append((item_id, capability, node))
            return self._chunks(chunk)
        if kind == "call":
            capability, node = str(data.get("capability") or ""), str(data.get("node") or "")
            ok = bool(data.get("ok"))
            code = data.get("code")
            facts = {"input": data.get("input"), "ref": data.get("ref"), "code": code}
            match = next((o for o in self._open if o[1] == capability and o[2] == node), None)
            if match is not None:
                self._open.remove(match)
                return self._chunks(
                    self.log.update(
                        match[0],
                        status="ok" if ok else "failed",
                        detail=None if ok else str(code or "error"),
                        data=facts,
                    )
                )
            _, chunk = self._add(
                "tool",
                _name(capability, node),
                status="ok" if ok else "failed",
                detail=None if ok else str(code or "error"),
                data=facts,
            )
            return self._chunks(chunk)
        if kind == "note":
            _, chunk = self._add("note", status="info", detail=str(data.get("text") or ""))
            return self._chunks(chunk)
        if kind == "step_check":
            _, chunk = self._add(
                "step_check",
                str(data.get("tool") or ""),
                status="info",
                detail=str(data.get("note") or ""),
            )
            return self._chunks(chunk)
        return []

    def _done(self, data: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for item_id, _, _ in self._open:  # a call that never got its result
            out.append(self.log.update(item_id, status="stopped"))
        self._open = []
        failed = data.get("status") != "done"
        reason = data.get("error") or data.get("stop_reason") or data.get("status")
        facts = {"calls": data.get("calls"), "evidence": len(data.get("evidence") or [])}
        if self._check:
            out.append(
                self.log.update(
                    self._check,
                    status="failed" if failed else "ok",
                    detail=str(reason) if failed and reason else None,
                    data=facts,
                )
            )
        else:
            _, chunk = self._add(
                "check",
                status="failed" if failed else "ok",
                detail=str(reason) if failed and reason else None,
                data=facts,
            )
            out.append(chunk)
        return [c for c in out if c]


def _task(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"task_id": data["task_id"]} if data.get("task_id") else {}
    if isinstance(data.get("order"), dict):  # the fixed order of the check (pruefauftrag T4.1)
        out["order"] = data["order"]
    return out


def _name(capability: str, node: str) -> str:
    return f"{capability} @ {node}" if node else capability


class ThinkingTrace:
    """Turns the streamed reasoning of a model into one growing ``thinking`` item. The text is
    published at most every ``interval`` seconds (a model thinks in tokens, the browser does not
    need each one); :meth:`end` closes it when the model starts to answer or to call."""

    def __init__(self, log: ActivityLog, interval: float = 0.4) -> None:
        self.log = log
        self.interval = interval
        self._id = 0
        self._text = ""
        self._last = 0.0

    @property
    def open(self) -> bool:
        return self._id != 0

    def feed(self, text: str) -> list[str]:
        if not text:
            return []
        self._text += text
        if len(self._text) > 4 * MAX_DETAIL:  # a very long thought keeps only its end
            self._text = self._text[-2 * MAX_DETAIL :]
        now = time.monotonic()
        if self._id == 0:
            self._id, chunk = self.log.add("thinking", detail=self._text)
            self._last = now
            return [chunk] if chunk else []
        if now - self._last >= self.interval:
            self._last = now
            chunk = self.log.update(self._id, detail=self._text)
            return [chunk] if chunk else []
        return []

    def end(self) -> list[str]:
        if self._id == 0:
            return []
        chunk = self.log.finish(self._id, "ok", detail=self._text)
        self._id, self._text = 0, ""
        return [chunk] if chunk else []
