# Block programs

A function of a program (PRG-00, D104) written as typed blocks. Code: `hub/src/topcoat_hub/blocks/`. API: `/api/blocks` (admin).
A program is JSON; the validator (`program.validate`) runs when it is saved and before every run.

```json
{"name": "research_report", "inputs": {"question": "text"}, "stack": [
  {"b": "agent", "app": "research", "depth": "thorough", "prompt": "{{question}}", "out": "draft"},
  {"b": "check", "in": "draft", "question": "question", "pick": {"other_than": ["draft"]}, "out": "verdict"},
  {"b": "repeat_until", "cond": "verdict.ok", "max": 3, "stack": [
    {"b": "revise", "draft": "draft", "issues": "verdict", "out": "draft"},
    {"b": "check", "in": "draft", "question": "question", "pick": {"other_than": ["draft"]}, "out": "verdict"}]},
  {"b": "file_report", "in": "draft", "title": "{{question}}"}]}
```

## Types and variables

`text`, `texts` (a list), `bool`, `verdict` (`ok`, `issues`, `model`), `compare` (`agree`, `verdict`, `text`, `differences`, `judge`).
A variable is written before it is read. Templates (`{{question}}`, `{{verdict.issues}}`) may name only defined variables.
After an `if`, only what both branches define exists.

## Blocks

| block | does | writes |
|---|---|---|
| `ask` | one request to a model (`prompt`, `system`, `pick`, `max_tokens`, `temperature`) | text |
| `agent` | a task of an application (`app`, `depth`) under the person's rights and the application's tool ceiling (D103) | text |
| `check` | a model reads `in` (against `question`) and answers `ok` + `issues`; `ok` with issues is not ok | verdict |
| `revise` | a model rewrites `draft` with the `issues`; default pick: the author's model | text |
| `set` | a text from a template | text |
| `if` | `cond` (`v.ok`, `not v.ok`, `c.agree`, a `bool`), `then`, `else` | what both define |
| `repeat_until` | `while not cond`: body, bounded by the **progress guard** (a round that changes none of the variables the body writes ends the loop) and an optional count `max` (0 = none) | body |
| `parallel` | 2 to 8 `lanes`, at most `max_parallel` at a time (1 = one after another); lanes write different variables | lanes |
| `compare` | the third agent: verdicts by a fixed rule (`all`, `majority`), texts by a judge model that did not write them (anonymous, order shuffled; `pick` runs twice with the order swapped and counts only a winner of both rounds; `merge`, `flag`) | verdict / compare |
| `for_each` | a body per item of a `texts` | body |
| `stop` | ends the run with a reason | |
| `file_report` | files `in` in the Ablage (a new version of the report the agent filed, else a new report) | |

## Picks

A pick chooses model and node from the pool (`pick.py`, pure): `fixed` (`model` or `model@node`), `same_as` (a variable's author),
`other_than` (variables whose authors are avoided; with no other model in the pool the best remaining one is used and the run says
the result is not independent), `kind` (default `chat`), `rank` (`best`, `fastest`, `largest`, `loaded`, `rated`: the best score of the bench suite named by `suite`, default `answer`; with no current rating it falls back to `best` and the run notes say so), `node`, `min_tps`, `exclusive` (unload the other models of the node before the request: a small card holds one model at a time; skipped while the node answers somebody else).
Only models of the node pools are candidates (`NodePolicy.pools`).

## Run

`POST /api/blocks/runs {"name": "research_report"|"program": {...}, "inputs": {...}}` → `202`, `GET /api/blocks/runs/{id}` shows
status, variables, authors, notes, and the steps (`step` start/done/end, `pick`, `branch`, `loop_end`, `run`), `POST .../stop`.
At most 3 runs per person at once; runs are kept in memory for 30 minutes.

Not yet (R4/R5): the editor, saving programs per function of an application, hooks in the agent run.

## Hooks of the agent block (R5)

The `agent` block takes `hooks` (`agent_hooks.py`) that switch the fixed checks of one agent run on top of the application's frame: `step_check` (a small model looks at every tool result; **off by default**, the setting `step_check_enabled` is now off too), `claims` (rules on the answer: a claim about passing tests, a branch or an announced step needs backing), `second_reader` (a second model reads the draft against the tool results), `repeat_guard` (1 to 20 equal calls), `stall_guard` (0 to 50 model turns without new evidence). A hook that is not named keeps what the frame gives. The tool ceiling, the rights of the person and the node whitelist are not hooks. The chat's own functions (answer pick, tool decision, reactions, second look) are **not** block programs yet; they need parity tests against the old path first.

## The bench (R6)

`hub/src/topcoat_hub/bench/`: shipped suites (easy: `answer`, `instruct`, `json`, `review`; harder: `reason`, `context` (facts in a long text), `tools` (a tool call as JSON), `german`, `review2` (subtle errors); small general cases, no private data), scored by rules only (`scoring.py`: must / must_not / regex / words / JSON keys, never a model as judge), `BenchService` (one run per node at a time, last place in the queue, stoppable, models of the pool only) and `model_ratings` (one row per finished suite; the suite `version` goes up when a case changes, older ratings are shown but never rank). API `/api/bench` (admin): `suites`, `runs` (start, read, stop), `ratings`. Parameter sweeps per model (`model_profiles`) are not built yet.
