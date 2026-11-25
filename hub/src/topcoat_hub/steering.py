"""Steering a running check: a note from the person, pause and resume (E7-01, D56).

A check used to run to its end, and the person could only watch or stop it. With the chain of
activity in view, one sees a call fail or the model drift and can say so *now*: a short note that
the check reads before its next turn, or a pause. The board lives in memory next to the running
tasks (a check is a task of this hub process); nothing here changes what the check may do: a note is
text from the person who asked, in the same place a tool result would be, and the check's rights,
tools and limits stay exactly what they were (D30). Pausing does not use up the time budget.
"""

import asyncio
import re
import time
import uuid

MAX_NOTE_CHARS = 500
MAX_NOTES_PER_TASK = 8
MAX_PAUSE_SECONDS = 900.0  # a pause nobody ends is ended after this: the check goes on

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class SteeringError(Exception):
    """The note cannot be taken; ``code`` says why (``empty``, ``too_many``)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def clean(text: str) -> str:
    """A note as it reaches the model: no control characters, one blank at most between words,
    cut at :data:`MAX_NOTE_CHARS`."""
    text = _CONTROL.sub("", text).strip()
    return " ".join(text.split())[:MAX_NOTE_CHARS]


class SteeringBoard:
    def __init__(self) -> None:
        self._notes: dict[uuid.UUID, list[str]] = {}
        self._paused: dict[uuid.UUID, asyncio.Event] = {}  # set = running, cleared = paused

    def push(self, task_id: uuid.UUID, text: str) -> str:
        """Queue a note for the check; returns it as it will be read."""
        note = clean(text)
        if not note:
            raise SteeringError("empty", "the note is empty")
        queue = self._notes.setdefault(task_id, [])
        if len(queue) >= MAX_NOTES_PER_TASK:
            raise SteeringError("too_many", "the check has not read the earlier notes yet")
        queue.append(note)
        return note

    def take(self, task_id: uuid.UUID) -> list[str]:
        """The notes waiting for the check, oldest first (they are not kept)."""
        return self._notes.pop(task_id, [])

    def pause(self, task_id: uuid.UUID) -> None:
        self._paused.setdefault(task_id, asyncio.Event()).clear()

    def resume(self, task_id: uuid.UUID) -> None:
        event = self._paused.get(task_id)
        if event is not None:
            event.set()

    def is_paused(self, task_id: uuid.UUID) -> bool:
        event = self._paused.get(task_id)
        return event is not None and not event.is_set()

    async def wait_while_paused(
        self, task_id: uuid.UUID, limit: float = MAX_PAUSE_SECONDS
    ) -> float:
        """Block while the task is paused (at most ``limit`` seconds); returns how long it waited,
        so the caller can give that time back to the check's budget."""
        event = self._paused.get(task_id)
        if event is None or event.is_set():
            return 0.0
        started = time.monotonic()
        try:
            await asyncio.wait_for(event.wait(), limit)
        except TimeoutError:
            event.set()  # nobody came back: the check goes on by itself
        return time.monotonic() - started

    def forget(self, task_id: uuid.UUID) -> None:
        """The check is over."""
        self._notes.pop(task_id, None)
        event = self._paused.pop(task_id, None)
        if event is not None:
            event.set()  # nothing may stay blocked on it
