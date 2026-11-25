# Flows

A flow is the set of steps between a question and its answer, drawn as a graph. This page is the
user guide; the reasons are in `docs/ARCHITECTURE.md` (D59).

Open **Admin → Flows**. The list on the left holds the flows; the canvas shows the selected one.

## The nodes

| node | what it does | exits |
|---|---|---|
| Start | where a run begins: the question that was asked | `out` |
| Model | asks a model once and hands on the answer (optionally as a JSON object) | `ok`, `fail` |
| Plan | a model splits the task into at most N steps (`title`, `goal`, `check`) | `ok`, `fail` |
| Look at servers | the read-only check of the servers, with the rights of the person who runs it | `ok`, `fail` |
| Steps with check | does the steps of a plan one by one; a second model reads each result | `ok`, `fail` |
| Fork | sends the run one way or the other by a condition | `yes`, `no` |
| Result | where a run ends: what is handed back | — |

A flow needs exactly one Start and at least one Result, no cycles, and every node must be
reachable from the Start. The editor refuses what can never be right (a connection into the
Start, a node to itself, an exit that does not exist) and **Check graph** names every other fault
at the node it belongs to.

## Handing values on

Text parameters, the prompts included, may contain references:

- `{{input.question}}` — the question
- `{{prev.out}}` — what the node just before handed on
- `{{plan.out}}`, `{{plan.out.steps[0].title}}` — the output of any node by its name, and a path
  into it
- `{{params.max_steps}}` — a parameter of the node itself
- in a step runner: `{{step.title}}`, `{{step.goal}}`, `{{step.check}}`, `{{notes}}`, `{{answer}}`

Nothing is evaluated: a reference is a path of names and list positions, and a path that leads
nowhere is an empty string. A template that is exactly one reference hands on the value itself (a
list stays a list), which is how a plan reaches the step runner.

## Branching, side by side, failing

- Two edges from the same exit, or from the Start, run **at the same time** (up to
  `flow_max_parallel`). A node with several incoming edges waits for all of them.
- A branch that was not chosen is **skipped as a whole**: the nodes behind it do not run, and a
  Result behind it is not reached.
- A node that fails leaves by its `fail` exit if an edge is drawn from it. Without such an edge the
  run stops and says which node failed and why.
- A run that never reaches a Result fails with that sentence; a run that takes longer than
  `flow_run_max_seconds` is ended.

### The step runner

For each step of the plan the executor (the check of the servers, or a model) does the step and a
second model — another one than the executor when the nodes serve more than one — answers
`{"ok": …, "issues": […]}`. A result that does not pass is done again with what was found, up to
**Tries again**. With **Check while the next step runs** the next step starts while a result is
checked, and the steps with findings are repeated afterwards; without it every step is checked
and repeated before the next one starts. A step that stays refused is counted (`failed`), not fatal:
the output carries `failed`, `unverified` and the text of all steps, so a Fork after it can decide.

## Versions

Every save is a new version. A run keeps the version it started with. **Put back** makes an earlier
version the newest one (nothing is rewritten). A built-in flow cannot be changed; **Save as a
copy** makes one of your own. Names are unique.

## Trying and watching

Type a question and press **Run what is drawn**: the graph on the canvas runs, saved or not, so a
change is tried before it is kept. While it runs the canvas shows the graph it started with, the
state of every node (waiting, working, done, failed, skipped) with its time and what it handed on,
the edges that were taken, the chain of what happened (the same chain the chat shows) and the
result. **Stop** ends the run.

A node that has **not started yet** can be changed for the run that is going: change its settings
and press *Apply to this run*. The saved flow is not touched; save it to keep the change. A node
that has started cannot be changed.

## What a flow cannot do

A flow arranges the read-only pieces differently; it adds no power. A run has the rights of the
admin who starts it, its check is that person's task, and what a check can do to a machine is
look. A change to a machine still needs the one click of the person (D15, D43). Flows are for
admins; a run is theirs (`user_id` on every run).

## A flow as the way a chat answers

In a conversation an admin can pick a flow under **Answer path** (top of the chat; "Usual" is the
way every conversation answered before). From then on each question of that conversation is
answered by the flow:

- The chain of the run grows in the chat as it happens, the same chain as on this page, and is
  stored with the answer.
- The flow can use `{{input.question}}` and `{{input.history}}` (the earlier turns, as lines of
  `user: …` / `assistant: …`, at most 8000 characters).
- The text of the **Result** node is the answer (its title, if it has one, goes in bold above it).
  A run that fails ends the answer with the reason; the failed step stays in the chain.
- **Stop** ends the run; regenerating runs the flow again.
- A conversation of a person without admin rights, a flow that was deleted (the conversation is
  released) and a hub with flows switched off all answer the usual way. Continuing a cut answer
  is a usual-path feature.

The flow gets no memory notes, no persona and no second look from the chat; if it should have
them, they are nodes of the flow.

## Not yet

Editing the built-in prompts outside flows and replaying recorded runs. See the roadmap (stage 7).
