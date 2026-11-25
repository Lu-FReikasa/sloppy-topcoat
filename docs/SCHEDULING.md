# Scheduling: which model, which node, in what order

> **Status: decided (ARCHITECTURE D85 to D90, maintainer, 2026-10-30), built in part.** Built: SCH-02 (a summary is
> background work; a check never loads a model that pushes another out, may use the author's own model and records
> its node; speeds are measured from the first token of any kind: sections 4 and 7, in part) and SCH-03 (section 3:
> the queue as a pure choosing function with a size from `n_parallel`, the node chosen before the prompt, the
> record of model requests; D94 and D95; the lists per job still come from the old roles). **Built:** the table
> (section 2, SCH-04, UI-02: stored, read by the resolver and the gateway, edited on the page, the older lists converted once at
> start; `wait_seconds` is not used by the queue yet). **Not built:** the chains (section 5, SCH-05), the bench and the ratings in the product (section 7, SCH-06,
> QLT-01) and the GUI of section 8 (UI-02, OBS-01). Numbers measured on the way: the measurements. How
> the hub works today is in [OVERVIEW.md](OVERVIEW.md); this page is the target. The work is split into briefs, see
> the roadmap section 2.

The rule fits in one sentence:

> Every piece of work goes into one queue on the hub, and when a node has room it takes the most
> important work it is allowed and able to do.

Everything else on this page is a detail of that sentence. If a change makes the sentence false or
needs a second sentence to explain, it is the wrong change.

## 1. Goals

1. **Simple to understand.** A person can predict from one table where work will run. The GUI can say
   why something runs where it runs, or why it waits.
2. **No idle node while work piles up** on another, as long as the idle node is allowed to do the work
   and has a model for it.
3. **Works on one machine.** Hub and a single node with a single model is a supported setup, with
   checks. Several nodes are recommended because they are faster, never because something needs them.
4. **No surprises.** No model is loaded for background work unless there is room. No configured rule
   is bypassed by a special path.
5. **Readable afterwards.** Each piece of work leaves a record of what was chosen and why.

## 2. One table

The whole configuration of model and node choice is one table with one row per application, plus two
system rows. It lives in the node policy (`hub_settings.node_policy`, key `applications`).

| row | priority | models, in order | nodes, in order | wait for the first model |
|---|---|---|---|---|
| Chat | high | `gemma-12B` | `node-amd`, `node-laptop` | 10 s |
| Development | high | `coder-30B`, `gemma-26B` | `node-amd` | always |
| Projects | normal | `gemma-26B`, `gemma-12B` | `node-amd`, `node-nvidia` | 60 s |
| Administration | normal | `gemma-12B` | all | 30 s |
| Check | normal | `qwen-14B`, same as the answer | `node-laptop`, `node-nvidia`, all | do not wait |
| Background | low | `qwen-14B`, same as the answer | `node-laptop`, `node-nvidia`, all | do not wait |

The values are an example. Rows come from `proto/ui/applications.json` (applications) and two fixed
system rows, `check` and `background`. A row may be left empty:

| field | when empty |
|---|---|
| priority | `normal`; `background` is `low` |
| models | what is loaded on an allowed node, else the smallest model that fits. So a fresh install with one model needs no configuration. |
| nodes | all nodes |
| wait | 30 s; `check` and `background` do not wait |

Two special values exist:

* **`same_as_answer`** in a model list (check, background): the model that wrote the answer. It is the
  default of both system rows, and it is what makes a one-model setup work.
* **`all`** in a node list: every other node, after the ones named before it.

What a person picks in a conversation (`model` or `model@node`) wins over the table. Requests through
the `/v1` gateway name their model and use the node list of a row `api` (priority `normal`).

The embedding model and the pause of all background work stay separate settings. A node that should
stay free of some work simply is not in that row's node list; a machine that plays games is handled
by its node state (`busy_gaming`), as today.

**What this removes:** the node default models (`defaults`), the four role lists and their application
copies (`roles`, `app_roles`), `order`, `background_nodes`, `no_chat`, `no_background`,
`quality_models`, `second_look_model`, `user_settings.default_model`, `handoff_slow_nodes`, the
hub-wide default model, `auto` (the largest model that is fast enough), and every fallback to "any
other model". The role `agent` is not a separate list any more: an agent run of an application uses
that application's row and only asks for more context (`min_context`).

## 3. One queue

The queue lives in the hub, in memory. A request does not choose a node when it arrives. It waits
without a node until there is room (*late binding*), and that replaces the queue per node
(`PrioritySlots`), where a request was stuck behind a node that had been chosen too early.

**A piece of work (`Job`) carries:** work label, application, `user_id`, conversation, message or task,
the row's lists, `min_context`, the node it would like to stay on, the jobs it waits for, priority,
time of arrival.

**Room** means a free slot of a loaded model (`n_parallel` of that model on that node, reported by the
node), or a node that can load the model.

**When room appears, or a job arrives, the hub goes through the waiting jobs:**

1. **Allowed and able.** Only jobs whose row allows the node, whose model the node has, whose
   `min_context` is possible there, and whose dependencies are finished.
2. **Most important first.** Higher priority first; within a priority the oldest first. Waiting raises
   a job one level every five minutes (a setting), so nothing starves.
3. **Best model the row allows.** Take the highest model in the list that can run *now*. A higher
   model that cannot run now is waited for only as long as the row says.
4. **Loading.** `high` and `normal` may load a model and may push another out. `low` loads only into
   free memory and never pushes anything out. `check` loads only into free memory too.
5. **Staying.** A follow-up in a chat or the next turn of an agent run prefers the node it was on,
   because the prompt cache lies there. An agent run stays on its node unless that node fails; a chat
   stays within the row's waiting time.
6. **Keeping out of the way.** `low` work does not start on a node where more important work runs or
   waits. Other nodes keep working.

Running work is **never cancelled** to make room. Important work waits for a free place.

**Why one queue and not one per task or per node type:** a queue per kind of work brings the problem
back, because a node would have to decide which queue to look into. A job's lists already say which
nodes fit, so the hub filters one list.

**What stays idle, on purpose:** a node with no allowed work or no model for it. The GUI says so ("3
jobs wait; `node-nvidia` is free but has no model they may use").

**After a restart** the queue is empty. An answer has its own hub task (D39) and fails visibly;
schedulers create their background work again. A durable queue was dropped before and stays dropped.

## 4. Context and checks

The hub already holds context the right way, and that is what makes all of this possible.

**How context works today:**

* **Every model request is complete in itself.** The hub stores the conversation and assembles the
  whole prompt for each request (`chat.build_prompt`, with the budget from the context length of the
  model on that node). llama-server keeps nothing the hub depends on.
* **A slot** of llama-server has its own window (`-np` slots, `-c` = context x slots, `kv_unified` off).
  Its cache only saves work: with `cache_prompt` the common start of a prompt is not computed again.
  It holds no meaning, and a lost cache costs time, not correctness.
* **A check is already a fresh context.** `ChatReviewer.review` builds a new prompt from the database:
  the question, up to five earlier messages, recalled notes, tool results and the answer. It does not
  continue the author's context.
* **A summary** is built the same way from the stored messages.

**So a check does not need a second node.** Three things are separate:

| what | what it gives | needed? |
|---|---|---|
| fresh context | the checker does not carry the author's reasoning and is not led by it | **always** |
| another model | other systematic mistakes, especially in what a model believes it knows | helps, optional |
| another node | no quality at all: more speed, no lost cache, can run in parallel | helps, optional |

A check compares an answer with evidence (notes, tool results). For that, the same model with a fresh
context is a real check. It is weaker where the model has to judge from its own knowledge. We measure
this instead of assuming it: part B of [EVAL.md](EVAL.md) can run the same cases with the same and with
another model, so the catch rate and the false alarms can be compared.

**Rule:** a check may use the author's model (the rule "another model than the author" is removed). The
stored review records whether it was the same model and the same node, and the GUI says so.

## 5. Chains

A job can wait for other jobs. The chain after an answer:

```
answer ─────────────────┐
check, prepare (optional)┴─► check, verdict ─► summary ─► notes
```

* **A summary is written only after the check**, so a false statement does not land in the summary. If
  the check is skipped or fails, or does not finish within a set time (default 10 minutes), the summary
  is written and its statements are stored as unchecked. It never waits for ever.
* **Parallel preparation.** If a second node has room while the answer runs, the check starts there and
  takes in tool results as they finish, as prompts that only grow at the end, so each one reuses the
  cache. The answer comes last, and the verdict is then quick. Claims about the answer can only be
  judged once it exists. **Off by default** (`TOPCOAT_CHECK_PARALLEL`) until measured on real machines:
  the gain depends on the prompt cache.
* **Without room** the check simply runs after the answer. Nothing else changes.

## 6. One machine

The same rules, nothing special:

```
node-amd, one model, one slot:
answer ─► (quiet time) ─► check ─► summary ─► notes     each with its own fresh prompt, one after the other
```

* All rows are empty, or their lists hold one entry. `same_as_answer` gives the check its model.
* The queue runs one job at a time, so no two contexts compete.
* **The price is time and the cache.** A check on the only slot pushes the chat's prompt out of the
  cache, so the next message computes its prompt again. llama-server may keep some of it in RAM, which
  has to be verified for the pinned version.
* **Quiet time.** On a node with no free slot, a check of an answer waits until the chat is quiet
  (default 30 s), so it does not stand in front of the person's next message. A running check is not
  cancelled.
* A hub on the same machine as its only node stays supported. Docs and defaults must not assume a
  second machine, and tests cover a one-node setup for every chain.

## 7. Measurements

Per application, model and node, the hub folds what it sees anyway into a small table:

| value | source | used for |
|---|---|---|
| tokens per second, time to first token, load time | gateway | **automatic**: estimating which node is quickest |
| aborts, context full, invalid JSON, failed tool calls | gateway, agent steps | shown; warnings |
| check verdicts per author model | `chat_reviews` | shown as a hint only |
| checked and unchecked statements of summaries | `chat_digests` | shown as a hint only |
| the person's feedback | `messages` | shown as a hint only |
| tests passed, push accepted | PPH-05, verification by code | shown; the hardest signal |

These hints are not comparable: a model that gets harder questions looks worse. So:

* **Speed may decide on its own.** It changes which node runs a job, never which model.
* **A running rating from the second look (D92, QLT-01).** The verdicts of the second look and the
  checked statements of summaries are folded into a rating per model and application, with the number
  of samples. It is shown next to the bench and feeds the recommendation; it does not steer.
* **A floor as default (D92).** A row with no model, or one below the floor, falls back to a minimum
  requirement or recommendation per application. A chosen row wins over it.
* **Quality only makes suggestions.** Comparable numbers come from the **bench**
  (`scripts/dev/topcoat_eval.py`, deterministic scoring, no model as judge): the same tasks per
  application, node and model. The GUI then says "`coder-30B` passes 18 of 20 development tasks,
  `gemma-26B` 11 of 20: use it?". The person changes the table with a click.

## 8. What the GUI shows

**Administration, Machines.** One compact row per machine, with what it does now: state, CPU, GPU, RAM,
VRAM, work (kind, chat title, message, model, seconds). Virtual machines below their host. A machine
with one node does not show the node as a second thing. Waiting jobs below, each with the reason.
Another person's chat is shown without its title.

**Administration, Models.** Three tabs:

* **Models:** the catalogue, what exists where, tags and measured values.
* **Applications:** the table of section 2, and the suggestions of the bench.
* **Rules:** the pause, the settings of the queue, and a live preview: "a chat message would now run on
  `node-laptop` with `gemma-12B`, because `node-amd` has no free slot and the wait of 10 s is over".

## 9. How it maps to the code

| today | target |
|---|---|
| `node_policy.py`: roles, defaults, lists | the table; migration from the stored policy once at start |
| `model_resolver.py`, `node_chain.py` | one function that picks for a job and returns the reason |
| `gateway.py`, `PrioritySlots`, `candidates`, `gateway_max_streams_per_node` | **built** (SCH-03): `JobQueue` in `job_queue.py`, capacity from the node, `Gateway.reserve`; `candidates` stays until the table |
| `chat_review.py` `pick_reviewer`, `chat_digest.py` `pick_checker`, `_summarise` | the rows `check` and `background` |
| `node_speeds`, `node_benchmarks` | measurements per application, model and node |
| `api/scheduling.py`, `SchedulingPage.tsx`, `ActivityPanel.tsx` | the GUI of section 8 |
| text log, `chat_digests.checker_*` | **built** (SCH-03 step 3): `job_records`, see D95 and brief SCH-03-hub-queue; the log line stays |
