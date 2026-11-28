"""One answer's turn (issue #205, step 2): :class:`Turn`, everything the answer needs, and
:func:`prepare`, which stores the person's message and reads that in one short transaction.

The helpers it shares with the routes (the views of a message, the history, the defaults) still
live in :mod:`topcoat_hub.api.chat` and are looked up there when a turn is prepared (see
:func:`topcoat_hub.chat_engine.router`)."""

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from topcoat_hub import app_scope, chat, chat_images, chat_policy, quality, tool_groups
from topcoat_hub.activity import ActivityLog
from topcoat_hub.auth import CurrentUser
from topcoat_hub.chat_engine import router
from topcoat_hub.chat_store import ChatStore, title_from
from topcoat_hub.models import Message, User
from topcoat_hub.segments import parse_breaks

SEGMENT_BLOCK_TOKENS = 600  # at most this much goes to hand-over and recalled segments


@dataclass
class Turn:
    """Everything one answer needs, read in one short transaction."""

    conversation_id: uuid.UUID
    requested: str  # model or model@node for the gateway
    model: str
    history: list[tuple[str, str]]  # stored messages; a continued answer is the last one
    user_prompt: str | None
    address: str
    capabilities: chat.Capabilities
    memory_enabled: bool
    temperature: float | None
    new_user_message: dict[str, Any] | None
    started: float  # monotonic time of the request, for first-token and total time
    continues: Message | None = None  # the truncated answer being continued
    # Notes the previous answer used (its ``memory_refs``): given again for a follow-up such
    # as "Wie sicher bist du dir?".
    last_refs: tuple[uuid.UUID, ...] = ()
    # an earlier answer of this conversation already asked about an outdated note (E3-05: at most
    # one such question per conversation)
    care_asked: bool = False
    # The question mentions the machine world without clearly asking about the user's own: it is
    # answered plainly and the browser offers a check (``meta.check_offer``).
    check_offer: bool = False
    # a server check ran in this turn: its findings back what the answer says (answer_guard)
    checked: bool = False
    # The model is offered ``look_at_my_systems`` in this turn and decides itself (E3-04); the
    # groups of tools it would look at (labels for the function's description).
    decide: bool = False
    decide_groups: tuple[str, ...] = ()
    # the machines and group keys the fixed fields may name, and the tools the scope offers
    # (pruefauftrag T3.1/T3.2); empty when the model is offered the old one-field function
    decide_targets: tuple[str, ...] = ()
    decide_keys: tuple[str, ...] = ()
    decide_available: tuple[dict[str, Any], ...] = ()
    # the programs the model may propose a hand-over to (``hand_over``): (key, what it can do)
    handover_targets: tuple[tuple[str, str], ...] = ()
    # the conversation belongs to a program of the desktop (not one a program opened by the API)
    in_program: bool = False
    # the nodes / tool groups the user allowed in this conversation and the internet switch (E3-04),
    # settled with the user's default: always a dict, ``internet`` always set
    scope: dict[str, Any] | None = None
    internet: bool = True
    # the node the web search of this answer comes from (the hub's pick; None = no node offers it)
    internet_node: str | None = None
    # quality level of this answer and the models it uses (E3-04, quality.py)
    quality: str = "balanced"
    verify_model: str | None = None
    # the chat policy at this answer's effort (the setting, chat_policy.py)
    policy: chat_policy.ChatPolicy = chat_policy.DEFAULT
    second_look: bool = False
    # the persona and core stamp the system prompt of this answer was built from (E3-06, 11a)
    persona_ref: str | None = None
    core_stamp: str | None = None
    # the message the answer is written into (a placeholder with status "running", or the
    # truncated one that is continued); set when the run starts (E3-07)
    message_id: int | None = None
    # the hub is going down: a cut answer is "interrupted", not "stopped"
    interrupted: bool = False
    # E3-07 D3: the id the run is registered under (the placeholder), and the language of the user
    # (the hub's own sentences, such as the confirmation bubble, are written in it)
    run_key: int | None = None
    empty_answer: bool = False  # the model finished without a word: nothing was stored (B-A)
    language: str = "en"
    # hand-over of the last closed segment and recalled older ones (continuous conversation)
    segment_block: str = ""
    # the answer list put another node first than usual (model_resolver.py): who it replaces
    chain_from: str | None = None
    chain_state: str | None = None
    # the person (or the picker's memory of their last choice) named the model or the node
    picked: bool = False
    # the node chosen before the prompt is built (SCH-03 step 2)
    placement: Any = None
    # the application (workspace) of the conversation: the notes this answer may use
    application: str = "chat"
    # E7-01 (D56): what the hub does for this answer, in order; streamed as ``activity``
    # events and stored with the answer
    activity: ActivityLog = field(default_factory=ActivityLog)
    # the hub started this answer (a finding of the second look), the user did not ask for it
    from_hub: bool = False
    # a thorough research started as a job: its watcher writes the progress and the end, so the
    # run must not settle the message (it would show "error" in between)
    research_job: bool = False
    # the question a "ja bitte" accepted (the offer first, the request before it as context)
    accepted_question: str = ""
    # a "ja bitte" to an offer to explain: answered without a check, told what the yes agreed to
    accepted_hint: str = ""
    # where the conversation was started (a desktop window, conversation_context.py)
    context: dict[str, Any] | None = None
    # the state-changing actions this user may have proposed in this conversation's scope
    # (ToolService.proposable_tools): an offer to change something stays only if one does it (#223)
    actions: tuple[dict[str, Any], ...] = ()
    # the conversation as a check's task reads it (task_pairs): answers without report cards
    task_history: list[tuple[str, str]] = field(default_factory=list)
    # D125: the history with the images where they stand (only when a model that sees answers),
    # the bytes of the images that go to it by id, and what happened to them (``meta.vision``)
    vision_history: list[tuple[str, str]] | None = None
    image_data: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    vision: dict[str, Any] | None = None
    vision_chunk: str = ""  # the activity note on it, sent right after ``meta``

    @property
    def last_user_text(self) -> str:
        if self.accepted_question:
            return self.accepted_question
        return next((c for r, c in reversed(self.history) if r == "user"), "")


async def prepare(
    request: Request,
    user: CurrentUser,
    conversation_id: uuid.UUID,
    content: str | None,
    *,
    continue_last: bool = False,
    hidden: str | None = None,
    images: list[chat_images.Encoded] | None = None,
) -> Turn:
    """Store the user's message (or drop the last answer to regenerate it, or pick up the
    truncated one to continue it) and collect what the model needs. ``hidden``: the hub speaks up
    (:func:`speak_up`) -- a note for the model that is not stored and not shown as a message of the
    user. Raises HTTPException for unknown conversations / nothing to answer."""
    api = router()
    started = time.monotonic()
    async with request.app.state.sessionmaker() as db:
        store = ChatStore(db, user.uuid)
        conv = await store.get_conversation(conversation_id)
        if conv is None:
            raise api._http(404, "not_found", "unknown conversation")
        messages = await store.messages(conversation_id)
        new_user_message = None
        continues: Message | None = None
        if continue_last:
            if (
                not messages
                or messages[-1].role != "assistant"
                or messages[-1].status not in ("truncated", "interrupted")
            ):
                raise api._http(409, "nothing_to_continue", "the last answer is not truncated")
            continues = messages[-1]
        elif content is not None:
            if not messages and not conv.title:
                await store.update_conversation(conversation_id, title=title_from(content))
            msg = await store.add_message(conversation_id, "user", content)
            added = (
                await chat_images.ChatImageStore(db, user.uuid).add(conversation_id, msg.id, images)
                if images
                else []
            )
            messages.append(msg)
            new_user_message = api.message_view(
                msg, [chat_images.image_view(r.id, r.width, r.height) for r in added]
            )
        elif hidden is not None:
            if not messages:
                raise api._http(409, "nothing_to_answer", "the conversation is empty")
        else:  # regenerate: everything the last question got — bubbles, progress line, report card
            while messages and messages[-1].role == "assistant":
                await store.delete_message(conversation_id, messages[-1].id)
                messages.pop()
            if not messages or messages[-1].role != "user":
                raise api._http(
                    409, "nothing_to_answer", "the conversation has no message to answer"
                )
        pictures = await chat_images.ChatImageStore(db, user.uuid).of_messages(
            [m.id for m in messages if m.role == "user"]
        )
        chain_ids = await api._chain_ids(store, conv)
        d = await api._defaults(db, request, store)
        account = await db.get(User, user.uuid)
        language = (account.language if account else None) or "en"
        # the application's tools are the most this conversation may use (app_scope.py)
        limit = await app_scope.ceiling(db, user, conv.application)
        await db.commit()
    scope = app_scope.cap(tool_groups.resolve_scope(conv.tool_scope, d["user"]["internet"]), limit)
    level = quality.level_of(conv.quality or d["user"]["quality"])
    chosen = quality.models_for(level, conv.model, d["effective_model"])
    model = chosen["answer"]
    policy = await chat_policy.load(request.app.state.sessionmaker, level)
    requested = f"{model}@{conv.node}" if conv.node else model
    chain_from = chain_state = None
    if conv.model is None and conv.node is None and not continue_last:
        # no pick of the person: the answer role of this application (model_resolver.py); with
        # no list ordered, or none of its nodes able to answer now, it stays the default model
        choice = await request.app.state.gateway.resolver.resolve(
            "answer", application=conv.application, fallback=model, if_none="default"
        )
        model, requested = choice.model, choice.requested
        chain_from, chain_state = choice.fallback_from, choice.fallback_state
    if policy.node and "@" not in requested:  # placement by the policy, unless chosen
        requested = f"{model}@{policy.node}"
    segment_block = ""
    if request.app.state.settings.main_thread_enabled and not continue_last:
        segments = request.app.state.segments
        plan = await segments.plan(
            user.uuid,
            conversation_id,
            messages,
            next((m.content for m in reversed(messages) if m.role == "user"), ""),
            SEGMENT_BLOCK_TOKENS,
            parse_breaks(conv.segment_breaks),
            chain_ids,
        )
        segment_block = plan.block  # the running summary of the chat: never waited for
        messages = plan.messages  # only the current segment is replayed verbatim
    # D125: the images of the replayed messages; a model that cannot see reads that one was there
    shown = {m.id: pictures[m.id] for m in messages if m.role == "user" and m.id in pictures}
    history = api.history_pairs(
        messages,
        keep_last=continue_last,
        extra={k: chat_images.hidden(len(v)) for k, v in shown.items()},
    )
    vision_history: list[tuple[str, str]] | None = None
    image_data: dict[str, tuple[str, bytes]] = {}
    vision: dict[str, Any] | None = None
    if shown and not continue_last:
        sight = await chat_images.sight(
            request.app.state.gateway, requested, picked=bool(conv.model or conv.node)
        )
        if sight.sees:
            model, requested = sight.model, sight.requested
            newest_first = [
                (i["id"], i["bytes"]) for k in sorted(shown, reverse=True) for i in shown[k]
            ]
            async with request.app.state.sessionmaker() as db:
                image_data = await chat_images.ChatImageStore(db, user.uuid).data(
                    chat_images.pick_for_prompt(newest_first)
                )
            vision_history = api.history_pairs(
                messages,
                extra={k: chat_images.marked([i["id"] for i in v]) for k, v in shown.items()},
            )
        vision = {
            "sees": sight.sees,
            "model": sight.model,
            "images": len(image_data),
            **({"routed_from": sight.routed_from} if sight.routed_from else {}),
            **({"node": sight.node} if sight.node else {}),
        }
    for_task = api.task_pairs(messages)
    if hidden is not None:
        history.append(("user", hidden))
        for_task.append(("user", hidden))
    last_answer = next((m for m in reversed(messages) if m.role == "assistant"), None)
    last_refs = tuple(
        uuid.UUID(r["id"])
        for r in (last_answer.memory_refs or [] if last_answer else [])
        if isinstance(r, dict) and isinstance(r.get("id"), str)
    )
    care_asked = any(
        isinstance(r, dict) and r.get("ask")
        for m in messages
        if m.role == "assistant"
        for r in (m.memory_refs or [])
    )
    memory_on = tool_groups.memory_on(scope, d["user"]["memory_enabled"])
    second_look = policy.wants_second_look(level)
    verify_model = None
    if second_look:
        verify_model = model
    turn = Turn(
        conversation_id=conversation_id,
        requested=requested,
        model=model,
        history=history,
        user_prompt=d["user"]["system_prompt"],
        address=d["user"]["address"],
        capabilities=capabilities(request, memory_on),
        memory_enabled=memory_on,
        temperature=d["user"]["temperature"],
        new_user_message=new_user_message,
        started=started,
        continues=continues,
        last_refs=last_refs,
        care_asked=care_asked,
        quality=level,
        verify_model=verify_model,
        policy=policy,
        second_look=second_look,
        scope=scope,
        internet=scope["internet"],
        language=language,
        segment_block=segment_block,
        chain_from=chain_from,
        chain_state=chain_state,
        picked=bool(conv.model or conv.node),
        application=conv.application or "chat",
        in_program=conv.application is not None,
        context=conv.context,
        task_history=for_task,
        vision_history=vision_history,
        image_data=image_data,
        vision=vision,
    )
    if vision is not None and (not vision["sees"] or vision.get("routed_from")):
        # the chat says what happened to the images: another model looked, or none could
        event = "vision_routed" if vision["sees"] else "vision_blind"
        where = f"{vision['model']} @ {vision['node']}" if vision.get("node") else vision["model"]
        _, turn.vision_chunk = turn.activity.add(
            "note", where, status="info", data={"event": event}
        )
    return turn


def capabilities(_request: Request, memory_enabled: bool) -> chat.Capabilities:
    """What the assistant can do in a chat right now: text, and its notes about the user if
    memory is on for this user. Later stages (knowledge and web search, tools) switch their
    entries on here, per user."""
    return chat.Capabilities(memory=memory_enabled)
