"""What a live test of an application has to show (plain functions, tested in hub/tests/test_live_scripts.py).

A live test that only asks "did an answer come back" says little about the application. Each judge here names what
the application is for and turns the numbers of the run into sentences about what is missing; an empty list is a pass.
The thresholds are deliberately modest (a small model on an 8 GB card must be able to pass) and sit in one place.
"""

import re

# Research, frame ``thorough``: a documentation that rests on sources it read
RESEARCH_MIN = {"chapters": 3, "searches": 2, "pages": 2, "sources": 3, "hosts": 2, "claims_total": 3, "chars": 4000}
RESEARCH_MIN_BACKED_SHARE = 0.5  # of the claims, at least this share has evidence behind it

# the words with which a run says it was cut off by a limit
LIMIT_WORDS = re.compile(r"maximale anzahl|werkzeugaufrufe (?:erschöpft|aufgebraucht)|tool calls? (?:limit|exhausted|ran out)|limit reached", re.I)


def judge_research(task: dict, meta: dict, body: str) -> list[str]:
    """What is wrong with a thorough research run and its report, as sentences."""
    problems: list[str] = []
    if task.get("status") != "done":
        problems.append(f"the task ended {task.get('status')!r}, not done")
    if task.get("stop_reason"):
        problems.append(f"the run ended on {task['stop_reason']}, not by itself (a limit or a stall)")
    chapters = len(meta.get("chapters") or [])
    have = {
        "chapters": chapters,
        "searches": int(meta.get("searches") or 0),
        "pages": int(meta.get("pages") or 0),
        "sources": int(meta.get("sources") or 0),
        "hosts": len(meta.get("hosts") or []),
        "claims_total": int(meta.get("claims_total") or 0),
        "chars": len(body or ""),
    }
    for key, least in RESEARCH_MIN.items():
        if have[key] < least:
            problems.append(f"{key}: {have[key]}, at least {least} expected")
    total, backed = have["claims_total"], int(meta.get("claims_backed") or 0)
    if total and backed / total < RESEARCH_MIN_BACKED_SHARE:
        problems.append(f"only {backed} of {total} claims have evidence behind them")
    return problems


def judge_blocks(view: dict) -> list[str]:
    """What is wrong with a run of the block program ``research_report`` (agent, check, revision, filing)."""
    problems: list[str] = []
    if view.get("status") != "ok":
        problems.append(f"the run ended {view.get('status')!r}: {str(view.get('error', ''))[:160]}")
    if not view.get("filed"):
        problems.append("nothing was filed in the Ablage")
    variables = view.get("variables") or {}
    if len(str(variables.get("draft", ""))) < 2000:
        problems.append("the draft is shorter than 2000 characters")
    verdict = variables.get("v")
    if not isinstance(verdict, dict):
        problems.append("the check left no verdict")
    return problems


def judge_dev(answer: dict, task: dict | None, model: str, nodes: list[str]) -> list[str]:
    """What is wrong with the answer of a Development chat that had to read a repository, as sentences."""
    problems: list[str] = []
    if answer.get("status") != "complete":
        problems.append(f"status is {answer.get('status')!r}, not complete")
    text = (answer.get("content") or "").strip()
    if not text:
        problems.append("the answer is empty")
    if answer.get("model") and not str(answer["model"]).startswith(model.partition("@")[0]):
        problems.append(f"answered by {answer['model']}, not {model}")
    if answer.get("node") and answer["node"] not in nodes:
        problems.append(f"answered on {answer['node']}, not on {','.join(nodes)}")
    if LIMIT_WORDS.search(text):
        problems.append("the answer says it was cut off by a limit")
    if text and not re.search(r"\d+\.\d+\.\d+", text):
        problems.append("the answer names no version number (the file VERSION was asked for)")
    if task is None:
        problems.append("no agent task belongs to the chat: the tools were not used")
        return problems
    if task.get("status") != "done":
        problems.append(f"the check ended {task.get('status')!r}, not done")
    # a stall the progress guard ended is a legitimate end of a check (the answer is judged on its content);
    # a count, the clock or a repeat is a limit the agent ran into
    if task.get("stop_reason") and task["stop_reason"] != "no_progress":
        problems.append(f"the check ended on {task['stop_reason']}, not by itself")
    if int(task.get("tool_calls") or 0) < 1:
        problems.append("no tool was called: the answer is not from the repository")
    return problems
