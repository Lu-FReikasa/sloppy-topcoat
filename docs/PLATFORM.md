# Platform: applications, flows and one home for every setting

Status: **partly built**: "Status of this plan" at the end and the roadmap (stage 8) say what.
It builds on `docs/FLOWS.md` (the flow editor), `docs/DESKTOP.md` (programs on the desktop) and
`docs/ARCHITECTURE.md` (D56 to D60). Numbers
below come from the code and from one export of a running hub (168 active memory notes); where a
figure is a rough estimate, it says so.

## 0. In one paragraph

Topcoat grew by pages: a chat, an admin console behind it, then workspaces next to the classic
pages, and flows next to hard-coded pipelines. The same decision is therefore made in several
places (which model, which node first, how often to look, what a check may do), and a feature
added to one surface is missing from the other (the calm chat window did not show the chain
that the admin chat showed). The proposal: **one UI** (workspaces only), **one kind of thing that
decides behaviour** (a flow, started by a trigger, with *policy nodes* instead of scattered
settings), **applications** (Chat, Research, Dev, Admin, later Documents, Music, Image) as the
unit that bundles windows, rights, memory area and flows, and **one editor** for flows and
applications, with live view across nodes.

## 1. What we found

### 1.1 Surfaces

- Two UIs live side by side: the classic pages (`App.tsx` renders about 25 views through one
  `?:` chain, a user dock, an admin side bar with eleven hand-written links) and the workspaces
  (`#/w`), switched by a stored mode. Every page exists twice in effect: once as a page, once as a
  module.
- Two chat surfaces: the calm window (`talk/TalkPage.tsx`) and the full chat view
  (`chat/ConversationView.tsx`, also the archive and admin view). Each has its own live progress,
  its own pickers and its own tests. Features land in one and are forgotten in the other.
- The menu is code (a new page means a route, a parser branch, JSX, a set membership and i18n).

### 1.2 Settings

| where | what | how many |
|---|---|---|
| `Settings` (environment, `config.py`) | limits, intervals, timeouts, addresses, secrets | 174 fields |
| live parameters (`live_settings.py`, "Params" page) | agent 13, memory 10, chat 8, gateway 7, coordinator 6, flows 4, knowledge 2, inventory 1 | 51 |
| `hub_settings` rows | default model, **model per quality level**, node policy, escalation limits, tool modes, app groups, a legacy blob | 7 keys |
| per person (`user_settings`) | prompt, temperature, default model, address, memory on/off, escalation, **quality level**, tool scope, internet, persona | 11 fields |
| pages | Params, Scheduling, Care, chat settings (user and hub), yield and pressure per node, config editor, access | 7+ |

The same question has several answers: *which model answers a chat message?* is decided by the
person's default model, the conversation's picker, the hub default, the model per quality level,
the node chain (`chat_node_chain`), the node policy and `gateway_node_order`. Some of these are
leftovers from before flows (the model per quality level, the quality level itself, the
environment node order next to the stored node policy).

### 1.3 Periodic and triggered work

Five independent loops, each with its own interval setting and its own `asyncio.sleep`: inventory
(300 s), memory consolidation (1800 s), memory care (900 s, plus an hour of the day), knowledge
re-indexing, embedding back-fill. Next to them, work started by events (after an answer: the running
summary, second look). None of them can be seen, paused or changed as a whole except on
the Scheduling page, and none can be combined ("when the inventory changes, check X").

### 1.4 Memory

Kinds are `person`, `preference`, `operational`, `rule`, `style`, `episode`. In the export, of 168
active notes 150 came from chats (not typed by the person) and 75 are episodes. By a crude keyword
count (repository, test, branch, container, build, ...), **about 70 notes are about development**,
and 45 of those are episodes: the dev topic has no kind of its own (`operational` was meant for
the homelab), so it falls into the episode bucket and ages like small talk. The strict single
axis (`kind`) forces every note into a drawer chosen by a model.

### 1.5 Flows today

Seven node types, one manual way to start a run (plus "answer path of a conversation"), parallel
branches, versions, live parameter changes per run, stored runs, admin only. A flow already shows
what a run did; it cannot start by itself, cannot decide *where* (which node) a step runs, and
holds none of the settings above.

## 2. What other editors do (and what we take)

Drawn from a short web search (sources at the end) and from general knowledge of these tools
(where I did not check a claim here, it is background knowledge and should be verified before it
decides anything).

| tool | idea worth taking | what we leave |
|---|---|---|
| **Home Assistant** | trigger, condition, action as the shape of every automation; several triggers per automation; a condition can ask *which trigger fired* | YAML as the only truth |
| **Node-RED** | context with scopes (flow, global); shared *config nodes* (one definition, many users); schedules must survive restarts (its docs warn about long intervals and restarts); subflows | free JavaScript function nodes |
| **ComfyUI** | execution cache keyed by the inputs of a node, so only the changed part of a graph runs again; a node may opt out of caching | custom Python nodes |
| **LangGraph Studio** | a graph view for debugging and a simple chat view for use: two modes of one thing | tied to one framework |
| **Dify, Langflow, Flowise** | knowledge and retrieval as ordinary nodes; chat as the front, the graph as the back | an integration marketplace |
| **Kestra** | triggers as first-class, flows as declarative, diffable documents (our graph JSON is already one) | a separate server stack |
| **n8n, Windmill** | webhook and schedule triggers on the same canvas; self-hosting cost (n8n's queue mode needs Redis, Node-RED needs no database) | queue and worker infrastructure |

What we deliberately do **not** take: nodes that run free code, a marketplace of third-party
connectors, a separate scheduler service. Topcoat stays one hub plus Postgres, and a node can only
do what the hub's named, schema-checked actions allow (security baseline).

## 3. The model

```
Application   a named area of work: windows, rights, memory area, flows        "what I am doing"
Flow          nodes joined by ports, started by a trigger                      "how it is done"
Trigger       what starts a flow: timer, message, event, manual, chain         "when"
Node          one step: trigger, data, model, placement, tool, control, output "what"
Policy node   a node that holds a decision that used to be a setting           "which, how much"
Shared config a policy node used by many flows (a Node-RED config node)        "one place to change"
Window        a module instance in a layout (see DESKTOP.md)                   "where I see it"
```

### 3.1 Applications

```json
{
  "key": "dev", "title": "app.dev", "icon": "{}",
  "windows": ["chat", "workshop", "repos", "tests", "memory:dev"],
  "grants": { "roles": ["admin"], "tool_groups": ["github", "workshop"], "internet": true },
  "memory_area": "dev",
  "flows": { "answer": "dev-answer", "background": ["dev-test-watch"] },
  "defaults": { "persona": "sachlich", "model_role": "deep" }
}
```

An application is also a **security boundary** (decision of the maintainer): each one keeps its
own memory, its own rights and its own flows. *Chat* has no access to server information. When a
request needs the servers, the chat does not guess: it shows a button "pass this to Admin?" that
opens the question in the Admin application (a hand-over of exactly this request, nothing of the
chat's memory). Applications can coordinate **after a release by the person**, one hand-over at a
time. Every row of a person gets an application key next to `user_id`; queries are scoped by both.
This is also what makes several users safe later: the boundary is already in the data.

An application is a *bundle*, not a new mechanism: its `flows.answer` is the answer path that
conversations in it use (today `conversations.flow_id`), its `grants` narrow what a person may use
(intersection, never extension), its `memory_area` scopes recall and writes, its `windows` are the
workspace of the desktop (`DESKTOP.md`). Chat, Research, Dev and Admin are the first four; Documents, Music and
Image come with their tool groups.

### 3.2 Two canvases, one editor

- **Map** (new): applications as large nodes; around each one its attachments (windows, rights,
  memory area, answer flow, background flows). Shared configs sit between applications and are
  wired to several. Opening an attachment opens its own canvas.
- **Flow** (exists): the graph of one flow, with live view, versions, parameter changes per run.

One inspector, one version history, one "run what is drawn", one live view. A flow can be opened
from the map, from a run in the chat, or from a trigger's history.

### 3.3 Node catalogue (v2)

New nodes in bold; the last column is the setting or code path they take over.

| category | node | ports | replaces |
|---|---|---|---|
| trigger | **Manual** | out | "run what is drawn" |
| trigger | **Message** (a conversation of application X got a message) | out | the hard-wired answer path |
| trigger | **Timer** (interval, daily at, cron) | out | inventory, memory, knowledge, care intervals |
| trigger | **Event** (task done or failed, node online or offline, inventory changed, report ready, memory run ended) | out | after-answer hooks, "check when X" |
| trigger | **Chain** (another flow ended) | out | glue code |
| data | **Recall memory** (area, query, how many) | ok, fail | `memory.recall` in the pipeline |
| data | **Write memory** (kind, area; a proposal, the usual care rules apply) | ok, fail | the learning step after an answer |
| data | **Knowledge search** | ok, fail | the knowledge tool |
| data | **Window context** (what the focused window shows, untrusted) | out | `{{input.window}}` |
| data | **Variable** (get or set, scope flow or application) | out | none today |
| model | Model (exists) | ok, fail | sampling and effort settings |
| model | **Placement** (which node and model: ordered candidates, must be loaded, background preference, waits, fallbacks) | ok, fail | node order, background nodes, node policy, node chain, model per quality level |
| model | **Second look** (a different model reads the draft) | pass, found | review, pre-answer check, verify |
| model | **Limits** (turns, calls, time, parallel children) | out | `agent_*` and `coordinator_*` |
| tool | **Tool call** (one named capability, read-only) | ok, fail | single reads from checks |
| tool | Check (exists, the read-only agent) | ok, fail | |
| tool | **Proposal** (a state-changing action as a card with its exact effect) | done, declined | the proposal path (D43) |
| control | Fork, Plan, Steps (exist) | | |
| control | **Parallel / Join**, **For each**, **Retry / Catch**, **Delay**, **Sub-flow**, **Once per** (de-duplication) | | |
| output | Result (exists), **Notify** (inbox card), **Say** (a message into a conversation), **Log** | | |

### 3.4 Settings become policy, with three layers

A value is found in this order: the node (a flow's own choice), then the application's default,
then the hub default. The hub default is today's setting, kept as the last resort and as the safe
fallback. What a setting turns into:

| today | becomes |
|---|---|
| model per quality level | **dropped**: the level no longer picks a model (section 3.6) |
| the quality level of a person or conversation | **kept as "effort"** (quick, balanced, careful), independent of model, node and tools; a variable every node can read (section 3.6) |
| `gateway_node_order`, `gateway_background_nodes`, `node_policy`, `chat_node_chain` | **Placement** node, usually as a shared config ("gpu first, laptop for background") |
| `agent_*`, `coordinator_*` | **Limits** node, default per application |
| chat sampling and effort, default context | **Model** node parameters |
| `memory_*` intervals, `memory_care_hour`, inventory and knowledge intervals | **Timer** triggers of system flows |
| memory thresholds, half-life | parameters of the **Recall** and **Write memory** nodes and of the care flow |
| gateway timeouts | stay as hub defaults (an "expert" page); a Placement node may narrow them |
| secrets, paths, ports, TLS, database URL | stay in the environment, never in the editor |
| person preferences (prompt, address, persona, internet, memory on or off) | stay in "Mine", because they belong to a person and not to a behaviour |

### 3.5 System flows

The behaviours that are code today (answering, the check, memory consolidation, memory care,
digest, review, inventory, knowledge re-index) get a **built-in flow** each. The
node implementations stay in Python; the *composition* becomes data. Rules that keep it safe:

- A built-in flow is read-only; **Copy to edit**, and **Put back** the built-in.
- The chat answer is **built directly as a flow** and tuned afterwards (decision of the
  maintainer); the old code path is removed once the flow answers. The way back is the previous
  image and the database backup that `update-hub.sh` writes before every switch.
- One application at a time, starting with Chat. No big bang.

### 3.6 Effort

Decision of the maintainer: the chat offers one simple choice, **how much trouble the agent
takes** (quick answer, or a considered, checked one). It reflects how serious the request is; it
does not point at a fixed model or flow, and it is independent of tools and model choice.

Today (`quality.py`) the level already is per conversation and does four things: it picks the
limits of the server check (quick: 4 turns, 2 calls, 90 s; balanced: 14, 12, 180 s; careful: 16,
14, 300 s with larger excerpts), it picks a **model per level** (an admin setting, the coupling
to drop), it switches the second look on at *careful*, and it decides when a request is handed
to the queue. A fifth lever, `reasoning_effort` per model family (`chat_effort`), exists but its
default table is **empty**, so a plain chat answer does not change with the level unless the
hub environment names the model families.

Proposal: **effort is a variable of the run** (`{{effort}}`), set by the choice in the chat. Every
node parameter may hold an *effort table* (a value per level) instead of one value:

| node | quick | balanced | careful |
|---|---|---|---|
| Limits | 4 turns, 2 calls, 90 s | 14, 12, 180 s | 16, 14, 300 s |
| Model | `reasoning_effort` low | medium | high |
| Second look | off | off | on, by another model |
| Plan / Steps | skipped | skipped | plan, each step checked |

The tables are edited in the flow editor, so tuning "how careful is careful" needs no release.
The effort offer is a **suggestion, never a switch the hub flips alone**: when a request looks
serious (it would change a machine, touches a repository, asks about something that went wrong),
the chat shows a button "check this carefully?", the same mechanism as the existing check offer.

### 3.7 Inbox

Decisions of the maintainer. The **inbox** is where a session starts (on the desk, the letters on
the tray). It shows what the person has not read yet, each item with the **application** it
comes from: the result of a hand-over, a finished background run, a proposal waiting for its click,
a failure, a node that came back.

- It is **one list in three groups**: **Decide** (an urgent item that waits for the person: a
  proposal waiting for its click, a question about the notes, a fix whose drafts keep failing),
  **Errors** (a failed task or hand-over, a mistake the second look found) and **Results**
  (everything else). Within a group the newest comes first.
- A **read item leaves the inbox**. Nothing is filed, pinned or marked (D111): the thing the item
  is about (the task, the hand-over, the report) stays in its application, which is where the
  person finds it again. An item can be deleted.

```
inbox_items  id, user_id, application, kind, source_type, source_ref, title, summary, ref,
             urgent, created_at, read_at, resolved_at
```

The summary is short and the rest is a reference (a run, a hand-over, a task), never a copy of
sensitive text: an item from Admin shows what the person may see in the inbox and opens the
application for the rest.

#### Reading and urgent items

- Clicking an item **opens** it and marks it **read**; it leaves the inbox at once.
- An item carries the tag **urgent** when it needs an action or attention: a proposal waiting
  for its click, a failure, a stuck task. Opening an urgent item marks it read, but it stays
  visible (red, in its group) until the matter is **resolved**: the proposal answered, the failure
  acknowledged or fixed, the task running again.
- "Mark all as read" reads everything except what still waits for an action.
- There are **no marks** (comments, Post-Its, stickers, highlights), **no board** and **no regular
  re-check** of pinned answers any more: D111 replaced D68 and the board of D61. The maintainer did
  not use them, and re-checking an answer nobody asked about again cost model time.

#### Applications as correspondents

The whole system reads like a **correspondence between people with their own duties**. Every
application has a *letterhead*: its **responsibility** (what it is for), its **clearances** (tools,
memory scope, knowledge sources it may use) and its **abilities** (flows and windows). A hand-over
is a letter from one correspondent to another; the stored dossier is the carbon copy. The map
editor (section 3.2) edits exactly these three things.

### 3.8 Hand-over between applications

Decision of the maintainer: when Chat cannot do what was asked (the request needs the servers, a
repository, files), it shows a **button in the chat**. The button hands the request on in the
background, and the agent prepares a **proper hand-over** instead of forwarding the sentence:

1. **Dossier.** The agent writes a documented request: the question in full, the part of the
   conversation that matters (chosen by the agent, quoted), what is asked, in which form the
   result should come, and a list of what it **kept back** and why. The person sees the dossier
   folded under the button **before** the click; the click sends exactly that.
2. **Record.** The hand-over is stored (`handovers`: from, to, dossier, hash, status, summary). The
   dossier cannot change after it was sent (the hash, as for a proposal, D43). Both applications
   show it, so the person can always see what was asked and what was exchanged.
3. **Work.** The receiving application works with its own tools, memory and flow. Chat's memory
   is not handed over, only the dossier.
4. **Result.** A **summary** arrives in the inbox, marked with the application it comes from; the
   person may open the application for the details. The receiving agent writes the summary and
   keeps sensitive details out of it.
5. **Structural guard.** The agent's care is not the only protection: a result stays an item of
   the receiving application and does **not** enter the sending application's memory or context
   unless the person pins it. Separation holds even when a model slips.

Coordination in the other direction (Admin asking Chat) works the same way and needs the same
click. There is no standing release between applications.

**The Project application is different in one respect (D81-D84).** It plans over several steps and
needs what the other applications found out. Between the Project application and Research,
Development and Administration, *read-only* requests and their results follow typed messages along
the edges of the registry without a click, inside the grants of the project (`scope`, level `free`;
`confirm` keeps a click per request). A result that the code verified and a restricted reviewer run
accepted becomes a dated, sourced part of the project's Akte and reaches the agent's context through
the digest and the tools; the structural guard above holds until then (raw foreign text never reaches
an agent that can write). *Changing* anything outside the Project application stays an order that
needs the person's click, or a mandate that the person created for one action kind, one repository
and one exact branch (D83). Chat, Incognito (3.9) and the hand-over above are unchanged.

### 3.9 Incognito

Decision of the maintainer: a button in the regular chat opens an **incognito folder**. A
conversation in it is **context-free**: its own generic system prompt, **no tools, no servers, no
memory, no wiki, no shared set**; only **web search and the general internet**. It is not
recorded. (The mode that exists today, D53, keeps the conversation and only takes tools and
notes away; it becomes a small switch "do not learn from this chat".)

- **It may ask the other applications.** The request is a hand-over (section 3.8) that the person
  approves; the person passes the answer into the incognito context. The receiving application
  records the **request and its result** (traceability), but **not the context** it came from:
  the sender is just "incognito", with no conversation and no excerpt beyond the dossier the person
  approved. The answer also lands in the incognito folder.
- **Nothing ends it by itself.** No idle time, no deletion when the tab closes. Incognito tabs
  stay until the person deletes them (the burn button: the letter burns or goes through the
  shredder on the desk, the lines dissolve into noise characters in the Terminal) or the hub
  restarts. They live only in the hub's memory, so a restart removes them, as intended.
- **Bounded memory**: at most 20 tabs and 200 messages per tab, shown in the window.

The guarantee is **"never written", not "deleted afterwards"**: a deleted row still lives in
Postgres until vacuum, in the write-ahead log and in every backup. So no row is created for an
incognito conversation (conversations, messages, memory, digests, segments, reviews, flow
runs, agent tasks, inbox). The proof is a test, not a promise: one complete incognito turn must
leave **zero new rows in every table** and must not put the question text in the logs.

What it does **not** hide, and the window says so in one line: a web search leaves the hub (the
search engine's own logs); a model node keeps the prompt in its working memory until it is
overwritten and may log it depending on its settings (to be checked per node); the host's swap is
outside Topcoat; the person can copy what they see; and a request to another application is
recorded there with its result.

## 4. Triggers and the scheduler

- **Storage**: `flow_triggers` (flow, kind, config, enabled, owner, `next_at`, `last_at`,
  `last_status`, missed-run policy). The next time is stored in Postgres, not kept in memory, so a
  restart neither forgets nor doubles a run (the Node-RED docs warn about exactly this).
- **Service**: one `TriggerService` ticks, fires due timers, subscribes to the event bus for event
  triggers and to the chat store for message triggers. Missed runs: *skip* (default) or *run once*.
- **What a run knows**: `{{trigger.kind}}`, `{{trigger.event}}`, `{{trigger.at}}`, so one flow can
  serve several triggers and a Fork can ask which one fired (Home Assistant's "triggered by").
- **Limits**: per person runs per hour, a chain depth of three, a stored run for every firing, a
  visible pause switch per flow and one for the whole hub.
- **Rights**: a triggered run has the rights of its owner and is **read-only by default**. Any
  state change is a **Proposal** that waits in the owner's inbox for one click (D15, D43). No
  automatic confirmation, ever, whoever started the run.
- **Across nodes**: a **Placement** node records where each step ran (hub node, model, waiting
  time, fallback). The live view shows it on every step, so a run can be followed across the
  network of nodes and models.
- **First use** (the maintainer's priority): a node came online. `node_updated` is published on
  every change of a node's summary, so the trigger compares with the last known state and fires
  only on the change to *online*. Today a handed-over task that waits for a fast node polls every
  `handoff_poll_seconds`, and a task that ended because no tools were reachable
  (`stop_reason` `tools_missing` or `no_tools`) stays ended until someone presses "continue".
  The system flow *Resume what was stuck*: Event (node online) -> find the person's tasks that
  waited or ended for lack of a node -> For each, resume (the existing `resume`, with its limits
  on active tasks) -> Notify in the inbox ("three tasks continue"). All current tasks belong to
  Chat, so this serves Chat first. The exact stop reasons to include are checked when it is built.
- External model nodes stay out until the redaction layer exists (stage 3).

## 5. One UI

Decision: **workspaces are the only UI**; the classic pages go. Backups of the old UI exist.

Removed: the page chain in `App.tsx`, the user dock and admin side bar (the shell draws the menu
from the registry), the stored mode and its switch, the `classic` strings, the separate calm chat
page. Kept: the module registry, the shells, the stack form for narrow screens, deep links
(`#/admin/models` and friends redirect to the window that owns the page).

Chat becomes **one module** with a "details" switch (the chain folded or open, the pickers shown
or hidden). That removes the whole class of bug where a feature reached one chat surface and not
the other. The Settings window keeps only what is about a person. The admin pages become modules
of the **Admin** application.

Two shells stay, renamed (decision of the maintainer). Both open with **an empty chat
window and the inbox**, nothing else; windows are added on demand.

- **Terminal** (was "tile"): the default. Idle animations in ASCII while nothing happens. Every
  application has **one colour**; its tag, its windows' frames and its inbox lines carry it.
- **Schreibtisch** (was "desk"): a physical **tray with letters** and a pin board with **red
  threads** that join topics, as in a detective film. A thread is drawn from relations the data
  already has (a memory note that disputes or replaces another, a chat that continues another, a
  task and the conversation that started it, a flow run and its chat, a hand-over and its
  answer) and, later, by the person.

#### The logic behind how things look

One rule for both: **the material says where something comes from and how far to trust it; the
weight says how important it is.** What needs a decision looks sealed; what only informs looks
open.

| application | Terminal | Schreibtisch |
|---|---|---|
| Chat | green, plain lines | postcards and ordinary letters, handwriting |
| Research | amber; results as numbered pages (`[2/5]`) | a **stack of papers** with a paper clip; the key lines **highlighted** with a marker |
| Dev | cyan; diffs and test lines in their own colours | index cards and labelled folders, **blue Post-Its** for short status, test results as a printed strip |
| Admin | red or magenta with a `RESTRICTED` header; a prompt that needs a typed word | **Top Secret** envelopes: manila, red stamp, tied with string; opening breaks the seal |
| Documents (later) | white on dark, like paper | folders |
| Music (later) | violet | sheet music with pencil marks |

| kind of message | Terminal | Schreibtisch |
|---|---|---|
| information | one tagged line `● [CHAT]` | a postcard or a **Post-It** |
| result | the tag, a title and a count | a stack of papers, the changed line in **marker** |
| needs a decision (proposal) | the line stays inverted until answered | a **sealed envelope** with a red tag |
| failure | red line, blinks until read | a **red Post-It** on the tray's edge |
| hand-over | `Chat -> Admin` with the dossier opening below | an **internal-mail envelope** with a routing slip, moved from one tray to the other |
| unread | a filled dot | the envelope is unopened and lies on top; read items move to a stack |

## 6. Memory

Decision of the maintainer, recorded as ARCHITECTURE D74 (it replaces the earlier "shared set plus
application" rule):

- **Every program has a memory of its own, apart from the others.** Chat, Research, Development
  and Administration each keep what they learned; no program reads another program's memory, and
  a program's memory page shows its own notes only.
- **The core** is what holds for the person everywhere: who they are, what they like, how they
  want answers (the kinds `person`, `preference`, `style`, `rule`). Every program reads it. A
  fact about a place or a day of work (`operational`) is always some program's own.
  The core is shown in Settings › Memory.
- **The frame of a program** sets its assistant's tone and a few binding rules (Administration
  answers like an operator on duty, Chat like a well-informed friend). It stands in the core block
  after the system rules and comes before the person's own style where they differ. An admin sets
  it on the program.
- **Tags in one tree per person.** Every note carries one to three tags that say what it is about.
  The top level is fixed (person, preferences, everyday, knowledge, development, infrastructure);
  below it the person and the assistants add tags — an assistant only when no tag fits, and the
  person sees what it added. Tags make notes findable: a word of a question that names a tag
  recalls the notes below it.
- **A branch says who is responsible for its topic** (inherited down the tree: infrastructure →
  Administration, development → Development, person → the person) and whether it is **sensitive**
  (health, finance, private by default): notes of the core with a sensitive tag are hidden from a
  program until the person releases the branch for it.
- **The cross-check** compares the memories in the care run. Two notes of different programs that
  contradict each other are reported to the program responsible for the topic — not to the owner
  of either note. That program decides in its own frame (keep, correct, retire each note, or ask
  the person) and alone chooses what the other program is told. A topic of the person, or a
  sensitive one, is always a question to the person.
- **The wiki** (the knowledge base) is readable by every agent, read-only, by default; the release
  is per source or collection.
- Migration: the old tags and the person's deny and allow rules became the tree; every note got a
  tag; a fact about a place that sat in the shared set went to Chat.

## 7. Order of work

| step | what | size | ships alone | deletes |
|---|---|---|---|---|
| P1 | **One UI**: workspaces only, an empty chat window and the **inbox** as the opening state, chat as one module, redirects for old addresses | M | yes (web only) | classic pages, dock, side bar, mode switch, calm page |
| P2 | **Memory areas** and the Dev corner (migration, store, recall scope, page, export) | M | yes | the single-axis page |
| P3 | **Triggers**: timer, event, message, manual; `TriggerService`; system flows shown read-only | L | yes | none yet |
| P4 | **Policy nodes**: Placement, Limits, Second look, Recall, Write memory; the chat answer as a built-in flow, replacing the code path | L | yes | the old answer path in `api/chat.py` |
| P5 | **Settings move**: quality levels and node settings into flows; a slim "hub defaults" page | M | yes | model per level, quality level, duplicated pages |
| P6 | **Application map**, application CRUD with rights, and the **hand-over** (button, dossier, record, summary into the inbox) | L | yes | the workspace admin form |
| P7 | more nodes: Parallel/Join, For each, Retry/Catch, Sub-flow, Delay, Notify, Say, Proposal inbox | M each | each alone | |
| P8 | tool groups for Documents, Mail draft, Chords, Image (separate briefs) | per group | each alone | |

Reasoning: P1 and P2 are independent and shrink the surface everything else sits on, so they go
first. P3 before P4 because triggers are what make flows *run by themselves*, and P4 then has
things to start from. P5 must follow P4; deleting a setting before its node exists would leave a gap.
P6 comes when there are enough flows to bundle.

## 8. Invariants (from the baseline, none of them negotiable)

- A flow arranges read-only pieces; it adds no power. Rights are the owner's, intersected with
  the application's grants.
- State changes are proposals with one click of a person (D15); no blanket approvals; no
  self-confirmation, also not from a trigger.
- Every row of a person carries `user_id`; triggers, runs and memory areas are scoped by it.
- Nothing that sends data to an external model without the redaction layer.
- No free-code nodes. Nodes are named actions with schemas.
- A built-in flow can always be put back to its original; the way back for a whole release is the
  previous image and the backup.

## 9. Decisions and open questions

Decided by the maintainer:

1. **Effort** stays as one simple choice (quick, balanced, careful), independent of model, node and
   tools; the model per level goes (section 3.6).
2. **Two shells**, **Terminal** and **Schreibtisch**; both start with the **inbox** and an empty
   chat window.
3. **Applications**: Chat, Research, Dev, Admin. **Homelab is part of Admin**. They act as
   correspondents with a responsibility, clearances and abilities.
4. **Each application has its own memory, rights and flows**; a **shared set** (everything about
   the person) is available everywhere, filtered by tags; the **wiki** is readable by every agent,
   released per collection (section 6).
5. **Hand-over** between applications by a button, with a dossier the person sees before the click,
   a stored record, and a summary in the inbox (section 3.8).
6. **Incognito**: context-free, web only, may ask other applications with the person's approval,
   stays until the person deletes it or the hub restarts (section 3.9).
7. **Reading**: a click opens and marks read, and the item leaves the inbox; **urgent** items stay
   until resolved; no filing, board, marks or re-check (section 3.7, D111).
8. **The first trigger** is "a node came online", to resume stuck tasks (section 4).
9. **No compare run**: the chat answer is built directly as a flow and tuned afterwards.

Defaults the implementation takes unless the maintainer objects (each can be changed later in the
editor or in a setting):

- Tags denied everywhere except where released: `private`, `health`, `finance`.
- Wiki collections: Chat reads the ones the person marks "general"; until the person marks any,
  Chat reads none (the safe default), Admin reads all.
- An application's notifications are on and the person can turn them off per application.

## Status of this plan

Built: P1 (section 5), P2 (section 6), the incognito folder (3.9), the first trigger (a node
comes online, section 4), the hand-over (3.8, D65), effort as a run variable with tables
(3.6, D66), the chat policy nodes (3.4, D67) and the inbox in three groups (3.7, D111; marks
and the re-check of D68 were built and removed again).
Not built: the flow-editor side of the triggers (3.3), the application map (3.1, 3.2), the chat
answer itself as a flow (3.5; the policy already decides how it is made), settings moving into
flows, and the new Schreibtisch look (5). See the roadmap, stage 8.

Known limit of what is built: the workspaces still give Chat every tool group; now that the
hand-over exists, narrowing it is a decision for the application map (P6).

## Sources

- Home Assistant automations: <https://www.home-assistant.io/docs/automation/condition/>,
  <https://www.luisllamas.es/en/home-assistant-automations-triggers-conditions-actions/>
- Node-RED context and the inject node: <https://www.nodered.org/docs/user-guide/context>,
  <https://flowfuse.com/node-red/core-nodes/inject>
- ComfyUI execution and caching: <https://www.mintlify.com/Comfy-Org/ComfyUI/concepts/execution>,
  <https://pyshine.com/ComfyUI-Node-Graph-Execution-Engine-Source-Tour/>
- LangGraph Studio: <https://docs.langchain.com/langgraph-platform/langgraph-studio>
- Comparisons (secondary, vendor-adjacent): <https://www.lowcode.agency/blog/n8n-vs-node-red>,
  <https://www.lowcode.agency/blog/n8n-vs-windmill>, <https://www.gamut.so/blog/n8n-alternatives>,
  <https://futureagi.com/blog/dify-vs-flowise-vs-langflow-2026/>
