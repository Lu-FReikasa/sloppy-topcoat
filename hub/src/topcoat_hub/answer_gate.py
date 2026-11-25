"""The gate before an answer is shown (Gründlich, the plan 2a).

The answer is made, checked against its evidence, and made again with the findings until the check
passes or the revisions are used up. The model call and the check are passed in, so the loop is
tested without a model or a database. Not wired into ``api/chat.py`` yet: that needs the answer
held back while it streams (brief R1-01, round R2).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from topcoat_hub.quality import Verification

# A hard limit whatever the stage says: an answer is never revised more than this many times.
MAX_REVISIONS = 3

# Makes one answer. Gets the findings of the previous answer, or None for the first one.
Produce = Callable[[list[str] | None], Awaitable[str]]
# Checks one answer. None when the checker cannot answer: the answer then stands unchecked.
Check = Callable[[str], Awaitable[Verification | None]]


@dataclass
class Gated:
    text: str
    answers: int
    checked: bool
    ok: bool
    issues: list[str] = field(default_factory=list)


async def gate(produce: Produce, check: Check, revisions: int) -> Gated:
    """Make and check answers until one passes or ``revisions`` revisions were made.

    ``revisions`` is the number of *re*-answers: 0 makes one answer and checks it once.
    The result says what is shown: the last answer, whether it was checked, and the findings
    that remain open when the revisions are used up.
    """
    if revisions < 0:
        raise ValueError("revisions cannot be negative")
    limit = min(revisions, MAX_REVISIONS)
    feedback: list[str] | None = None
    answers = 0
    while True:
        text = await produce(feedback)
        answers += 1
        verdict = await check(text)
        if verdict is None:
            return Gated(text, answers, checked=False, ok=False)
        if verdict.ok:
            return Gated(text, answers, checked=True, ok=True)
        if answers > limit:
            return Gated(text, answers, checked=True, ok=False, issues=list(verdict.issues))
        feedback = list(verdict.issues)


def revision_note(issues: list[str]) -> str:
    """The instruction that goes to the model with its findings for the next answer."""
    found = "\n".join(f"- {issue}" for issue in issues)
    return (
        "A check of your previous answer found statements that the evidence does not show or that "
        f"contradict it:\n{found}\n"
        "Answer the question again. Keep what is right, correct or remove these statements, and do "
        "not add facts you have not looked at."
    )
