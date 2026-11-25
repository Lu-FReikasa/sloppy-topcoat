# Evaluating a running hub: `scripts/dev/topcoat_eval.py`

A measurement tool for the owner: run it against the **production hub after every update** and
compare the result with the one before. It measures the system - the local models, the note base,
the check logic of the hub. It trains nothing (local models are not trained; what improves is the
note base and the checks). Single file, Python standard library only, like `agent_bench.py`.

```
scripts/dev/topcoat_eval.py selftest
scripts/dev/topcoat_eval.py run --hub URL --key-env ENVVAR --facts FILE --out results.json \
    [--parts A,B,C,D] [--model M] [--review-model NAME@NODE] [--node N] [--label TEXT]
scripts/dev/topcoat_eval.py compare OLD.json NEW.json
```

## The four parts

| Part | Question it answers | Path through the hub |
|------|---------------------|----------------------|
| **A** facts | Does the assistant know the lab? | a fresh chat per question through the chat API (`/api/conversations`, tools included) |
| **B** second look | Does the reviewer catch real mistakes without crying wolf? | `REVIEW_SYSTEM` + numbered notes through `/v1/chat/completions` |
| **C** memory | Is the right thing remembered, corrected, forgotten? | scripted multi-turn chats, then the notes read back through `/api/memory` |
| **D** agent tasks | Can the model do small development tasks? | the tasks of `agent_bench.py` through `/v1` (reused, not copied) |

Scoring is deterministic everywhere (substring checks, parsing, state read back). **No model is
used as judge.**

### A - facts against ground truth

Each question of the facts file is asked in a new chat; the runner waits for the final answer and
scores it. Recorded per case: latency, model and node (from the answer's message), the answer text
(first 600 characters) and whether the answer carried sources (`memory_refs` or a `report` in the
message; `sources_exposed` says whether the API shows the field at all).

Facts file (JSON; your real one stays **outside the repo**):

```json
{"questions": [
  {"id": "vram-nvidia",
   "question": "How much VRAM does node-nvidia have?",
   "must": ["24", ["GB", "GiB"]],
   "must_not": ["12 GB"],
   "tags": ["hardware"]}
]}
```

* `must`: every item has to occur. An item is a string or a list of alternatives (any of them).
  Case-insensitive, whitespace-normalised substring match.
* `must_not`: none of these strings may occur.
* `tags`: free text; the summary gives a pass rate per tag.
* A question needs `must` or `must_not` (a pure `must_not` question checks that the assistant does
  not invent something).

The facts file may also contain the keys `review_cases` and `memory_scenarios` (see below) so that
one private file holds everything.

### B - the second-look reviewer

Cases (`--review-cases FILE`, default `scripts/dev/eval-example/review_cases.json`):

```json
{"id": "incomplete-notes-bind", "kind": "incomplete_notes",
 "question": "...", "answer": "...", "notes": ["...", "..."],
 "expected": "ok", "expect_claim": "optional substring", "contradicting_notes": [2]}
```

* `expected`: `ok`, `hint` or `error` - the verdict the hub's `verdict_of` should derive from the
  reviewer's findings (`error` = a finding carries a note as evidence, `hint` = findings without).
* `contradicting_notes`: 1-based numbers of the notes that **really** contradict the answer (`N1`,
  `N2` ...). Required for `error` cases; empty for the others.
* `expect_claim`: a finding of the right verdict must quote this text, otherwise the case fails.
* `kind` groups the cases in the summary. The example data covers three kinds:
  `incomplete_notes` (correct answer, notes that merely do not mention the claim -> `ok`; this is
  the production failure where "not mentioned" was taken as a contradiction), `contradicted`
  (wrong answer, a note contradicts it -> `error`) and `matching` (correct answer, matching note ->
  `ok`).

The runner builds the same request as `ChatReviewer.review` (temperature 0, `max_tokens` 500, notes
as `N1: ...`), sends it with `--review-model` (else `--model`, else the first non-embedding model of
`/v1/models`) and parses the reply with the hub's own `parse_findings` / `verdict_of`. They are
imported from `hub/src/topcoat_hub/chat_review.py` when the hub package and its dependencies can be
imported, otherwise a stdlib copy inside the script is used (`review_kit` in the results says
which; a test keeps the copy equal to the hub's code). The copy lacks the secret filter.

Like the hub (`ChatReviewer.review`), the runner then asks the **second, narrow question** for every
finding that names a note (`CONFIRM_SYSTEM`, same request as `ChatReviewer.confirms`): the note
stays as evidence only if the reviewer answers `contradicts`; otherwise the finding is a hint.
`--skip-confirm` leaves that out and measures the first look alone. Part B has **no default
model**: pass `--review-model NAME@NODE` with the model the hub's second look really uses (the
first model of `/v1/models` is not that).

A case whose answer is correct (`expected: ok`) passes unless the verdict is `error`: a `hint`
(finding without a note) cannot make the agent speak up, so it is not a failure but counted in
`hint_rate`. Keep the answers of such cases free of extra claims the notes do not carry, otherwise
a hint is justified.

Reported: `false_error_rate` (ok cases judged `error`, the only verdict that can make the agent
speak up), `false_alarm_rate` (ok cases judged error/hint), `hit_rate` (hint/error cases with the
expected verdict, and the claim if given), `major_unsupported_share` (of the `major` findings with
an attached note: the share whose note is **not** in `contradicting_notes`), `unparseable` (replies
that were no `{"findings": [...]}` object - these cases fail), and a pass rate per `kind`.

### C - memory behaviour

Scenarios (`--memory-scenarios FILE`, default `eval-example/memory_scenarios.json`) are scripted
multi-turn chats of a **test user**. `{run}` in any text is replaced by the run id (`ev` + 8 hex
characters), so the notes of a run can be told apart and removed afterwards.

```json
{"id": "withdraw-by-chat", "steps": [
  {"say": "Please remember: the contact room of {run} is B-17.",
   "expect": {"note": {"contains": ["B-17"], "evidence_in": ["live", "doc", "assumption"]}}},
  {"say": "Forget what I told you about the contact room of {run}, that was wrong.",
   "expect": {"forgotten": ["B-17"]}},
  {"ask": "Which room is the contact room of {run}?", "new_chat": true,
   "expect": {"answer": {"must_not": ["B-17"]}}}
]}
```

A step is `say`/`ask` (a user message; `new_chat: true` starts a new chat, so the answer cannot
come from the chat history) or `delete_note: {contains: [...]}` (the user deletes the note on the
memory page, `DELETE /api/memory/{id}`). Expectations of a step:

* `note {contains, status (default active), evidence_in}`: such a note exists, with an evidence
  value (`live`, `doc`, `assumption`) that is allowed. Checks `note_stored`, `evidence_value`.
* `contradiction {old, new, outcome}`: after the user contradicts an earlier statement. Outcome
  `clarify` needs a clarifying question (open question mentioning the facts, or the `memory` event
  of the answer with `questions` > 0) or a `disputed` note; an old note that has silently become
  `superseded` with nobody asking is reported as *silent overwrite*. `replace`: the new note is
  active and the old one is not. `clarify_or_replace`: either (for an explicit "Correction: ...").
  Check `contradiction_handled`.
* `forgotten [items]`: no active/disputed note matches any more. Check `forgotten`.
* `answer {must, must_not}`: scores the answer of this step like part A (`answer_text`); if notes
  were forgotten before, their ids must also be absent from the answer's `memory_refs`
  (`deleted_note_not_used`).

The hub extracts notes in the background, so after every step the runner re-reads the notes and
the open questions until all expectations hold or `--settle` seconds (default 25) have passed.
Notes are found through the run id in their text and through the scenario's chats
(`/api/conversations/{id}/memory`), because the extraction may paraphrase.

**Cleanup**: at the end (also after Ctrl-C) the runner soft-deletes the notes of its chats and the
notes carrying its run id, and deletes the chats it created (their titles start with
`[eval-<run id>]`). Nothing else is touched. A soft-deleted note stays in the memory page's
"deleted" list; `--purge-deleted` additionally calls `purge-deleted`, which removes **all** deleted
notes of the test user.

### D - agent tasks

`agent_bench.py`'s tasks (and their hidden checks) run against `<hub>/v1` with the same API key
(`--model`, `--agent-tasks rename,off-by-one`, `--max-turns`). Nothing of the repository is
touched; each task works in a temporary directory.

## Setup

1. **A test user** on the hub with its own API key (`tc_...`). Never use your own account: parts A
   and C create chats and notes, and the memory of the test user is part of what is tested. Memory
   must be switched on for it. Give it the same chat settings (default model, tool scope) you
   want to measure.
2. Put the key in an environment variable, e.g. `export TOPCOAT_EVAL_KEY=tc_...` (it is read from
   there, never printed and never written to the results file).
3. Write your facts file (see the format above and `scripts/dev/eval-example/facts.json`, which
   is synthetic). Keep it, and the results, outside the repository.
4. `scripts/dev/topcoat_eval.py selftest` - checks the tool itself offline.

```sh
scripts/dev/topcoat_eval.py run --hub https://hub.example.lan:8443 --key-env TOPCOAT_EVAL_KEY \
    --facts ~/topcoat/facts.json --out ~/topcoat/eval/2026-10-14.json \
    --test-user-confirmed --label "after PR 115" --insecure   # --insecure: self-signed hub CA
scripts/dev/topcoat_eval.py compare ~/topcoat/eval/2026-10-07.json ~/topcoat/eval/2026-10-14.json
```

## Safety rules the runner enforces

* Talks only to the hub URL given; paths are relative to it, redirects are not followed, every
  request has a timeout (`--timeout`, one whole chat answer `--case-timeout`).
* `https://` only; `http://` needs `--allow-http` (local tests).
* Parts A and C need `--test-user-confirmed`; they write only under the key's own user.
* Refuses to run when `/api/me` says the key belongs to an `admin`, unless `--allow-admin`.
* Rate limit: `--pause` seconds (default 2) between cases; hard cap `--max-cases` (default 100,
  counted over all parts; the rest is counted as `skipped_by_max_cases`).
* Three failed requests in a row stop the current part (the hub is probably gone).
* Ctrl-C: the current case is dropped, cleanup runs, the results so far are written (`aborted`
  says why), exit code 130. The results file is also rewritten after every part.
* Exit codes: 0 done, 1 run aborted (hub unreachable ...), 2 refused / bad arguments, 130 Ctrl-C.

## The results file

```json
{"schema_version": 1, "timestamp": "...", "label": "...", "run_id": "ev1a2b3c4d",
 "hub": {"version": "...", "image_tag": "... or null", "user_role": "user"},
 "review_kit": "hub", "parts": ["A","B","C"], "models_seen": [...], "nodes_seen": [...],
 "aborted": null, "skipped_by_max_cases": 0, "cleanup": {...},
 "summary": {"A": {...}, "B": {...}, "C": {...}},
 "cases": [{"id": "...", "part": "A", "passed": true, "details": {...}, "latency_ms": 5123,
            "metrics": {...}, "summary": "..."}]}
```

`hub.image_tag` is filled from the `/api/health` reply when it exposes `image_tag`, `image`, `tag`
or `build` (today only `version`). The URL is not stored.

## Reading the results

* Per part `pass_rate`, `errors` (cases that failed for transport reasons, not on content) and the
  median latency. Compare runs with the same `--model` / `--node`: `models_seen` / `nodes_seen`
  show what actually answered.
* **A**: `by_tag` shows which kind of knowledge broke; `cited_rate` the share of answers with
  sources. A fact that fails in many versions is a gap in the note base, not in the model.
* **B**: `false_alarm_rate` should be near 0 - every false alarm interrupts the operator.
  `hit_rate` is the cost of that. `major_unsupported_share` > 0 means the reviewer attaches notes
  that do not contradict the claim (the "not mentioned" error); `by_kind.incomplete_notes` is the
  number to watch.
* **C**: `check_rates` per check (`note_stored`, `contradiction_handled`, `forgotten`,
  `deleted_note_not_used`, ...). A failed `deleted_note_not_used` is the worst case: the assistant
  uses what the user withdrew.
* **D**: solved rate and mean turns per model.
* `compare` prints regressions (passed before, fails now) first, then improvements, added/removed
  cases, the change of every numeric summary value and the largest latency increases.
  `--fail-on-regression` makes the exit code 1 if anything regressed. Single cases of a
  non-deterministic model flip; look at repeated regressions and at the rates.

## What a case set should look like over time

Add a case whenever production shows a mistake (a wrong answer -> part A with `must`/`must_not`; a
false review -> part B; a note that was not forgotten -> part C). Keep the file private; share only
synthetic examples.

## Tests

`hub/tests/test_topcoat_eval.py` (unit tests of the scoring, the review cases of all three kinds,
`compare`, the safety rules, a scripted hub for A, B, C and D) and `topcoat_eval.py selftest`
(the shipped example data against a scripted hub: it passes every case, and each deliberate flaw -
wrong facts, a reviewer that treats "not mentioned" as contradiction, a silent overwrite, a note
that is not forgotten, a withdrawn note that is still used, nothing remembered - is caught by
exactly the cases meant to catch it). No network, no Postgres.

## Other measuring scripts (`scripts/dev/`, 2026-10-31)

All are standard library Python (and `jsonschema` for the first), meant to run **on the hub host**, where the admin
token is read from `hub.env` by the script itself and never printed or put on a command line. Results and what they
showed: the measurements. They go through `/v1` or the API of the hub and write no chat, note or
project row, except the two `live_*` scripts, which make `TEST` chats.

| script | what it measures | notes |
|---|---|---|
| `hubapi.py METHOD PATH [body.json] [--jq FILTER]` | calls the hub API with the token from `hub.env` | the base of the others; the certificate of the loopback address is not verified |
| `changeset_bench.py export OUT` / `run --tools OUT ...` | does a model call the project tools right: six requests, up to three turns, scored by rules, `--extra` adds request parameters | `export` needs the hub package (`uv run` in `hub/`) and writes the tool definitions the agent really sends and the schemas; `run` can score other tool files (before/after a change) |
| `bench-changeset.sh NODE OUT "tools1.json tools2.json" MODEL...` | `changeset_bench.py` over models and tool files | unloads all models of the node first, skips results that exist |
| `bench-review.sh NODE OUT MODEL...` | part B of `topcoat_eval.py` (second look) over models | `/v1` only, admin allowed |
| `bench-models.sh NODE OUT MODEL...` | `agent_bench.py` over models | too easy to tell models apart |
| `chat_compare.py` / `run-chat-compare.sh` | six everyday questions, answers kept to read; tokens and seconds, `--extra '{"chat_template_kwargs":{"enable_thinking":false}}'` for a thinking switch | no score; `max_tokens` 1200 makes a thinking model look worse |
| `live_chat_check.py QUESTION [--model M@N]` | one chat titled `TEST ...`: model, node, first token, tokens | the chat stays |
| `live_project_check.py PROJECT_ID --model M@N --case status\|tree\|two_drops\|all` | the project agent in a real chat on the TEST project: what was prepared in both change tables | refuses only the rows made since its start and stops if something else is pending; the project is never changed |
| `param_sweep.py` (run, compare) and `run-sweep.sh` | the same questions over models and request-parameter variants, scored by rules, compared between runs | see the section below; synthetic example cases only |
| `deploy-overlay.sh` | not a measurement: the 10-second way onto the hub ([DEVELOPMENT.md](DEVELOPMENT.md)) | |

Rules that held every time: use the chats and the project with the prefix `TEST`; **count both change tables**
(`project_changes` and `project_change_sets`) before writing "nothing was prepared"; unload the other models of the
node before a timing; say in the report what ran on a fake and what on the real machine.

## The parameter sweep: `param_sweep.py` (2026-11-03)

For the question "does the answer get better or worse, faster or slower, when I change a setting?" - the thinking
switch, sampling, an effort level, another quantisation of a model (just another model name). One plan names the
models, the **variants** (request parameters merged into each request, e.g. `chat_template_kwargs`), a case file
and the number of repeats. Every combination of model, variant, case and repeat is one request through `/v1`; the
answer is scored by rules (`must`, `must_not`, `regex`, word limits, JSON keys; no model as judge), and the run keeps
seconds, tokens, tokens per second, the finish reason and the answer text. The summary is per model and variant:
pass rate (also per tag), errors, cut-off answers, median seconds, mean speed. `compare OLD NEW` prints what changed
per model and variant and the single cases that passed before and fail now.

```sh
scripts/dev/run-sweep.sh node-amd scripts/dev/eval-example/sweep-example.json ~/sweep/thinking gemma-4-12B gpt-oss-20b
python3 scripts/dev/param_sweep.py compare ~/sweep/before/gemma-4-12B.json ~/sweep/after/gemma-4-12B.json
```

* `run-sweep.sh` runs on the hub host (token from `hub.env`, never on a command line), unloads the node's other models
  before each model and writes `<outdir>/<model>.json` and `SUMMARY.txt`.
* **Server arguments** (`n_gpu_layers`, `ubatch`, flash attention, cache types, the context) are not request parameters:
  change them in the node's configuration of the model, run again with `--label "ubatch 1024"` and compare the two
  files. The results record the label and the date, so write the setting into the label.
* The shipped case file `eval-example/quality_cases.json` is synthetic (twelve everyday questions in German and English:
  explanation, estimate, translation, honesty about what the model cannot know, JSON output, code, a refusal). A real
  set from real questions that went wrong stays outside the repository and is added to over time, like the facts file.
* What it does not show: a rule check is a floor, not a grade of style; twelve cases and two repeats are noisy, so a
  difference of one case is not a result; timings of the first request of a model include its load. For the project
  tools use `changeset_bench.py`, for the second look part B of `topcoat_eval.py`.
* Tests: `hub/tests/test_param_sweep.py` and `param_sweep.py selftest` (a scripted gateway: a "thinking" variant that
  answers worse and slower, an error row, compare).
