"""The pipeline of one answer (issue #205, step 1): what a chat message turns into, from the
reservation of a node to the stream of the model's answer, and how a failure before the first word
reaches the request that waits for it.

The steps it calls (the reaction, the issue command, the secretary, the check, the hand-over, the
answer itself) still live in :mod:`topcoat_hub.api.chat`; they are looked up there when an answer
runs, so this module and that one can import each other and a test that replaces a step there
still reaches the pipeline. They move here in the next steps."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from fastapi import Request
from fastapi.responses import Response

from topcoat_hub import chat, chat_images, chat_tools, local_tools
from topcoat_hub.auth import CurrentUser
from topcoat_hub.gateway import CONTEXT_FULL_MESSAGE, GatewayError, Upstream, is_context_full
from topcoat_hub.job_log import set_job_context
from topcoat_hub.memory import Recalled, is_followup

if TYPE_CHECKING:
    from topcoat_hub.api.chat import Turn

log = logging.getLogger(__name__)

RECALL_MAX_TOKENS = 400  # at most this much of the prompt goes to memory notes


def _api() -> Any:
    """The router module, where the steps of the pipeline still are (see the module docstring)."""
    from topcoat_hub.api import chat as api_chat

    return api_chat


async def pipeline(
    request: Request,
    user: CurrentUser,
    turn: "Turn",
    tools: str,
    outcome: "asyncio.Future[Response | None]",
) -> AsyncIterator[str]:
    """The answer's events. Until the model has started to answer, a failure is reported through
    ``outcome`` (an HTTP error response) and nothing is yielded; ``outcome`` is ``None`` once the
    answer starts."""
    api = _api()
    try:
        # (aclosing: a stop or a shutdown reaches the generators inside, which store the text)
        async with contextlib.aclosing(
            pipeline_events(request, user, turn, tools, outcome)
        ) as events:
            async for chunk in events:
                yield chunk
    except asyncio.CancelledError:
        if not outcome.done():  # stopped before the model answered: the reader gets the end
            outcome.set_result(None)
        raise
    finally:
        if not outcome.done():  # a crash before the model answered: the request hears about it
            outcome.set_result(api._error(500, "internal_error", "the answer could not be started"))


async def reserve(state: Any, turn: "Turn") -> Any:
    """The place for this answer. A model or node the person picked wins, but a pick that cannot
    be served now (its machine is off, away or kept free) does not end in an error: the row of the
    program in the table of applications answers instead, and the answer says whom it replaces
    (the same label as any other hand-over to the next node)."""
    try:
        return await state.gateway.reserve(turn.requested)
    except GatewayError as exc:
        if not turn.picked or not (exc.status == 503 or exc.code == "node_not_found"):
            raise
        choice = await state.gateway.resolver.resolve(
            "answer", application=turn.application or "chat", if_none="first"
        )
        if choice.node is None or choice.requested == turn.requested:
            raise
        try:
            placement = await state.gateway.reserve(choice.requested)
        except GatewayError:
            raise exc from None
        away = turn.requested.partition("@")[2]
        turn.chain_from, turn.chain_state = away or turn.requested, "offline"
        turn.requested, turn.model = choice.requested, choice.model
        return placement


async def pipeline_events(
    request: Request,
    user: CurrentUser,
    turn: "Turn",
    tools: str,
    outcome: "asyncio.Future[Response | None]",
) -> AsyncIterator[str]:
    api = _api()
    state = request.app.state
    emoji = await api._reaction_for(request, turn)
    if emoji is not None:
        outcome.set_result(None)
        async with contextlib.aclosing(api._reaction_events(request, user, turn, emoji)) as events:
            async for chunk in events:
                yield chunk
        return
    issue = api._issue_request(turn)
    if issue is not None:
        # "Problem: …" files an issue (the person's own command, no model); a complaint in
        # Projekte gets a card that files it on a click (project_issues.py)
        outcome.set_result(None)
        async with contextlib.aclosing(api._issue_events(request, user, turn, *issue)) as events:
            async for chunk in events:
                yield chunk
        return
    set_job_context(
        user_id=user.uuid,
        conversation_id=turn.conversation_id,
        application=turn.application or "chat",
    )  # the record of the model requests of this answer (SCH-03 step 3); the row of the table
    try:
        turn.placement = await reserve(state, turn)
        route = turn.placement.route
        context_length = turn.placement.context_length or state.settings.chat_default_context_tokens
        budget = chat.prompt_budget(context_length)
        # Notes about the user that fit this message (E2-02); never for a continuation.
        recalled: list[Recalled] = []
        if turn.memory_enabled and turn.policy.recall and turn.continues is None:
            recalled = await state.memory.recall(
                user.uuid,
                turn.last_user_text,
                min(RECALL_MAX_TOKENS, budget // 5),
                also=turn.last_refs if is_followup(turn.last_user_text) else (),
                ask_allowed=not turn.care_asked,
                application=turn.application,
            )
        # The secretary decides who does the task, by the contracts (D112, issue #36).
        decision = await api._secretary_route(request, user, turn, tools, bool(route.candidates))
        if decision is not None:
            outcome.set_result(None)
            async with contextlib.aclosing(
                api._secretary_events(request, user, turn, decision, recalled, context_length)
            ) as routed_events:
                async for chunk in routed_events:
                    yield chunk
            return
        # A question about the user's servers is answered after a check (E3-01d).
        checking, refusal = await api._plan_check(
            request, user, turn, tools, bool(route.candidates)
        )
        if refusal is not None:
            outcome.set_result(refusal)
            return
        handed = None if checking else await api._start_handoff(request, user, turn, tools, route)
        if handed is not None:
            outcome.set_result(None)
            async with contextlib.aclosing(
                api._handoff_events(
                    request, user, turn, route, handed[0], handed[1], recalled, context_length
                )
            ) as handoff_events:
                async for chunk in handoff_events:
                    yield chunk
            return
        mode = (
            api.research_mode(turn.application, turn.quality, turn.from_hub) if checking else None
        )
        if mode is not None:
            # in Research a check is a research of its own (at "thorough") or the offer of one
            # (at "balanced"): the chat starts it or offers it, says so and goes on
            outcome.set_result(None)
            events = (
                api._research_start_events(request, user, turn)
                if mode == "job"
                else api._research_offer_events(request, user, turn)
            )
            async with contextlib.aclosing(events) as started:
                async for chunk in started:
                    yield chunk
            return
        if checking and turn.application == "project":
            found = await api._iteration_target(request, user, turn)
            if found is not None:
                # "make the next version of 1.8.2": a card that starts the next iteration with
                # the message as feedback (project_iterations.py); the task text stays
                outcome.set_result(None)
                async with contextlib.aclosing(
                    api._iteration_events(request, user, turn, *found)
                ) as offering:
                    async for chunk in offering:
                        yield chunk
                return
        if checking and turn.application == "project":
            target = await api._write_target(request, user, turn)
            if target is not None:
                # "rewrite these tasks": the hub reads, the chat model writes, the hub builds the
                # set (project_write.py); no agent loop
                outcome.set_result(None)
                async with contextlib.aclosing(
                    api._write_events(request, user, turn, *target)
                ) as writing:
                    async for chunk in writing:
                        yield chunk
                return
        if checking and not turn.handover_targets:
            turn.handover_targets = await api._handover_targets(request, user, turn)
        if checking and (owner := api.project_owner(turn)) is not None:
            # the check the hub would start has no project tools: the hand-over instead
            outcome.set_result(None)
            async with contextlib.aclosing(
                api._handover_answer_events(request, user, turn, owner)
            ) as handing:
                async for chunk in handing:
                    yield chunk
            return
        if checking:
            outcome.set_result(None)
            async with contextlib.aclosing(
                api._checked_answer(
                    request,
                    user,
                    turn,
                    route,
                    checking,
                    api.notes_on_topic(recalled, turn.last_user_text),
                    context_length,
                    budget,
                    order=api._order_of(turn, "{}")[
                        0
                    ],  # a check the hub starts: one machine, or none
                )
            ) as checked:
                async for chunk in checked:
                    yield chunk
            return
        # What changes with the question (the recalled notes) goes last, into the final user
        # turn, so the model can reuse its prompt cache for everything before it (D27).
        notes = chat.notes_context([n.prompt_line() for n in recalled])
        notes = await api._brief_block(request, user, turn) + notes
        if turn.segment_block:  # the hand-over comes first, capped by what the context allows
            cap = max(budget // 4, 0) * chat.CHARS_PER_TOKEN
            notes = turn.segment_block[:cap] + notes
        notes = await api._second_look_block(request, user, turn) + notes
        notes += turn.accepted_hint  # last, next to the person's "ja": what it agreed to
        earlier = await chat_tools.earlier_check(
            request.app.state.sessionmaker, user.uuid, turn.conversation_id
        )
        if earlier is not None:  # a follow-up without a check still sees what was read (1.3)
            room = min(max(budget // 3, 0) * chat.CHARS_PER_TOKEN, chat_tools.EARLIER_CHARS)
            notes = chat_tools.earlier_block(earlier, room) + notes
        core_text = await api._core_text(request, user, turn, context_length)
        upstream: Upstream | None = None
        for attempt in (0, 1):
            # Small contexts get the short system prompt: the fixed rules must not eat the window.
            system = chat.system_prompt(
                turn.user_prompt,
                turn.address,
                turn.capabilities,
                compact=context_length < chat.COMPACT_CONTEXT_TOKENS,
                core=core_text,
                where=await api._where(request, user, turn, route, context_length),
            )
            sent: list[dict[str, Any]] | None = None
            if turn.continues is None:
                prompt = chat.build_prompt(system, turn.history, budget, notes)
                if turn.vision_history is not None:
                    # D125: the same prompt with the images where they stand, for a model that sees
                    seen = chat.build_prompt(system, turn.vision_history, budget, notes)
                    sent, _ = chat_images.expand(seen.messages, turn.image_data)
                    sent[0] = {
                        **sent[0],
                        "content": f"{sent[0]['content']}\n\n{chat_images.DATA_RULE}",
                    }
            else:
                continued = chat.build_continue_prompt(system, turn.history, budget, notes)
                if continued is None:
                    outcome.set_result(
                        api._error(
                            409,
                            "context_too_small",
                            f"The context of {turn.model} ({context_length} tokens) is too small "
                            "to continue this answer. Raise the context length of the model on "
                            "its node (runner.model_context_lengths) or write a new message.",
                        )
                    )
                    return
                prompt = continued
            payload: dict[str, Any] = {
                "model": turn.requested,
                "messages": sent if sent is not None else prompt.messages,
                "stream": True,
                "stream_options": {"include_usage": True},
                # llama-server keeps the KV cache of the last prompt of a slot and only processes
                # what differs from it; the prompt is ordered for that (D27)
                "cache_prompt": True,
            }
            offered = []
            if turn.decide:
                offered.append(
                    chat_tools.decider_tool(
                        turn.decide_groups,
                        turn.decide_targets,
                        turn.decide_keys,
                        web=turn.capabilities.web_search,
                    )
                )
                offered.extend(local_tools.definitions(set(turn.decide_keys)))
            if turn.handover_targets:
                offered.append(chat_tools.handover_tool(dict(turn.handover_targets)))
            if offered:
                payload["tools"] = offered
                payload["tool_choice"] = "auto"
            if turn.temperature is not None:
                payload["temperature"] = turn.temperature
            payload.update(chat.sampling_params(turn.model, state.settings.chat_sampling))
            payload.update(chat.effort_params(turn.model, turn.quality, state.settings.chat_effort))
            upstream = await state.gateway.forward(
                turn.requested,
                "POST",
                "/v1/chat/completions",
                payload,
                accept="text/event-stream",
                placement=turn.placement,
            )
            if turn.chain_from and upstream.fallback_from is None:
                upstream.fallback_from, upstream.fallback_state = turn.chain_from, turn.chain_state
            if offered and upstream.status in (400, 422) and attempt == 0:
                # a server without --jinja refuses ``tools``: this model decides by the heuristic
                # from now on, and this message is asked again without the function
                upstream.close()
                api._no_decider(request).add(turn.requested)
                turn.decide = False
                turn.handover_targets = ()
                turn.capabilities = replace(turn.capabilities, decider=False)
                continue
            break
        if upstream is None:  # unreachable: the loop always forwards once
            outcome.set_result(api._error(502, "model_error", "the model could not be asked"))
            return
    except GatewayError as exc:
        outcome.set_result(api._error(exc.status, exc.code, exc.message, exc.retry_after))
        return
    try:
        if upstream.status != 200:
            detail = b"".join([c async for c in upstream.body])[:2000].decode("utf-8", "replace")
            upstream.close()
            log.warning("chat: model answered %s: %s", upstream.status, detail)
            if is_context_full(detail):
                outcome.set_result(
                    api._error(
                        503,
                        "context_full",
                        CONTEXT_FULL_MESSAGE,
                        state.settings.gateway_retry_after_seconds,
                    )
                )
                return
            outcome.set_result(
                api._error(502, "model_error", f"the model refused the request ({upstream.status})")
            )
            return
        on_tool = None
        if turn.decide:

            def on_tool(name: str, arguments: str) -> AsyncIterator[str]:
                if name in local_tools.LOCAL:  # read-only functions of the hub itself (T3.6, T3.8)
                    return api._local_answer(
                        request,
                        user,
                        turn,
                        route,
                        name,
                        arguments,
                        context_length,
                        budget,
                        recalled,
                    )
                # the model decided to look: in Research that is a research (or its offer), too
                mode = api.research_mode(turn.application, turn.quality, turn.from_hub)
                if mode == "job":
                    return api._research_start_events(request, user, turn)
                if mode == "offer":
                    return api._research_offer_events(request, user, turn)
                order, focus = api._order_of(turn, arguments)
                return api._checked_answer(
                    request,
                    user,
                    turn,
                    route,
                    "decided",
                    api.notes_on_topic(recalled, turn.last_user_text),
                    context_length,
                    budget,
                    focus,
                    order,
                )

        after_done = None
        # a revision is asked with what the model was shown, the images included
        shown_prompt = replace(prompt, messages=sent) if sent is not None else prompt
        held = api._held_for(turn, shown_prompt, on_tool, request.app.state.settings)
        if held is None and turn.second_look and turn.verify_model:
            # "thorough": a second look at every answer; without a check there is no evidence, the
            # reviewer marks what looks wrong or cannot be known without looking (quality.py)

            def after_done(message: dict[str, Any]) -> AsyncIterator[str]:
                return api._verify_answer(request, user, turn, message, "")

        outcome.set_result(None)  # the model answers: the request becomes a reader of the run
        async with contextlib.aclosing(
            api._answer(
                request,
                user,
                turn,
                upstream,
                prompt,
                context_length,
                recalled,
                on_tool=on_tool,
                after_done=after_done,
                held=held,
            )
        ) as answer:
            async for chunk in answer:
                if held is not None and held.hides(chunk):
                    continue  # the text waits for its check; the activity around it is shown
                yield chunk
    finally:
        # the model's stream ends with this answer, however it ends: a failure between the
        # forward and the moment the answer reads it (issue #205) left it open, and the place it
        # holds on the node made the next message there wait forever (close() is idempotent)
        upstream.close()
