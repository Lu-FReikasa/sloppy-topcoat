"""The Research program's way of working: three frames instead of fixed limits.

Research was run with the settings of a server check: three web searches, two pages, twenty tool
calls, ten minutes, 3500 characters of a page, 1536 tokens for an answer, and a prompt that calls
the agent "the diagnosis agent of a home-lab operator" and asks for "at most 4 findings". That is
why its answers were short. Development and Projects, which have no web, depend on Research for
frameworks and error profiles, so Research is built for depth:

* **Three frames**, chosen by the one who asks (the request's ``depth``):
  ``quick`` (skim: bullet points with links), ``default`` (a precise answer from several hosts)
  and ``thorough`` (a documentation in the manner of a scientific paper, checked by a second
  model). A frame decides the *shape* of the result and when it is finished, not a count.
* **No fixed limits.** A run may take hours when it makes progress. It ends when its frame is
  satisfied or when it stands still: ``research_stall_turns`` model turns in a row without a new
  source or a new piece of evidence. Counts and times exist only as live settings (0 = none), so
  a limit can be added later without a change of code.
* The frame is read from the request prompt (``depth: <frame>``), so it survives a restart of the
  hub: a task that is started again reads the same line.

Pure functions; the loop (``agent_loop.py``) and the prompts (``agent_prompts.py``) call them.
"""

import re
from dataclasses import dataclass, replace
from typing import Any

APPLICATION = "research"
DEPTHS = ("quick", "default", "thorough")
DEFAULT_DEPTH = "default"

# what "no limit" is inside the loop: large, finite numbers keep every comparison and every timeout
# calculation ordinary (a float infinity does not survive asyncio's timers)
UNLIMITED_COUNT = 1_000_000
UNLIMITED_SECONDS = 30 * 24 * 3600.0

# the effort stage of a Research chat (Schnell, Normal, Gründlich) is the frame of its run
CHAT_DEPTH = {"fast": "quick", "balanced": "default", "thorough": "thorough"}

_ALIASES = {
    "quick": "quick",
    "skim": "quick",
    "brief": "quick",
    "default": "default",
    "standard": "default",
    "normal": "default",
    "thorough": "thorough",
    "deep": "thorough",
    "paper": "thorough",
}
_DEPTH_LINE = re.compile(r"^depth: *([A-Za-z]+) *$", re.MULTILINE)


def depth_of(value: Any) -> str:
    """A frame from whatever the request said: the schema allows ``quick``, ``standard``,
    ``thorough`` and free text. Anything else is ``default``."""
    if not isinstance(value, str):
        return DEFAULT_DEPTH
    words = re.findall(r"[a-z]+", value.lower())
    return _ALIASES.get(words[0], DEFAULT_DEPTH) if words else DEFAULT_DEPTH


def depth_line(value: Any) -> str:
    """The line of the request prompt that carries the frame."""
    return f"depth: {depth_of(value)}"


def depth_in_prompt(prompt: str) -> str:
    """The frame a request prompt names (in its header, the first lines), else ``default``."""
    head = "\n".join((prompt or "").splitlines()[:8])
    found = _DEPTH_LINE.search(head)
    return depth_of(found.group(1)) if found else DEFAULT_DEPTH


@dataclass(frozen=True)
class Persona:
    """The pieces of the agent's system prompt that differ for Research."""

    intro: str
    web: str
    style: str


# What the asking agent reads in the tool description of a request to Research. Quality has
# priority, but a thorough run can take hours and occupies a machine: it is for a decision or a
# documentation, not for a fact that one page settles.
DEPTH_GUIDE = (
    "Choose depth from what the answer is for: quick = a fact, an error message or a link, "
    "skimmed in minutes; default = a precise answer you will build on (how a framework does "
    "something, causes and fixes of an error, versions and pitfalls); thorough = a "
    "documentation like a scientific paper for a decision or a new technology, which may take "
    "hours. Do not ask for thorough when one page would settle it."
)

_INTRO = (
    "You are the research agent of a private workshop. Other programs of the person (development, "
    "planning, administration) have no internet and rely on what you find out: documentation of "
    "frameworks and tools, causes and fixes of errors, comparisons. You read the web and the "
    "person's notes through read-only tools and write a report. You cannot change anything and "
    "you never say that you did."
)

_WEB = {
    "quick": (
        "Skim: a few searches and the most promising pages are enough. Stop when the question "
        "is answered; say what you did not read."
    ),
    "default": (
        "Search as often as the question needs and read the pages that matter, not only the first "
        "hit. Prefer primary sources (official documentation, the project's repository, release "
        "notes) and name the version a statement holds for. Use at least two different hosts for "
        "anything you state as a fact. Stop when every point of the request is covered or you "
        "can show that it cannot be found."
    ),
    "thorough": (
        "Work like a researcher writing a paper. Search broadly first, then read the primary "
        "sources in full, follow references, and look for sources that disagree. Keep working "
        "while you find new sources or evidence; there is no time limit, but a turn that brings "
        "nothing new is a turn lost. Record what you searched for, and what you could not find."
    ),
}

_STYLE = {
    "quick": (
        "Give an overview: a few short points, each with its source. A summary of at most a "
        "paragraph. No long report."
    ),
    "default": (
        "Write a structured answer: the direct answer first, then the details that matter "
        "(versions, steps, pitfalls), each claim with its evidence mark. The summary may be as "
        "long as the answer needs; there is no limit of 4 findings. Name what is uncertain. "
        "For a long answer write sections with research.report.section and keep the summary as "
        "its abstract."
    ),
    "thorough": (
        "Write a report in the manner of a scientific paper, in Markdown, as if the question were "
        "an assignment at a university. Write it section by section with research.report.section "
        '(research.report.show lists what is done). The FIRST section is titled "Vorwort" (in an '
        'English report "Foreword"): what the report is about, what you paid special attention '
        "to, what you left out on purpose and what you could only explore or prove "
        "insufficiently. Open the Vorwort with a box for a quick reader: a quote block whose first "
        'line is "> **Auf einen Blick**" (English: "> **At a glance**"), then 3 to 5 lines "> - …" '
        "with the main results and the recommendation. Then the chapters, each titled by its "
        "content: the question and scope; the findings, every statement with its evidence mark "
        "[E1]; where sources disagree and why; conclusions and a recommendation. Do not write a "
        "chapter on how you searched, a table of contents, a list of sources or the box of "
        "limits: the hub builds them from what you read. Make the pages easy to read: compare "
        "options, tools or versions in a Markdown table (a column per property, a mark per "
        "row); show a process, a pipeline or an architecture as one small diagram in a "
        "```mermaid block (flowchart LR or TD, at most 12 nodes, labels in double quotes, no "
        "HTML), at most one per section, only when it explains more than a sentence would. "
        "Where a picture shows what words cannot (an example result, an interface), place it "
        "with research.report.image (a picture of a page you read, or one web.images finds) "
        "and put the line it answers into the section. "
        "As long as the topic needs, "
        "but one section per call of about 400 words at most: a longer call is cut off and lost, "
        "so write more sections instead. Read at least three different sources before you write "
        "the chapters, and cite them; one page is not a report. "
        "The summary of your final answer is a short abstract. Before you finish, check that "
        "every point of the request is covered and every claim has its mark."
    ),
}

_ANCHORS = (
    (
        re.compile(r"You are the diagnosis agent.*?never say that you did\.", re.DOTALL),
        "intro",
    ),
    (
        re.compile(
            r"Ask the web little: at most 3 searches and 2 pages per question, and stop as "
            r"soon as a page answers it \(further web calls are refused\)\."
        ),
        "web",
    ),
    (
        re.compile(
            r"Keep the diagnosis short: a summary of 2-4 sentences and at most 4 findings, one "
            r"sentence each\.|Keep it short: at most 4 findings, one sentence each\."
        ),
        "style",
    ),
)


def persona(depth: str) -> Persona:
    depth = depth_of(depth)
    return Persona(intro=_INTRO, web=_WEB[depth], style=_STYLE[depth])


def personalise(system: str, depth: str) -> str:
    """The agent's system prompt with the diagnosis wording replaced by Research's. A piece that
    is not in the prompt (it was reworded) is left alone: the rest still holds."""
    p = persona(depth)
    for pattern, part in _ANCHORS:
        system = pattern.sub(lambda _m, part=part: getattr(p, part), system)
    return system


def limits_for(settings: Any, depth: str, base: Any) -> Any:
    """``base`` (an ``agent_loop.Limits``) with the research frame applied. 0 in a setting means
    no limit."""
    depth = depth_of(depth)

    def count(value: int) -> int:
        return int(value) if int(value) > 0 else UNLIMITED_COUNT

    def seconds(value: float) -> float:
        return float(value) if float(value) > 0 else UNLIMITED_SECONDS

    common = {
        "observation_chars": int(settings.research_observation_chars),
        "context_chars": int(settings.research_context_chars),
        "max_output_tokens": int(settings.research_output_tokens),
        "native_output_tokens": int(settings.research_output_tokens) + 2048,
        "min_context": int(settings.research_min_context),
        "max_stalled_turns": int(settings.research_stall_turns),
        "stop_web_at_usable": False,
        "max_web_attempts": UNLIMITED_COUNT,
    }
    if depth == "quick":
        return replace(
            base,
            **common,
            max_turns=count(settings.research_max_turns) if settings.research_max_turns else 40,
            max_tool_calls=(
                count(settings.research_max_tool_calls) if settings.research_max_tool_calls else 16
            ),
            time_budget_seconds=seconds(settings.research_quick_seconds),
            max_web_searches=4,
            max_web_fetches=6,
        )
    return replace(
        base,
        **common,
        max_turns=count(settings.research_max_turns),
        max_tool_calls=count(settings.research_max_tool_calls),
        time_budget_seconds=seconds(settings.research_time_budget_seconds),
        max_web_searches=UNLIMITED_COUNT,
        max_web_fetches=UNLIMITED_COUNT,
    )
