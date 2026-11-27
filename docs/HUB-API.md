# Hub behaviour and web API

> Split out of [PROTOCOL.md](PROTOCOL.md) on 2026-10-30; the text is unchanged. Each section says what the
> hub does on top of the node protocol (agent, proposals, memory, knowledge base, model gateway, message
> kinds) and which HTTP endpoints the web GUI uses. The node side is in [PROTOCOL.md](PROTOCOL.md).

### The agent (E3-01a, ARCHITECTURE D30)
A background task per user in which the model looks at servers through the read-only capabilities
the user may run and writes a diagnosis. Nothing in it changes state (proposals and confirmed
execution are E3-01b, below). Node protocol: unchanged for reading — the agent uses `capability.invoke` through the
same hub service as the GUI ("Tool nodes").

- **REST** (any user, also with an API key — but never with admin rights for a key, D29; every route
  is scoped to the caller, another user's task is `404`): `POST /api/agent/tasks {prompt (1–4000), application? (`research` only: the program's tool ceiling, a thorough one ends as a report in the Ablage), depth? (`quick|default|thorough`, with `research`),
  model?}` → `201` task (`409 no_capabilities` if the user may run nothing on a connected tool
  node, `429 too_many_tasks`, `503 agent_disabled`); `GET /api/agent/tasks`;
  `GET /api/agent/tasks/{id}` → task + `steps` + `evidence`; `POST …/cancel`; `POST …/continue`
  (`409 not_resumable` unless the task is canceled, failed, or done with a `stop_reason`).
- **Task**: `status` `queued | running | done | failed | canceled`, `stop_reason` (`canceled`,
  `max_turns`, `max_tool_calls`, `time_budget`, `repeated_calls`, `bad_format`,
  `model_unavailable`, `no_tools`, `internal`), `summary`, `diagnosis {summary, findings[], next,
  notes[]}`, `step_count`, `tool_calls`, `runs`, `via` (`gui` | `api`), `model` (null = the hub's
  default chat model).
- **Finding** `{claim, tag, evidence[], confirm_with, downgraded?}`: `tag` is `live` (seen on a
  machine during this task — cites evidence refs that show it), `doc` (knowledge base, E3-03) or
  `assumption` (not seen; says which call would settle it). **The hub enforces the tags:** a finding
  tagged `live`/`doc` keeps the tag only with at least one cited, existing evidence of that kind;
  otherwise it becomes an `assumption` with `downgraded: true` (and a note); unknown references are
  dropped.
- **Evidence** `{ref (E1, E2 …), tag, source {node, capability, input, at, call_id}, excerpt
  (≤ 8000 chars), bytes}`: every successful tool result; a refused or failed call is an observation
  but no evidence (nothing was seen).
- **Steps** are the transcript: `assistant` (what the model said, raw), `observation` (what it was
  shown: a tool result with its `evidence` ref, an `error`, or a `note`), `note` (for the GUI). A
  task can be rebuilt from them.
- **The protocol with the model** is prompt-based, not the model's native tool calling, so it works
  with any instruct model: every reply is **one JSON object** — `{"action":"tool","node","capability",
  "input","reason"}` or `{"action":"finish","summary","findings","next"}`. The request also carries
  `response_format: {"type": "json_object"}` (UNVERIFIED on the pinned llama.cpp build; a model that
  answers 400/422 is asked again without it). A reply that is not a valid action is answered with
  the reason (three in a row fail the task with `bad_format`). What the model may use is listed in
  its system prompt: the capabilities the task's user may run on the connected tool nodes, nothing
  that changes state — a call for anything else is answered "you cannot use that" and never reaches a
  node.
- **Data, not instructions.** Every tool result reaches the model inside `<<<DATA nonce>>> …
  <<<END nonce>>>` (a fresh random nonce per result), declared in the system prompt to be data from a
  machine. Text in it cannot change the task, the tools or the rights; the loop has no tool that
  could act on it.
- **Limits of one run** (`TOPCOAT_AGENT_*`): 24 model turns, 16 tool calls, 600 s, 3500 characters of
  one tool output shown to the model (a log keeps its newest lines; the evidence keeps up to 8000),
  20 000 characters of prompt (older tool outputs are replaced by a line pointing at the evidence
  first), 3 active tasks per user, an identical call is answered from the evidence and three of them
  end the run. When a limit is hit the model is asked **once** to write its diagnosis with what it
  has (`done` with the `stop_reason`); if it cannot, the diagnosis says that none was written.
  `Continue` starts a new run with fresh limits and the old transcript.
- **Cancel and restart.** Cancel stores `canceled`, abandons the current model call and the wait for
  a tool, and lets nothing further be written; a tool call already sent to a node finishes there
  (read-only, time-limited). After a hub restart the tasks that were queued or running continue
  from their last finished step (an answer that asked for a call the hub never made is acted on
  first); the tool nodes need a moment to reconnect, so the run waits up to
  `TOPCOAT_AGENT_TOOLS_WAIT_SECONDS` (60) for a usable tool before failing with `no_tools`.
- **Rights.** The agent acts with the rights of the task's user (D20): the capabilities it offers are
  `ToolService.usable_tools` for that user — the admin on the GUI: every read-only capability of a
  connected tool node; everybody else, and every task started with an API key: their grants. Every
  call is in the audit log (`tool_calls`, `via: agent`, `task_id`).
- **Live progress.** SSE events `agent_task {id, status, step_count}` and `agent_step {task_id, seq}`
  on `/api/stream`, delivered **only to the owner's streams** (the event carries a hub-internal
  `_user` that is filtered out and never sent); the GUI refetches on them and polls as a safety net.

### Proposals and confirmed execution (E3-01b, ARCHITECTURE D43)
The agent never changes anything. After a diagnosis it may attach **proposals**; a proposal runs
only after the signed-in user's click on that one proposal. Off by default
(`TOPCOAT_PROPOSALS_ENABLED=true` on the hub, plus the node settings below).

- **Node.** Two state-changing capabilities, `read_only: false` in the inventory, offered only
  where `config.tools` names what may be restarted (see "Tool nodes"): `docker.restart
  {container, timeout_seconds?(1–60)}` and `service.restart {unit}`. `capability.invoke` never runs
  them (`not_allowed`). **`capability.execute {capability, input, proposal, hash}`** runs exactly
  those, and only where `allowed_actions` contains `capability.execute` and the capability is in
  `config.capabilities`; a read-only capability is refused here (`not_allowed`), a missing or
  malformed `proposal`/`hash` is `invalid_args`. The node checks glob list, input and Docker proxy
  again, logs `proposal` and `hash` next to the action, and answers like `capability.invoke`.
  Neither capability is in the default tool-node config: an admin adds them (and the action) by
  editing the node's config.
- **Stacks (`compose.restart`).** The third write capability restarts a whole compose project
  (`project`, `timeout_seconds?`): every container of it that is *running* is restarted one after
  the other, those another container depends on first (the `com.docker.compose.depends_on` label;
  ties and cycles fall back to the name), stopped containers stay stopped (`docker restart` would
  start them), at most 20. It adds **no Docker endpoint**: it is a series of the same single
  restart call, so the write proxy and its tests are unchanged. The gate is all-or-nothing:
  **every** running container of the project must match `restart_containers`, otherwise the whole
  stack is refused before anything is touched. It stops at the first failure and the error says
  which containers were restarted and which failed. It is offered exactly where `docker.restart`
  is (write proxy and list set), and an admin adds `compose.restart` to `capabilities` like the
  other write capabilities.
- **Manual proposals (the inventory's buttons).** The person can ask for a restart themselves:
  `POST /api/agent/proposals/manual {node_id, capability, input}` (signed-in user only, `403
  gui_only` for any API key; `403 proposals_off`; `404 not_found` for what this user may not run
  there; `422 invalid_input`). The answer is an ordinary **pending proposal** (`origin: "manual"`,
  `task_id: null`, no evidence) whose `reason`/`effect`/`who_notices`/`way_back` are the **hub's own
  templates** in the user's language — nothing from a log or page can be in them. It is hashed,
  expires like any other, and runs only on the second click on `…/execute`, with the same checks
  and audit (`via: "proposal"`, no task). Asking for the same change twice keeps one open
  proposal. `GET /api/agent/proposals/{id}` reads one proposal of the caller. Manual proposals are
  not in `GET /api/agent/proposals/pending` (the card is open in front of the person). Offered for
  `docker.restart`, `compose.restart` and `service.restart`. `GET /api/inventory` adds `can:
  {read: [...], change: [...]}` per node: the log and change capabilities *this caller* may use
  there right now (connected node, grants, proposals on, node allows `capability.execute`).
- **Hub.** `agent_proposals` (per user, `user_id` on every row and query): node, capability, input,
  `reason`, `effect`, `who_notices`, `way_back`, evidence refs, `content_hash` (SHA-256 over all of
  it), `status` `pending | running | executed | failed | refused | expired`, `expires_at`
  (`TOPCOAT_PROPOSAL_TTL_SECONDS`, 1800), `result`, the audit row. The model proposes inside
  `finish` (`proposals: [{node, capability, input, reason, effect, who_notices, way_back,
  evidence}]`, at most `TOPCOAT_PROPOSALS_MAX_PER_TASK` = 3 kept). The hub **checks each one
  mechanically** and drops the rest with a note in the diagnosis: the capability is one the task's
  user may confirm on that node (admin on the GUI, or a grant that names the capability — `*` does
  not cover writes); the input fits the schema; all four texts are non-empty; it cites live
  evidence; and what the input names (a container, a unit) appears in the output of a *listing or
  status* tool among that evidence (`docker.ps`, `docker.inspect`, `service.status`,
  `service.failed`, `compose.projects`) — a line in a log that says "restart X" cannot put X into a
  proposal. Only tasks started on the tasks page get proposals (not chat checks). A new diagnosis of
  the same task expires its older pending proposals.
- **REST** (owner only; another user's proposal is `404`): `GET /api/agent/tasks/{id}/proposals`
  (and `proposals` in `GET /api/agent/tasks/{id}`); `POST /api/agent/proposals/{id}/execute` and
  `…/refuse` — **an API key is refused (`403 gui_only`), an admin's too.** Execute answers
  `403 proposals_off`, `409 not_pending` (already decided, running, refused, expired — a second
  click, or two at once, runs it once), `409 content_changed` (the row no longer matches its hash:
  not run). Otherwise `200` with the proposal: `executed`, or `failed` with the node's reason. The
  run is audited in `tool_calls` with `via: "proposal"` and the task id, and the task gets a note
  step. Event `agent_proposal {id, task_id, status}` (owner only).
- **No blanket approval**: there is no "run all", no "remember my answer", no setting that
  confirms for the user, and nothing the agent reads can reach the execute route.
- **Follow-up.** "Did it help?" is an ordinary read-only task the GUI starts from the executed
  proposal.

### The coordinator (E3-08, ARCHITECTURE D44)
`POST /api/agent/tasks {prompt, model?, coordinator: true}` starts a task of `kind: "coordinator"`
(`503 coordinator_disabled` when `TOPCOAT_COORDINATOR_ENABLED=false`; the other refusals are those
of a normal task). It looks at nothing itself: it splits the question across the machines the user
may use, starts one read-only check per machine, waits and merges. Node protocol: unchanged.

- **Task fields**: `kind` (`single | coordinator`), `parent_id` (set on a check). A check is an
  ordinary task with `origin: "child"`, `scope {nodes: [one machine], internet: false}` and tighter
  limits; it is not on the Tasks list but readable like any task of the owner (another user: `404`).
- **`GET /api/agent/tasks/{id}`** of a coordinator adds `children: [{id, node, question, status,
  stop_reason, summary, step_count, tool_calls}]`. The steps hold the plan (`Plan: …`, with the
  list in `data.plan`) and one note per started check.
- **Result**: `diagnosis.findings[]` are the checks' findings, each with `node`, its evidence refs
  re-pointed at the evidence the coordinator copied from the checks (`source.child` = the check);
  `summary` and `next` come from one merge call (a join of the checks' summaries when that fails);
  `notes` list checks without a result. `status` is `done` when at least one check delivered, else
  `failed` with `stop_reason: "children_failed"`; `stop_reason: "time_budget"` when the budget cut
  unfinished checks off.
- **Limits** (settings): `TOPCOAT_COORDINATOR_PARALLEL` (2), `…_MAX_CHILDREN` (6),
  `…_TIME_BUDGET_SECONDS` (900), `…_CHILD_TURNS` (12), `…_CHILD_TOOL_CALLS` (8),
  `…_CHILD_TIME_SECONDS` (300). Cancel on the coordinator cancels every check; a hub restart
  continues the checks and the coordinator picks them up from its stored plan.

### The chat checks servers (E3-01d, ARCHITECTURE D31)
`POST /api/conversations/{id}/messages` takes `tools`: `auto` (default), `on` or `off`
(`POST …/regenerate?tools=`). **`auto`** runs a server check before the answer when the question
is about the user's machines and the user may use at least one capability on a connected tool
node; **the model decides** (E3-04): in `auto` mode a model that makes native tool calls (the
agent's tool mode for it is not `json`, its server accepted `tools` before) is offered one function,
`look_at_my_systems {what?}`, in the answer's own stream (`tool_choice: auto`, one function instead
of every capability keeps the prompt small). If it answers in text, that stream is the answer, as
before; if it calls the function, the browser gets `discard` (drop a lead-in the model said before
it called), then the check runs (`meta` with `checking: "decided"`, the steps) and the answer follows
from it; the model's `what` is added to the check's task. A server that answers 400/422 to `tools`
is asked again without them and remembered per model until the hub restarts. The system prompt lists
**every group of tools the user may use** (Docker, systemd/journal, host health, Omada, Uptime Kuma,
DNS/ping/certificates, the notes, the web — `tool_groups.py`) so the assistant never says it has no
access to something that is offered, and names the function. A node name in the message still starts
the check at once (no extra model turn). For a model that cannot call functions the old heuristic
decides, with three outcomes. **Check:** a
tool node's name or its last part (unless that is itself a word like *docker*), or the user's own
systems (*mein/unser/bei mir/auf dem Server*) with a question about state or a fault or a request to
look (*läuft, warum, Fehler, prüf, welche … laufen*), or such a question about the machines *right
now*. **Offer:** the question mentions the machine world but does not say whose or what is wanted
("Warum startet der Container neu?"): it is answered plainly, `meta` carries `check_offer: true`
and the browser shows "Check on my servers" under the answer (that sends "Please check this on my
servers." with `tools: "on"`). **Nothing:** a general question or a how-to ("was ein Docker-Container
ist", "wie installiere ich …", "what is …"): no check, no offer — a keyword such as *docker* alone
never starts a check. **`on`**
always checks (409 `no_capabilities` if the user may use no tool; `auto` then answers plainly),
**`off`** never. A continuation never checks.

The check is an agent task (E3-01a, the user's own rights, audited, `origin: "chat"`, shorter
limits: 10 turns, 8 calls, 150 s; on a slow stand-in machine only 5 turns, **2 calls**, 200 s,
smaller excerpts) started by the hub; the chat stream then reports it, **before**
the answer is streamed:
`meta` (with `checking: "forced" | "detected" | "decided"`, and — when the machine the user would expect is not
available — `fallback_from`/`fallback_state` with the machine that does the work instead and
`check_max_calls` (2): the browser warns **before** the check that it is slower and limited),
`tools_start {task_id}`, `tool_step {seq, kind: plan|call|note, …}` (a plan is the model's reason
for a call; a call has `capability`, `node`, `input`, `ok`, `ref`, `code`), `tools_wait` (keep-alive),
`tools_done {task_id, status, stop_reason, error, reason, calls, evidence[]}`, then `delta`…/`done`
as usual. `reason` is the node's refusal (`insufficient_memory`) if the model could not be loaded.

The **answer** is written from the check's findings and evidence excerpts (in a `<<<DATA>>>` fence,
declared data) placed before the user's message; the system prompt says how to tag: `[live]` with
the evidence (E1) for what a result shows, `[doc]`, `[assumption]`; general knowledge gets no tag
and is never `[live]`. Unlike notes from the memory,
these tags stay in the answer. If the check could not be made the block says so and the model is
told not to guess. A check that **failed after it had collected evidence** (a cut-off diagnosis, a
time-out) is *incomplete*, not absent: the block says so, lists what was seen, and the answer
starts by saying the check is incomplete and then uses the evidence (the panel reads "Check
unvollständig, das wurde gesehen" with the evidence below it). **Nothing is learned into the memory** from such an answer (the facts are the
servers', not the user's). The stored assistant message carries `agent_task_id`; steps and evidence
are read with `GET /api/agent/tasks/{id}` (the task is not in the task list unless
`?include_chat=true`). The user pressing stop during the check cancels the task.

### Quality levels of the chat (E3-04)
`conversations.quality` (`fast` | `balanced` | `thorough`, `null` = the user's default),
`user_settings.chat_quality` (default `balanced`; `PUT /api/chat/settings {quality}`, a save without
it keeps the stored one), `PATCH /api/conversations/{id} {quality}` (null resets),
`PUT /api/hub/settings {quality_models: {fast?, balanced?, thorough?}}` (admin; the model per level,
only touched when present; `{}` clears). `GET /api/chat/settings` returns `user.quality` and
`hub.quality_models`; the conversation view `quality`; `meta` of an answer `quality` and, at
`thorough`, `verify_model`.
- **fast** — the model of the level (if the conversation has no model of its own), a check of at most
  2 tool calls and 4 model turns, no second look.
- **balanced** — the checks as before (10 turns, 8 calls).
- **thorough** — 16 turns, 14 calls, 300 s, and after a complete checked answer the **big model**
  (`quality_models.thorough`, else the answer's model; the machine that holds it, the deep role) reads
  the answer once more against the evidence. SSE after `done`: `verifying {model}`, then
  `verification {message_id, available, ok, issues[≤4], model, at}`; the result is
  stored with the answer (`messages.verification`). A verifier that is not reachable or
  does not answer with the JSON object `{supported, issues}` leaves the answer without a
  verification (`available: false`).
- A model picked in the conversation always wins for the answer and the check; on a slow stand-in
  machine the slow limits (2 calls) cap every level.

### Which servers and tools the assistant may use in a conversation (E3-04)
`conversations.tool_scope` = `{"nodes": [name…] | null, "groups": [key…] | null, "internet": bool |
null}`, `null` (or all three `null`) = every server and group the user may use and the user's default for
the internet. Group keys (`tool_groups.py`): `docker` (docker.*, compose.*), `journal` (journal.*,
service.*), `host` (system.*, disk.*, health.*, logs.*, inventory.*, files.*, windows.*), `network` (omada.*), `monitors` (kuma.*),
`reach` (certs.*, net.*), `notes` (docs.*, knowledge.*). **The web (`web.*`) is not a group**: it is the
`internet` switch (below), so choosing servers or groups never removes it (`web` in `groups` is dropped).
- `GET /api/chat/tools` → `{groups: [{key, label}], nodes: [{name, groups[]}], internet: {node}}`: what
  the user *could* choose — the groups and nodes of the capabilities they may run (their grants, D20);
  a node that only offers the web is no "server". `internet.node` is the node web search comes from
  (the hub's pick), `null` when no node is released for it to this user.
- **Internet** (E3-04): `tool_scope.internet` is per conversation (`null` = the user's default,
  `user_settings.internet`, default `true`, set with `PUT /api/chat/settings {internet}`; `null` keeps
  it). When on, the assistant is offered the web capabilities of **one** node the hub picks among those
  released to the user (most web capabilities, then by name), whatever servers are chosen; when off, no
  web capability and the assistant is told that Internet is switched off here (so it can say so and
  point to the switch instead of claiming it cannot). The internet choice is not part of "the last
  choice" new conversations start with. `meta` carries `internet` and, when on, `internet_node`; the
  panel line shows *Internet: on (via node) / off*. Data from before (migration 0021): a conversation
  whose groups lacked `web` keeps the internet off.
- `PATCH /api/conversations/{id} {tool_scope}` (`null` = everything; unknown keys are 422),
  `POST /api/conversations {tool_scope}`; the last choice is stored in `user_settings.tool_scope`
  and is the start of a new conversation that names none. Per user (D20).
- The scope is applied **after** the grants and only ever removes: to the assistant's description of
  what it can do and to the `look_at_my_systems` function, to the check task (`agent_tasks.scope`,
  read by the loop on every turn — a tool outside the scope is "you cannot use that" and never reaches
  a node), and a scope that leaves nothing is a plain chat (a forced check answers 409
  `no_capabilities`; with only the internet left the assistant still has the web). `meta` carries
  `tool_scope` of an answer whose servers or groups are restricted; the chat shows it.

### The knowledge base (E2-03 slim, ARCHITECTURE D34)
Notes are indexed **through a tool node**. New capabilities (read-only, offered only when
`config.tools.docs_roots` releases a directory — default deny; `config.tools.docs_exclude` adds
globs to the fixed exclude list):

| capability | input | output |
|---|---|---|
| `docs.list` | `root?` (one of `docs_roots`, optional with a single root), `limit?` (≤ 1500, default 500), `after?` (path cursor) | `files[]` (`path` relative with `/`, `size`, `mtime`), `total`, `excluded` (a count, never names), `truncated`, `next_after`, `walk_limited` |
| `docs.read` | `root?`, `path`, `offset?` | one page (≤ 24 KiB, cut at a line and at a character): `path`, `offset`, `size`, `mtime`, `content` (secrets masked), `masked`, `next_offset` (null at the end) |

Only `.md`, `.markdown`, `.txt`; links are never followed and nothing outside the root is read;
the path is refused with one fixed message whether it is excluded, missing, a link out or not text.
The **exclude list** (any path segment, case-insensitive glob): `*SENSIBEL*`, `*ZUGANGSDATEN*`,
`*.key`, `*.pem`, `*.env`, `.env*`, `*password*`, `*passwort*`, `*secret*`, `*credential*`,
`*.kdbx`, `id_rsa*`, `id_ed25519*`, `.git` — on the node **and** in the hub
(`knowledge_rules.py`), which never asks for such a path, never indexes one, and drops a hit whose
path matches the list even if it grew after indexing.

Hub: sources (`knowledge_sources`: owner, name, `shared`|`private`, node, root, status, counts), files
(size, mtime, content hash, the *rule names* of held-back chunks) and chunks (heading path, text,
vector or NULL). Chunks follow Markdown headings (≤ 1200 characters); the secret filter runs on every
chunk — a chunk with a password/key/token is **not indexed** (only a long mixed token, common in
URLs, is replaced by a marker); a change is found by size/time and confirmed by a content hash, a
vanished file drops its chunks; a run also fills missing vectors (embedding model was down, or
changed). Search is hybrid: Postgres full-text (stop words dropped) and cosine distance, rank-fused;
while `TOPCOAT_EMBEDDING_MODEL` is unset the word-hashing stand-in is used (the GUI says so).

| route | |
|---|---|
| `GET /api/knowledge/sources` | the sources the caller may search (own and shared); `root`/`error` only for the owner |
| `POST /api/knowledge/sources` | admin (dev token): `{name, node, root?, visibility}`; asks the node once (`docs.list`), 404 `unknown_node`, 422 `root_refused`; indexing starts in the background |
| `GET /api/knowledge/sources/{id}` | one source; the owner also gets `blocked_files` (`path`, `rules`, `chunks`: what the secret filter held back) |
| `POST /api/knowledge/sources/{id}/reindex`, `DELETE …` | admin, owner only |
| `GET /api/knowledge/search?q=&limit=&source_id=` | hits: `source`, `path`, `heading`, `text`, `mtime`, `via` (`directory`\|`search`), then `score` (search) or `entity` and `lines` (directory) — only from sources the caller may see; `directory` (`file`, `source`, `age_days`, `entries`, `matched`, `note` when stale) when a directory was found |
| `GET /api/knowledge/status` | embedder id, `transitional`, the effective exclude list, `hit_chars` (the most of a hit shown), `blocked` = `{chunks, files[{source, path, rules, chunks}]}` for the caller's own sources (never the content) |

**The directory (E3-04, `10_KI_Verzeichnis.md`).** A file of that name anywhere in a source (the
shortest path wins) maps hosts and services to the sections of the notes about them. It is
generated (`scripts/dev/make_kb_index.py`) and parsed by `knowledge_directory.py`; the format:

```
## node-docker
aliases: docker host, dock
- `03_Server/node-docker.md:120-180` Docker stack
- `04_Dienste/kuma.md` § Uptime Kuma einrichten
- node-docker -> a.md:1-9, b/c.md           (one-line form, also accepted)
```

An entry is a `##`/`###` heading, an optional `aliases:` line and list items naming a note with
lines (`path:120-180`, `path (Zeile 120-180)`, `path Z. 7–9`) or a heading (`§ Name`); a `#`
heading, fenced blocks and `<!-- -->` lines are ignored. `docs.*` first looks for the entries a
question names (a name or alias of at least 3 characters as a whole word, longest first, at most 3
entries), reads exactly the listed sections (at most 6, the line range or the section under the
heading — not whole files) and puts them first (`via: "directory"`, with `entity` and `lines`); the
similarity search fills the remaining places and a section is never listed twice. Only what the
node listed is read: a path that is not in the source's file list, that the exclude list matches, or
that is unsafe (`..`, absolute, backslash) is skipped without asking the node, and every section goes
through the secret filter (one with a secret is dropped). The directory file itself is read for the
lookup but not indexed as a note. Its age (`mtime`) is reported as `directory.age_days`; older than
`TOPCOAT_KNOWLEDGE_DIRECTORY_STALE_DAYS` (14) adds a `note` that newer sections are not listed.

**Contradictions.** A note may be out of date. The agent's finish schema has an optional
`contradicts` (references of `doc` evidence) per finding; it is kept only on a `live` finding that
cites `live` evidence, otherwise it is dropped with a note. The check block then tells the chat
to say "the notes say X, what was seen live is Y" and to suggest updating the note; the task page
shows the hint on the finding.

**Embeddings.** Without `TOPCOAT_EMBEDDING_MODEL` the hub starts with the transitional hashing
embedder and looks every 2 minutes for a served model whose id contains `bge-m3`
(`TOPCOAT_EMBEDDING_AUTO`, default on); when it appears the hub switches to it for good (a node that
goes away later only means "keywords only", as before) and starts a knowledge re-index, which
re-embeds every chunk whose vector comes from another embedder; memory notes follow with the next
consolidation.

**In the agent:** next to the machines' capabilities (never alone: a task does not start on notes
only) the agent is offered `knowledge.search` on the pseudo node `knowledge` when the user has
something to search; `{query, limit?}`; every hit list is stored as **`[doc]` evidence** (path,
heading, excerpt) — documents, not a look at the machine, so the diagnosis keeps the tag `doc`.
Settings: `TOPCOAT_KNOWLEDGE_EXCLUDE` (extra globs), `TOPCOAT_KNOWLEDGE_REINDEX_SECONDS` (default
21600, 0 = only by hand).

### Upload sources of the knowledge base (E2-03 rest)
A source has `kind: "node"` (a folder read through a tool node, as above) or `kind: "upload"` (files
the admin put in by hand; no node, `node: ""`). Both are searched together and follow the same
rules: only `.md`/`.markdown`/`.txt`, the exclude list (also on the file name), the secret filter per
passage, visibility per user (D20).

- `POST /api/knowledge/uploads {source_id?, name?, visibility?, filename, text}` (admin; an API key is
  refused like for every source action): puts one file into the upload source `source_id`, or creates
  one called `name` when `source_id` is empty. A directory part of `filename` is dropped; a file with
  the same name replaces the old one. `201` with the source view plus `file {path, chunks, blocked}`.
  Errors: `422 bad_file` (not a note, or not text), `422 excluded`, `422 name_required`, `413
  too_large` (> 1 MiB), `404 not_found`, `409 not_upload` (the source is read through a node).
- `DELETE /api/knowledge/sources/{id}/files?path=` (admin): removes one file of an upload source (`204`;
  `404` when the source or the file is unknown). Deleting the source works as before.
- `POST /api/knowledge/sources/{id}/reindex` on an upload source asks no node: it only recounts and
  gives passages without a vector the current embedder's.

### Memory care (E3-05, ARCHITECTURE D37)

Hub only: no node message changes, no protocol version bump. Everything is per user (D20); migration
`0022_memory_care` adds the columns and the status `archived`.

**A note** (`GET /api/memory`, `GET /api/memory/{id}`, `GET /api/conversations/{id}/memory`) carries, next
to the E2-02 fields: `kind` (`person` | `preference` | `operational`), `stage` (`sure` | `older` |
`stale`, the only form of reliability the model sees), `reliability` and `usefulness` (0..1, for the GUI),
`last_verified_at`, `verified_count` (live checks; a check counts `TOPCOAT_MEMORY_LIVE_CHECK_WEIGHT`
= 2 times a statement), `expires_at` (volatile notes only), `verify_with` (`{capability, input}` of a
read-only capability, or `null`; no longer written or used since D124, older notes may still carry one), `recalled_count`, `used_count`, `last_used_at`, `feedback_positive`,
`feedback_negative`, `halflife_days` (this note's, grown by its confirmations). `status` may now be
`archived`.

| Request | Meaning |
|---|---|
| `GET /api/memory?status=&evidence=&kind=&q=` | `status` accepts `archived`; archived notes are listed only with `status=archived` or a search `q`. Extra response fields: `kinds` (live notes per kind), `care` = `{running, runs[10], settings}` (`settings`: the care numbers, read-only; they are `TOPCOAT_MEMORY_*`). `runs` is the consolidation's only |
| `POST /api/memory/{id}/feedback` `{helped: bool}` | the user's word on a note put into an answer; counts three times as much as a recall or a use, deletes nothing |
| `POST /api/memory/{id}/archive` | put an active note away (`409 not_active` otherwise) |
| `POST /api/memory/{id}/restore` | now also brings an archived note back |
| `POST /api/memory/care` → `202 {started}` | start a care run for the caller (`409 memory_disabled`, `already_running`, `no_node_available`) |

**A run** (`memory_runs`, `kind` `consolidation` | `care`; `trigger` `nightly` | `idle` | `manual` for a care run; `status` `running` | `ok` | `stopped_budget` | `paused` | `no_model` | `error`; a care run no longer stops at a budget since D124) of kind `care` has
`stats = {expired, archived, proposed, changes[]}` plus the cross-check's counts (`crosschecked`,
`discrepancies`, `agreed`, `decided`, `asked_person`, `tokens`) and `core_review` when the core was looked at.
Runs from before D124 also carry `checked`, `unchanged`, `changed`, `asked`, `unverifiable`, `skipped`,
`failed`, `no_node` and `model`. Each `changes[]` entry is `{action, item_id, text, kind, version, reason, undo}` with `action`
`expired` | `archived` | `proposed` (older runs also `verified` | `changed` | `asked` | `unverifiable`); `undo` is `restore` (bring the
note back: `POST /api/memory/{id}/restore`), `revert` (back to `from_version`: `POST
/api/memory/{id}/revert`) or `null` (nothing changed in the content). `changed` adds `from_version`,
`new_text`, `evidence[]`; `asked` adds `question_id`, `observed`, `evidence[]`. The versions the run writes have `actor: "care"` and
`source.care = {run, task, evidence[]}`.

**A question** (`GET /api/memory/questions`) may have `origin: "care"` (topic `care`, one candidate = the
note the user said, `observed` = what the live check saw); it is answered like any other question.

**The chat.** `meta.memory[]` and a stored message's `memory_refs[]` gain `stage`, `kind` and, when the
assistant was allowed to ask about that note in passing, `ask: true` (at most one per conversation). The
prompt text never contains numbers: only the stage ("older and not confirmed again", "possibly
outdated …").

**The care task** is an ordinary agent task (D30) with `origin: "care"`, hidden from the Tasks list, run under
the user's own rights, with the tool group of the note's capability only — `notes` and the internet
are never part of it.

Knowledge hardening (after a measurement): a **hit is the whole chunk** (at most 1200 characters;
`TOPCOAT_KNOWLEDGE_HIT_CHARS` can only shorten it) and carries `ord`, the chunk's ordinal in its file;
a directory reference **without lines** is named by the note's first heading and counts as chunk 0;
the same section from the directory and from the search is listed once (same `path` and `ord`); the
directory file (`10_KI_Verzeichnis.md`) is never a chunk (chunks of an older index are removed at
the next index run and never returned); the secret filter's `password` rule needs a **value** behind
the word for knowledge text ("Passwort: x", "password=x", "Passwort ist Sommer2024"; "Anmeldung ohne
Passwort" and "Das Passwort ist abgelaufen" pass) — notes, answers and everything that leaves the hub
keep the strict rule; every other rule is unchanged. What the filter held back is reported, never
silent: `blocked_files`, `GET /api/knowledge/status` and, for a section the directory pointed at,
`directory.withheld = [{path, rule}]` in a search.

### Model gateway (E1-05, ARCHITECTURE D18)
The hub reaches models only through the node session; `llama-server` binds to `127.0.0.1`.

**Messages.** The hub sends `proxy_request {stream_id, model, method, path, headers, body}`
(`body` base64, at most 1 MiB decoded; only `content-type` and `accept` are forwarded). The
node answers with `proxy_chunk {stream_id, seq, data}` messages in order — the first one also
carries the upstream `status` and `content_type` — and one `proxy_end {stream_id, status,
error?}`. Without chunks, `proxy_end` is the whole answer. `proxy_cancel {stream_id}` stops a
stream; the node then sends nothing more for it. Chunks are at most 32 KiB of data.

**Node rules.** Forwarding needs the permission `models.proxy` in `allowed_actions` (default
since E1-05, added to existing configs by migration 0007, D14). Only `POST
/v1/chat/completions`, `POST /v1/completions`, `POST /v1/embeddings` and `GET /v1/models` of a
loaded model are forwarded. Refusals come as `proxy_end`: 403 `not_allowed` (permission or
path), 409 `not_loaded`, 503 `busy` (a watcher rule matches, the node yielded its VRAM, or 16
streams run already), 413 `invalid_args` (body too large), 501 `not_implemented` (runner
without HTTP endpoint), 502 `runner_error`. A stream that sends nothing for 5 minutes ends
with `error.code = timeout`. Requests count as model use for the idle TTL.

**Loading.** The heartbeat lists models being loaded in `loading`. A `models.load` for a model
that is already loading waits for that load (no second process), e.g. when the gateway asks
for a resident model the pressure controller is reloading.

**Hub endpoints.** `GET /v1/models`, `POST /v1/chat/completions`, `POST /v1/completions`,
`POST /v1/embeddings`, OpenAI-compatible. Authentication: `Authorization: Bearer <API key>`
(per user, `POST /api/keys` in the GUI, stored as SHA-256) or the dev admin token.
`model` is a model name as the nodes report it; `model@node` pins a node. Routing:
1. candidates are connected nodes in state `available` that have the model and allow
   `models.proxy` (plus `models.load` if it is not loaded); `busy_gaming` and `yielded` nodes
   are skipped (E1-06);
2. order: loaded → loading → on disk, then `TOPCOAT_GATEWAY_NODE_ORDER` (comma-separated node
   names), then name;
3. the hub loads the model if needed — once per node and model, however many requests wait —
   and never unloads another model to make room;
4. a node answering `not_loaded` gets one load and retry; `busy` or a failed load moves on to
   the next candidate.

5. **The queue (SCH-03, D94).** Each node has one queue in the hub, as large as the sum of `n_parallel` of its
   loaded models (absent counts 1; no setting). A request takes a place before it is sent; the order is the place
   of the work (chat, after an answer, other), then arrival; idle work waits while chat or answer work runs on the
   node; running work is never cancelled; a request that waits longer than the first-byte limit gets 503 `busy`.
   `TOPCOAT_GATEWAY_QUEUE_AGING_SECONDS` (default 0 = off) lets a waiting request climb one place per period. A chat
   answer first calls `Gateway.reserve`, which picks the node the way routing does and has the model loaded, so the
   prompt is sized for that node; no place is held until the request starts.

Errors are OpenAI-style `{"error": {"message", "type", "code"}}`: 400 `invalid_request` /
`invalid_json`, 401, 404 `model_not_found` / `node_not_found`, 413 `request_too_large`, 503
`model_unavailable` with `Retry-After` (model only on busy nodes or no node could serve), 504
`timeout`. The response header `X-Topcoat-Node` names the node that answered. A client that
disconnects cancels the stream on the node.

### Core, personas, prompt snapshots (E3-06, ARCHITECTURE D38)

Hub only: no node message changes, no protocol version bump. Everything personal is per user (D20);
migration `0023_core_memory` adds the columns and tables. Nodes are untouched: the core is built by the
hub and sent as the system message of the chat request like before.

**Notes.** `memory_items` has the kinds `style`, `rule` (next to `person`, `preference`,
`operational`) and the fields `core`, `scope` (`user` | `household` | `system`), `example`, `weak`.
`GET /api/memory` still lists facts only; the instructions live under `/api/core`. A note mirrored from
a note file has `source_type=kb`, `source_ref {path, sha256, source, verified, check_hint, aliases}`.

| Request | Meaning |
|---|---|
| `GET /api/core?context=` | the caller's core: `budget {total, used, context, parts{system, persona, rules, profile, household, recent}{budget, used}, cut[], examples_dropped}`, `rules[]`, `profile[]` (pinned first), `household[]`, `system[]` (`{id, lang, text, version}`), `recent[]` (the latest chats as key terms), `proposals[]`, `preview` (the block exactly as a new conversation gets it), `admin`. `context` (1024..1000000, default 16384) picks the budget: below 8192 the small core (500) |
| `POST /api/core/items` `{kind: style\|rule, text, example?, core?}` | the user writes an instruction (their own click); `422 secret_blocked` if the text looks like a secret |
| `PATCH /api/core/items/{id}` `{text?, example?, core?}` | change, or pin / unpin a note (a version each; undo with `POST /api/memory/{id}/revert` or `/restore`) |
| `DELETE /api/core/items/{id}` | soft delete (restorable) |
| `POST/PATCH/DELETE /api/core/household/items[/{id}]` | the household's basics; **admin only** (`403` otherwise, an API key never counts as admin); everybody reads them in the core |
| `PUT /api/core/system/{lang}` `{text}` · `GET /api/core/system/{lang}/versions` | the fixed rule block of `en` / `de`; changing it is **admin only**, a new version each time; the assistant can never change it |
| `GET /api/core/proposals` | open proposals: `{id, action: add\|change\|remove\|pin\|unpin\|kb_edit, kind, text, example, core, weak, reason, evidence{quote?, markdown?, path?, review?}, diff{before, after}, target_id, origin: chat\|care, status}` |
| `POST /api/core/proposals/{id}/accept` `{text?, example?}` | one click (with `text`/`example` the user edited it first): the change takes effect as a version, actor `user`; for `kb_edit` it only closes the proposal (the assistant writes no file; the Markdown to copy is in `evidence.markdown`) |
| `POST /api/core/proposals/{id}/reject` | no; the same wish is not proposed again for 90 days. At most 10 proposals are open per user; a text from a web page, a tool or a note file never makes one |

**Personas** (`/api/personas`, personal, D20; templates readable by all).

| Request | Meaning |
|---|---|
| `GET /api/personas` | `templates[]`, `own[]` (each `{id, key, name, version, scope, status, components, example{de,en}, defaults, text, tokens, truncated, ref}` — `text` is the rendered persona in the user's language, at most 400 tokens, `truncated` says something was left out), `default_id`, `max_tokens`, `wizard {state, show, banner}` |
| `POST /api/personas` `{name, components, example?, scope?}` | a personal persona from blocks (`components`: `role{de,en}`, `address` du/sie, `humor` 0/1/2, `length` short/long, `ask` ask_first/read_first, `transparency`, `emojis`; or `{free_text}`); `scope: household` is an admin's template. The secret filter runs on every text |
| `PUT /api/personas/{id}` · `DELETE /api/personas/{id}` | change (a new version if the content changed) / archive; templates: admin only (`403`); another user's persona is `404`. `{voice}` (D127) names the voice of `voice.speak` the persona speaks with (`null` = the node's own); it is kept in `defaults.voice` and makes no new version |
| `PUT /api/personas/default` `{persona_id}` | the persona new conversations start with (null = none); existing conversations keep theirs |
| `GET /api/personas/stats` | per persona the user's own count: `answers`, `helped`, `unfit` |
| `GET /api/personas/questionnaire` | the first-start questionnaire (a file of the hub): `scenes[]` (three, each with three answers in different tones), `questions[]` (six, with defaults), `roles`, `none_role` |
| `POST /api/personas/questionnaire/preview` `{answers}` | `{role, components, text, tokens, samples[2]}`; the same answers give the same text; nothing is stored. `answers`: `{scenes: {plex: a\|b\|c\|none, …}, address, humor, length, transparency, ask, emojis}` (a missing question has its default; unknown ids or values `422`) |
| `POST /api/personas/questionnaire/confirm` `{answers, name?}` | the user's click: the persona is created, becomes their default, the form of address is applied |
| `PUT /api/personas/wizard` `{state: later\|skipped}` | put the wizard away; a new user without conversations gets `wizard.show`, anybody else without a persona `wizard.banner` — never a compulsion |

**Conversations and messages.** `POST/PATCH /api/conversations` take `persona_ref` (a persona id; `null` on
`PATCH` = none; unknown or another user's `404`); a new conversation without one pins the user's default.
`GET /api/conversations/{id}` adds `persona {id, name, key, version, status}`, `prompt_hash` (of the first
answer) and `prompt_changes[]` (`{message_id, prompt_hash, persona_ref, first, at}` for every answer whose
prompt differs from the one before). A message carries `prompt_hash`, `persona_ref` (`<id>@<version>`) and
`feedback` (`helped` | `unfit` | null). `POST /api/conversations/{id}/messages/{mid}/feedback`
`{verdict: helped\|unfit\|null}` sets or takes back the user's verdict (it feeds the count per persona; there
was no answer-level feedback before).
`GET /api/chat/prompt-snapshots/{hash}` → `{hash, text, persona_ref, persona, core_version, created_at}`: the
system prompt exactly as it was sent, only the user's own (another user's hash is `404`).

### Answers that outlive the connection (E3-07 D1, ARCHITECTURE D39)

Hub and GUI only, no node message changes. Migration `0024_answer_runs` adds the message states
`running` and `interrupted`.

| Request | Meaning |
|---|---|
| `POST /api/conversations/{id}/messages` · `/regenerate` · `/continue` | as before (`text/event-stream`), but the answer is made by a task of the hub: the response is the first subscriber. Every SSE event is numbered (`id: n`, from 0); the first event `meta` now carries `message_id` (the message the answer is written into). A failure before the model has started is an HTTP error as before; later ones are `error` events |
| `GET /api/chat/messages/{id}/stream?offset=n` | follow an answer from event `n` on (0 = replay everything, as a reload does), as many readers as you like; ends with the run. A run that is over and no longer held in memory, or the hub restarted: one `snapshot` event `{conversation_id, message}`. Another user's message: `404` |
| `POST /api/chat/messages/{id}/stop` | stop the answer for real; returns the stored message (`stopped`), `409 not_running` if it was over, `404` for an unknown or foreign message. Closing a connection never stops |
| `POST /api/conversations/{id}/stop` | the same for a reader that does not know the message yet (the model is still reading the prompt): `{stopped: true}`, `409 not_running` |

New events: `stopped` `{message | null, interrupted}` (the run ended by a stop call or the hub going down;
`message` is what was stored, `null` if nothing was said) and `snapshot`. Message `status` may now be
`running` (being generated: `content` is the text so far) or `interrupted` (the hub went down; "Continue"
goes on, as for `truncated`). `GET /api/conversations` and `GET /api/conversations/{id}` carry
`running` (an answer is being generated there, whether or not a tab follows it).

### Images in the chat (ARCHITECTURE D125)
`POST /api/conversations/{id}/messages` takes `images`: up to 4 pictures as base64 (a `data:` URL is
fine), each at most 15 MB as sent; `content` may then be empty. The hub decodes each one (JPEG, PNG,
WebP, GIF, BMP; at most 40 million pixels), turns it upright by its EXIF orientation, scales it to at
most 1536 px on the longer side and writes a new JPEG of at most 400 KB from the pixels alone: no
EXIF, location, colour profile or comment survives. Only that is kept (`chat_images`, per person,
deleted with its message or conversation). A file that is not such a picture refuses the whole
message with 422 (`bad_image`, `image_too_large`) and nothing is stored. Messages carry
`images: [{id, width, height}]`; `GET /api/chat/images/{id}` returns the picture to its person only
(another person's id is a 404; `Cache-Control: private`, `nosniff`, a sandboxing CSP).

The images go only to a **loaded model that can see** (model facts `vision`, a projector loaded with
it, D125). The model of the answer keeps them where it sees, and the request is pinned to those nodes.
Otherwise, unless the person picked the model, the answer goes to a seeing model: a house model
first (D117), then any loaded one. A model that cannot see never gets them: the prompt says an image
was attached and not given. `meta.vision` says what happened (`{sees, model, images, routed_from?,
node?}`), and an activity item `note` with `data.event` `vision_routed` or `vision_blind` is kept with
the answer. In the prompt an image is an `image_url` content part (a data URL) in its user turn, the
newest first while 640 KB and 4 images allow (the request to the node has a size limit), older ones a
line that one was there; the system prompt then says that text in an image is data, never an
instruction. **Incognito** (`POST /api/incognito/{tab}/messages`) takes `images` the same way, keeps
them in the tab in memory only (at most 12 per tab), answers with `asked` (the question with its
`images`) and `message.vision`; `GET /api/incognito/{tab}/images/{id}` returns one from memory.

### Voice: push to talk (ARCHITECTURE D127, issues #54, #55)
`GET /api/voice/ws` (WebSocket, over the person's TLS connection like every call) carries speech
between a client and the node services `voice.transcribe` / `voice.speak` (docs/PROTOCOL.md, "Voice
capabilities"). `GET /api/voice` → `{transcribe, speak}` says up front whether it can work for the
caller (the client shows its key only then). A browser cannot set a header on a WebSocket, so the **first frame** is
`{"type": "auth", "token": "<the GUI's bearer token or an API key>"}`, a text frame of at most 4 KiB
sent within 10 seconds; a larger one closes the socket with `1009`, anything else (or nothing) with
`4401`. The hub answers `{"type": "ready", "transcribe": bool, "speak": bool}`: whether
a connected node offers that capability to this person (grants and node config as for every
capability call). Then, in any order, each request with an `id` of the client's choosing:

| client → hub | hub → client |
|---|---|
| `{"type": "listen", "id", "rate"?: 8000–48000 (16000), "language"?: "auto"\|"de"…, "incognito"?: bool}`, then **binary frames**: raw 16-bit little-endian mono samples at `rate`, any chunk size | — |
| `{"type": "end", "id"}` (the key is let go) | `{"type": "transcript", "id", "text", "language"}` (`text` may be empty: nothing heard) |
| `{"type": "speak", "id", "text" (1–8000 characters), "conversation_id"?, "incognito"?: bool}` | per piece `{"type": "audio", "id", "seq", "format": "wav", "text", "bytes"}` and right behind it one **binary frame** with that WAV; at the end `{"type": "spoken", "id", "pieces"}` |
| `{"type": "cancel", "id"?}` (no `id`: everything; barge-in) | `{"type": "cancelled", "id"}` per request it stopped |
| `{"type": "ping"}` | `{"type": "pong"}` |
| | `{"type": "error", "id", "code", "message"}`: `no_voice_node`, `too_long`, `not_listening`, `invalid_input`, `busy`, or the code of the capability call (`not_granted`, `node_offline`, `timeout`, …). The socket stays open. |

A recording holds at most 2.5 MB of WAV (about 78 s at 16 kHz); more is refused with `too_long`
and nothing is sent to the node. Speech requests are spoken **one after the other in the order
asked**; the hub cuts each text into sentences without their Markdown (code is not read out, a link
is its text) of at most 600 characters, so the first sound comes after the first sentence. The client
can therefore send an answer sentence by sentence while the chat still writes it. `conversation_id`
picks the voice: the persona of that conversation of this person (else their default persona),
`defaults.voice`; a voice the node does not have falls back to the node's own.

**What is kept:** nothing of the audio. Each node call is audited like any capability call, but its
input and output are withheld (`{"withheld": "voice: …"}`, no excerpt); with `incognito` no audit row
is written at all. **The hub writes no message here**: the client posts the transcript as the
person's message through the normal chat (`POST /api/conversations/{id}/messages`, or the incognito
tab), so it goes through every rule of the chat. A spoken "yes" is only text: it never confirms an
action, approval cards stay click or key (D15, D83); the socket has no message that reaches one.

### Message kinds and the report card (E3-07 D3, ARCHITECTURE D41)

Hub and GUI only. Migration `0025_message_kinds` adds `messages.kind` (`say` | `progress` | `report`,
default `say`: every message that exists is a `say`) and `messages.report` (JSONB).

A request that needs a finding (a server check runs: forced, detected or decided by the model) is
answered in four steps, each a message of its own:

1. a **confirmation bubble** (`kind: say`), a sentence the hub writes itself in the language of the
   message (*Schau ich mir an.* / *On it — I'll take a look.*);
2. a **progress line** (`kind: progress`) that exists while the tools run and shows
   `capability · node — reason`; it is deleted when the result is there (a hub that was killed leaves
   it behind: the next start turns it into an `interrupted` bubble);
3. the **report card** (`kind: report`): built by the hub from the diagnosis and the evidence of the
   check, never by the model. `content` is its Markdown text (what a follow-up question is answered
   from), `report` its structure:

   ```json
   {"version": 1, "task_id": "…", "status": "done", "incomplete": false, "incomplete_reason": null,
    "summary": "…", "calls": 2,
    "findings":    [{"claim": "…", "tag": "live|doc|web", "evidence": ["E1"], "contradicts": ["E3"]}],
    "assumptions": [{"claim": "…", "confirm_with": "docker.inspect", "evidence": []}],
    "proposal":    {"text": "…", "impact": "…", "rollback": "…", "advice_only": true},
    "evidence":    [{"ref": "E1", "tag": "live", "node": "…", "capability": "…", "input": {},
                     "excerpt": "…", "truncated": false}],
    "notes": []}
   ```

   Findings and assumptions are kept apart (an assumption is a finding the check could not back with
   evidence of its kind). `proposal.impact` / `rollback` are empty when the check did not give them;
   the GUI then says *not stated by the check* instead of inventing something. An excerpt is cut at
   2000 characters (the end of a log), all of them at 30000;
4. the **closing bubble** (`kind: say`) from the model: one or two sentences. The model is told not to
   repeat the card, gets `max_tokens` 200, and the hub cuts what it writes to two sentences and 400
   characters.

A question that needs no finding stays one `say`. No card is made when the check has neither a
diagnosis nor evidence; the closing bubble then says what happened.

New SSE events of `POST …/messages`, `/regenerate` and the attach stream: `bubble` `{message}`,
`progress` `{message_id, text}` (`text` empty until the first tool is known), `progress_done`
`{message_id}` and `report` `{message}`; `tools_start` / `tool_step` / `tools_done` stay. The `plan`
step now carries `capability` and `node`. One run is found under the ids of all its messages
(`GET /api/chat/messages/{id}/stream`, `…/stop`). The agent's `finish` may carry `impact` and `rollback`
beside `next`. Regenerating removes everything the last question got (bubbles, card, progress line).
`message_view` carries `kind` and `report`.

### Honesty by mechanism (E3-07 D4, ARCHITECTURE D42)

Hub and GUI only; no migration. The `memory` event of a chat answer (after `done`) gets `not_noted`
(`secret` | `no_statement` | `too_long` | `nothing_found` | `memory_off` | `pending` | `failed`) when the user
asked to note something (or the answer claimed to have done so) and nothing was noted; it is also sent when
nothing else happened to say so (`remembered`, `confirmed` … empty). A user who asked outright waits up to four
times `TOPCOAT_MEMORY_NOTE_WAIT_SECONDS` for the result. The agent's diagnosis carries `coverage`
`{asked: [node], not_asked: [node]}` (and `impact` / `rollback` with `next`, D41); a finding's `claim` that says
something is absent names the nodes asked when others were not (`scoped: true`); the report card has the same
`coverage`. Chat requests carry extra sampling parameters by model family (`repeat_penalty` for `gemma`, setting
`TOPCOAT_CHAT_SAMPLING`).

**A claim of a change (AGT-01).** The closing answer of a check is read for two kinds of sentence
(`claims.claimed_change`): one that says a change was made ("I have set the tasks to dropped") and one that says
a change is ready to confirm. The hub holds them against what it has: the change sets prepared for the person's
click (`ChangeStore.for_task`), the proposal on the report card and the calls the check had refused
(`failed_calls`: tool, code, message). With nothing on record, the text of the answer gets a line "Hinweis des
Hubs: …" / "Note from the hub: …": nothing was changed, no change waits for a click, and which calls were refused
and why. With a change on record and an answer that says it was made, the line says it only waits for the click.
A check that was not started because the user already has `TOPCOAT_AGENT_MAX_ACTIVE_PER_USER` tasks gets the line
"no server check was made, and that says nothing about your servers" (`claims.refused_note`). The patterns are
strict (an offer, a question, a statement about history are not claims); a missed claim costs nothing. One reading
is wider since 2026-10-31: a clause that says the changes "warten auf deine Bestätigung" / "wait for your
confirmation" is a claim even when the check made no writing call at all, unless the clause is negated or an offer
(unit-tested; not seen live).
`GET /api/reviews` carries `reviewer_node` and `same_model` (the check was made by the model that wrote the
answer); the second look's `verification` and `verifying` event carry the model name without the node.

### The record of model requests and the live view (SCH-03 step 3, ARCHITECTURE D95)

Migration `sch03_job_records`. One row in `job_records` per request the gateway served or refused, written when the
request ends: `created_at`, `started_at`, `finished_at`; `user_id`, `conversation_id`, `message_id`, `task_id`,
`flow_run_id`, `application` (empty when the caller did not name them; no caller sets `message_id` or `flow_run_id` yet: `set_job_context` is called by the chat answer
and at the start of an agent run); `work` (the label: `chat`, `agent`, `memory`, `digest_summary`, `review`, ...),
`caller` (the chain of functions), `requested`, `chosen_model`, `chosen_node`, `place` (0 chat, 1 after an answer,
2 other), `source` and `reason` (only their defaults `picked` and empty so far; real values come with the table, SCH-04), `outcome` (`done`, `failed`, `cancelled`) with `error_code` (`busy`, `timeout`,
`node_offline`, `http_<status>` or the node's own code), `waited_ms` (from the request to its start, i.e. the time in the queue),
`model_loaded` (it was loaded when the request was routed), `duration_ms`. Never a prompt or an
answer. Work labelled `incognito` is not recorded. A failure to write is logged and ignored. There is no endpoint that
reads the table yet (OBS-01, administrators only, D91); the first numbers were read with SQL
(the measurements 6).

`GET /api/activity` (admin) lists per node `work` (running requests: `kind`, `model`, `seconds`, and `application`,
`conversation_id`, `message_id`, `task_id` where the caller named them), `queued` (counts by place) and, new,
`waiting`: the requests that wait, in the order they will be served, each with `place`, `seconds`, `kind`, `model` and the
same ids. The Machines page shows them under the queue counts.

### Live parameters (E7-03, ARCHITECTURE D58) and the thinking switch (D97)

`GET /api/admin/params` lists the parameters that can be changed while the hub runs (key, group, kind, bounds, default,
value, whether overridden); `PUT /api/admin/params/{key}` with `{"value": ...}` sets one (validated, stored under
`hub_settings` as `live.<key>`, noted in `setting_changes`), `DELETE` puts the value of the environment back,
`GET /api/admin/params/{key}/history` lists the changes. `chat_effort` is a JSON object
`{family: {fast|balanced|thorough: {reasoning_effort?, chat_template_kwargs?}}}`; the family is a part of the model name
(the longest match wins). Set on 2026-10-31 for `gemma` and `qwen3.5` (`fast`, `balanced`:
`{"chat_template_kwargs": {"enable_thinking": false}}`). It applies to chat answers only.

### Results of an employee in two forms (D112, issue #35)

The messages between the applications are typed by `proto/messages/registry.json` (D81). The result of an
employee (`research.result`, `dev.result`, `admin.result`) carries two texts:

- `summary`: for the person, as the employee wrote it.
- `brief`: for the secretary, at most 600 characters. An employee may write its own; otherwise the hub writes
  it from the payload when the message is filed (`result_forms.with_brief`, called by
  `ProjectMessages.add_message`): the kind of result, the first sentence of the summary, how many findings,
  the branch, tests and acceptance of a Development result, the required points without a finding, the open
  points.

What of a result may enter the secretary's conversation is `result_forms.for_secretary`: the summary, the
brief and references (`{"kind": "file" | "branch", "ref": …}`), each through the private-data filter of D65
(`redaction.private_kinds` with the node names, the user names and the own domains). A text that holds a
secret or an identifier of the operator's network becomes a pointer ("The result holds private details; they
stay in Research. Open it there."), a reference that names one is left out, and `withheld` lists the kinds
found. The findings and their quotes, and the content of files and records, never enter the secretary's text.

### Work cards and "talk to the employee directly" (D112, issue #37)

Migration `so02_order_cards` adds two links to `orders`: `chat_message_id` (the secretary's message that said the
task was handed on) and `direct_conversation_id` (the employee's own conversation the button opened).

`GET /api/conversations/{id}/orders?language=en|de` lists the work cards of one of the person's conversations,
oldest first (at most 50; 404 for a conversation that is not the person's): `id` (the order), `employee` (`research`,
`dev`, `admin`), `employee_title` (from the contract, in the language asked for, else the person's), `topic`,
`status` (`sent`, `queued`, `running`, `waiting`, `done`, `failed`, `stopped`), `report_when_done`, `created_at`,
`chat_message_id` (the GUI shows the card under that message; `null` for an order from before the migration, which
stands after the last message written before it), `summary` (a finished order's result as `result_forms.for_secretary`
lets it into the secretary's conversation, else `null`), `withheld` (the private kinds the filter found) and
`direct_conversation_id`.

`POST /api/orders/{id}/direct?language=en|de` is the button "talk to … directly": it returns
`{conversation_id, application}`, the employee's own conversation about the order. The first click makes it in the
employee's application (title: the task; persona: the person's default, else the contract's) with one assistant
message in the employee's words: the task, what to find out, and its result as it wrote it (the employee's own
context, D74: the filter of D65 stands only in front of the secretary), its question back, or that it is not done
yet. A later click returns the same conversation while it exists, else makes a new one. 404 for an order that is not
the person's. The button is the person's own click and changes nothing but the person's conversations.
