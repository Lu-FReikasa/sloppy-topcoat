# Development

How to run, test and extend sloppy-topcoat. Read [CLAUDE.md](../CLAUDE.md) first.
To just try it on real machines, see [QUICKSTART.md](QUICKSTART.md).

## Layout

```
hub/     Python 3.12, uv, FastAPI, SQLAlchemy 2 + Alembic, pytest, ruff
node/    Go, single static binary `topcoat-node` (no CGO), go test, golangci-lint
web/     React + Vite + TypeScript, i18next (English + German catalogues), vitest, eslint
proto/   JSON Schemas for node <-> hub messages (proto/v1, plus v0 for old nodes) + valid/invalid examples
deploy/  docker compose dev stack, example env file
```

Prerequisites: Python 3.12 + [uv](https://docs.astral.sh/uv/), Go (version in `node/go.mod`),
Node.js ≥ 22.12, Docker (for the dev database), GNU make, golangci-lint v2 (only for
`make lint-node`).

## Check before you push

`make check` (= `scripts/dev/local-ci.sh`) runs what CI runs, locally: node (vet, vet for
Windows, `-race`, golangci-lint, cross-compile), web (lint, typecheck, vitest, build, the layout
checks), hub (ruff, migrations up/down, pytest on 4 parallel workers) and the integration test.
`make check-fast` skips `-race`, pytest, the layout checks and the integration test (about 3
minutes). The hub parts start a throw-away PostgreSQL on port 55432 (needs the `postgresql` and pgvector packages). Not covered
here, CI only: the Windows tests and the Docker image smoke test. pytest runs with
`pytest-xdist`; every worker gets its own database (`topcoat_test_gw0`, ...), and parametrized
test ids must be stable (no `uuid4()` in `parametrize`).

## Fast loop (the default way of working)

The full checks exist for the moment before a push, not for every edit. Hosts live in `.dev/hosts.env`
(gitignored, template `scripts/dev/hosts.env.example`); the scripts read it themselves.

| while working | command | time |
|---|---|---|
| tests that belong to the changed files | `scripts/dev/remote-ci.sh changed` | seconds |
| one test file or `-k` | `scripts/dev/remote-ci.sh tests/test_x.py -k name` | ~10 s |
| formatting | `scripts/dev/remote-ci.sh fmt` (ruff in the container, files copied back) | ~5 s |
| web | `npx vitest run <file>`, `npx tsc --noEmit` in `web/` | seconds |
| onto the hub | `scripts/dev/deploy-overlay.sh push --web` | ~10 s |

Live (on the hub host, one model on one or more nodes, each node on its own): `live_suite.py MODEL NODE[,NODE]` pauses the hub's background work, unloads the node first and runs `live_research.py fresh`, `live_dev.py` (a read-only
question in the Development chat) and the block program `research_report` one after the other; `--only research|dev|blocks` runs
one. Each restores the row it changed, the suite unloads the node at the end and lets background work go on. Without the pause the
background rows (memory care, summaries) load their own models next to the one under test. Do not run `remote-ci.sh` meanwhile if the test
machine is the node.

Before a push, once: `remote-ci.sh` (all hub tests, ~2 min), `remote-ci.sh lint`, web `npm test`. Then push, open the PR and
carry on; do not wait for GitHub CI (hub job ~13 min). `remote-ci.sh` ships only the files that differ from `origin/main`
after the first run and keeps its pgvector container up (`remote-ci.sh stop` removes it, `reset` ships the whole tree again
after a pull or rebase). Do not run it during a live model test on the same machine; it shares the CPU.

## Dev loop

### Option A — everything in docker

```sh
cp deploy/dev.env.example deploy/dev.env     # set a DB password and a dev admin token
make dev                                     # Postgres+pgvector, hub on https://:8443, web dev server on :5173
```

The hub container runs the Alembic migrations on start, prepares TLS (its own CA in the
`hub-tls` volume, names from `TOPCOAT_TLS_HOSTNAMES`) and serves the built GUI on
`https://localhost:8443`. The web dev server on `:5173` hot-reloads and proxies `/api` to the hub. Open either,
enter the dev admin token from `deploy/dev.env`.

### Option B — native processes (faster iteration)

```sh
cp deploy/dev.env.example deploy/dev.env
make db          # only Postgres in docker, bound to 127.0.0.1:5432
make hub-run     # python -m topcoat_hub.serve: migrations, TLS (CA in hub/.tls), https://localhost:8443
make web-run     # vite on :5173, proxies /api to HUB_URL (default https://localhost:8443)
```

### A fake node

```sh
# In the GUI: "Knoten hinzufügen" → copy the code and the CA fingerprint, then:
make pair CODE=ABCD-1234 FP=sha256:<hex> NODE_NAME=fake-1   # identity in .dev/fake-1 (gitignored)
make fake-node NODE_NAME=fake-1              # topcoat-node run --fake
```

The node shows up in the overview within a second; loading/unloading a model in the detail
view changes the fake VRAM bar. Run a second one with another `NODE_NAME`. To shape the
fake hardware, pass a profile: `node/bin/topcoat-node run --fake --fake-profile
node/fake-profile.example.json --data-dir .dev/fake-1`.

Plain `topcoat-node run` (without `--fake`) uses the real providers (GPU/CPU inventory,
process watcher, llama-server runner). On first start it downloads the pinned llama.cpp build
for the detected backend into `<data dir>/runtime` (state `degraded` until then). Real
machines: [INSTALL-NODE.md](INSTALL-NODE.md). The fake node also simulates downloads and
benchmarks (`fake.World.DownloadStep` / `BenchDelay`).

### Hub configuration

Environment variables (prefix `TOPCOAT_`, see `hub/src/topcoat_hub/config.py`):

| variable | default | meaning |
|---|---|---|
| `TOPCOAT_DATABASE_URL` | `postgresql+psycopg://topcoat:topcoat@localhost:5432/topcoat` | SQLAlchemy URL |
| `TOPCOAT_DEV_ADMIN_TOKEN` | empty (= GUI API locked) | static bearer token of the seeded dev admin; at least 32 characters (`serve` refuses a shorter one or the example value). 30 failed sign-ins per address and minute answer 429 until the minute is over |
| `TOPCOAT_PROTO_ROOT` | `<repo>/proto` | directory with the JSON Schemas of every protocol version (`v0/`, `v1/`) |
| `TOPCOAT_CHAT_DEFAULT_CONTEXT_TOKENS` | `8192` | prompt budget base when the node does not report the loaded model's context length |
| `TOPCOAT_EMBEDDING_MODEL` | empty | embedding model served through the gateway (`/v1/embeddings`, bge-m3 after E2-03); empty = the transitional model-free hashing embedder |
| `TOPCOAT_EMBEDDING_AUTO` | `true` | with no `TOPCOAT_EMBEDDING_MODEL`: use the hashing embedder until a served model with `bge-m3` in its id appears (checked every 2 minutes), then switch to it and re-embed |
| `TOPCOAT_EMBEDDING_WATCH_SECONDS` | `120` | how often the hub looks for the bge-m3 model (`0` = never in the background; the tests set this) |
| `TOPCOAT_KNOWLEDGE_DIRECTORY_STALE_DAYS` | `14` | a knowledge directory (`10_KI_Verzeichnis.md`) older than this is reported as possibly missing newer sections |
| `TOPCOAT_KNOWLEDGE_HIT_CHARS` | `1200` | the most of a knowledge hit that is shown; a hit is the whole chunk (1200 characters at most), so a larger value changes nothing, a smaller one shortens the hits |
| `TOPCOAT_SHUTDOWN_GRACE_SECONDS` | `3` | on shutdown, fire-and-forget work (note extraction, a started run) may finish for this long; what is left is cancelled and awaited before the database engine is disposed |
| `TOPCOAT_MEMORY_MODEL` | empty | model for the memory consolidation (empty = the hub's default chat model) |
| `TOPCOAT_MEMORY_CONSOLIDATION_INTERVAL_SECONDS` | `1800` | how often the idle consolidation looks for unchecked notes (`0` = never on its own; the GUI can still start a run) |
| `TOPCOAT_MEMORY_IDLE_SECONDS` | `0` | quiet time without a chat answer that scheduled background work waits for (0 = none: the node's queue serves chat first and takes background work as soon as nothing more important is left; a node must be `available`) |
| `TOPCOAT_MEMORY_RUN_MAX_TOKENS`, `TOPCOAT_MEMORY_RUN_MAX_SECONDS` | `6000`, `300` | budget of one consolidation run |
| `TOPCOAT_MEMORY_NOTE_WAIT_SECONDS` | `8` | how long the chat waits after an answer for the "remembered: …" line |
| `TOPCOAT_MEMORY_HALFLIFE_PERSON_DAYS`, `…_PREFERENCE_DAYS`, `…_OPERATIONAL_DAYS`, `…_VOLATILE_DAYS` | `365`, `180`, `14`, `1` | half-life of the trust in a note by kind (E3-05, D37); every confirmation lengthens it by `TOPCOAT_MEMORY_HALFLIFE_BOOST` (`1.5`) up to `TOPCOAT_MEMORY_HALFLIFE_BOOST_MAX` (`8`) times |
| `TOPCOAT_MEMORY_LIVE_CHECK_WEIGHT` | `2` | a live check counts this many times a user statement |
| `TOPCOAT_MEMORY_VOLATILE_TTL_HOURS` | `24` | how long a volatile note ("service X is down") is kept; `0` = not stored at all |
| `TOPCOAT_CHAT_SAMPLING` | `{}` | extra sampling parameters of chat requests by model family (JSON, e.g. `{"gemma": {"repeat_penalty": 1.15}}`; part of the model id, longest match wins; `{}` as a family's value switches its built-in default off). Built in: `gemma` → `repeat_penalty` 1.1 |
| `TOPCOAT_CHAT_EFFORT` | `{}` | what each quality level sends to a model family to set how long it thinks (JSON: family → level → `reasoning_effort` and/or `chat_template_kwargs`, e.g. `{"qwen": {"fast": {"reasoning_effort": "low"}, "thorough": {"reasoning_effort": "high"}}}`; longest family match wins; nothing built in, so a model without an entry is asked as before). Whether a model's chat template honours it is UNVERIFIED on real hardware |
| `TOPCOAT_MEMORY_STAGE_SURE`, `TOPCOAT_MEMORY_STAGE_OLDER` | `0.7`, `0.35` | reliability from which the assistant is told "sure" / "older"; below: "outdated, please check" |
| `TOPCOAT_MEMORY_CHECK_THRESHOLD` | `0.5` | unused since D124 (the care run checks no note); still shown on the memory page |
| `TOPCOAT_MEMORY_ARCHIVE_USEFULNESS`, `…_ARCHIVE_RELIABILITY`, `…_ARCHIVE_IDLE_DAYS` | `0.3`, `0.25`, `90` | a note is archived only if usefulness **and** reliability are below these **and** it was unused for that many days; age alone never archives |
| `TOPCOAT_MEMORY_CARE_HOUR` | `3` | the care run's night hour in the hub's time zone (`TOPCOAT_TZ`, default Europe/Berlin; `-1` = no fixed hour, only after idle) |
| `TOPCOAT_MEMORY_CARE_INTERVAL_SECONDS` | `900` | how often the hub looks whether a care run is due (`0` = never on its own; the GUI can still start one) |
| `TOPCOAT_MEMORY_CARE_IDLE_EVERY_HOURS` | `12` | a care run after idle at most this often per user (besides the night) |
| `TOPCOAT_MEMORY_CARE_MAX_NOTES` | `10` | unused since D124 (the care run checks no note); still shown on the memory page |
| `TOPCOAT_CORE_TOKENS` | `1000` | token budget of the core block at the start of every system prompt (estimate: 3 characters per token); a model context below 8k automatically gets at most 500 |
| `TOPCOAT_CORE_RECENT_TOKENS` | `150` | of that, the room of "Discussed last" (key terms of the latest chats, D100) |
| `TOPCOAT_CORE_RECENT_CHATS`, `TOPCOAT_CORE_RECENT_DAYS` | `2`, `14` | how many of the latest chats of the program it names, and how old the newest may be |
| `TOPCOAT_CHAT_DIGEST_ENABLED` | `true` | the two looks at a finished chat: the author model writes a summary, another model checks it and learns what the chat taught (stored as unverified notes) |
| `TOPCOAT_CHAT_DIGEST_IDLE_SECONDS` | `600` | a chat counts as finished after this rest; every answer restarts the timer, nothing runs on a clock |
| `TOPCOAT_CHAT_DIGEST_MIN_MESSAGES` | `4` | shorter chats are not looked at |
| `TOPCOAT_CHAT_DIGEST_RETRIES` | `3` | tries (one rest period apart) when somebody is chatting or the nodes are busy |
| `TOPCOAT_CHAT_DIGEST_MAX_LEARNED` | `3` | notes one chat can add |
| `TOPCOAT_CHAT_DIGEST_CATCHUP_HOURS`, `…_CATCHUP_MAX` | `48`, `10` | after a hub restart: unfinished chats of this age (newest, at most this many) arm their timers once |
| `TOPCOAT_REVIEW_TICK_SECONDS` | `0` | was `900`. `0` = answers get their second look when a chat is finished; a value above 0 adds the old fixed-interval look |
| `TOPCOAT_WEB_DIST` | `<repo>/web/dist` | built GUI, served at `/` if present |
| `TOPCOAT_ACTION_TIMEOUT_SECONDS` | `30` | how long the REST call waits for a node's `action_result` |
| `TOPCOAT_PAIRING_CODE_TTL_SECONDS` | `900` | pairing code lifetime |
| `TOPCOAT_TLS_MODE` | `auto` | `auto` (own CA), `files` (own certificate), `off` (behind a TLS reverse proxy / tests) |
| `TOPCOAT_TLS_DIR` | `<repo>/hub/.tls` (image: `/data/tls`) | CA and server certificate in mode `auto` |
| `TOPCOAT_TLS_HOSTNAMES` | empty | names/IPs for the server certificate (loopback is always added) |
| `TOPCOAT_TLS_CERT_FILE`, `_KEY_FILE`, `_CA_FILE` | empty | mode `files` |
| `TOPCOAT_PUBLIC_URL` | empty | URL printed in pair commands (reverse proxy) |
| `TOPCOAT_HOST`, `TOPCOAT_PORT` | `0.0.0.0`, `8443` | listen address of `python -m topcoat_hub.serve` |

Auth is dev-only: every endpoint depends on `topcoat_hub.auth.api_user`, which maps the static
token to the seeded `dev-admin` user (or an API key to its owner). OIDC replaces that one
dependency later.
Personal endpoints (keys, conversations, chat settings) use `api_user`: the admin token or the
API key of any user, so a second user can exist before OIDC (created in the DB).
Admin endpoints use `require_admin`, which accepts only the admin token — a key never opens them.

### API overview

| method | path | auth | purpose |
|---|---|---|---|
| GET | `/api/health` | – | liveness |
| GET | `/api/me` | admin token | current user incl. GUI `language` (`en` / `de` / null = browser) |
| PUT | `/api/me/language` | admin token | `{language: "en" \| "de" \| null}`: store the caller's GUI language |
| GET | `/api/nodes` | admin token | nodes with state + latest inventory/heartbeat |
| GET | `/api/nodes/{id}` | admin token | node detail incl. config and last 50 events |
| GET | `/api/nodes/{id}/events?limit=` | admin token | event log |
| POST | `/api/nodes/{id}/actions` | admin token | `{name, args}` → node `action_result` body; a successful manual `models.unload` clears `auto_reload` of resident models, `models.load` sets it again (E1-06) |
| PUT | `/api/nodes/{id}/config` | admin token | replace the node config (validated against `node_config` schema, pushed to the node) |
| PUT / DELETE | `/api/nodes/{id}/residents/{model}` | admin token | "Automatisch bereithalten": create or replace the model's `config.residents` entry (`{priority?, context_length?, auto_reload?}`, defaults 50 / – / true) or remove it (idempotent); returns `{config, config_hash, changed}` |
| GET | `/api/nodes/{id}/benchmarks` | admin token | latest 200 `node_benchmarks` rows |
| POST | `/api/pairing-codes` | admin token | new one-time pairing code |
| GET / POST / DELETE | `/api/keys`, `/api/keys/{id}` | user (admin token or API key) | the caller's gateway API keys (the key is only in the POST response) |
| GET / POST | `/api/conversations` | user (admin token or API key) | the caller's conversations (`?q=` matches the title) / create `{title?, model?, node?}` (E2-01, D21) |
| GET / PATCH / DELETE | `/api/conversations/{id}` | user | open with messages / change `title`, `model`, `node` (null = automatic) / delete; another user's id is 404 (D20) |
| POST | `/api/conversations/{id}/messages`, `/regenerate`, `/continue` | user | `{content}` / replace the last answer / go on with a `truncated` answer (appended to it; 409 `nothing_to_continue` if it is not cut off, 409 `context_too_small` if the model's context cannot hold a useful tail); answers as SSE `meta`, `delta`, `done`, `error`; 503 JSON with `retry_after` when no node can serve the model |
| GET | `/api/chat/models` | user | models for the picker with their nodes |
| GET / PUT | `/api/chat/settings` | user | the caller's default model, system prompt, temperature, form of address (`du` / `sie`) and the effective defaults |
| GET | `/api/memory` | user | the caller's notes (`?q=` substring, `?status=`, `?evidence=`; default: all but deleted) with counts, blocked-secret count, memory on/off, embedder, running flag, the last runs and the open clarification questions (E2-02, D22) |
| GET / PATCH / DELETE | `/api/memory/{id}` | user | note with its versions / edit `{text?, evidence?}` (new version; a text that looks like a secret is 422 `secret_blocked`) / soft delete; another user's id is 404 (D20) |
| POST | `/api/memory/{id}/revert`, `/restore`, `/resolve` | user | `{version}` back to an earlier content (new version) / bring a deleted note back / `{keep: this\|other\|both}` decide a `disputed` pair |
| POST | `/api/memory/forget-all`, `/purge-deleted`, `/consolidate` | user | soft-delete everything / remove deleted notes permanently / start a consolidation run now (202; 409 `no_node_available`, `already_running`, `memory_disabled`) |
| POST | `/api/memory/{id}/undo` | user | take back a note the assistant wrote because of a statement: it is deleted and every note it replaced or withdrew is active again; related questions are closed (D19 extended) |
| GET | `/api/memory/questions` | user | the caller's open clarification questions (`id`, `topic`, `origin`, `candidates[]` with text, time and confirmations); another user's questions never appear (D20) |
| POST | `/api/memory/questions/{id}/answer` | user | `{note_id}` this note is right / `{none: true}` none of them / `{text}` (also together with `none`) what is true; applied like an explicit statement of the user. 404 unknown or closed, 422 `unknown_note`, `empty_answer`, `secret_blocked` |
| POST | `/api/memory/questions/{id}/dismiss` | user | put a question aside; it is asked again only when the notes changed or the group grew |
| GET | `/api/conversations/{id}/memory` | user | notes remembered from one conversation |
| PUT | `/api/scheduling/default-model` | admin token only | `{model}`: the default of the pool (null = the best answering model of the pools) |
| GET | `/v1/models` | API key or admin token | models on connected nodes (OpenAI format, `topcoat.nodes` per model) |
| POST | `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` | API key or admin token | OpenAI-compatible gateway (E1-05, HUB-API "Model gateway") |
| GET | `/api/stream` | admin token | Server-Sent Events: `node_updated`, `node_event`; a keepalive comment every `TOPCOAT_SSE_KEEPALIVE_SECONDS` (2 s), since the GUI shows the hub as lost after 5 s without a byte |
| GET | `/api/tls`, `/api/tls/ca.pem` | none | TLS mode, CA fingerprint, CA certificate |
| POST | `/api/node/pair` | pairing code | node pairing (see docs/PROTOCOL.md) |
| POST | `/api/node/challenge` | none | single-use nonce for the node session |
| WS | `/api/node/ws` | signed nonce (node key) | node session (see docs/PROTOCOL.md) |

### The API contract: `proto/api/openapi.json`

The hub's OpenAPI document is committed, so the web client has something to be checked against.
After a change to a hub route or to a model it returns or takes (and after a FastAPI or pydantic
update), regenerate it and commit the result:

    cd hub && uv run python -m topcoat_hub.openapi_contract

`--check` only compares. Two tests hold the contract: `hub/tests/test_openapi_contract.py` fails
when the app's schema differs from the file, and `web/src/api/contract.test.ts` fails when
`client.ts` calls a path or method the file does not have (it reads the calls from the source; a new
transport helper beside `request`, `chatStream` and `imageBlob` goes into its `TRANSPORT` table).
WebSocket routes are not part of OpenAPI; the file lists them under `x-topcoat-websockets`.

## Overlay deploy (a short loop to a running hub)

Building an image, moving it and recreating the hub takes the better part of an hour. For a hobby hub
that is also the place where development is tried, `scripts/dev/deploy-overlay.sh` skips the image:
it copies `hub/src/topcoat_hub`, `hub/migrations` and `proto` (and, with `--web`, a built `web/dist`)
into `overlay/` next to the compose file on the hub host and recreates only the hub container with
those directories mounted over the image's copy. A push takes about ten seconds.

```bash
export TOPCOAT_OVERLAY_HOST=hub.example.lan     # an ssh alias; TOPCOAT_OVERLAY_DIR defaults to ~/topcoat
scripts/dev/deploy-overlay.sh push [--web]      # commit first: the build name is <commit>-overlay
scripts/dev/deploy-overlay.sh status            # image, overlay on/off, overlay commit, health
scripts/dev/deploy-overlay.sh off               # back to the code inside the image
```

* A dump (`backup/topcoat-before-<commit>-overlay.dump`) is written before the first start with
  migrations that differ from the last overlay. A hub that does not answer within
  `TOPCOAT_HEALTH_WAIT` seconds after a push is switched back to the image code by the script.
* After a successful push the same commit goes into the dev node's workshop
  (`scripts/dev/push-live-to-devnode.sh`, `TOPCOAT_DEVNODE_HOST`): branch `live`, and `main` when it
  is a fast-forward, so the assistant reads the code the hub runs. The node side is
  `scripts/dev/devnode-live-import.sh`, installed once with one sudo rule (see its header); then
  `scripts/dev/workshop_live.py` moves the clone onto `topcoat/live` through `repo.sync`/`repo.start`.
* The overlay is **not** a release: it is not reproducible from an image tag. End a session with a
  clean image (`scripts/build-hub-image.sh`, `deploy/update-hub.sh`); that run uses the base compose
  file only and so removes the overlay.
* Python dependencies and the Dockerfile are not part of the overlay: a change in `uv.lock` needs an
  image.
* `scripts/dev/hubapi.py` (call the API from the hub host, the admin token is read from `hub.env`
  there and never printed) and the `bench-*.sh` scripts next to it are meant for the same loop.

## Tests and lint

```sh
make test        # hub (pytest), node (go test), web (vitest)
make lint        # ruff, go vet + golangci-lint, eslint + tsc
make e2e         # builds the node, starts a real hub + two `run --fake` nodes, loads/unloads
                 # a model via REST, checks the SSE stream, restarts the hub
```

Hub tests need a Postgres 16 with pgvector. They use `TOPCOAT_TEST_DATABASE_URL`
(default `postgresql+psycopg://topcoat:topcoat@localhost:5432/topcoat_test`) and **drop and
recreate the schema** there — never point it at a database you care about. With the docker
stack: `docker compose -f deploy/docker-compose.dev.yml --env-file deploy/dev.env exec db
createdb -U topcoat topcoat_test` and set the password in the URL. A container that was created with
`POSTGRES_DB=topcoat` does not have `topcoat_test`, and the database needs the `vector` extension
(`pgvector/pgvector:pg16` has it; a plain Postgres package needs `postgresql-16-pgvector`). Create the
cluster with the encoding UTF-8: with `SQL_ASCII` psycopg returns bytes and the first migration fails with
`TypeError: cannot use a string pattern on a bytes-like object`.

**Run the hub tests on Linux, not on Windows** (SCH-01). psycopg async refuses the Windows event
loop, and a Windows checkout on a slow file system makes a run take far longer. On a local Windows
machine `test_gateway.py` and `test_gateway_context.py` were seen to hang; on Linux (Ubuntu 24.04,
PostgreSQL 16 with pgvector, `uv sync --frozen`) both files finish in about 30 s, alone and under
`-n 4`, and the whole suite takes about 10 minutes with `-n 4`. The Windows hang was not traced
further because the platform is not supported for hub tests. Use WSL on the Linux file system.

Every hub test has a timeout of 120 s (`timeout` in `hub/pyproject.toml`, from `pytest-timeout`;
the slowest test takes about 10 s). A test that blocks fails with a timeout and a stack dump
instead of stopping the run. Do not raise it to hide a hang; a single test that really needs more
can say `@pytest.mark.timeout(300)`.

Known flaky tests (they pass alone, fail rarely under load): see the list below.

CI (`.github/workflows/ci.yml`) runs the same: hub (ruff, migrations up/check/down, pytest
against a `pgvector/pgvector:pg16` service), node (vet, `go test -race`, golangci-lint,
cross-compile `linux/amd64` and `windows/amd64` with `CGO_ENABLED=0`), web (eslint, tsc,
vitest, build, and the layout checks `shell-layout` and `sheets-layout` in Chromium, see "Layout
checks in CI") and the integration test. The overlay checks (`overlay-layout`, `overlay-capture`) are
not part of CI.

### Flaky hub tests

Fixed (SCH-01): `test_metrics.py::test_a_connected_node_has_a_history_and_an_offline_node_none`
failed in roughly one run of four, alone and under load. The cause was in the code: the metrics
history compared the time of a new heartbeat with the rounded time of the last stored one, which can
lie up to 0.05 s later, so two heartbeats in quick succession lost the second one.

Still listed because they were seen failing only under parallel load (`-n 4`) before SCH-01 and did
not fail in the runs made for it, so the cause is not known: `test_node_ws.py::test_unsupported_version_is_read_only`
and some `test_memory_*` tests. If one fails, run it alone before suspecting a change. A test that
fails every time is a real failure; the handover of 2026-10-29 lists `test_release_script` (2 tests)
and `test_steering_api::test_a_note_and_a_pause_reach_the_check_between_two_turns` as failing on
`main`, which was not seen on Linux in the runs for SCH-01.

Measuring a *running* hub between versions (facts, the second-look reviewer, memory behaviour,
agent tasks) is a separate tool, not part of CI: `scripts/dev/topcoat_eval.py`, see
[EVAL.md](EVAL.md).

## Hub tests on the maintainer's Windows PC (WSL)

What worked, and what cost time, as of 2026-10-31.

* **Run the hub suite in WSL Debian, never on Windows** (psycopg async refuses the Windows event loop; the Windows
  file system seen from WSL makes a run take more than 40 minutes). Docker runs in WSL too (not Docker Desktop):
  the container `topcoat-test-pg` (`pgvector/pgvector:pg16`, database `topcoat_test`). Work in a clone on the
  native Linux file system (`git clone <the Windows repository> ~/work`), then in `hub/`:
  `TOPCOAT_TEST_DATABASE_URL=postgresql+psycopg://topcoat:topcoat@localhost:5432/topcoat_test uv run pytest -n 4 -q`.
  Put commands in a script file and call `wsl -d Debian -- bash <script>`: quoting through PowerShell or Git Bash breaks
  on `{{...}}`, `$` and `/mnt` paths (Git Bash needs `MSYS_NO_PATHCONV=1`).
* **Times:** about 8 minutes with 8 workers on an idle PC; 22 to 26 minutes with `nice -n 19` and 4 workers while models
  run on the same PC (it is also `node-amd`). A single file is seconds; use it while developing and the whole suite once
  before a merge.
* After the machine was idle the container often says "the database system is starting up": wait for
  `docker exec topcoat-test-pg pg_isready -U topcoat -d topcoat_test`.
* **Each xdist worker has its own database** (`topcoat_test_gw0`, ...), made once and kept. After a switch to a branch with
  other migrations the databases still carry the revision of the old branch and `downgrade base` fails with an unknown
  revision: drop `topcoat_test*` and create an empty `topcoat_test`.
* **Tests share the tables of their worker's database.** A test that counts rows of a table it does not own must empty it
  first (`test_job_log.py`); a test that uses a fake node right after `online_node` must wait until the model appears in
  `/v1/models` (`eventually`), the inventory arrives a moment after the node is online.
* **Load-dependent tests** fail now and then in a full run and pass alone: `test_memory_selfcorrect` (two tests),
  `test_project_search`, `test_model_transfers`, `test_steering_api`, `test_chat` (`unavailable_model`),
  `test_internet_switch`. Run the file alone twice before calling it a failure.
* `ruff check src tests` and `ruff format --check src tests` in `hub/` before every commit (the tests do not run them;
  a line that is too long passed the suite once and would have turned CI red).
* **Go:** the workshop tests need Linux (`file://` URLs, symbolic links); run them in a `golang` container with the whole
  repository mounted, working directory `node/`. **Web:** `npm run typecheck`, `lint`, `test` work on Windows;
  `npm run build` works too since the component is `QuickAddForm.tsx` (it used to differ from `quickAdd.ts` only in case).
* **Image:** `scripts/build-hub-image.sh` from a clean clone of `main`; build once before the real build to warm the layers
  (the second build took 4 to 7 seconds), then `scripts/smoke-test-hub-image.sh <image>`, `docker save | gzip`, copy with a
  checksum, `deploy/update-hub.sh --dry-run --load <file> <image>`, then the real run.

## llama.cpp pins

`node/internal/llamacpp/releases.json` lists the official llama.cpp release archives per
OS/backend with their SHA-256. To pin a new release: add it there (keep the old one so hubs can
still pin it), set `default`, and push — the workflow `llama.cpp pins`
(`.github/workflows/llamacpp-pins.yml`) downloads every archive and prints the actual
SHA-256 values (it fails on mismatches; copy the printed values after checking the release
page). The workflow runs only when the pin file changes. Locally:
`python3 node/scripts/verify-llamacpp-pins.py`.

Windows-specific code runs in CI on `windows-2025` (job `node (go test on windows)`); lint
for both targets locally with `make -C node lint`.

## Changing the protocol

1. Edit the schema in `proto/v1/` (the current version; `v0` is frozen) and
   `docs/PROTOCOL.md` first. Breaking change → new version directory and bump
   `PROTOCOL_VERSION` on both sides.
2. Add or adjust examples in `proto/examples/v1/valid|invalid/` (file name prefix = schema
   name). Both the hub (`hub/tests/test_protocol.py`) and the node test suite validate every
   example, and the node validates every message it produces against the schemas.
3. Implement on both sides.

## Database migrations

```sh
cd hub
uv run alembic revision --autogenerate -m "what changed"   # review the generated file!
uv run alembic upgrade head
uv run alembic check                                       # models and migrations in sync
```

**A build never runs against a database that is ahead of it.** `python -m topcoat_hub.serve` first
compares the database's Alembic revision with the migrations it ships (`dbrevision.py`, also with
`--no-migrate`); an unknown one — a newer or a test build deployed before — is one log line ("database
is at revision X which this build does not know … newer build deployed before?") and exit status 1,
not an Alembic traceback, and the database is not touched. Alembic cannot compare expression indexes:
the full-text index of `knowledge_chunks` is migration-only (`MIGRATION_ONLY_INDEXES` in
`migrations/env.py`).

Every table that holds data belonging to a person gets a `user_id` column and every query on
it is scoped by that id (CLAUDE.md security baseline).

When a change adds a default permission to `DEFAULT_ACTIONS` (`hub/src/topcoat_hub/protocol.py`),
add a data migration that calls `topcoat_hub.configmigrate.add_default_actions(...)` so existing
nodes get it too (ARCHITECTURE D14; example: `0004_default_actions_e1_02.py`).

## Real llama-server in a sandbox
The unit tests use a fake llama-server. To check what depends on a real process but not on the
answer (flags, memory figures, prompt-cache behaviour, suspend/resume, priorities) without a
real model: unpack a llama.cpp release (`scripts/` and `node/internal/llamacpp/releases.json`
name the pinned one; the CPU build is enough), write a tiny random GGUF with
`scripts/dev/make_tiny_gguf.py` and use it — `scripts/dev/measure_prompt_cache.py` does this
for the prompt order of the chat (E2-05 A4). Its numbers are prompt tokens processed, not
answer speed.

## Building from source and releases

```sh
make node-cross                       # node/bin/topcoat-node-{linux-amd64,windows-amd64.exe}
cd node && ../scripts/build-node-release.sh test-local dist    # like CI, with SHA256SUMS
docker build -f hub/Dockerfile -t topcoat-hub:local .          # hub image (context: repo root)
scripts/smoke-test-hub-image.sh topcoat-hub:local              # TLS/migration/GUI smoke test
```

There are two ways to a hub image and node binaries (ARCHITECTURE D123):

- **CI build (the usual way).** Every green push to `main` builds both (job `publish`, see below):
  the hub image goes to `ghcr.io/lu-freikasa/sloppy-topcoat-hub:sha-<commit>` (and `:test`), the
  node binaries with `SHA256SUMS` are a workflow artifact of that run. You roll the image out
  with `release.sh --image ghcr.io/...`; you check the node hashes and sign the release in the
  browser (D80), and the hub serves it through the update channel (D50).
- **Local build.** The commands above, or `release.sh` without `--image`: a release never depends
  on GitHub being reachable.

### Rolling a hub image out: `scripts/dev/release.sh`

```sh
scripts/dev/release.sh --dry-run --image ghcr.io/lu-freikasa/sloppy-topcoat-hub:sha-<commit>   # CI image
scripts/dev/release.sh --dry-run                                                             # local build
```

One command, one question per step, `--dry-run` prints everything and changes nothing:
backup tag (on `origin/main`, or on the local `main` while there is no remote; you push it) →
`make check` → `docker build`, or `docker pull` when `--image` names a registry image (its first
part is a host such as `ghcr.io`; it says so when the image's `sha-<commit>` is not the checkout
the checks ran on) → smoke test → the running production image is saved on the server
(the way back) → the new image is sent (`docker save | gzip | ssh`) → production through
`deploy/update-hub.sh` (revision check, database dump, health check, automatic rollback; a dry run
of it comes first and production is always asked, `--yes` or not). The servers come from
`TOPCOAT_PROD_SSH`, `TOPCOAT_PROD_DIR`, `TOPCOAT_BACKUP_REMOTE` (see the header of the script);
nothing real is in the repository. It needs bash with `docker`, `ssh` and `make` (WSL or Git Bash
on Windows). The test only checks the order of the steps, build or pull, and that a dry run
changes nothing.

`make check` needs Go, Node.js with npm, `uv` and a PostgreSQL 16 with pgvector (see
`scripts/dev/local-ci.sh`). Run it on a Linux clone (`git clone` into the home folder of WSL, not
under `/mnt/c`): on a Windows folder it would rebuild `hub/.venv` for Linux and break the Windows
environment. Answer `N` at the check step where those tools are missing.

Windows PowerShell: `cd node; $env:CGO_ENABLED = "0"; go build -trimpath -o topcoat-node.exe ./cmd/topcoat-node`.

GitHub Actions minutes are blocked for now (billing): run `make check` locally. Whether a
self-hosted runner is also stopped by that is not verified; try it with one job before moving
the workflow (`runs-on: [self-hosted, linux]`), and never give a runner on the family's Docker
server the registry or update keys.

CI (`.github/workflows/ci.yml`) builds and smoke-tests the hub image on every PR (job `image`).
Every green push to `main` runs job `publish` (release channel **test**, ARCHITECTURE D123):
- hub image `ghcr.io/lu-freikasa/sloppy-topcoat-hub:test` and `:sha-<commit>`,
- node binaries with version `test-<commit>` and `SHA256SUMS` as the workflow artifact
  `topcoat-node-test-<full commit>` (GitHub → Actions → the run of the CI workflow on `main` →
  *Artifacts*; kept 30 days, download needs a GitHub login). They are unsigned: nothing is
  published as a release, and the signing key never reaches the CI.

Every action in `.github/workflows/` is pinned to a full commit SHA with the version as a
comment (`uses: actions/checkout@<sha> # v4.4.0`). To update one, take the commit of the new tag
(`git ls-remote --tags https://github.com/<owner>/<action>`; for an annotated tag the `^{}`
line) and change the SHA and the comment together.

Channel `prod` (version tags) follows in stage 5.

## Adding a node provider

Providers are interfaces in `node/internal/providers/` (`GPUProvider`, `ModelRunner`,
`ProcessWatcher`, `Capability`, plus system info). Each has a fake in
`node/internal/providers/fake/` and real implementations in its own sub-package.

1. Implement the interface in the provider's package (e.g. `gpu/nvml.go`). No CGO; load
   native libraries dynamically if needed. Keep new dependencies out unless justified in
   the PR.
2. Wire the selection in the agent (by OS or by `config.runner.type`).
3. Add unit tests that run without the hardware (parse recorded API responses / sysfs
   fixtures in `testdata/`, or an `httptest` server for HTTP APIs such as llama-server's).
4. Mark everything you could not run on the real machine with
   `// UNVERIFIED on real hardware` and list it in the PR; remove the marker once it has been
   tested locally.

## Generating the knowledge directory

`scripts/dev/make_kb_index.py` writes `10_KI_Verzeichnis.md` (format: HUB-API.md, "The directory")
for a notes folder. It needs only Python 3.12 and imports the hub's parser, so the format cannot
drift; it skips what the knowledge base would not read (exclude list, other file types, files over
1 MiB). The file is written atomically, so it can run from cron on the machine that holds the notes.

```
python scripts/dev/make_kb_index.py --root /srv/notes                      # entities = note titles, ## sections
python scripts/dev/make_kb_index.py --root /srv/notes --names names.txt    # entities from a list
python scripts/dev/make_kb_index.py --root /srv/notes --check              # exit 1 if the file would change
```

`names.txt`: one `name: alias, alias` per line (`#` comments), kept outside the notes folder; every
section whose heading or first lines mention a name or alias becomes a reference of that entry.
Then re-index the source (or wait for the periodic run) so the hub sees the new file.

## Layout checks in CI

Two layout checks open the built GUI in a real Chromium against the faked hub
(`web/scripts/lib/fake-hub.mjs`, every `/api` call is answered from memory) and run in the web job of
CI after the build, so a layout regression they catch turns the job red: `npm run shell-layout` (the
navigation shell, below) and `npm run sheets-layout` (Sheets: the library table fits and does not
break its titles or keys, the stage's tools end inside the window and are not covered, both shells,
English and German, phone and desktop widths). Together they take about two minutes. `playwright` is
a devDependency, so `npm ci` installs it; the browser comes once with `npx playwright install chromium`
(CI installs the headless shell with `--with-deps --only-shell`). `CHROMIUM=<path>` uses another
Chromium binary.

```
cd web && npm run build && npm run shell-layout && npm run sheets-layout
```

The older chat check (`npm run layout`, `layout-check.mjs`) was removed in #208: it measured the chat
page of the first GUI, which the navigation shell replaced, and timed out waiting for its message box.

## Layout check of the navigation shell

`web/scripts/shell-check.mjs` checks the navigation shell (`webshell/WebShell.tsx`) the same way, against
the faked hub: the shell fills the window and a chat's message box stays inside it, the header never
scrolls sideways or cuts text (320 px up, English and German, a long user name), the phone drawer opens
below the header and the drawer and the user menu close on Escape and on a tap outside, with the focus
back on the button that opened them. All three looks, seven sizes from 320x640 to 1366x768. CI runs
it (see above):

```
cd web && npm run build && npm run shell-layout                     # all 216 checks
SIZES=320x640,1280x800 LANGS=de LOOKS=terminal SHOTS=/tmp/shots npm run shell-layout
```

## GUI checks in the desktop overlay (first)

The maintainer uses the GUI mainly in the **desktop overlay** of the client (`desktop/`, one see-through
window per screen with the hub's desktop, `?overlay=1&screen=<id>`). GUI work is checked there first and in
the browser's navigation shell second. Two tools, so nobody has to take screenshots by hand:

**1. Simulated overlay (anyone, no client, no hub).** `web/scripts/overlay-check.mjs` loads the built GUI as
the client does: `?overlay=1&screen=s1`, with `window.topcoatClient.config` set before the page starts. Against
the faked hub it checks one, two and three programs, a maximised one and the open command palette, on four
screens (1920x1080, 2560x1440 at 150 %, 1366x768, 1280x800), three looks, German and English: overlay mode
is on, the page paints no ground that covers the screen, nothing scrolls, the taskbar and the programs stay
on the screen and off the taskbar, no text is cut in title bars, taskbar or navigation, no page errors.
The faked hub sends the hub's security headers (`security_headers.py`), so every GUI check (overlay,
shell, sheets) also fails on a Content-Security-Policy refusal: a change that needs a script,
picture or connection from somewhere else shows up here, not first on the hub.
`SHOTS` keeps each step twice: see-through, and `-desk.png` over a busy desktop with the client's tint.

```
cd web && npm run build && npm run overlay-layout                         # all 120 checks
SCREENS=2560x1440@1.5 LANGS=de LOOKS=terminal SHOTS=/tmp/shots npm run overlay-layout
CHANNEL=msedge npm run overlay-layout                                     # Windows: the installed Edge
```

**2. The real overlay (the maintainer's PC).** Start the client with the developer's switch
`TOPCOAT_DEVTOOLS_PORT` (`desktop/src-tauri/src/devtools.rs`: WebView2 opens the DevTools protocol on
127.0.0.1 only, off unless the variable is set). `web/scripts/overlay-capture.mjs` attaches to it, takes a
screenshot of every overlay page that is shown and runs the same audit. It only looks: no click, no typing,
no navigation, so the desktop on the hub stays as it is. `SHOW=1` presses the key for every screen itself.

```powershell
$env:TOPCOAT_DEVTOOLS_PORT = "9222"; & "$HOME\Documents\Dev\topcoat-client\topcoat-desktop.exe"
cd web; $env:SHOW = "1"; npm run overlay-capture        # screenshots in web/overlay-shots/
```

Both need `playwright` or `playwright-core` (`npm i -g playwright-core`; with `NODE_PATH` set to
`npm root -g`). Neither is a CI job.


## Your own checks

Manual checks for your own hardware and services belong in your local copy, not in the repository.