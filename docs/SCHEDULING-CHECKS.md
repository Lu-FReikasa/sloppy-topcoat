# Checks between the scheduling steps

For the maintainer. Each step of the roadmap 2.1 gets two gates before the next one starts:

1. **Automated gate**: commands that must be green in CI or locally (see
   the hand-over 2026-10-29 section 3 for how to run the hub suite).
2. **Manual checks** on the real machines, in the shape of the test template.

Use placeholders only (`node-amd`, `node-laptop`, `hub.example.lan`). Do not commit real names, addresses or
secrets. Log line shapes below come from the briefs; compare them with the real log before trusting a grep.

## Baseline (before step 1, and again before every roll-out)

Record these once, so later steps can be compared. Read-only. Taken on 2026-10-30:
the hand-over 2026-10-30-baseline-b1-b3; repeat the same queries after
each step.

- **B1 Placement.** Count assistant messages per node in the last 14 days (the handover found 143 of 145 on
  `node-amd`). Expected: you have a number to compare with after SCH-03.
- **B2 Stray loads.** In the hub log, count `gateway:` lines whose caller is `_summarise` and lines that load a
  model nobody configured. Expected: a count per day.
- **B3 Speeds.** Note `node_speeds` and the newest bench per model and node (the brief SCH-02 lists the wrong
  values: 26B faster than 12B). Expected: the table you compare with after SCH-02.

## Gate per step

| step | automated gate | manual check |
|---|---|---|
| SCH-01 | `pytest hub/tests/test_gateway.py hub/tests/test_gateway_context.py` finishes; a deliberately blocked test fails by timeout; CI green | M-SCH-01 |
| SCH-02 | the tests of the brief (summary obeys `no_background`, no "any other model", check may use the author's model, speeds with and without a `<think>` block) | M-SCH-02 |
| SCH-03 | the fake-node scenarios of the brief; the context test (a prompt is sized for the node it runs on) | M-SCH-03 |
| SCH-04 | migration up and down on a copy of the production database; old settings gone | M-SCH-04 |
| SCH-05 | the three fake setups (one node/one slot, one node/two slots, two nodes) | M-SCH-05 |
| UI-01 | `npm run lint`, `typecheck`, `test`, `build`, `npm run layout` (removed in #208: `npm run shell-layout` and `sheets-layout` run in CI now) | M-UI-01 |
| UI-02 | same web gate | M-UI-02 |
| OBS-01 | hub tests of the view; privacy test (another person's content is never shown) | M-OBS-01 |
| SCH-06 | bench tests on fakes | M-SCH-06 |
| HOST-01 | Go tests of the new actions; schema validation tests; a test that no path outside the allowed roots can be read | M-HOST-01 |

Always, for every step: the full hub suite; the known failures are listed in the handover of 2026-10-29
section 3 and must not grow. Reset the test database after switching to a branch with fewer migrations.

## Manual checks

### M-SCH-01 — the gateway tests do not hang
**Goal:** the two files finish on Linux and in CI.
**Steps:** run both files twice on the Linux test box.
**Expected:** both finish; the PR says whether the hang was Windows only.
**Status:** not run

### M-SCH-02 — no model loads by accident
**Goal:** an old chat closing does not load the 26B model.
**Setup:** `no_background` contains `node-amd`; the 26B model is unloaded.
**Steps:** 1. Let a chat whose last answer used the 26B model close (or trigger its summary). 2. Watch the hub
log and the node's loaded models for 10 minutes.
**Expected:** no load of the 26B model on `node-amd`; the summary runs where the rules allow, or waits.
**Evidence:** hub log lines with `_summarise`; node model list. Compare with baseline B2.
**Status:** passed on 2026-10-31 in the changed form (the 26B model is gone from every node, so a chat answered
by `gpt-oss-20b` on `node-amd` was used, the model unloaded, then the chat closed): the summary ran on the model of
the role `background` on `node-laptop`, nothing was loaded on `node-amd`; all 53 `_summarise` requests since the
update went to that one model. The idle wait was not waited out: the summary started right after the second
answer (the hand-over 2026-10-31-rollout-and-live-checks).

### M-SCH-02b — speeds are plausible
**Steps:** send one answer with a long thought and one without on the 12B and the 26B model; read
`node_speeds`.
**Expected:** the 26B model is not faster than the 12B; load speed has samples after a load. Compare with B3.
**Status:** passed in part on 2026-10-31: 12B on `node-amd` 59 t/s over 6 samples (benchmark 72, before 730); load
speed has samples (790 and 943 MB/s on `node-amd`, 257 on `node-laptop`). The 26B model could not be compared
(it is not on any node). `gpt-oss-20b` shows 176 t/s on one sample against 32 in the benchmark: watch it.

### M-SCH-02c — a check never loads a model, and says who checked
**Setup:** a chat whose model is loaded on `node-amd`; the model of the role `check` is on disk only.
**Steps:** 1. Answer a question in the chat. 2. Wait for the second look (about a minute). 3. Open the note
under the answer if there is one, and read the hub log lines `pick: review of message ...` and
`pick: chat check by ...`.
**Expected:** no model is loaded for the check; the log line names the model and node and says
`same_model=True` when the answering model checked; the note under the answer says "checked by X, the model
that wrote the answer" in that case. A second node with a loaded model is taken before the author's node.
**Status:** passed in part on 2026-10-31: every second look and summary was picked with a log line
(`pick: ...`, source, `same_model=False`), the second look ran on the other node's loaded model, nothing was
loaded. `same_model=True` and the note text under the answer were not tried. Found: `pick: chat check by ...` is
logged and then `chat summary not checked: ... (node policy)` follows, so on two model nodes a summary is not
checked at all.

### M-AGT-01 — a claim of a change is held against the record
**Setup:** the test project `TEST ...` with three throwaway tasks.
**Steps:** 1. In the Projects chat ask for a change that the model gets wrong, or provoke a refused proposal
(for example ask it to set a status that does not exist). 2. Read the closing answer.
**Expected:** if the answer says something was set or is ready to confirm and no change waits for your click,
the line "Hinweis des Hubs: Es wurde nichts geändert ..." is under it, with the refused call and its reason.
With a change on record and an answer that says it was made, the line says it only waits for your click.
**Evidence:** the stored message; the hub log line `chat: a claimed change the record does not back`.
**Status:** failed in part on 2026-10-31. A refused status was reported honestly ("nothing was changed", the refused
call and the allowed values). A valid request was refused by the hub (wrong keys), yet the closing answer said the
change waits for a click and no note was added: the proposal text on the report card counted as a waiting change.
Fixed (tests in `hub/tests/test_claims.py`), rolled out in `local-f9b814b`; a claim of about 270 characters was
still skipped by the patterns there. `local-dd8f7b3` decides by the record as well, but it counted only single
changes: in its live runs (and in those of `local-d388f48`) the 12B model had retried and a valid change set was on
record, yet the hub wrote "no change waits for your confirmation" (an earlier version of this entry wrongly said
the hub had refused everything). `local-5e5cfad` counts the pending change sets of the task. Repeated on
2026-10-31 with that image: **passed**, 3 of 3 runs with a valid set on record and no false note. The test task
stayed `todo`, the test sets were refused by me. Not seen live since the fix: a refused call with no set on record
(the case the note is for), because the 12B model now retries and succeeds; that case is covered by the unit tests.

### M-SCH-03 — work is spread, nothing is cancelled
**Goal:** a second request goes to the second node while the first runs.
**Setup:** two nodes reachable, each with a slot.
**Steps:** 1. Start a long answer in one chat. 2. Start a second chat at once. 3. Start a game on one node
while work waits (`busy_gaming`).
**Expected:** the second runs on the idle node; the game start does not cancel running work; waiting work
moves. The job record shows waiting time per node. Compare with B1.
**Status:** not run

### M-SCH-04 — the table replaces the old policy
**Steps:** roll out to a copy of the hub first; check the table against the old stored policy; downgrade once.
**Expected:** every old role and list appears as a row with a priority; downgrade works; no old setting left in
the GUI.
**Status:** not run

### M-SCH-05 — chains
**Steps:** finish a chat on a setup with one machine; then on two. Leave `TOPCOAT_CHECK_PARALLEL` off.
**Expected:** the summary appears only after the check; a failed or timed-out check does not lose the summary
silently. Measure the parallel check separately before switching it on (handover section 4).
**Status:** not run

### M-UI-01 / M-UI-02 — the pages
**Steps:** open Machines, then Models (three tabs) with real data, in a desktop and a phone-width window, in
English and German.
**Expected:** one row per machine, a VM below its host, no page scroll at phone width, no untranslated key.
**Status:** not run

### M-OBS-01 — the decision log
**Steps:** start a summary on a resting chat and find its row; unload a model by hand, send a request that
needs it, follow the load back to the request (from the brief).
**Expected:** both are traceable; a row of another person shows no content.
**Status:** not run

### M-SCH-06 — bench
**Steps:** run the first bench on `node-amd`; read the suggestions.
**Expected:** values per application; suggestions only suggest (D90).
**Status:** not run

### M-HOST-01 — host reading on Windows
**Setup:** `windows.*` capabilities switched on and read roots set on the PC (handover section 4).
**Steps:** the list in the PR of HOST-01.
**Expected:** Topcoat's logs and `ProgramData` readable; a user's profile folder refused; nothing is changed.
**Status:** not run

## Rollback note

Each roll-out needs a hub image and a database copy tested first. SCH-04 is the only step with a migration of
settings; do not roll it out without the downgrade tried once.
