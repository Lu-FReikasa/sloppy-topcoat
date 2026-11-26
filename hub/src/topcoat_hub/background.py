"""The work the hub does on its own (ARCHITECTURE D75): when each service runs and what it does.

Every background service of the hub — the two looks at a finished chat and the Nachlese, the
tidy-up of the memory, the care run, continuing stuck tasks, re-reading the knowledge base — has a
task here: a trigger (when it runs) and one step per part of the work (whether it is done, which
model, how much in one go). The values come from the hub's settings (``TOPCOAT_*``); a service
whose interval is 0 is not started at all. (Until the flows were taken out an admin could copy a
background flow and change its values; none was ever in use.)"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from topcoat_hub.config import Settings


@dataclass(frozen=True)
class Step:
    on: bool = True
    model: str = ""  # empty = the hub's rule for this step
    limit: int = 0  # 0 = the hub's own number


@dataclass(frozen=True)
class Background:
    """What the settings say for one task."""

    task: str
    on: bool = True
    idle_minutes: float = 0.0
    every_hours: float = 0.0
    hour: int = -1
    event: str = ""
    steps: dict[str, Step] = field(default_factory=dict)

    def step(self, key: str) -> Step:
        return self.steps.get(key, Step())

    def limit(self, key: str, default: int) -> int:
        n = self.step(key).limit
        return n if n > 0 else default


@dataclass(frozen=True)
class StepSpec:
    key: str
    label: str
    text: str
    defaults: Callable[[Settings], dict[str, Any]] = lambda s: {}


@dataclass(frozen=True)
class TaskSpec:
    key: str
    title: str
    text: str
    trigger: Callable[[Settings], dict[str, Any]]
    steps: tuple[StepSpec, ...]


TASKS: dict[str, TaskSpec] = {
    t.key: t
    for t in (
        TaskSpec(
            "chat_end",
            "A chat ends",
            "After every answer the model that gave it brings the running summary of the chat up "
            "to date (every answer is treated as the last one; a new section starts from this "
            "summary and never waits for it). When the chat has rested, another model checks the "
            "summary and notes what the chat taught (as unverified notes), the checked summary "
            "becomes the hand-over note, and a model other than the author re-reads the answers "
            "(Nachlese).",
            lambda s: {
                "on": s.chat_digest_enabled,
                "idle_minutes": s.chat_digest_idle_seconds / 60,
                "event": "a chat has rested",
            },
            (
                StepSpec(
                    "summary",
                    "Summary by the author",
                    "After every answer the model of that answer adds what is new to the running "
                    "summary (empty model = that model; a chat without an answer: the hub's "
                    "default).",
                ),
                StepSpec(
                    "check",
                    "Second look at the summary",
                    "Another model checks the summary (empty = any model other than the author).",
                ),
                StepSpec(
                    "learn",
                    "Keep what the chat taught",
                    "New notes from the check, stored as unverified (limit = notes per chat).",
                    lambda s: {"limit": s.chat_digest_max_learned},
                ),
                StepSpec(
                    "review",
                    "Nachlese",
                    "A model other than the author re-reads the answers for checkable mistakes "
                    "(limit = answers per round).",
                    lambda s: {
                        "on": s.review_enabled,
                        "limit": s.review_batch,
                    },
                ),
            ),
        ),
        TaskSpec(
            "consolidation",
            "Tidying the memory",
            "While nobody chats: duplicates are merged, contradictions marked, outdated or badly "
            "worded notes corrected — inside one memory at a time.",
            lambda s: {
                "on": True,
                "every_hours": s.memory_consolidation_interval_seconds / 3600,
                "event": "the hub is idle",
            },
            (
                StepSpec(
                    "merge",
                    "Merge, mark, correct",
                    "A model looks at groups of similar notes (empty model = the memory's).",
                ),
            ),
        ),
        TaskSpec(
            "care",
            "The care run",
            "At night and every few idle hours: expired notes are struck, useless ones archived, "
            "the core reviewed (proposals only) and the memories cross-checked.",
            lambda s: {
                "on": True,
                "hour": s.memory_care_hour,
                "every_hours": s.memory_care_idle_every_hours,
                "event": "the night hour, or the hub is idle",
            },
            (
                StepSpec("expire", "Strike expired notes", "Passing states past their time."),
                StepSpec("archive", "Archive what nobody needs", "Useless, unreliable and idle."),
                StepSpec(
                    "core_review",
                    "Review the core",
                    "Contradictions and room-takers in the core become proposals.",
                ),
                StepSpec(
                    "crosscheck",
                    "Cross-check the memories",
                    "A discrepancy goes to the program responsible for its topic.",
                ),
            ),
        ),
        TaskSpec(
            "resume",
            "Continue stuck tasks",
            "When a node comes online, tasks that stopped for lack of one go on.",
            lambda s: {"on": s.resume_stuck_enabled, "event": "a node comes online"},
            (
                StepSpec(
                    "resume",
                    "Continue the tasks",
                    "As the person's own 'continue' would (limit = tasks per person and arrival).",
                    lambda s: {"limit": s.resume_stuck_max},
                ),
            ),
        ),
        TaskSpec(
            "knowledge",
            "Re-read the knowledge base",
            "The folders of the knowledge base are indexed again.",
            lambda s: {
                "on": True,
                "every_hours": s.knowledge_reindex_seconds / 3600,
                "event": "the interval has passed",
            },
            (
                StepSpec("reindex", "Index the folders", "Read the changed files again."),
                StepSpec(
                    "project_reports",
                    "Index the project reports",
                    "The documents, decisions and facts of the projects (their current versions "
                    "and the project descriptions) are written into the person's own source "
                    "'Projekte'; what was replaced or deleted is taken out again.",
                ),
            ),
        ),
        TaskSpec(
            "project_reminders",
            "Project reminders",
            "Every minute the hub looks for reminders of tasks that are due and puts them in the "
            "inbox (after downtime it catches up, but not with what is too old), and once a day "
            "it lists the overdue tasks of a person who asked for that, outside their quiet hours.",
            lambda s: {
                "on": True,
                "every_hours": s.project_reminder_tick_seconds / 3600,
                "event": "the minute is up",
            },
            (
                StepSpec(
                    "fire",
                    "Deliver due reminders",
                    "Reminders that are due go to the inbox (limit = hours a reminder may be "
                    "late and still be delivered).",
                    lambda s: {"limit": s.project_reminder_catchup_hours},
                ),
                StepSpec(
                    "digest",
                    "Daily list of overdue tasks",
                    "One inbox item a day for a person who chose an hour for it.",
                    lambda s: {"on": s.project_digest_enabled},
                ),
            ),
        ),
    )
}


def defaults(task: str, settings: Settings) -> Background:
    """What the hub's settings say for the task."""
    spec = TASKS[task]
    trig = {"on": True, "idle_minutes": 0.0, "every_hours": 0.0, "hour": -1, "event": ""}
    trig.update(spec.trigger(settings))
    steps = {}
    for st in spec.steps:
        values = {"on": True, "model": "", "limit": 0, **st.defaults(settings)}
        steps[st.key] = Step(bool(values["on"]), str(values["model"] or ""), int(values["limit"]))
    return Background(
        task=task,
        on=bool(trig["on"]),
        idle_minutes=float(trig["idle_minutes"]),
        every_hours=float(trig["every_hours"]),
        hour=int(trig["hour"]),
        event=str(trig["event"]),
        steps=steps,
    )


def current(service: Any, task: str, settings: Settings | None = None) -> Background:
    """What a service is to do now: what the settings say for its task."""
    return defaults(task, settings or service.settings)
