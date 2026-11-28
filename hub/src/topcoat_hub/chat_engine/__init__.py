"""The chat engine, moved out of :mod:`topcoat_hub.api.chat` step by step (issue #205).

The routes stay in :mod:`topcoat_hub.api.chat`; the engine they start lives here."""

from typing import Any


def router() -> Any:
    """The router module, where the steps of the engine that have not moved yet still are.

    They are looked up there when an answer runs, not imported at the top: the router imports
    this package, so the two can import each other in either order, and a test that replaces a
    step on :mod:`topcoat_hub.api.chat` still reaches the engine. This goes away once every step
    has moved (issue #205, step 3)."""
    from topcoat_hub.api import chat as api_chat

    return api_chat
