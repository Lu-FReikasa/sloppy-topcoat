"""The variants of a bench run (PRG-00 R6): one setting of the request changed, the rest as it is.

A variant is a named preset, never free text from outside: the bench can say "does this model call
tools better with the thinking switched off" without a way to send anything else to a node. The
empty name is the model as it is asked every day; only its ratings rank models and decide a pick."""

from typing import Any

DEFAULT = ""

VARIANTS: dict[str, dict[str, Any]] = {
    DEFAULT: {},
    "nothink": {"chat_template_kwargs": {"enable_thinking": False}},
    "think-low": {"reasoning_effort": "low"},
    "think-high": {"reasoning_effort": "high"},
    "cold": {"temperature": 0.0},
    "warm": {"temperature": 0.7},
}


def listing() -> list[dict[str, Any]]:
    return [{"name": n or "default", "key": n, "extra": e} for n, e in VARIANTS.items()]
