"""The best setting of a model for a kind of work (PRG-00 R6): what the bench showed, taken by the
admin with one click and applied by the gateway to the requests of that kind.

A profile is ``{model: {function: variant}}`` where the variant is one of the fixed presets of
``bench.variants`` (``nothink``, ``think-low`` ...). Nothing else can be stored, so nothing but
those presets can ever be added to a request. What the caller set itself is never overridden: a
request that names a temperature or a thinking switch keeps it."""

from collections.abc import Mapping
from typing import Any

from topcoat_hub.bench.variants import DEFAULT, VARIANTS

PROFILE_KEY = "model_profiles"  # hub_settings: {model: {function: variant}}
FUNCTIONS = ("answer", "tools", "extract", "review")

# work labels of the hub's own checks that read an answer (they are "review" work), and those that
# extract something into a fixed shape
_REVIEW = frozenset({"review", "quality", "precheck", "core_review", "digest_check"})
_EXTRACT = frozenset({"memory", "digest_summary", "care", "handover"})


class ProfileError(ValueError):
    """The profile is not valid."""


def function_of(payload: Mapping[str, Any], label: str = "") -> str:
    """The kind of work a chat request is: calling tools, reading an answer, extracting a fixed
    shape, or answering."""
    if payload.get("tools"):
        return "tools"
    if label in _REVIEW:
        return "review"
    if label in _EXTRACT or payload.get("response_format"):
        return "extract"
    return "answer"


def check(profiles: Any) -> dict[str, dict[str, str]]:
    """The stored profiles as a clean dict; anything that is not a known function and preset is an
    error (a bad value never reaches a request)."""
    if profiles is None:
        return {}
    if not isinstance(profiles, dict):
        raise ProfileError("profiles must map models to their settings")
    out: dict[str, dict[str, str]] = {}
    for model, settings in profiles.items():
        if not isinstance(model, str) or not model.strip() or "@" in model or len(model) > 255:
            raise ProfileError(f"{model!r} is not a model name")
        if not isinstance(settings, dict):
            raise ProfileError(f"{model}: functions must map to a setting")
        clean: dict[str, str] = {}
        for function, variant in settings.items():
            if function not in FUNCTIONS:
                raise ProfileError(f"{model}: function is one of {', '.join(FUNCTIONS)}")
            if variant not in VARIANTS or variant == DEFAULT:
                raise ProfileError(
                    f"{model}: setting is one of {', '.join(v for v in VARIANTS if v != DEFAULT)}"
                )
            clean[function] = variant
        if clean:
            out[model.strip()] = clean
    return out


def apply(
    payload: dict[str, Any], profile: Mapping[str, str] | None, function: str
) -> dict[str, Any]:
    """``payload`` with the setting stored for ``function`` added, keys the caller set kept."""
    variant = (profile or {}).get(function)
    if not variant or variant not in VARIANTS:
        return payload
    extra = {k: v for k, v in VARIANTS[variant].items() if k not in payload}
    return {**payload, **extra} if extra else payload


# --- thinking per model family (issue 2.23, D97) -----------------------------------------------
# The hub's code says "no thinking" one way: ``chat_template_kwargs.enable_thinking = False`` (the
# switch of the Qwen3, Qwen3.5 and Gemma templates). gpt-oss has no such switch: its chat template
# reads ``reasoning_effort`` (low / medium / high), and it thought through the whole budget of a
# step that asked for none (live 2026-11-23: the search-term step of _find_and_read went from 600 to
# 1800 tokens and ended empty). The gateway translates the switch per family right before a request
# leaves, so no caller needs to know the model. A caller that set ``reasoning_effort`` itself keeps
# it. gpt-oss still thinks a little at "low", so a step that asked for no thinking gets room for it.

GPT_OSS_THINK_RESERVE = 600  # tokens of reasoning gpt-oss takes even at "low" (2026-11-23: 200-600)


def family_of(model: str) -> str:
    """The family of a model id, for the rules that differ per family ("" when none is known)."""
    name = model.lower()
    if "gpt-oss" in name or "gpt_oss" in name:
        return "gpt-oss"
    if "coder" in name and "qwen" in name:
        return "qwen-coder"  # an instruct model without a thinking mode
    for family in ("qwen3.5", "qwen3", "qwen2.5", "gemma", "llama", "mistral", "phi"):
        if family in name:
            return family
    return ""


def thinking(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    """``payload`` with the thinking switch said the way ``model``'s family understands it."""
    kwargs = payload.get("chat_template_kwargs")
    if not isinstance(kwargs, dict) or "enable_thinking" not in kwargs:
        return payload
    family = family_of(model)
    if family != "gpt-oss":
        return payload  # the Qwen and Gemma templates read the switch; others ignore it harmlessly
    think = bool(kwargs["enable_thinking"])
    effort = payload.get("reasoning_effort") or ("medium" if think else "low")
    rest = {k: v for k, v in kwargs.items() if k != "enable_thinking"}
    out = {
        **payload,
        "reasoning_effort": effort,
        # the template variable of gpt-oss; llama-server's own field alone is not reliable
        # (llama.cpp issue 15130)
        "chat_template_kwargs": {**rest, "reasoning_effort": effort},
    }
    tokens = payload.get("max_tokens")
    if not think and isinstance(tokens, int) and tokens > 0:
        out["max_tokens"] = tokens + GPT_OSS_THINK_RESERVE
    return out


def same_family(a: str | None, b: str | None) -> bool:
    """Whether two models (``model`` or ``model@node``) are the same model or of one known family:
    a reviewer of the author's family repeats the author's blind spots (issue 2.19: gpt-oss checked
    gpt-oss and confirmed what it had written)."""
    if not a or not b:
        return False
    ma, mb = a.partition("@")[0], b.partition("@")[0]
    if ma == mb:
        return True
    fa = family_of(ma)
    return bool(fa) and fa == family_of(mb)
