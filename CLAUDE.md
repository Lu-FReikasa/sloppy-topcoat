# Working rules for this repository

Applies to every agent session (local or cloud) and to humans.

## Language
- Code, comments, commit messages, docs: **English**.
- User-facing GUI strings: **English by default, German fully supported**, via the i18n layer
  (keys in code, catalogues `web/src/i18n/en.json` and `de.json`). Every new string goes into
  both catalogues; CI fails if their keys differ. The language is chosen per user (stored on
  the hub), falling back to the browser language.

## Never commit
- Real hostnames, domain names, IP addresses, MAC addresses, usernames of real people.
  Use placeholders: `hub.example.lan`, `node-amd`, `node-nvidia`, `10.0.0.x`.
- Secrets of any kind (API keys, tokens, passwords, private keys, certificates).
  Configuration lives outside the repo; the repo only ships `*.example` files.
- Model weights or other large binaries.

## How to work
The maintainer wants features built and tried fast. Rules that protect the security baseline and the
privacy of the repo stay strict; everything else is a default you may bend when it saves time.

**Contributors without the maintainer's machines.** If there is no `../sloppy-topcoat-docs/` folder and no
`.dev/hosts.env`, you are working from a clone of the public repository. Then:
- your task is a **GitHub issue**; it is the brief. Issue #1 ("Start here") explains the workflow. Read the decisions in
  `docs/ARCHITECTURE.md` that the issue names; a decision whose file starts with a *Status* line that says replaced or
  removed is history.
- test on your own machine: `make check-fast` while working, `make check` before you push (the hub tests need
  Postgres with pgvector and run on Linux or WSL, see "Tests and lint" in `docs/DEVELOPMENT.md`). The rules below
  about `remote-ci.sh`, `live_suite.py`, the overlay deploy and hand-overs are for the maintainer's sessions; skip them.
- open a pull request against `main` with `Closes #<issue>`; the maintainer reviews, merges and deploys.

- **Read what you need, not everything.** Start with the newest hand-over in the private docs folder
  (`../sloppy-topcoat-docs/`, dated files, outside git) and the task brief there. `SPEC`/`ARCHITECTURE`/decisions only for the part you touch. If a brief conflicts with
  the architecture, say so in the PR description and pick the simpler side.
- **Hardware.** Cloud sessions have no GPU, audio or LAN: hardware-specific code sits behind an interface with a fake;
  mark untested real code `UNVERIFIED on real hardware`. Local sessions may test on the dev machines
  (hosts in `.dev/hosts.env`, see `scripts/dev/hosts.env.example`).
- **GUI checks: the desktop overlay first.** The maintainer works in the client's overlay (`desktop/`), so GUI changes
  are checked there first and in the browser's navigation shell second. Take the screenshots with the tools, not by
  hand: `npm run overlay-layout` (simulated, anyone) and `npm run overlay-capture` (the real client, maintainer's PC);
  see "GUI checks in the desktop overlay" in `docs/DEVELOPMENT.md`.
- **Tests are for behaviour, not for ceremony.** New logic and bug fixes get a test (fakes are fine). Pure docs, copy,
  styling, renames and config need none. Do not write tests that only mirror the implementation.
- **Test loop: targeted while working, full once before a push.** `scripts/dev/remote-ci.sh changed` (seconds) or with
  pytest arguments while developing; the full suite (`remote-ci.sh`, ~2 min) plus `remote-ci.sh lint` once before the
  push. `remote-ci.sh fmt` formats. Web: `npx vitest run <file>` while working, `npm test` and `npx tsc --noEmit` before the push.
  Details: `docs/DEVELOPMENT.md` ("Fast loop").
- **CI on GitHub is a safety net, not a gate you wait for.** Open the PR and go on working. Red checks that were already
  red on `main` (Go lint, Windows tests) do not block a merge; fix them in their own PR when it pays.
- **One PR per theme, not per brief.** Related rounds may share a branch. Work on a branch and merge into `main` through a PR;
  never push to `main` directly. Pushing a feature branch and opening a PR is allowed without asking.
- **Live checks cover Research and Development together.** Both depend on the same parameters (pools, picks, context),
  so a model on a node is judged with `scripts/dev/live_suite.py MODEL NODE[,NODE]` (research report, a Development chat, the block
  program `research_report`, each node on its own); it pauses background work, restores the rows and unloads the node. `node-amd` (his PC) is allowed since 2026-11-05; mind that he may be using it.
- **Deploy to the hub** with the overlay (`scripts/dev/deploy-overlay.sh`) as soon as the targeted tests are green;
  a database dump first when the schema changes. End a session with a clean image only when a release is wanted.
- **Handover.** One short note at the end of a session (what is true now, what is next), as a dated file in the private
  docs folder. Plans, task briefs, hand-overs and measurements live there, never in the repo (they may name real
  hosts); `docs/` keeps the reference pages and decisions, which the hub's own agents read. No note per
  round, no long retellings of what git already shows. Decisions go into `docs/decisions/` only when they are
  decisions someone will ask "why" about.
- **Protocol changes** start in `proto/` (JSON Schema) and `docs/PROTOCOL.md`, then code on both sides. Bump the protocol
  version on breaking changes.
- **Node binary** stays small: no CGO unless unavoidable, no heavy dependencies; name every new dependency in the PR.
- **PR text** says what was verified and what was not, in a few lines.

## Security baseline (non-negotiable)
- Nodes never expose a generic remote shell. They execute only named, schema-validated
  actions from a whitelist that the hub assigns per node.
- Nodes connect **outbound** to the hub; nodes open no listening port (the one exception is the single-use socket of a model transfer, ARCHITECTURE D51). Model servers
  (`llama-server`) bind to loopback (`127.0.0.1`) only; the hub reaches them through the
  node session, never directly.
- All hub traffic is encrypted (TLS, D11). Plain HTTP exists only for automated tests and
  local development and needs an explicit opt-in flag on both sides.
- Every DB row that belongs to a person carries a `user_id`; queries are scoped by it.
- The AI assistant is read-only unless the user confirms a concrete state-changing action or the
  **scope** of one (ARCHITECTURE D15, D83): a click on a card that shows exactly what it covers (action
  kinds, target, purpose, expiry). Inside such a scope follow-up attempts need no new click, but the
  checks that guard the action run on every attempt and are never part of what an approval can
  waive. Never implement blanket approvals ("approve all future changes") or AI self-confirmation:
  an agent may propose an action and ask for a retry; it never creates, widens or confirms an approval.
- The hub makes no calls to external LLM APIs (the escalation was removed, ARCHITECTURE D33).
  A new one needs a decision first and must go through the redaction layer (`hub/src/topcoat_hub/redaction.py`).
