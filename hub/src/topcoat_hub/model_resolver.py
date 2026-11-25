"""Which node and model a piece of work uses: one place (consolidation step 3).

Before, the answer to "which model answers?" was spread over a dozen settings and seven copies of
"the hub's default or the environment's". Now a caller names what the model is for (a *role*) and
the application it works for, and gets a :class:`Choice` with the reason. The order of the sources:

1. what the person picked in the conversation (``model`` or ``model@node``), if anything;
2. the **table of applications** (SCH-04, D85): the row of the application, or for the roles
   check and background the rows of that name, if the row has models or nodes. Its models and
   nodes in order are tried model first: the best model the row allows on any node it allows;
   ``same_as_answer`` is the model that wrote the answer, ``all`` the rest of the nodes;
3. the hub's default model, for work whose row says nothing.

(The older lists, the default model per node and the environment variables that steered this are
turned into rows of the table when the hub starts, ``legacy_policy.convert``.)

``auto`` picks, on the node, the largest model that is still fast enough for the role, by the speeds
the hub has measured (:func:`best_model`): a 30B model that crawls at 12 tokens a second does not
answer a chat when a model almost as good gives 200."""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from topcoat_hub import model_facts, node_chain
from topcoat_hub.node_policy import ALL_NODES, AUTO, ROLES, SAME_AS_ANSWER, AppRow, Entry

if TYPE_CHECKING:
    from topcoat_hub.gateway import Gateway

log = logging.getLogger(__name__)

# The slowest a model may be (measured tokens per second) to count as fast enough for a role. The
# answer is what a person waits for; background work and the second look may take their time.
MIN_TPS = {"answer": 30.0, "agent": 15.0, "background": 5.0, "check": 5.0}


# names that mark a model that embeds text and cannot chat
def is_embedding_name(name: str) -> bool:
    return "embedding" in model_facts.detect(name)


@dataclass(frozen=True)
class ModelFacts:
    """What the hub knows about one model on one node."""

    name: str
    size_mb: float | None = None
    tps: float | None = None  # measured tokens per second here
    loaded: bool = False


def best_model(models: list[ModelFacts], role: str) -> ModelFacts | None:
    """The model ``auto`` picks on a node for ``role``.

    The largest of the measured models that is fast enough (size stands for quality: a bigger model
    of the same speed class answers better); with none fast enough, the one that is loaded (it is
    what the node runs), then the fastest measured one; without any measurement the smallest, the
    safest first guess. Embedding models never answer."""
    usable = [m for m in models if not is_embedding_name(m.name)]
    if not usable:
        return None
    floor = MIN_TPS.get(role, MIN_TPS["answer"])
    fast = [m for m in usable if m.tps and m.tps >= floor]
    if fast:
        return max(fast, key=lambda m: (m.size_mb or 0.0, m.loaded, m.tps or 0.0))
    loaded = [m for m in usable if m.loaded]
    if loaded:
        return max(loaded, key=lambda m: (m.size_mb or 0.0, m.name))
    measured = [m for m in usable if m.tps]
    if measured:
        return max(measured, key=lambda m: m.tps or 0.0)
    return min(usable, key=lambda m: (m.size_mb if m.size_mb is not None else float("inf"), m.name))


@dataclass(frozen=True)
class Choice:
    """A node and model for one piece of work, and why."""

    model: str
    node: str | None  # None: any node that has the model (the gateway's order decides)
    reason: str
    fallback_from: str | None = None  # the node that would have answered, if another one does
    fallback_state: str | None = None  # why that one was passed over (``busy_gaming`` ...)
    min_context: int = 0
    # every node the row allows for this model, in the row's order: the request may go to any of
    # them and the pool's queue takes the first that is free (a node is chosen late, not here)
    nodes: tuple[str, ...] = ()

    @property
    def requested(self) -> str:
        if self.nodes:
            return f"{self.model}@{','.join(self.nodes)}"
        return f"{self.model}@{self.node}" if self.node else self.model


class ModelResolver:
    def __init__(self, gateway: "Gateway") -> None:
        self.gateway = gateway

    async def default_model(self) -> str:
        """The model for work that no row of the table decides: the best answering model the pools
        offer (there is no global default to set: models are released per node, then chosen per
        application). With no node serving anything, an empty name (the caller's route check then
        names the reason)."""
        from topcoat_hub.gateway import BLOCKED_STATES  # the gateway imports this module

        views = await self.gateway.node_views()  # the pool only
        # a node with a game on it is passed over while another is free (live 2026-11-18: every
        # block program failed at once with "node is busy" while node-amd ran a game)
        free = [
            v.name
            for v in views
            if v.connected and getattr(v, "state", "available") not in BLOCKED_STATES
        ]
        up = free or [v.name for v in views if v.connected]
        if house := self.house_default(up):
            return house
        facts = {
            v.name: [
                ModelFacts(name, v.sizes_mb.get(name), v.speeds.get(name), loaded)
                for name, loaded in v.models.items()
            ]
            for v in views
            if v.connected
        }
        best = best_model([f for fs in facts.values() for f in fs], "answer")
        return best.name if best is not None else ""

    def house_default(self, connected: Iterable[str]) -> str:
        """The house model of the first connected node that is no worker (workers only when no
        other node is up), or an empty name without house models. Work that no row steers (a
        block program, a script) takes it instead of the fastest measured model: a new house model
        has no speed measured yet and would never win ``best_model``."""
        policy = self.gateway.policy
        up = set(connected)
        order = [n for n in policy.house if n not in policy.workers]
        order += [n for n in policy.house if n in policy.workers]
        return next((policy.house[n] for n in order if n in up), "")

    def min_context(self, role: str, application: str | None = None) -> int:
        """The context a role needs at least: an agent run reads tool results and writes a
        diagnosis; a research run reads whole pages. Others take what the node gives."""
        if role != "agent":
            return 0
        if application == "research":
            return self.gateway.settings.research_min_context
        return self.gateway.settings.agent_min_context

    def row_of(self, role: str, application: str | None) -> tuple[str, AppRow] | None:
        """The row of the table that decides for this work, if it says anything: the rows
        ``check`` and ``background`` for those roles, else the row of the application."""
        # work of no application (a script, the API) is the chat's, as a conversation without one
        key = role if role in ("check", "background") else (application or "chat")
        row = self.gateway.policy.applications.get(key)
        return (key, row) if row is not None and row.decides else None

    def house_entries(self, role: str, application: str | None) -> list[Entry]:
        """``(node, house model)`` for the work, or empty without house models. Background work goes
        to the workers first, then to the other nodes; everything else only to the nodes that are
        no worker. A row that names nodes keeps its order and leaves out the rest."""
        policy = self.gateway.policy
        house = policy.house
        if not house:
            return []
        workers = [n for n in policy.workers if n in house]
        main = [n for n in house if n not in workers]
        key = role if role in ("check", "background") else (application or "chat")
        named = policy.row_nodes(key)
        if named:
            main = [n for n in named if n in main]
        nodes = [*workers, *main] if role == "background" else main
        return [(n, house[n]) for n in nodes]

    async def _row_entries(self, key: str, row: AppRow, same_as: str | None) -> list[Entry]:
        """The row as ``(node, model)`` entries, the best model first: for each model, each node.
        No model means ``auto`` on every node, no node all nodes; ``same_as_answer`` is the model
        of the answer (left out where nobody told it)."""
        nodes = [n for n in row.nodes if n != ALL_NODES]
        if not nodes or ALL_NODES in row.nodes:
            # the nodes the row does not name come in the configured order, then by name
            known = sorted(
                (v.name for v in await self.gateway.node_views()), key=self.gateway._order_key
            )
            nodes += [n for n in known if n not in nodes]
        models: list[str] = []
        for model in row.models or (AUTO,):
            if model == SAME_AS_ANSWER:
                model = same_as or ""
                if model and "@" in model:
                    model = model.partition("@")[0]
            if model and model not in models:
                models.append(model)
        return [(n, m) for m in models for n in nodes]

    async def _stored(
        self, role: str, application: str | None, same_as: str | None = None
    ) -> tuple[list[Entry], str]:
        """The row for the role as a list and where it came from; empty when the row says
        nothing (the caller then uses the hub's default model). With house models the nodes'
        own models decide, the row only names the nodes (:meth:`house_entries`)."""
        if entries := self.house_entries(role, application):
            return entries, "the house models"
        if (found := self.row_of(role, application)) is not None:
            key, row = found
            if entries := await self._row_entries(key, row, same_as):
                return entries, f"the row {key} of the table"
        return [], ""

    async def chain(
        self, role: str, application: str | None = None, same_as: str | None = None
    ) -> list[tuple[str, str]]:
        """``[(node, model)]``, best first, with ``auto`` replaced by the model it stands for;
        empty when nothing is ordered for the role (the caller then uses a default).
        ``same_as`` is the model of the answer a check or a summary belongs to."""
        return (await self._chain_from(role, application, same_as))[0]

    async def _chain_from(
        self, role: str, application: str | None, same_as: str | None = None
    ) -> tuple[list[tuple[str, str]], str]:
        """The chain and where its list comes from, for the reason of a choice."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        entries, where = await self._stored(role, application, same_as)
        out: list[tuple[str, str]] = []
        views = None
        for node, model in entries:
            if model != AUTO:
                out.append((node, model))
                continue
            if views is None:
                views = {v.name: v for v in await self.gateway.node_views()}
            view = views.get(node)
            if view is None:
                continue
            best = best_model(
                [
                    ModelFacts(
                        name,
                        view.sizes_mb.get(name),
                        view.speeds.get(name),
                        loaded,
                    )
                    for name, loaded in view.models.items()
                ],
                role,
            )
            if best is not None:
                out.append((node, best.name))
        if role == "answer" and self.gateway.choice_mode == "neu" and len(out) > 1:
            # the switch (choice.py): the profile of the answer prefers the best rated model that is
            # loaded; the others stay behind it in the old order
            from topcoat_hub import choice as choice_rules

            if views is None:
                views = {v.name: v for v in await self.gateway.node_views()}
            loaded = {
                (name, model): flag
                for name, view in views.items()
                for model, flag in view.models.items()
            }
            out = choice_rules.order_pairs(
                out,
                {k: bool(v) for k, v in loaded.items()},
                self.gateway.answer_quality,
                choice_rules.profile_for("chat"),
            )
        return out, where

    async def resolve(
        self,
        role: str,
        *,
        application: str | None = None,
        picked: str | None = None,
        fallback: str | None = None,
        if_none: str = "first",
        same_as: str | None = None,
    ) -> Choice:
        """The node and model for ``role``. ``picked`` is what a person chose (``model`` or
        ``model@node``); ``fallback`` the model to use when no list is stored (default: the hub's).
        When a list is stored but no node of it can take the work now, ``if_none`` says what comes
        back: ``first`` the first entry (the caller's route check then names the reason), or
        ``default`` the fallback model as if no list were stored."""
        need = self.min_context(role, application)
        if picked:
            model, _, node = picked.partition("@")
            return Choice(model, node or None, "chosen by the person", min_context=need)
        chain, where = await self._chain_from(role, application, same_as)
        if chain:
            got = await node_chain.pick(self.gateway, chain)
            if got is not None:
                reason = f"the first of {where} that can take it now ({got.node})"
                # the model is chosen here, the node later: every node the row names for this
                # model may take the request, whichever has a free place first
                same_model = tuple(dict.fromkeys(n for n, m in chain if m == got.model))
                return Choice(
                    got.model,
                    got.node,
                    reason,
                    fallback_from=got.fallback_from,
                    fallback_state=got.fallback_state,
                    min_context=need,
                    nodes=same_model if len(same_model) > 1 else (),
                )
            if if_none == "first":
                node, model = chain[0]
                return Choice(
                    model,
                    node,
                    f"no node of {where} can take it now: the first one",
                    min_context=need,
                )
        return Choice(
            fallback or await self.default_model(),
            None,
            f"no row of the table decides for {role}: the hub's default model",
            min_context=need,
        )
