# Overview: how the parts fit together (state of 2026-10-30)

One page for someone who has not read the decisions in [ARCHITECTURE.md](ARCHITECTURE.md). When
this page and an old decision disagree, this page is current; the decision list is the history and
the reasons. The planned change of model choice and queues is in [SCHEDULING.md](SCHEDULING.md); the
section on roles below stays true until it is built.

## The parts

| part | what it is | where |
|---|---|---|
| **hub** | Python (FastAPI, SQLAlchemy, Postgres + pgvector). One process: API, the node sessions, the model gateway, the agent, memory, knowledge, flows, background work. | `hub/` |
| **node** | One static Go binary per machine. Connects out to the hub, runs `llama-server` for models, offers named read-only capabilities (tool nodes) or the dev workshop. | `node/` |
| **web** | React + TypeScript, served by the hub. One desktop with programs. | `web/` |
| **proto** | JSON Schemas of the node protocol and the shared UI data (modules, applications). | `proto/` |

## Who talks to whom

```
 browser ──HTTPS: REST + one SSE stream (/api/stream), answers as SSE──▶ hub ◀──── Postgres
 OpenAI clients ──HTTPS /v1 (tc_ keys)──────────────────────────────────▶ hub
 node ──outbound WebSocket (wss://hub/api/node/ws, JSON envelope, ed25519 signed nonce)──▶ hub
 hub ──action / proxy_request over that same WebSocket──▶ node ──loopback HTTP──▶ llama-server
 node ◀──one-time TLS socket, pinned certificate, only while a model transfer runs──▶ node
 hub ──GET only──▶ GitHub (read tool)          hub ──via a tool node──▶ Docker, journal, Kuma, Omada, Keycloak, web
```

* Nodes never listen (except the transfer socket) and never run a free shell: the hub sends a
  *named action* from the whitelist the hub assigned to that node, validated against a schema.
* A model request goes hub → node session → `llama-server` on loopback; the hub never reaches a
  model server directly.
* Nothing is sent to an external model. (The escalation was removed in consolidation step 6.)

## One answer, step by step

1. The browser posts the message; the hub stores it and starts **one hub task per answer**
   (`chat_runs`); the browser is only a reader and can leave and come back (D39).
2. `api/chat._prepare` collects history, persona/core block (D38), policy, the application's tool
   ceiling (D69) and asks **`model_resolver`** which node and model (below).
3. Recall of notes (`memory`), optionally a read-only check of the servers (`agent_loop`, evidence
   tagged live/doc/web), then the model's answer streams through `gateway` → node → `llama-server`.
4. Afterwards, in the background: note extraction, the running summary (digest, whose key terms a new chat reads, D100), the second look.
   Every claim the hub makes about its own work (what was remembered, what was checked) comes from a
   fact in the hub, not from the model's words (D42). The same holds the other way round: when the
   closing answer of a check says that something was changed or is ready to confirm, the hub compares
   it with its record (`claims.py`) and writes the true state under it, with the refused calls and their
   reasons; a check the hub did not start (its task limit) is stated as that, not as a failed check.
   The system prompt names the program, the model and the machine the answer is made on (`_where` in
   `chat_engine/answer.py`; the hardware only for an admin), so the model can say where it runs.

## What the agent can see of a machine

A machine with a tool node is looked at through its capabilities. A machine without one, a model node,
is described by `topcoat.state` with `what=nodes` (admin only): state, memory, load, GPUs, the models it
has loaded and whether it is really connected. The agent prompts say so; an operation of a change set that
does not fit its schema is answered with the allowed keys and one valid example.

## Which node and model: roles (D78) and context (D79)

Four **roles** — `answer`, `agent`, `background`, `check` — each an ordered list of
`(node, model)`; the model may be `auto` (the largest model on that node still fast enough for the
role, by measured speed). An application may have its own lists. `model_resolver.resolve()` is the
only code that answers the question and returns the reason. Settings: page *Administration → Models
and roles*; stored in `hub_settings.node_policy`.

The hub says only what a use *needs* (`min_context_length`); the node sizes the context itself
(`memfit.AutoContext`) and reloads when a loaded model has too little and the node is idle.

The queue (SCH-03, D94): every node has one queue in the hub, as large as the node says (the sum of `n_parallel`
of its loaded models), served by place (chat, after an answer, other) and arrival; a chat answer lets the gateway
choose the node and load the model before its prompt is built. Every request leaves a row in `job_records`
(D95), and `GET /api/activity` lists the running and the waiting requests. Known gaps of today: a list only
decides who may answer, not who has room, so a request is still bound to its node when it enters that node's
queue (late binding comes with the table, SCH-04), and the lists per job are still the four roles.

A summary is background work: it uses the model of the role `background` (the flow may name another) and
obeys `no_background`. A check (the second look) never loads a model that pushes another out: it takes a
loaded model on a node that accepts work, another node before the author's node, another model before
the author's own, and the author's own model when it is the only one (D87, D88); the review and the digest
store the node of the check, and the chat marks a check by the model that wrote the answer. The speed of
a node is measured from the first token of any kind, the thinking included.

## Context of a request

Every model request is complete in itself: the hub stores the conversation and builds the whole prompt
each time (`chat.build_prompt`, sized by the context of the node that was chosen for the answer, D94). A llama-server slot
has its own window; its prompt cache saves time and holds nothing the hub depends on. A check and a
summary build fresh prompts from the database and do not continue the author's context.

## Machines, nodes and what they show

A node is a program on a machine. *Administration → Machines* shows the physical machines, each
with the utilisation of its CPU and GPU (a graph with the last half hour) and its RAM and VRAM
(bars); virtual machines hang below the host they run on, and the tool, dev and compute nodes below
the machine they run on. A node belongs to the machine its name points at (`tools-alpha` runs on
`alpha`); an admin names the machines, marks a VM and its host, and moves a node, on the same page
("Arrange machines"; stored as the hub setting `machines`, `hub/src/topcoat_hub/machines.py`). The
CPU figure is the heartbeat field `cpu_util` (all logical processors together; a node that cannot
measure it, or an older one, sends none and the page says so).

## Hand-overs and their Akte

Every hand-over from one application to another keeps a file (an *Akte*, `handover_file.py`): the
request as the person saw it, the steps, and the report with its sources: each finding with the
marks of its evidence and the passage that shows it, what could not be looked at, every tool call
with machine, input and excerpt. A finished one is also filed as Markdown in the person's private
knowledge source "Akten", so a later question finds it.

## The desktop

Programs (Chat, Research, Development, Administration, Projects, Incognito, Settings) are built-in
workspace templates (`hub/.../ui/seeds.py`); each lists its views. Chat's rail: inbox, the chats of
every application, incognito, hand-overs, settings, shelf, memory. A chat window is one calm view
(`talk/TalkPage`): stop, answer again, continue, effort, internet, export. Details of the look are
data plus CSS (`docs/DESKTOP.md`, and the theme plan in `docs/THEMES.md`).

## Rules that hold everywhere

`user_id` on every personal row and every query (D20); the assistant reads, a person confirms one
concrete change at a time (D15, D43); applications are a boundary: their own memory, tools, models
and a hand-over the person reads before it is sent (D65, D69, D74); the applications are one list,
`proto/ui/applications.json`.

## Operating the production hub

* Image: `scripts/build-hub-image.sh` → `deploy/update-hub.sh` (dump first, health check, way back).
* Backups: `deploy/backup-hub.sh` and the systemd timer in `deploy/`; the TLS volume is part of it.
* Node version: `VERSION` → `scripts/node-version.sh`; updates are signed (D50).
* Retention: node events 90 days, the audit log of capability calls 365 days (`retention.py`).
