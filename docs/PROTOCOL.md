# Node ↔ hub protocol (version 1)

> **Scope of this file.** The node protocol: transport, pairing, the session, the message table,
> the actions and the capabilities of tool nodes, signed updates, model transfer, versioning and the
> build service. What the **hub** does with it (the agent, memory, the knowledge base, the model gateway,
> messages and the hub's HTTP API for the web) is in [HUB-API.md](HUB-API.md). The JSON Schemas in
> `proto/v1/` are the source of truth for the node messages.

Transport: one WebSocket per node, opened **by the node** to `wss://<hub>/api/node/ws`.
Messages are JSON objects. The JSON Schemas in `proto/` are the source of truth; this file
explains them. Every message has:

```json
{ "v": 0, "type": "<message type>", "id": "<uuid>", "ts": "<RFC 3339 UTC>", "body": { } }
```

Replies reference the request with `"re": "<id of request>"`.

## Transport security (D11)
The hub serves HTTPS/WSS itself (TLS mode `auto`: certificate from the hub's own CA; mode
`files`: an admin-provided certificate) or sits behind a TLS-terminating reverse proxy (mode
`off`). The node trusts the hub through, in this order:
1. the CA pinned at pairing time (`--ca-fingerprint sha256:<hex>` from the GUI's pair command;
   the node accepts the hub only if a certificate in the presented chain has that SHA-256
   fingerprint and the chain verifies against it, host name included) and stored as
   `hub-ca.pem` in its data dir,
2. a CA file given at pairing time (`--ca-file`), also stored as `hub-ca.pem`,
3. otherwise the operating system's trust store.

The one exception to "nodes connect outbound only" is the single-use socket of a direct model
transfer between two nodes, pinned to an ephemeral certificate per transfer (E2-07, D51, see
"Direct model transfer"). Nodes still never listen without an instruction from the hub.

`http://` hub URLs are refused unless the node was paired with `--insecure-http` (tests and
local development only). `GET /api/tls` returns `{mode, ca_fingerprint, ca_available}`,
`GET /api/tls/ca.pem` the CA certificate (both without authentication).

## Pairing (once per node)
1. Admin clicks "add node" in the GUI → hub creates a one-time pairing code (valid 15 min).
   The response also carries `hub_url` (the configured public URL or null) and
   `ca_fingerprint`, so the GUI can print the complete command.
2. On the machine: `topcoat-node pair --hub https://hub.example.lan:8443 --code ABCD-1234
   --ca-fingerprint sha256:<hex>` (or `install` with the same arguments).
3. Node generates an ed25519 key pair, sends `POST /api/node/pair {code, name, os, arch,
   public_key, role?}`. `role` is `gpu` (default), `tools` (`--role tools`, E2-04) or `dev` (`--role dev`, E6-01): a tool node
   gets a config without a runner and with the read-only capabilities (see "Tool nodes"), a dev node one with
   the workshop capabilities (see "Dev nodes").
4. Hub returns `{node_id}` and stores the public key. The node keeps the private key in its
   data dir (`node.key`, owner-only permissions). The code is burned.

   Errors: `400 invalid_request`, `404 invalid_code`, `410 code_expired` (expired or used),
   `409 name_taken`. Schemas: `proto/v1/pair_request.schema.json`, `pair_response.schema.json`.

## Session
Before every connect the node fetches a single-use nonce: `POST /api/node/challenge
{node_id}` → `{nonce, expires_in}` (valid 60 s; the hub answers the same way for unknown node
ids). It signs `topcoat-node-auth-v1\n<node_id>\n<nonce>` with its key and opens the
WebSocket with `Authorization: Topcoat-Ed25519 <node_id>.<nonce>.<signature>` (signature
base64url without padding). A missing, reused, expired or wrongly signed nonce is refused
during the handshake (HTTP 403). Schemas: `challenge_request`, `challenge_response`.

The first message must be `hello` with the node's own `node_id`. A second connection of the
same node replaces the first one (close code 4409). Other close codes: 4400 protocol error,
4408 no hello in time.

| Direction | type | body (summary) |
|---|---|---|
| node → hub | `hello` | `node_id`, `agent_version`, `protocol_version`, `config_hash` |
| hub → node | `config` | full desired config (roles, model dirs, runner incl. `backend` / `llamacpp_release` / `threads` / `default_context_length` / `model_context_lengths`, watcher rules, capabilities, idle TTL, `residents`, `pressure`) + `config_hash` |
| node → hub | `inventory` | hardware (CPU incl. P/E-core counts, RAM, GPUs with VRAM total; iGPUs flagged `integrated` with `shared_total_mb`), runner type + llama.cpp backend/release/threads, downloaded models (with `own`: in the node's own models directory or in a configured `model_dirs` entry), loaded models (pid, port, backend, measured memory) |
| node → hub | `heartbeat` | every 10 s: RAM used, `cpu_util` (all logical processors together in percent since the previous heartbeat, when known; optional, an older node sends none), per-GPU VRAM used (and `shared_used_mb` for iGPUs; `gpu_util` = utilisation of the whole GPU in percent, when known; `foreign_vram_used_mb` / `foreign_gpu_util` = other programs, `foreign_shared_max_mb` = largest shared-memory use of one other program, when known), loaded models, `loading` = models being loaded right now, node state |
| hub → node | `action` | `name` + `args` (see below) |
| node → hub | `action_result` | `re`, `ok`, `result` or `error` |
| node → hub | `event` | state changes: `model_loaded`, `model_unloaded`, `game_detected`, `game_ended`, `download_progress`, `state_changed`, `benchmark_started`, `benchmark_result`, `benchmark_error`, `benchmark_finished`, `runtime_installed`, `auto_reload_paused`, `model_load_failed`, `model_load_retry`, `paused`, `resumed`, `sleeping`, `error` |
| node → hub | `queued_events` | events collected while the hub was unreachable (each keeps its own `id`/`ts`; the hub ignores ids it already stored) |
| hub → node | `error` | `code`, `message`, optional `re`: `upgrade_required`, `invalid_message` |
| hub → node | `proxy_request` | gateway request for a loaded model: `stream_id`, `model`, `method`, `path`, `headers`, `body` (base64) — see "Model gateway" |
| node → hub | `proxy_chunk` | `stream_id`, `seq`, `data` (base64, ≤ 64 KiB); the first chunk carries `status` and `content_type` |
| node → hub | `proxy_end` | `stream_id`, `status`, optional `error` |
| node → hub | `proxy_status` | `stream_id`, `state` (`paused` \| `running`), optional `reasons`: the answer stands still because the node froze its model for local use — see "Yielding to local use" |
| hub → node | `proxy_cancel` | `stream_id`: the client went away, stop forwarding |

Watcher rules match an executable name (`process`) or a folder prefix of the executable
path (`path_prefix`, e.g. a Steam library), both case-insensitive. After a match the node
waits `grace_seconds`, then unloads all models; it reports `busy_gaming` until
`cooldown_seconds` (default 60) after the last matching process ended.

Node state: `available` · `busy_gaming` · `degraded` (runner unreachable) · `yielded` (models were
unloaded because other programs need the memory, E1-06) · `paused` (models frozen for the person
using the machine, E2-05 B) · `offline` (hub-side only). Precedence when several apply:
`busy_gaming`, `degraded`, `yielded`, `paused`, `available`.

## Actions (stage 1)
| name | args | effect |
|---|---|---|
| `models.list` | – | refresh model inventory |
| `models.load` | `model`, optional `context_length`, `n_parallel`, `ttl_seconds` | load a model via the runner |
| `models.unload` | `model` or `all: true` | unload |
| `models.download` | `source` = `{"type": "huggingface", "repo", "file", optional "revision"}`, optional `target_name` | start download in the background, progress via `download_progress` events |
| `models.delete` | `model` | delete local files (requires `confirm: true`) |
| `capability.invoke` | `capability`, `input` | run one read-only capability of a tool node (E2-04, see "Tool nodes") |
| `capability.execute` | `capability`, `input`, `proposal` (uuid), `hash` (SHA-256 hex) | run one **state-changing** capability the user confirmed (E3-01b, see "Proposals and confirmed execution"); the mirror of `capability.invoke` |
| `build.manage` | `capability`, `input` | run one step of the build service (NS-01, D80; see "Build service"): only a capability named `build.*` with `manage: true`; the hub starts it only on a signed-in admin's click on the Releases page; the build service never signs |
| `hub.manage` | `capability`, `input` | run the roll-out of the hub on the production host (NS-01, phase 5): only the capability `hub.update` (`image`, `digest`, optional `dry_run`), which checks that the image on the host has the digest that was built and tested and then runs `deploy/update-hub.sh` (revision check, dump, health check, automatic way back); the hub sends it only on the admin's own click |
| `workshop.manage` | `capability`, `input`, optional `repo` | run one of the person's own steps on a local repository of the dev workshop (D76, see "Local repositories"): only a capability with `manage: true`, which no other action runs |
| `node.benchmark` | `models` (non-empty list of model names), optional `quick` (bool) | start a `llama-bench` run in the background (E1-02, see below) |
| `node.update` | optional `version` (`MAJOR.MINOR.PATCH[-pre]`) | fetch the newest (or the named) signed build of the node's channel and stage it for the updater (E2-06, see "Signed updates"); answers `{"started": true, "channel": …}` at once |
| `models.send` | `transfer_id` (uuid), `model`, `token`, `peer_addresses` | open the single-use transfer socket for one model and report `transfer_ready` (E2-07, see "Direct model transfer"); answers `{"started": true}` |
| `models.receive` | `transfer_id`, `model` (target name), `size`, `sha256`, `token`, `cert_sha256`, `endpoints` | pull the model from the source node's socket (E2-07); answers `{"started": true, "model": …}` |

Unknown action names and invalid args are rejected by the node with `error.code = "not_allowed"` / `"invalid_args"`.
An action is only executed if its name is in the node's `config.allowed_actions` (assigned by
the hub). Other error codes: `not_implemented`, `runner_error`, `busy` (node is `busy_gaming`),
`internal`, `capability_failed` (E2-04). Hub-side only (REST): `node_offline`, `timeout`.

### Long-running actions (E1-02)
`models.download` and `node.benchmark` answer immediately and continue in the background, so
the hub's action timeout does not apply to the work itself.

- `models.download` → `result: {"started": true, "model": "<name>"}`. The model name is
  `target_name` or the file name without `.gguf`. Events `download_progress` with `data`
  `{model, status: "running"|"done"|"failed", downloaded_bytes, total_bytes, error?}`
  (throttled to about one per 2 s while running). The file is stored in
  `<data dir>/models/<name>.gguf`, resumed from a `.part` file after interruptions and
  verified against the SHA-256 Hugging Face publishes for the file (LFS `oid`); a mismatch
  deletes the partial file and reports `failed`.
- `models.delete` → args `model`, `confirm: true`; deletes only files inside the node's own
  `models/` directory (never files in the configured `model_dirs`). Loaded models are
  unloaded first.
- `node.benchmark` → `result: {"started": true, "benchmark_id": "<uuid>"}`. Refused with
  `busy` while the node is `busy_gaming` or another benchmark runs. Events:
  `benchmark_started {benchmark_id, models, quick}`, one `benchmark_result {benchmark_id,
  model, backend, threads, n_gpu_layers, pp_tps, tg_tps}` per measurement (pp512 / tg128),
  `benchmark_error {benchmark_id, model, backend, threads, n_gpu_layers?, error}` for a run that
  failed (E2-05: the benchmark goes on with the other runs), and
  `benchmark_finished {benchmark_id, ok, error?, failed_runs?, recommended_threads?}`. If a game starts
  the benchmark is aborted (`ok: false, error: "aborted: game detected"`).
  A node ends what hangs: one `llama-bench` run that takes more than 10 minutes counts as failed
  (`benchmark_error`, "timed out after 10m0s") and the benchmark goes on; a whole benchmark that takes
  more than 2 hours ends with `ok: false, error: "timeout: ..."`, its results so far are kept. A node that
  is restarted during a benchmark never sends `benchmark_finished`; the web treats a `benchmark_started`
  older than 2.5 hours without an end as lost and allows a new start.
  The hub stores every `benchmark_result` in `node_benchmarks` and, when
  `recommended_threads` is present, sets `config.runner.threads` and sends the new config.

Thread sweep (CPU work): candidates are *P-cores only*, *P-cores + half the E-cores*,
*all physical cores* and *all logical threads* (duplicates removed; non-hybrid CPUs give
physical cores vs. all threads). The candidate with the highest `tg_tps` on the CPU
backend becomes `recommended_threads`. A `quick` run uses one repetition and only the
first and last candidate. On nodes whose GPU is integrated, every model is measured on the
GPU backend **and** on the CPU (`--device none`), so the two can be compared.

**GPU runs let llama.cpp fit the model (E2-05).** A GPU run is `llama-bench -fitt 1024` without
`-ngl`, so a model larger than the (shared) device memory is offloaded partially instead of
failing; `n_gpu_layers` in `benchmark_result` is what llama-bench reports (absent = unknown).
A run that fails (Vulkan `ErrorOutOfDeviceMemory` …) emits `benchmark_error` and the next run
still happens; `benchmark_finished` has `ok: false` only if **every** run failed (or the
benchmark was aborted) and `failed_runs` counts the failures otherwise. Models with a
`model_backends` entry are measured with their own backend and layer count.

### llama.cpp runtime (E1-02)
`config.runner.backend` (`auto` | `cuda` | `vulkan` | `cpu`) selects the build; `auto` maps
NVIDIA → `cuda`, AMD/Intel → `vulkan`, no GPU → `cpu`. `config.runner.llamacpp_release`
pins the release; the node only installs releases listed with SHA-256 values in its
built-in pin list (`node/internal/llamacpp/releases.json`), so upgrading llama.cpp means a
new node version plus a hub config change. While the build downloads, the runner reports
`available: false` with a progress `detail`; once installed the node emits
`runtime_installed {release, backend}`.

### Backend per model, partial offload and resource guards (E2-05, ARCHITECTURE D27)
- `config.runner.model_backends`: `{<model>: {backend?: cuda|vulkan|cpu, gpu_layers?: 0..1000}}`.
  A model without an entry uses the node's backend; an entry with another `backend` uses that
  build of the pinned llama.cpp release (installed next to the default one, in the background
  after the config arrived — a load before that fails with the install status). Processes of
  different backends run side by side. `gpu_layers` fixes the number of layers on the GPU
  (`-ngl`, 0 = none); without it llama.cpp fits as many layers as the device memory holds
  (`--fit on`, which the pinned b11223 has by default and reports in its log). Changing this map
  or `memory_guard` restarts the runner (its models are unloaded and reloaded by residents).
  An empty map (`{}`) is valid and means "no overrides" (removing the last entry in the GUI
  leaves one behind); the same holds for `model_context_lengths`.
- **Integrated GPU: the whole model or the CPU (E2-05 B).** An iGPU reports its shared *system*
  memory as free device memory, and llama.cpp's host/pinned buffers come out of the same budget as
  the offloaded layers, so a partial offload never worked on the real laptop (49/49 layers, and
  also the half-layer retry, died of an allocation failure after ~30 s each). For a machine whose
  only GPU is integrated the node therefore plans **no** partial offload: if weights + KV cache
  + the memory-guard reserve fit into `shared memory − margin − what loaded models already hold on
  the iGPU` (margin = `max(4096, ¼ of the shared memory)` MB) the model loads with `-ngl all --fit off`, otherwise it goes
  straight to the CPU (`--device none`; the CPU build is used when it is installed, and the model
  reports backend `cpu`). One attempt, no failed ones in between. `gpu_layers` in
  `model_backends` still wins. A **discrete** GPU keeps llama.cpp's own fit with its default target.
  The benchmark still measures an iGPU with `llama-bench -fitt <margin>` (a partial offload where
  it works, a `benchmark_error` where not).
- `model_backends.<model>.no_host` (bool, additive): passes `--no-host` ("bypass host buffer
  allowing extra buffers to be used", present in the pinned b11223) to that model's server.
  **UNVERIFIED on real hardware:** whether it lets a partial offload work on an iGPU without pinned
  memory — an experiment for `node-laptop`, off by default.
- `model_backends.<model>.n_cpu_moe` (integer, additive): passes `--n-cpu-moe N` (the expert weights
  of the first N layers of a Mixture-of-Experts model stay in the system RAM, present in the pinned
  b11223) to that model's server, together with `-ngl all --fit off` unless `gpu_layers` is set. One
  explicit attempt, no staged retry. The GUI benchmark measures that model with `llama-bench
  -ncmoe N`. **UNVERIFIED on real hardware:** for a discrete GPU such as `node-amd`; on an iGPU it
  gains nothing (shared memory). Off by default.
- **Staged retry (discrete GPU).** If a load dies of an allocation failure (`failed to allocate`,
  `out of memory`, `ErrorOutOfDeviceMemory` …) the node tries again with fewer resources: fit →
  half of the layers → CPU only (`--device none`; the model then reports backend `cpu`). An
  integrated GPU has one GPU attempt at most, then the CPU. Every retry is an event
  `model_load_retry {model, attempt, plan, error}`. A configured `gpu_layers` is never retried with
  another value; a failure that is not about memory is not retried.
- **Every attempt is guarded.** The memory guard runs again before *each* attempt with the plan's
  real footprint (a CPU fallback needs the whole model in RAM); an attempt that would not fit ends
  the load with `insufficient_memory` instead of starting. Before the next attempt the node waits
  until the failed server's process is gone and its memory is free again (bounded); a failed server
  that cannot be stopped blocks the retry rather than piling a second model on top of it.

### Loads run as jobs (E2-05 A fixes)
A load of a big model takes 30 s to several minutes, longer than a hub request should wait.
- `models.load` starts a **load job** on the node; further requests for the same model (an
  action plus a chat request) join it — one process, one `model_loaded` event. A model that is
  loaded already answers at once and reports nothing.
- The job outlives the action and the hub session. Its outcome is always an event:
  `model_loaded {model, vram_used_mb?}` or `model_load_failed {model, code, message}` (the
  message carries the llama.cpp reason). While the hub is unreachable the event is queued like
  any other.
- `models.unload` (one model or `all`) **cancels** a load of that model in progress: the
  half-started `llama-server` is **killed** (not asked politely — a load stuck in the allocation
  used to run on for ~90 s and then be unloaded), the waiting action gets the error code
  `canceled`, and nothing loads afterwards. The node emits the event
  `model_load_cancelled {model, reason, after_seconds}` (`reason`: `unload`, `unload_all`,
  `pause_timeout`) instead of `model_load_failed`; the process is gone before the
  next load may start.
- Hub side: `POST /api/nodes/{id}/actions` waits for `models.load` as long as the gateway does
  (`gateway_load_timeout_seconds`, 600 s), other actions 30 s. If the hub gave up anyway (504 or
  the session dropped) and the node's `action_result` arrives later, the hub stores it as an event
  `action_late_result {name, ok, error?}` (the hub remembers the last 64 timed-out actions per
  session).

### Local llama-server hardening (E2-05 A fixes)
Every `llama-server` start gets its own random 256-bit API key, handed over through a file only
the node's account can read (`<data>/run/llama-server-<port>.key`, removed right after the
start; never on the command line, which every local user can read) with `--api-key-file`. The
node adds it as a bearer token to the requests it forwards (`models.proxy`); a bearer token sent
by the hub is dropped. `/health` stays public (the node polls it). CORS is limited to
`localhost` origins without credentials (`--cors-origins localhost --no-cors-credentials`), so a
web page in the user's browser cannot talk to the model server. The servers still bind to
`127.0.0.1` only. Verified with the pinned b11223 (401 without the key, no
`Access-Control-Allow-Origin` for a foreign origin). Nodes get this with the update: their
servers restart with the runner.
- **Memory guard** (`config.runner.memory_guard {mode: refuse|warn|off, reserve_mb}`, default
  `refuse` / a reserve of the larger of 4096 MB and 12 % of the RAM — `reserve_mb: 0` or absent
  means that automatic value; 1 GB was too little on a 32 GB laptop that had a 7B on the iGPU
  plus a 30B on the CPU loaded and 1.6 GB free): before a load the node estimates weights (file size) + KV cache (exact, from
  the GGUF header: layers × KV heads × head sizes × context × 2 bytes, matching the llama.cpp
  log; **sliding-window attention** (Gemma 3/4) counts only the window — `attention.sliding_window`
  (+ one micro-batch of 512, per slot) — for the layers the file marks as sliding
  (`attention.sliding_window_pattern`: an integer n = every n-th layer is a full-attention layer,
  or one flag per layer, true = sliding), with the sliding layers' own `key_length_swa`/
  `value_length_swa`, KV heads given per layer, and layers that share another layer's cache
  (`attention.shared_kv_layers`) counting none; a window without a pattern counts as full attention) + overhead (300 MB + 2 % of the weights) and compares it with the free RAM. On the CPU
  backend and on an iGPU everything counts as system memory; with a discrete GPU only what does
  not fit into its free VRAM (plus 512 MB of host buffers). Too little: `models.load` fails with
  `insufficient_memory` (mode `warn`: the load happens and an `error` event with the same code
  is emitted). Resident reloads that fail this way wait like any failed load.
- **RAM pressure default:** `pressure.ram.enabled` left out is *on* for a node without a dedicated
  GPU (models in system memory: CPU backend, iGPU) and *off* with one; an explicit value wins.
  The hub's default config leaves it out; nodes configured before E2-05 keep their explicit
  `false` (set it in the GUI to change it).
- **Disk space:** the inventory's `hardware.disk_total_mb` / `disk_free_mb` describe the drive
  that holds the model directory (refreshed with every inventory, i.e. after downloads and
  deletes). `models.download` is refused with `insufficient_disk` when the file (size from the
  Hugging Face metadata, minus a resumable `.part`) plus 1 GiB does not fit; a check that cannot
  be made (Hugging Face unreachable) does not block.
- New error codes `insufficient_memory`, `insufficient_disk`, `canceled`; new event kinds `benchmark_error`, `model_load_failed`, `model_load_retry`, `model_load_cancelled` (E2-05 B).

### Context length of loads (E2-01 fixes)
A load that names no context (`models.load` without `context_length`, a gateway load, a
resident reload) uses, in this order: the `residents[].context_length` of the model, else
`config.runner.model_context_lengths[<model>]`, else `config.runner.default_context_length`,
else the node's built-in 8192. An explicit `context_length` argument always wins. The value is
the context of **one slot** (one request: prompt plus answer); the node reports it in the
heartbeat (`loaded[].context_length`, with `loaded[].n_parallel`) and the hub budgets chat
prompts against it. All three keys are additive in v1 (256 … 1048576).

### Context sized by the node (`min_context_length`, consolidation step 2)
`models.load` takes one more optional argument, `min_context_length`: "at least this much context
per slot, as much more as fits". It is the hub's way to say what a use needs (an agent run 16384, a
chat answer less) without knowing the machine. Without `context_length`, the node then picks the
largest value from a fixed ladder (4096, 6144, 8192, 12288, 16384, 24576, 32768, 49152, 65536 …)
that is

- at least `min_context_length` and at least what the node's own configuration would have used
  (`residents[].context_length`, `model_context_lengths`, `default_context_length`),
- at most `config.runner.max_context_length` (default 32768) and at most what the model was trained
  for (GGUF `context_length`),
- on a discrete GPU, small enough that the whole model with its KV cache stays on the device (a
  larger context that pushes layers into system memory makes the model slower, so the node does
  not grow it beyond the floor then); on the CPU and on an integrated GPU, small enough for the
  free memory minus the reserve (the same estimate as the memory guard, which keeps its own mode).

If not even the floor fits, the floor is used and the memory guard answers as it always did
(`insufficient_memory`). An explicit `context_length` wins over `min_context_length`. The context
the node settled on is reported as before (`loaded[].context_length`); a model that is already
loaded is not reloaded by `models.load` (the hub unloads it first when it needs a larger context and
the node is idle). A node that does not know the argument answers `invalid_args`; the hub then falls
back to `context_length`. Additive in v1: `models.load.min_context_length`,
`config.runner.max_context_length`.

### Slots of a model (`n_parallel`, first hardware tests of E3-01a)
llama-server started without `-np` runs 4 slots (auto) in **one shared window** of `-c` tokens
(verified with b11223: `n_slots = 4`, `kv_unified = true`): two long generations and an idle
slot's cached prompt used up the window, and a chat on the same model died with "Context size has
been exceeded". So the node always passes `-np N` (verified: with an explicit `-np` the slots do
not share, `-c 1024 -np 2` gives `n_ctx_seq = 512`) and starts the server with
`-c <context_length> × N`: **the configured context is what one request gets**, the window (and
the KV memory, which the RAM check accounts for) is N times that. `N` comes from, in this order:
`models.load` argument `n_parallel`, `config.runner.model_n_parallel[<model>]`,
`config.runner.n_parallel`, else **1** (1–16). 1–2 suit background and agent use: requests to a
busy model wait in the server's queue instead of eating each other's context. The hub keeps
**one agent generation per model** at a time on top of that. `loaded[].n_parallel` is absent for
nodes that predate this (read as 1, but such a node still runs auto slots).

### `--jinja` and native tool calls (E3-01c)
`config.runner.jinja` (default true) and `config.runner.model_jinja[<model>]` decide whether the
node starts llama-server with `--jinja` or `--no-jinja` (the model's own chat template engine,
the prerequisite of the OpenAI-compatible `tools` / `tool_calls`; b11223 has it on by default, the
node passes it explicitly). llama-server's own agent tools (`--tools`, `--agent`, `--mcp-…`: shell
and files) are never enabled. Both keys are additive in v1.

### Vision models and their projector (D125)
A model that can see is two GGUF files: the language model and its projector (architecture
`clip`, by convention `mmproj` in the name). The node never lists a projector as a model; it
loads a model together with its projector (`llama-server --mmproj <file>`) and reports the file
name as `models[].projector` in the inventory. The projector is looked for in the model's own
directory only: `config.runner.projectors[<model>]` (a file name, or `"none"`) wins; else by name
(`mmproj-<model>.gguf`, `<model>.mmproj.gguf`, `<model>-mmproj*.gguf`); else the only projector of
a subfolder below a models directory (a downloaded repository; with several, F16 before BF16
before Q8_0, then by name), never a lone projector in the top of a models directory. The hub's
model facts count a model with a projector as `vision`. Both keys are additive in v1.

### Image runner (D126)
`config.images {enabled, backend?: auto|vulkan|cuda|cpu, model_dirs?}` switches on the node's second
runner: stable-diffusion.cpp's `sd-server`, a pinned official release (`node/internal/sdcpp/releases.json`,
SHA-256 = GitHub's asset digests) installed below `<data dir>/runtime-sd` when the first image
model loads. Image models are `.gguf` and `.safetensors` files in `<data dir>/image-models` and
`model_dirs` (pickle-based `.ckpt`/`.pt` are not offered: loading one can run code). One image model
runs at a time, started with `--listen-ip 127.0.0.1` on a free port. `sd-server` has **no API key**
(none in the pinned release nor in master of 2026-11-25), so it is protected only by loopback and the
random port. There is no official Linux CUDA build: Linux NVIDIA uses Vulkan; the Linux builds need
glibc 2.43 (Ubuntu 26.04). The inventory reports `images {release, backend, installed, models[],
loaded[]}`. The actions `image.generate` / `image.edit` follow with milestone P6. Additive in v1.

**Tool mode of the agent (hub).** How the agent talks to a model is set per model — hub setting
`agent_tool_modes`, `GET/PUT /api/agent/settings` (PUT admin) — to `native` (one function per
capability plus `finish`, `tool_choice: required`, results as `tool` messages), `json` (the
prompt-based protocol) or `auto` (default, `TOPCOAT_AGENT_TOOL_MODE`): native, and the JSON
protocol for the rest of the run — and the hub's memory of the model until it restarts — if the
server refuses `tools` or the model answers twice in prose instead of calling a function. Both
modes store the same steps (the JSON action text, plus the native call beside it), so a run may
switch. `tool_choice: required` that a server rejects is dropped once (then `auto`).

**Speed: several calls in one reply, short plans.** The model may ask for up to
`max_parallel_calls` (4) independent looks in one reply (`parallel_tool_calls: true`); the hub
stores and runs them one after the other in the model's order, each as a call of its own (assistant
call, `tool` result, …), so the transcript is valid for any chat template and only the model turns
in between are saved (the calls count against the call budget; surplus or invalid ones are left
out). The prompts ask for a "reason" of at most 8 words, a diagnosis of at most 4 findings and the
compact form of a listing first (`docker.ps` without `detail`). The JSON protocol still makes one
call per reply.

**A cut-off answer (`finish_reason: length`).** The reply limit is 1536 tokens (JSON protocol) /
2048 (native), because a `finish` carries the summary and every finding. If a reply still hits it,
the hub does not call it "not a valid action": a `finish` whose `summary` is complete becomes a
diagnosis **without findings** (note: "the model's answer was cut off at the output limit: its
findings are missing"), anything else is answered with "your answer was too long … answer again,
much shorter", and three such answers in a row end the task as `bad_format` with the error "cut off
at the output limit … (too long)" — the evidence stays with the task either way.

**Context of agent loads (`TOPCOAT_AGENT_MIN_CONTEXT`, default 16384, 0 = off).** When the hub loads
a model for an agent, it passes `context_length` (the context of one slot) to `models.load` if
the node's own config gives less — a resident entry, `runner.model_context_lengths`,
`runner.default_context_length`, else the runner default 8192 — and never lowers a larger configured
value. A model that is already loaded with less is not reloaded: the run gets one note ("… is loaded
on … with a context of N tokens; an agent with tools needs at least 16384 …"), in the steps and in
the diagnosis' notes.

### `context_full`
A model error that says the window is used up — "Context size has been exceeded" in the stream
or HTTP 400 `exceed_context_size_error` before it — becomes the hub error code `context_full`
(chat: SSE `error {code: "context_full", retry: true}` and the stored message's error, or HTTP 503
with `Retry-After` before the stream starts; gateway `call`/agent: a retryable model failure). The
raw `/v1` passthrough is untouched.

### VRAM pressure and resident models (E1-06)
`config.residents[]` lists models the node keeps loaded whenever possible:
`{model, context_length?, priority (0–100, default 50, higher = more important), auto_reload
(default true)}`. The GUI edits entries through `PUT` / `DELETE
/api/nodes/{id}/residents/{model}` (event `config_changed {source: "residents", model,
resident}`, `resident: null` when removed). The hub clears `auto_reload` when a user unloads
the model through `POST /api/nodes/{id}/actions` and sets it again when the user loads it
(event `config_changed {source: "manual_unload" | "manual_load", models, auto_reload}`), and
pushes the new config right after the action result. Removing the entry ends "keep ready".
Residents with `auto_reload` are exempt from the idle TTL.

A node's **house model** (D117) is always one of its residents: the hub makes it an entry with
`auto_reload: true` when the house models change (`PUT /api/scheduling/house`; the replaced house
model's entry is removed) and again each time the node connects, before it sends the config
(event `config_changed {source: "house" | "house_reconnect", model, previous}`). A manual unload of
the house model therefore lasts until the next reconnect.

`config.pressure` (absent = off; missing fields = defaults):

| field | default | meaning |
|---|---|---|
| `enabled` | `true` | watch the node's **discrete** GPUs (iGPUs and CPU-only nodes are skipped) |
| `poll_seconds` | 2 | sampling interval |
| `sustain_seconds` | 3 | a pressure signal must hold this long before the first yield |
| `unload_below_free_mb` | 1024 | free VRAM (total − used by all processes) below this is pressure … |
| `margin_mb` | 1024 | … and yielding continues until free ≥ `unload_below_free_mb + margin_mb` |
| `reload_after_seconds` | 60 | reload conditions must hold this long continuously |
| `reload_headroom_mb` | 2048 | free VRAM needed on top of the model's last measured VRAM |
| `reload_max_foreign_gpu_util` | 30 | no reload while other programs use more GPU than this (%) |
| `min_off_seconds` | 120 | minimum time from a yield or game unload to the next reload |
| `reload_while_rule_matches` | `false` | allow reloads while a watcher rule matches or cools down |
| `foreign_shared_trigger_mb` | 1024 | *unmet demand*: one other process with at least this much shared GPU memory counts as waiting for VRAM (0 = signal off) |
| `ram.enabled` | open | same handling on system RAM for models on the CPU backend / an iGPU; left out = on for nodes without a dedicated GPU, off otherwise (E2-05); an explicit value wins |
| `ram.unload_below_free_mb` / `ram.margin_mb` / `ram.reload_headroom_mb` | 2048 / 1024 / 1024 | RAM thresholds |

**Pressure signals** (per discrete GPU, each must hold for `sustain_seconds`):
- *free VRAM* below `unload_below_free_mb` **and** other programs' VRAM (used minus this node's
  model processes) grew by at least 256 MB since the node's models last changed. The second
  condition keeps a model that fills the card by itself from being yielded right after a
  manual load;
- *demotion*: the dedicated VRAM of the node's model processes dropped by ≥ 256 MB below its
  peak while their shared (system) memory rose by ≥ 128 MB — on Windows the driver pages our
  buffers out instead of failing the other program's allocation. Needs per-process counters
  of both kinds (Windows PDH, amdgpu fdinfo); NVML reports no per-process system memory, so on
  NVIDIA/Linux only the free-VRAM signal exists;
- *unmet demand*: a single other process uses at least `foreign_shared_trigger_mb` of shared
  (system) GPU memory **and** free VRAM is below that shared use + `margin_mb`. Found in test 1:
  when LM Studio loaded a 20 B model next to our 7 B model, WDDM did not demote our buffers but
  gave LM Studio 4.8 GB dedicated + 7.7 GB shared while 2.7 GB stayed free — neither signal
  above fired. This signal has no foreign-growth guard: a program already waiting in system
  memory has priority, so a manual load next to it is yielded again after `sustain_seconds`
  (set the trigger to 0 to prevent that).
  Availability: Windows via PDH `GPU Process Memory(*)\Shared Usage` for every vendor (NVIDIA's
  "sysmem fallback" shows up here too). Linux NVIDIA: none — NVML has no per-process system
  memory, and the Linux driver is not known to fall back to system memory (an allocation that
  does not fit fails, which no signal can see; UNVERIFIED). Linux AMD: not measured — amdgpu
  reports GTT per process in fdinfo, but the service user cannot read other users' fdinfo and
  GTT is used routinely by desktop programs.

The signal that started a yield is reported as `signal: "low_free" | "demotion" |
"unmet_demand"` in `model_unloaded`.

**Yield:** unload one loaded model at a time — non-residents first, then lowest `priority`,
then least recently used — re-checking after each, until free VRAM ≥ `unload_below_free_mb +
margin_mb` and no unmet demand remains. Event `model_unloaded {model, reason: "vram_pressure" |
"ram_pressure", signal?, free_mb}`, state `yielded`. While unmet demand persists the node stays
`yielded` even with nothing left to unload.

**Reload:** the highest-priority resident with `auto_reload` that is on disk but not loaded is
loaded again when, continuously for `reload_after_seconds`: free VRAM ≥ its last measured VRAM
(else its file size) + `reload_headroom_mb`, where memory another program waits for in shared
memory (unmet demand) does not count as free; other programs' GPU utilisation ≤
`reload_max_foreign_gpu_util` (skipped where it cannot be measured); no watcher rule matches
and no cooldown runs (unless `reload_while_rule_matches`); `min_off_seconds` passed since the
last yield or game unload; no benchmark runs and the runner is healthy. One model at a time,
the timer restarts after each load. Event `model_loaded {model, reason: "pressure_cleared"}`
for a model that was yielded, `reason: "resident"` otherwise; the state returns to
`available` when nothing yielded is waiting any more. Nodes without a discrete GPU check free
RAM (`ram.reload_headroom_mb`) instead, so residents work on CPU-only nodes too.

**Flap guard:** a model yielded 3 times within 30 minutes is not reloaded automatically for 60
minutes: event `auto_reload_paused {model, until, yields}`. A manual load lifts the pause. A
failed automatic load is retried after 5 minutes (event `error`).

**Watcher rules** stay the fast path: a match unloads everything after `grace_seconds` without
waiting for pressure, and vetoes reloads while it matches (plus cooldown).

### Yielding to local use (E2-05 B, ARCHITECTURE D24)
Local programs and the person at the keyboard always come first. Three steps, configured per node
in `config.yield` (all fields optional; defaults in brackets):

1. **Lowest priority, always** (`low_priority` [true]): `llama-server` and `llama-bench` start at
   the lowest process priority — Windows `IDLE_PRIORITY_CLASS`, Linux nice 19 on every thread plus
   the idle I/O class (`ionice -c 3`). It applies to processes started afterwards.
2. **Freeze / thaw** (`enabled` [on for a node without a dedicated GPU, off with one]): every
   `poll_seconds` [5] the node looks at three signals; any of them freezes the model processes
   (Linux `SIGSTOP`, Windows `NtSuspendProcess`), none of them continues them at the same place
   (`SIGCONT` / `NtResumeProcess`):
   - **input**: keyboard/mouse input younger than `idle_seconds` [60]; freezes at once;
   - **cpu_load**: other programs (everything but the agent and its model servers) use more than
     `cpu_percent` [60, 0 = off] of the CPU for `sustain_seconds` [5]; ends after `resume_seconds`
     [30] below the threshold. While the node loads a model or benchmarks itself the signal stands
     still (its own CPU use is not foreign load);
   - **battery**: the machine runs on battery (`pause_on_battery` [true]); no effect without one.
   A signal that cannot be measured never counts. State `paused` (event `paused {reasons, models,
   streams}`, later `resumed {paused_seconds, reasons}`). While paused the node refuses new
   gateway requests (503 `busy`, so the hub asks another node), refuses `models.load` (`busy`) and
   reloads no resident. A request in flight is not a timeout: the node sends `proxy_status
   {stream_id, state: "paused", reasons}` (and `"running"` when it goes on) and keeps its own idle
   watchdog quiet; the hub does the same (`gateway_pause_timeout_seconds`, default 1 h, instead of
   the idle timeout) and forwards the status to the chat as an SSE event `status`.
3. **Unload** (`unload_after_pause_seconds` [900, 0 = never]): a pause that long unloads the models
   (`model_unloaded`, `reason: "pause_timeout"`). An answer in flight ends with `proxy_end` error
   `interrupted` (also for any other unload of a model mid-answer); the hub stores the partial
   answer as status `truncated` — the chat's *Continue* (D21) carries on, after the hub loaded the
   model again — and sends the SSE events `status {state: "interrupted"}` and `done`. Residents
   come back after the pause. A multi-step deep task will continue at its next step (part C).

**Seeing input** (`providers/activity`): Linux asks systemd-logind for the local active sessions'
idle hint (`loginctl`; no session = a headless server = "not known"). **Windows:** the service
account `NT SERVICE\topcoat-node` (D17) is refused by `WTSQuerySessionInformation` on the console
session ("Zugriff verweigert", seen on the real laptop), and `GetLastInputInfo` only describes the
calling process's own session. So the same binary runs as `topcoat-node input-helper` **in the
user's session** and answers the service over a local named pipe:
- Started by the scheduled task `topcoat-node-input` that `topcoat-node install` creates: trigger
  "at logon of any user", principal the group *Users* (no `LogonType`: schtasks refuses one next to a group, seen on the real laptop) — it runs only while
  someone is logged on, with that user's rights (no elevation) and **no stored password**
  (`S4U` is not used; it fails against a Samba KDC). `uninstall` ends and deletes the task.
- Pipe `\\.\pipe\topcoat-node-input-<session id>`: local clients only (`PIPE_REJECT_REMOTE_CLIENTS`,
  no network port), one instance, ACL `D:P(A;;GRGW;;;<service SID>)` — nobody else, not even
  Administrators, can connect. The service connects at the *anonymous* impersonation level and
  only trusts a pipe whose server process (`GetNamedPipeServerProcessId`) runs in the console
  session **and** is the installed `topcoat-node.exe` — the ACL keeps others out as clients, this
  keeps others out as the *server* (any user could create a pipe of that name first). Session and
  program file of that pid come from `NtQuerySystemInformation` (`SystemProcessInformation` for the
  session, `SystemProcessIdInformation` for the image path, package `internal/winproc`), the query
  of D17: the service account may **not** open the user's process (`OpenProcess`,
  `ProcessIdToSessionId` and `QueryFullProcessImageName` answered "Zugriff verweigert" in the
  hardware test) but may read the process list. The pipe's own session id
  (`GetNamedPipeServerSessionId`) is not used. A refusal names pid, session and program file.
- Wire: the request line `idle`, the answer one JSON line `{"v":1,"idle_ms":N}` (or `{"v":1,"error":…}`);
  lines are capped at 256 bytes. The helper exposes the idle time and nothing else.
- The node asks the helper first and falls back to the WTS query. If neither answers the agent
  logs why once ("no input helper answers in the console session …"), input stays "not known"
  and CPU load and battery keep working. The helper's own log is
  `%LOCALAPPDATA%\topcoat-node\input-helper.log`. UNVERIFIED on real hardware (the server check — the first
  version reported "served from session 0" for a helper running in session 1 —, RDP sessions are
  not looked at).

**Measuring the CPU load of other programs (Windows, `CPULoad`).** Every measure is blind
somewhere, so the node takes the highest of them (percent of all logical processors, the node's own
processes taken out in the same weighting):
- *time*: `GetSystemTimes` busy time. On a hybrid CPU under full load it stays below 100 % (67–68 %
  with twelve busy loops on the i7-1255U's 2 P + 8 E cores, measured) — this is why the default
  `cpu_percent` is **60**, not 70.
- *utility*: PDH `\Processor Information(_Total)\% Processor Utility`, what Task Manager shows
  (busy time weighted with the clock against the base clock; reaches 100 on hybrid CPUs).
- *hyperv*: PDH `\Hyper-V Hypervisor Logical Processor(_Total)\% Total Run Time` (where Hyper-V or
  WSL2 exists): the run time of the processors including guests, which the host's idle time does
  not see — a WSL2 guest at 100 % left the host "idle" (measured).
- *guest*: the CPU time of the VM host processes (`vmmem`, `VmmemWSL` …) as a floor.
The agent logs once which measures exist and which counters the service account could not read
("CPU load of other programs is measured with: time, utility; not available: hyperv: 0x…"). UNVERIFIED
on real hardware: which counters `NT SERVICE\topcoat-node` may read and whether it can open `vmmem`.

### Energy (E2-05 E, ARCHITECTURE D28)
A node that is only needed now and then should cost little while it waits.
- `config.power.profile`: `performance` (nothing slows down), `balanced` [default] (while
  nothing is loaded — no model, no load, no benchmark, no resident to reload, not paused — the
  pressure and yield polls run ×3 slower and the GPU is not asked at all), `saver` (×6 and the
  heartbeat ×3, at most 30 s at a heartbeat interval of 10 s). Anything that happens is reported at
  once by its own heartbeat, so nothing but the interval changes.
- **Wake-on-LAN.** The inventory lists the *physical* Ethernet and Wi-Fi adapters that can wake
  the machine (`hardware.network` `[{name, mac, kind: ethernet|wifi|other}]`, wired first, one entry
  per hardware address). Left out: bridges, VPNs, containers, Wi-Fi Direct and Hyper-V/WSL
  adapters, Bluetooth PAN, mobile broadband. Windows: `GetAdaptersAddresses` (type Ethernet or
  IEEE 802.11) joined with the driver's registry key — the device must sit on PCI, USB or ACPI
  and offer `*WakeOnMagicPacket`; an adapter whose settings cannot be read is kept. Linux: an
  interface with a `device` entry, and a wired one is dropped when `ETHTOOL_GWOL` says the
  driver cannot wake (an answer that is not given keeps it). The hub keeps them while the node is offline. `POST /api/nodes/{id}/wake` (admin)
  sends the magic packet (UDP broadcast to `wol_broadcast` [255.255.255.255], port `wol_port` [9])
  to the node's wired addresses (wireless only if there is no other) and records the event
  `wake_sent`; 409 `already_online` / `no_mac`, 502 `wake_failed`. A hub in a container needs host
  networking or the LAN's broadcast address for the packet to reach the LAN. Waking on demand by
  the gateway (a request for a model that only a sleeping node has) is not built yet.
- **Sleeping.** The action `node.sleep` (not in the default `allowed_actions`; an admin adds it)
  unloads the models and, 3 s after the result, suspends the machine (Linux `systemctl suspend`,
  Windows `SetSuspendState(hibernate=false)`), event `sleeping {reason: "action"}`. It refuses
  (`busy`) while a game runs, a model loads, a benchmark or download runs or an answer is being
  generated. `config.power.sleep_after_idle_seconds` [0 = never] lets the node do the same by
  itself after that long without any of that, without a loaded model being used (a loaded
  resident such as the house model does not keep it awake; models are unloaded before the sleep
  and residents load again after the wake), and with nobody at the machine (input younger than
  that time); event `sleeping {reason: "idle"}`. Both need
  Wake-on-LAN to come back, which is a BIOS/UEFI and network card setting (docs/INSTALL-NODE.md
  "Energy").
- **Waking on demand (plan step 1.7).** When a request finds no connected node for its model and
  the model is the **house model** of an offline node (pinned or allowed by its row), the hub
  sends the magic packet to that node's wired addresses (event `wake_sent {reason: "demand",
  model, addresses}`), lists the wait as activity `waking` and retries every second until the node
  serves or `TOPCOAT_WAKE_TIMEOUT_SECONDS` (default 240, 0 = off) ends it with `503 wake_timeout`.
  `TOPCOAT_WOL_BROADCAST` may name several addresses (comma-separated), e.g. the directed broadcast
  of the node's LAN when the hub sits in another subnet. UNVERIFIED on real hardware: `systemctl suspend` / `SetSuspendState` from the
  service account, and the magic packet reaching a sleeping machine.

### Dev nodes (E6-01 S1, ARCHITECTURE D52)
A dev node is a tool node for the dev workshop: `topcoat-node install --role dev`. The hub gives it
`roles: ["dev"]`, `runner: {"type": "none"}`, `allowed_actions: ["capability.invoke",
"capability.execute", "workshop.manage"]` and the `repo.*` and `workshop.*` capabilities. It holds clones of
registered repositories and offers only named, schema-checked operations (no shell).

- **Capabilities.** Read-only: `repo.status`, `repo.list`, `repo.read`, `repo.search`, `repo.outline`, `repo.diff`.
  `repo.outline` (plan step 1.11) is a map of the worktree: `{repo, path?, kinds?, limit?, max_symbols?, after?}`
  → `{repo, files: [{path, kind, lines, bytes, symbols: [{name, kind, start, end}], symbols_left?, skipped?}],
  total_after, next_after?, masked}`. `start`/`end` are 1-based lines for `repo.read`. Kinds: `py` (def/class by
  their indentation, module-level assignments), `ts` (TS/TSX/JS declaration lines and class methods), `go`
  (functions, `Receiver.method`, types), `json` (key paths), `md` (headings outside code fences), `css` (rule
  heads); the names follow the hub's `code_symbols.py`, without its `imports`. Files of other kinds and files
  that may hold credentials are left out; links are not followed. A page holds at most `limit` files (default
  50, at most 200) and about 40 KB; a file with more than `max_symbols` names (default 40, at most 200) keeps
  the outer ones (a class before its methods). Older nodes do not offer it: the hub then goes on without the map.
  Changing the scratch worktree: `repo.sync` (clone/fetch), `repo.start` (branch `topcoat/<topic>`),
  `repo.edit` (replace one exact text), `repo.write`, `repo.apply_patch`, `repo.reset`, and
  `repo.test` (the repository's test template in a throw-away container; offered only where
  `tools.workshop.runner` is configured). All of them take `repo`, the registered repository's name.
  Background test runs: `repo.test_start` (same template and limits as `repo.test`; an optional `path`
  runs one relative `.py` test file only, pytest template only, D108; it returns
  `{repo, job_id, state: "running", ...}` at once and the run goes on in the node), `repo.test_status`
  (`{repo, job_id, wait_seconds?}`: `state` is `running`, `passed`, `failed`, `timed_out_idle`,
  `timed_out_cap`, `canceled` or `error`, plus `elapsed_ms`, the end of the output (`log`) and, once ended,
  `passed`/`exit_code`; `wait_seconds` (at most 15) waits for the end or for that long; read-only)
  and `repo.test_cancel`. A run ends when the test ends, when it printed nothing new for
  `runner.idle_seconds` (default 180), when it hit `runner.timeout_seconds` (default 1800 for a
  background run) or when it is canceled - never because a call that asked for it timed out. Runs
  live in the node's memory (a restart ends them: the job is then unknown), at most 2 at a time and
  one per repository; an ended run stays readable for an hour.
  Leaving the workshop: `repo.push` (`read_only: false`, no `scratch`), run by `capability.execute`
  for a confirmed proposal only. Input `{repo, message, branch, diff_sha256}`: the node refuses
  unless the worktree is on `branch`, `branch` starts with `topcoat/` and is not the default branch,
  the staged diff against `HEAD` hashes to `diff_sha256` (what the person saw) and the secret scan
  finds nothing. It commits, then `git push origin HEAD:refs/heads/<branch>` — never forced. The
  answer is `{repo, pushed, branch, commit, compare_url?}`. `capability.execute` also carries the
  `repo` object of `capability.invoke` (URL, branch, token), and nothing else is added to its keys.
  `repo.diff` (without `stat`) reports `sha256`, `bytes` and `branch` of the whole change.
- **Large tasks (D109, D110).** The sub-tasks of a large task write one after another on its local
  branch `topcoat/v-<nr>`. `repo.start` takes `continue: true` for such a topic only (`v-<nr>`; for any
  other topic it is an input error): when the local branch exists it is checked out as it is, not reset,
  and the answer is `{repo, branch, based_on: branch, continued: true}`; when it does not exist yet the
  branch starts from the default branch as usual and the answer carries `continued: false`. Without the
  flag nothing changes. `repo.read` and `repo.outline` take an optional `branch` (only `topcoat/v-<nr>`,
  and it must exist in the workshop): they then read that branch from git's objects (`ls-tree`,
  `cat-file`) and check nothing out, with the checks of a worktree read (a clean relative path, no
  `.git`, no file that may hold credentials, the size limit, plain files only: no links, no
  submodules) and the same masking; the answer names the `branch`. Older nodes refuse both fields
  (`additionalProperties: false`).
- **Local repositories (D76).** The origin of a repository can live in the workshop itself, a bare
  repository at `<dir>/.origin/<name>.git` computed from the name (never a path from the hub). The
  call's `repo` object is then `{local: true, branch, template?, url: "", token: ""}`: `repo.sync`
  clones from that origin, `repo.push` pushes there, and git may use the `file` protocol for that
  call only. More capabilities:
  - `repo.branches` (read-only): `{repo}` → `{repo, default_branch, branches: [{name, commit,
    date, subject, ahead, behind, work, default}]}` of a local repository.
  - `repo.commit` (scratch): `{repo, message}` commits the worktree of a local repository on its
    branch `topcoat/<topic>` and pushes it to the local origin; never the default branch, refused
    for a remote repository (that is `repo.push`) and when the secret scan finds something.
  - The person's own steps, `manage: true`, run by the action `workshop.manage` only (never by
    `capability.invoke` or `capability.execute`, so no assistant reaches them):
    `workshop.create` `{repo, branch?, title?, description?}` (a new origin with a README as its
    first commit), `workshop.clone` `{repo}` with the remote as the call's `repo` (a full copy of
    it; the remote is not kept in the copy), `workshop.branch` `{repo, name, from?}`,
    `workshop.merge` `{repo, branch, into?, message?}` (fast-forward, else a merge commit in a
    temporary worktree; a conflict changes nothing and answers `conflicts`), `workshop.publish`
    `{repo, branch}` and `workshop.pull` `{repo, branch}` with the remote as the call's `repo`
    (never forced; a pull is a fast-forward only), `workshop.remove` `{repo, confirm: true}`.
- **Browser (E6-02, `browser.*`).** Offered only where `tools.workshop.runner.images.browser` is set and the
  runner is `docker`. A headless Chromium in a throw-away container (no network at all, read-only root
  file system, all capabilities dropped, a copy of the worktree mounted read-only) that serves the built
  GUI with a faked hub; the node talks to its helper through a mailbox directory (`in/<n>.json`,
  `out/<n>.json`), so it opens no port. All of them are `scratch` (an admin on the GUI and the agent acting
  for them, never an API key), one session per node, ended by `browser.close`, by an idle limit (5 minutes)
  or after 30 minutes. `browser.open` `{repo, mode?: built|dev, path?, theme?}` (a theme document is offered by the faked hub
  and chosen, the window desktop is drawn in it; a worktree without `web/dist` is then shown with the GUI
  the image was built with), `browser.viewport` `{width, height,
  dpr?, orientation?, scheme?, reduced_motion?}`, `browser.snapshot` `{scope?, layout?, max_nodes?}` (a
  compact tree with refs like `e12`), `browser.act` `{ref?, action: click|type|hover|scroll|key, value?}`,
  `browser.audit` `{checks?, matrix?}` (violations only, with selector and numbers), `browser.animations`
  `{trigger?, sample_ms?, properties?}`, `browser.console` `{since?}`, `browser.shot` `{region?, scale?,
  filmstrip?}` (a small picture; the hub offers it only to a model that can read images), `browser.close`.
  An answer is at most 48 KiB; a larger one is refused with the advice to ask for less. The model never
  sends JavaScript.
- **`manage`.** `inventory.capabilities[].manage: true` marks such a step: never `read_only`,
  never `scratch`; the hub never offers it to an assistant and runs it for the signed-in admin on
  its own page only.
- **`scratch`.** `inventory.capabilities[].scratch: true` marks a capability that is not
  `read_only` but changes only the node's own scratch area (the worktrees). `capability.invoke`
  runs it without a confirmation; the hub offers it to an admin on the GUI (and the agent acting
  for them) only, never to an API key or another user. A capability that is neither read-only nor
  scratch still needs `capability.execute` and a confirmed proposal (D15). `scratch` and
  `read_only` are never both true.
- **The repository comes with the call.** `capability.invoke` of a `repo.*` capability may carry
  a fourth key, `repo: {url, branch, template?, token?}`, put there by the hub from its
  `dev_repos` row. The node validates it (https only, no credentials in the URL, host on
  `tools.workshop.allowed_hosts`, plain branch name), uses the token for that one git process
  only (as a header scoped to the repository's host, through the process environment, never in
  arguments), and keeps neither: no log line, error or output holds it. Any other capability that
  receives `repo` refuses. The hub's audit row holds the input (without `repo`) only.
- **`config.tools.workshop`**: `dir` (default `<data dir>/workshop`), `allowed_hosts` (default
  `github.com`), `max_repo_mb` (default 500) and `runner`: `{type: none|docker, docker_host,
  host_dir, images: {template: image}, network, network_templates, memory_mb, cpu_milli,
  timeout_seconds}`. Test templates are `make-check`, `npm-test`, `go-test`, `pytest`; the command
  of each is fixed in the node, a repository chooses a name. A run gets a copy of the worktree
  (without `.git`, links or credential-looking files), no network unless the template is listed in
  `network_templates`, a read-only root file system, no capabilities, an unprivileged user, and is
  removed afterwards. UNVERIFIED against a real Docker daemon.
- **Files.** `.git`, credential-looking files (`.env`, `*.pem`, `*.key`, `id_rsa*`, `.npmrc` …) and
  symbolic links are never read, listed, searched, copied or written; pipeline files
  (`.github/workflows`) are never changed. Output is masked like every tool output.

### Tool nodes (E2-04, ARCHITECTURE D29)
A server can be a node without models: `topcoat-node install --role tools`. The hub gives it
`roles: ["tools"]`, `runner: {"type": "none"}` (no llama.cpp download, no GPU work, the node is
`available` as soon as it is connected), `allowed_actions: ["capability.invoke"]`, `yield` and
`pressure` off and `capabilities` = every read-only capability.

- **Offered vs. switched on.** The inventory lists what this node *build* offers:
  `capabilities: [{name, description, read_only, input_schema}]` (`input_schema` is a JSON Schema
  with `additionalProperties: false`). `config.capabilities` says which are switched on. A
  capability with `read_only: false` is listed but never run by `capability.invoke` (D15).
- **`config.tools.read_roots`** (HOST-01, D89; additive, protocol version unchanged): a list of at most 50
  absolute folders per node, assigned by the hub, that `files.list` and `files.read` may look into. **Empty
  or missing = none: the two capabilities are not offered at all.** A root is a drive-letter path
  (`C:\…`) or a Unix path; never a filesystem root, a UNC path, a device path (`\\?\`, `\\.\`), a path
  with `.`/`..` segments or a stream (`:`). Nothing is on until a person saves roots on the node's page.
- **`config.tools`** (optional): `docker_host` (`unix:///path` or `tcp://127.0.0.1:port`, default
  `unix:///var/run/docker.sock`; any other address is refused), `compose_roots` (the only
  directories compose/.env files are read from or listed; **empty or missing = none: default
  deny** — `compose.file` is refused with "no directories are released …", `compose.projects`
  still shows names, services and state but omits `working_dir`/`config_files` of every project
  outside the roots and says `files_released: false`), `connectors_file` (an absolute path; only
  the path — the credentials are in the file, see "Connectors" below), `timeout_seconds` (1–300,
  default 20); for confirmed restarts (E3-01b): `docker_write_host` (a **second** proxy (`topcoat-node docker-restart-proxy`) that
  allows only `POST /containers/{id}/restart`, same address rules as `docker_host`),
  `restart_containers` and `restart_units` (glob lists of what may be restarted; **empty or missing =
  nothing: default deny** — without them `docker.restart` / `service.restart` are not offered).
- **`capability.invoke {capability, input}`** → `result: {capability, output, bytes, duration_ms,
  at}` or an error: `not_allowed` (not switched on, not offered, not read-only, or the action is not
  allowed), `invalid_args` (input refused), `timeout`, `busy` (4 calls already run),
  `capability_failed` (it ran and failed: Docker unreachable, unknown unit …), `canceled`. The node
  runs at most 4 calls at once, each with a time limit, and logs every call (capability, input,
  duration, size — never the output).
- **The capabilities** (all read-only, none takes a command; the input is decoded strictly and
  unknown fields are refused):

| name | input | returns |
|---|---|---|
| `system.info` | – | host name, OS, kernel, uptime, load, CPUs, RAM and swap (`/proc`) |
| `system.memory` | `top?` (1–30, default 10) | what the RAM is spent on: `memory_mb` (programs, cache, shared/tmpfs, kernel, swap), `zfs_arc_mb`, `top_processes` (pid, name, RSS, swap, `vm` for a QEMU process, `container` id), `virtual_machines_mb`, `containers_mb`; never a command line. A container node sees the host's processes with the host's `/proc` mounted read-only and `TOPCOAT_HOST_PROC` set, else it says so in `note` |
| `disk.usage` | – | mounted data file systems with size, used, available, inodes; fullest first |
| `docker.ps` | `all?` (default true), `name?`, `detail?` (default false) | **compact by default** (21 containers were 7 KB in a 16K context): per container name, state, health, `restart_count`, `started_at` (plus `exit_code`/`oom_killed` for one that is not running) and `counts` by state, `compact: true`; `detail: true` adds image, id, status text, ports, compose project/service, finish time |
| `docker.inspect` | `container` | state, exit code, OOM kill, restart policy/count, health checks with their last outputs, mounts, networks — **no environment variables** |
| `docker.logs` | `container`, `lines?` (1–500, default 100), `since?`, `until?` (a duration back from now such as `15m`/`2h`/`3d`, or RFC 3339) | `log` (stdout and stderr with timestamps, newest last), `lines`, `truncated`, `masked` |
| `compose.logs` | `project`, `service?`, `contains?` (case-insensitive text a line must hold), `lines?` (1–200 **per container**, default 50), `since?`, `until?` | the logs of **every container of one compose project merged by time**, `[service]` in front of each line, newest last; `containers`, `lines`, `truncated`, `masked`, `unreadable` (a container whose log could not be read does not fail the stack); at most 20 containers (`stack_truncated`) |
| `compose.projects` | – | compose projects from the container labels: services and their state; `working_dir`/`config_files` only for projects inside `compose_roots` (`files_released`) |
| `compose.file` | `project`, `file?` | one compose or `.env` file of a project, secrets masked; `available` lists the others |
| `journal.logs` | `unit`, `lines?`, `since?`, `until?`, `priority?` (0–7) | journal lines of one unit (`journalctl -u`) |
| `service.status` | `unit` | `systemctl show` of one unit: load/active/sub state, result, main PID, exit status, restarts, memory, since |
| `service.failed` | – | units in the failed state |
| `shell.read` | `command` (≤ 4096 bytes), `timeout_seconds?` (1–60, default 15) | **only where `config.tools.shell_socket` is set and an admin added `shell.read` to the node's capabilities** (default off): one bash command line run by an OS user that can only read (no sudo, no docker group, nothing writable, no network); `output` (stdout+stderr merged, secrets masked, cut at 64 KiB), `exit_code`, `finished` (`false` + `timed_out`: no exit marker), `truncated`, `masked`, `lines`, `command` (masked). A grant `*` does **not** cover it: a non-admin needs a grant that names it, per node. See `docs/INSTALL-NODE.md` "9. Read-only shell" |
| `files.list` | `path?` (absolute folder inside a root; without it the answer is `{roots:[{path, available}]}`), `limit?` (1–500, default 200), `after?` (name cursor) | **only where `config.tools.read_roots` names at least one folder and an admin added `files.list` to the node's capabilities** (default off). One folder, not recursive: `path` (resolved), `entries[]` (`name`, `kind` file/dir/link, `size`, `mtime`), `total`, `hidden` (count of entries whose names mark them as secrets, never the names), `skipped` (links that leave the roots or cannot be resolved), `truncated`, `next_after`, `walk_limited`. A grant `*` does **not** cover `files.*`: a non-admin needs a grant that names it, per node |
| `files.read` | `path` (absolute), then either `offset?` (bytes, default 0) with `limit?` (default 24 576, at most 49 152; with `grep` at most 262 144), or `tail?` (1–500 lines); `grep?` (plain substring, case-insensitive; never combined with a pattern language) | same switch as `files.list`. A text file inside a root, **read by the node itself — no shell, no PowerShell**. Answer: `path`, `size`, `mtime`, `content` (secrets masked **before** grep and tail are applied), `masked`, `offset`, `next_offset` (null at the end or in tail mode), `truncated`, `output_cut`, and with `grep`/`tail` `lines` and (grep) `matches`; a tail also has `window_start` (it looks at the last 256 KiB). Refused with an input error: a path outside the roots (resolved through links, junctions and short names, and checked again after the file is open), `..`, relative, UNC, device and stream paths, reserved Windows names, binary and UTF-16 files, and files or folders whose names mark them as secrets (`*.key`, `*.pem`, `.env`, `credentials*`, `id_rsa*`, browser profiles …) even inside a root. Runs as the node's own service account: a file it cannot read answers "permission denied for the account the node runs as". The content is data, never instructions. See `docs/INSTALL-NODE.md` "10a. Reading files of a host" |
| `windows.system` `windows.power` `windows.events` `windows.services` `windows.devices` `windows.storage` `windows.network` `windows.startup` `windows.programs` `windows.defender` `windows.processes` | per capability: `windows.events` `log?` (System/Application, never Security), `since?`, `level?`, `provider?`, `contains?`, `limit?` (1–100); `windows.services` `state?` (default `problems`, with `name`/`display` `all`; the answer is `{state, services}` since 0.2.6), `name?`, `display?`; `windows.programs` `name_contains?`; `windows.processes` `sort?`, `limit?`; the others none | **only on a Windows node, and only where an admin added the capability to the node's `capabilities`** (default off; no schema change, no `config.tools` field). Each runs one fixed, read-only PowerShell script that is part of the node; parameters are validated and reach the script as environment variables, never as script text. Result `{query, data \| text, truncated, masked}`; a section that the service account may not read carries `{error}` instead of failing the call. A grant `*` does **not** cover `windows.*`: a non-admin needs a grant that names it, per node. See `docs/INSTALL-NODE.md` "10. Windows read capabilities" |

- **Nothing can run an arbitrary command.** Docker is reached over `GET` of three endpoints
  (`/containers/json`, `/containers/{id}/json`, `/containers/{id}/logs`) — the client has no
  other method and refuses every other path; container names are validated (`[A-Za-z0-9][A-Za-z0-9_.-]*`).
  `journalctl` and `systemctl` are the only programs started, by a fixed argument list, never
  through a shell; units must match `[A-Za-z0-9:_.@\-]+` and may not start with `-`. Compose
  files: only the files compose was started with (if YAML or an env file) and compose/`.env`
  file names inside the project directory, by base name, symlinks resolved and kept inside it.
- **Limits.** Output ≤ 64 KiB per call (a log keeps its newest part, a file its start, both say
  `truncated`), files ≤ 256 KiB, ≤ 500 log lines, ≤ 200 containers.
- **Masking on the node.** Everything that leaves the machine passes `Mask`, in two stages.
  (1) By shape, anywhere: private keys, provider tokens (`ghp_`, `AKIA`, `sk-` …), JWTs,
  `Bearer`/`Basic` credentials, `user:password@` in URLs (the password may contain `/`),
  `Password=…` in connection strings, `--password …` on command lines. (2) By key, **whatever the
  value looks like** (`1`, `true`, a phrase with spaces): the value of every key whose name says
  "secret" is replaced — at the start of a line (`.env`, compose `environment:`, YAML) and
  **anywhere inside a line** (`login password=x api_key=y token=z`, URL queries, JSON — also
  JSON inside a log field — and `Authorization: <scheme> <credential>`, where the scheme stays
  readable). Key names are split into words (`_`, `-`, `.`, camelCase): `pass`, `pw`, `pwd`,
  `auth`, `authorization`, `token`, `salt`, `jwt`, `dsn`, `psk` count as whole words; `password`,
  `passwd`, `passphrase`, `secret`, `credential`, `private_key`, `api_key`, `access_key`,
  `signing_key`, `encryption_key`, `license_key`, `bearer`, `cookie`, `webhook`,
  `connection_string`, `access_token`, `refresh_token`, `auth_token` … count inside longer names
  (`OPENVPN_PASSWORD`, `WIREGUARD_PRIVATE_KEY`, `clientSecret`) — but `passed`, `author`,
  `n_tokens` do not. Only an empty value, `null`, a `${VARIABLE}` / `$VARIABLE` reference or a
  YAML block indicator is left. A replaced value reads `«masked:<rule>»`; the output says how
  many (`masked`). The shape rules mirror the hub's secret filter (D19) except the "long token"
  rule, which would hide every container ID in a log. A list kept in step by hand.
- **`service.status` of a unit that does not exist** is an `invalid_args` error ("unit … not
  found", from `LoadState=not-found`), not an `inactive` unit.

**Times in log capabilities.** `since`/`until` of `docker.logs` and `journal.logs` accept a duration
back from now (`15m`, `2h`, `1h30m`, `3d`), the same in words (`1 hour ago`, `a day ago`), `now`,
`today`, `yesterday` (midnight UTC), a date or date and time (`2026-09-30`, `2026-09-30 07:30[:00]`,
UTC unless a zone is named) and RFC 3339.

**Hub (REST, D20).** `GET /api/nodes/{id}/capabilities` → `{node, offered, enabled, usable,
grants}` (`usable`: what the caller may run now); `POST /api/nodes/{id}/capabilities/{name}`
`{input}` → `{ok, call_id, capability, output, bytes, duration_ms, at}` or, for a capability that
ran and failed, `{ok: false, error}`; refusals are HTTP errors with `detail.code`:
`not_granted` 403, `not_allowed` 403, `capability_disabled` 403, `needs_confirmation` 403,
`unknown_capability` 409, `invalid_input` 422, `node_offline` 409, `timeout` 504.
Checks, in this order: grant → the node allows `capability.invoke` → switched on in
`config.capabilities` → offered **and read-only** (from the last inventory) → input fits
`input_schema`; the node checks its own side again.
- **Who may.** The admin on the GUI (dev token) may run every read-only capability; everybody else
  needs a grant per node and capability (`*` = all read-only ones). An API key is always a plain
  user — the key of an admin included. A caller without a grant learns nothing about the node:
  `GET …/capabilities` answers 404.
- **Grants** (admin only): `GET /api/nodes/{id}/grants`, `PUT /api/nodes/{id}/grants/{user_id}`
  `{capabilities: [...]}` (replaces; `[]` removes; event `grants_changed {user_id, capabilities,
  by}`), `GET /api/users`.
- **Audit.** `GET /api/tool-calls?node_id=&limit=` (a user sees their own rows, an admin all).
  Every call that reached the capability layer is a row in `tool_calls` — refused ones too (user,
  node, capability, input, `via` gui|api|agent, ok, code, error, output size, duration) plus an
  excerpt of the output cut to `tool_audit_output_bytes` (default 16 KiB, 0 = none) which is
  replaced by a note if the hub's secret filter flags it. The caller always receives the whole
  output; the audit keeps a cut.
- Settings: `TOPCOAT_TOOL_TIMEOUT_SECONDS` (45), `TOPCOAT_TOOL_AUDIT_OUTPUT_BYTES` (16384).

### Connectors: Uptime Kuma and Omada (E2-04c, ARCHITECTURE D32)
Read-only capabilities that ask **another system on the network** for a tool node. Their
credentials live in **one file on the tool node**, created by the maintainer (mode 600, owned by
the node's user, not a link; on Windows the ACL is the maintainer's to set — UNVERIFIED there),
default `/etc/topcoat-node/connectors.json` (`%ProgramData%\topcoat-node\connectors.json`), or the
path in `config.tools.connectors_file`. **Nothing about a credential passes the hub**: not the
configuration, not an input (a capability takes filters, never a URL or a key — a model cannot aim
a connector elsewhere or ask it for a secret), not an output (every string is scrubbed of the
file's secrets and masked like a log), not the audit. A file that others can read, a link, or a file
of another user is refused (the node logs why and offers no connector capability). Redirects are
never followed (they would carry the credentials along), answers are capped at 4 MiB, errors never
contain the request.

```json
{
  "uptime_kuma": {"url": "http://10.0.0.x:3001", "api_key": "uk1_…"},
  "omada": {"url": "https://10.0.0.x:8043", "omada_id": "…", "client_id": "…",
            "client_secret": "…", "site": "Default", "ca_file": "/etc/topcoat-node/omada-ca.pem"}
}
```
`uptime_kuma.api_key` is **optional**: Kuma without its own login serves `/metrics` openly (a proxy
releases it for the tool node only); without a key no `Authorization` header is sent, with a key it
is the password of HTTP basic auth. `ca_file` (preferred for a controller with its own certificate) or `insecure_skip_verify` (an explicit
decision) per section; the default is the system's trust store. A missing section means its
capabilities are **not offered at all** (they would only fail). The file is read when the node
starts or its config changes: restart the node after editing it. The new names are in the default
`config.capabilities` of new tool nodes; on an existing tool node switch them on in its page.

| capability | input | output |
|---|---|---|
| `kuma.monitors` | `status?` (all/down/up/pending/maintenance), `name_contains?`, `cert_days_below?`, `limit?` | `monitors[]` (name, type, status, url without user/query/push token, hostname, port, `response_ms`, `cert_days_remaining`, `cert_valid`), `counts`, `total`, `problems`; problems first |
| `omada.sites` | `limit?` | the controller's sites |
| `omada.devices` / `omada.clients` | `site?`, `limit?`, `match?` | `site`, `total`, `shown`, `items[]`: the controller's own simple fields (strings, numbers, booleans; nested data and secret-looking names dropped); clients keep a short list (name, hostName, mac, ip, ssid, apName, rssi, snr, signalLevel, rates, uptime, lastSeen …); ms time stamps become RFC 3339 |
| `omada.events` / `omada.audit_logs` / `omada.alerts` | `site?`, `limit?`, `match?`, `since?` (default 1d), `until?` | as above plus `since`, `until`; the window goes to the controller as `filters.timeStart/timeEnd` |
| `match` (every list) | text, alternatives with `\|`, any case; a MAC with colons is also looked for with dashes | the list is read page by page (at most 3000 rows); only rows with a string field containing the text: `matched`, `scanned`, a `note` when nothing matched |
| `omada.firmware` | `site?`, `device_mac` | the latest firmware the controller knows for that device |

Uptime Kuma: `GET /metrics`, HTTP basic auth with any user name and the API key as the password,
Prometheus text (`monitor_status` 1 up / 0 down / 2 pending / 3 maintenance, `monitor_response_time`
ms, `monitor_cert_days_remaining`, `monitor_cert_is_valid`) — names and codes taken from Uptime Kuma's
source and wiki. Omada Open API (controller 5.12+, an application with **client credentials and the
role Viewer**): `POST /openapi/authorize/token?grant_type=client_credentials` with `{omadacId,
client_id, client_secret}`, then `Authorization: AccessToken=<token>` (re-requested when it expires or the
controller answers -44112/-44113) on `GET /openapi/v1/{omadacId}/sites`, `…/sites/{siteId}/devices`,
`…/clients`, `…/logs/events`, `…/logs/alerts`, `…/audit-logs`, `…/devices/{mac}/latest-firmware-info`.
The site is always taken from the controller's own list (a model's text never becomes part of a path).
The logs need `filters.timeStart` and `filters.timeEnd` (ms): without them the controller answers HTTP 400
with a framework body (`{"status":400,"error":"Bad Request"}`, no `errorCode`), which the node passes on.
Checked against a real controller on 2026-11-17: sites, devices, clients, logs/events, logs/alerts
and audit-logs (firmware not yet). `omada.events` adds `window`: counts over the whole window by event key and of client events by the device they ran on (ap/switch/gateway), with a note when no client event ran on an AP. `scripts/dev/connector_smoke.py` calls every connector capability once.

#### Keycloak and the identity view (E3-12, ARCHITECTURE D47)
Connectors section `"keycloak": {"url": "https://10.0.0.x:8443", "realm": "home", "client_id": "…",
"client_secret": "…"}` (plus `ca_file` / `insecure_skip_verify`). The client is **confidential with a
service account** that holds only read roles of the `realm-management` client (`view-users`,
`query-users`, `query-groups`, `view-clients`, `query-clients`). Without the section the two
capabilities are not offered.

| capability | input | output |
|---|---|---|
| `keycloak.snapshot` | none | `realm`, `taken_at` and the sections `users`, `groups`, `apps`, each `{ok, error?, truncated?, total, items[]}` on its own: users (id, user name, display name, enabled, `source` directory/local; **no e-mail, attributes or credentials**), groups with `path`, `members`, `client_roles` and `realm_roles`, clients (id, name, enabled, `builtin`, roles) |
| `keycloak.user` | `user` (exact user name) | the person's groups and the client roles Keycloak itself computes as effective (direct, group and composite) |

Keycloak Admin REST (`POST /realms/{realm}/protocol/openid-connect/token` with
`client_credentials`, then `GET /admin/realms/{realm}/users`, `/groups` (+ `/{id}/children`,
`/{id}/members`, `/{id}/role-mappings`), `/clients` (+ `/{id}/roles`), `/users/{id}/groups` and
`/users/{id}/role-mappings/clients/{cid}/composite`) — taken from the Admin REST documentation, not
run against a real Keycloak. Reads are GET only; the token is re-requested on a 401. Caps: 300 users,
150 groups, 300 members per group, 100 clients; a cut list says so.

Hub API (read-only): `GET /api/identity[?refresh=true]` (one entry per source node the caller may run
`keycloak.snapshot` on: `people`, `groups`, `apps`, the chain between them, `age_seconds`, `notes`
with what the look cannot see; a failed source is `{node, error}`), `GET /api/identity/explain?person=&app=`,
`PUT /api/identity/app-groups` (admin; the hub's own note which groups an app reads by itself,
`{app_groups: {client_id: ["/group"]}}`; it changes nothing in Keycloak). The look is kept in the hub's
memory for `TOPCOAT_IDENTITY_CACHE_SECONDS` (60) and never stored. The chat tool
`identity.query {person?, app?, group?, query?}` reads the same view (tool group `identity`).
**Limits, always stated:** composite roles are not expanded in the snapshot, direct user role
mappings are not read, and an app that decides by a group claim appears only where the admin noted
which groups it reads ("configured by hand"). `keycloak.user` is the authoritative single answer.

### Web capabilities (E3-04, ARCHITECTURE D35)
Two read-only capabilities of a tool node, offered only where configured (`config.tools`):
**`web.search`** (`searxng_url` set) `{query (1–200 chars), limit 1–10 (5), time_range?, language?}`
→ `{query, count, results[{title, url, snippet (≤300), engine}], note}` from the maintainer's
SearXNG (`/search?format=json&safesearch=1`; no redirects; ≤2 MiB; hits without an http(s) URL are
dropped, user names and fragments removed from URLs), and **`web.fetch`** (`web_fetch: true`)
`{url, max_chars, format text|html}` → `{url (after redirects), status, content_type, title, text,
chars, truncated, note}`; `max_chars` 500–20000 (8000) for text, up to 600000 (300000) for `format: html`,
which returns the markup as it came for a parser of the hub (Sheets reads chord pages that way; the agent
loop refuses it to a model, `web_access.WebBudget`): http/https, ports 80/443/8080/8443, no `user:pass@`, no private, loopback,
link-local, CGNAT or reserved address (also IPv4-mapped, NAT64, 6to4; checked on every lookup answer
and at connect time, redirects too, ≤3), text types only, ≤1 MiB read. Output strings are masked
like logs. `config.tools.searxng_url` (http(s) URL, no credentials) and `config.tools.web_fetch`
(bool) are in `node_config.schema.json`. `web.fetch` also returns `images[{url, alt}]`: up to 8
pictures of the page that have a description (alt text of 4+ characters), absolute http(s) URLs.

Pictures for a report (2026-11-13), with the same guards: **`web.images`** (with `searxng_url`)
`{query, limit 1–10 (6), language?}` → `{query, count, results[{title, image, thumbnail, page, size,
engine}], note}` (SearXNG `categories=images`), and **`web.image`** (with `web_fetch`) `{url}` →
`{url, content_type, bytes, data (base64), note}`: PNG, JPEG, GIF or WebP proven by the first bytes
(SVG refused), ≤180 KiB so one answer stays under the output limit; the bytes are not masked, the
URL is. The hub never offers `web.image` to a model: only `research.report.image` calls it and keeps
the picture (`report_images`, served from `/api/reports/images/{id}` to its person only).

**What leaves the network is checked twice.** The hub's `ToolService.invoke` refuses a query or URL
with private data for **every** caller (agent, chat check, `POST /api/nodes/{id}/capabilities/…`) after
the grant checks and before the node is asked; the refusal is an audit row (`ok=false`,
`code=private_data`, input replaced by `{"withheld": …}`: the audit keeps no secret). The node checks
again (defense in depth, e.g. for a call that reaches it another way): an input that holds an IP
address (v4/v6), a MAC or e-mail address, a name of the operator — this machine's host name and
domain, the search domains of its resolver, `config.tools.private_names` — an internal-looking
domain (`.lan`, `.local`, `.internal`, `.home.arpa`, `.localdomain`, `.intranet`, `.corp`, `.home`,
`.private`) or a secret (the log masking rules) is refused **before anything is sent** with the
error code **`private_data`** (`common.schema.json`; the message names the kinds, never the value;
the node's log does not hold the input either). The hub keeps no input of a `private_data` result in
the audit row or the task step.

**The web budget (hub, per run).** At most `TOPCOAT_AGENT_MAX_WEB_SEARCHES` (3) searches and
`TOPCOAT_AGENT_MAX_WEB_FETCHES` (2) pages per question, and none once a fetched page with ≥ 500
characters of text exists (a usable source): further web calls are answered with `web_limit: …`
without being made (task step with `tool.code = "web_limit"`, no audit row, not counted).

Hub: the evidence of both has tag **`web`** (`agent_evidence.tag`; findings may be tagged `web`
and, like `live`/`doc`, keep the tag only with a cited evidence of that kind); a `web.fetch`
result's final URL is `source.url` of its evidence and shown with it. **Before** the call, the hub
checks what leaves the network (the query, the URL) with the redaction layer's detection (secrets,
IP/MAC/e-mail, node and user names, private and own domains): a hit is refused with the error code
`private_data` (naming the kinds, never the values, and the refused input is not stored) and the
node is never asked.
### Collecting capabilities (E3-04)
Four read-only capabilities of a tool node that replace several calls; none takes a command, a
program or a host from the caller (every schema is `additionalProperties: false`).
- **`health.check`** `{since (30m, 2h … default 1h), disk_warn_percent 50–99 (80), error_lines 1–5 (2)}`
  → `{ok, problems[] (one line each), host{load, memory_mb, swap_mb}, disks[] (over the threshold and
  the fullest), failed_units[], containers{total, counts, attention[compact rows]},
  errors[{source, count, at_least?, top[{count, message}] (the 3 most frequent distinct messages, numbers,
  addresses and ids ignored), last[]}] (top 15; containers that run and the journal's error-priority
  lines grouped by program; `at_least` when the journal read — 500 lines — hit its limit, so the count
  is a lower bound), unavailable[{section, error}], masked}`. A part that cannot be read is
  `unavailable`, never an error of the call.
- **`logs.search`** `{pattern (≤100), regex (RE2, default plain text), since, until, where
  all|containers|journal, container, unit, max_matches ≤200 (50)}` → `{matches[{source, line}]
  (newest last), counts, total_matches, truncated, scanned{containers, container_lines,
  journal_lines}, unavailable[]}`; at most 2000 lines per container and 3000 journal lines are read.
- **`certs.expiry`** `{warn_days 1–365 (21), target?}` over `config.tools.cert_targets`
  (`host` or `host:port`, default 443) → `{certificates[{target, subject, issuer, not_after,
  days_remaining, verify, names, expiring | error}], problems[], ok}`; offered only with targets.
- **`net.check`** `{target?}` over `config.tools.net_targets[{name, host, ports[], ping}]` and
  `config.tools.dns_names` → `{targets[{name, host, ping{sent, received, loss_percent, avg_ms} |
  ping{checked:false, ok:null, error}, ports[{port, open, ms}]}], dns[{name, ok, addresses|error, ms}],
  problems[], not_checked[], ok}`; offered only with targets. Ping is **three ICMP echo requests over an
  unprivileged ICMP datagram socket** (Linux; no program is started, the node's fixed program list does
  not contain `ping`): the account needs its group in `net.ipv4.ping_group_range`. A ping that cannot
  be sent (no right, or a system without the socket) is **not** a problem of the host: the row says
  `checked:false` and `not_checked[]` names it, `problems` stays free of it.
Hosts in the config are checked as host names or IP addresses (`node_config.schema.json`:
`net_targets`, `dns_names`, `cert_targets`, at most 50 each). All strings in the outputs are masked
like logs. The agent prompts tell the model to start with `health.check`.

### The inventory (E3-10, ARCHITECTURE D45)
One more read-only capability of a tool node, switched on by default (existing nodes get it with
migration 0029). It takes no input (`additionalProperties: false`) and starts only
`systemctl list-units` and `systemctl list-unit-files` (fixed arguments) next to the Docker API
`GET`s of `docker.ps`.
- **`inventory.snapshot`** `{}` → `{taken_at, host, containers, services}`. Each section is
  `{ok: true, data, total?, truncated?}` or `{ok: false, code: "not_configured"|"error", error}`:
  - `host.data`: `{hostname, os, kernel, arch, cpus, uptime_seconds, memory_total_mb}` (Linux);
  - `containers.data[]`: `{name, image, state, health?, restart_count, started_at?, compose_project?,
    compose_service?, ports?[]}` — at most 200, `truncated` says when more exist;
  - `services.data[]`: `{unit, active, sub, enabled?, description?}` — the services that are active,
    failed or enabled (idle static units are left out), at most 400.
  `not_configured` means the node has no such source (no Docker configured, not Linux): not a fault.
  A section that failed is **not** an empty one; the hub keeps what it knew (D45).
Hub API (read-only): `GET /api/inventory`, `GET /api/inventory/nodes/{id}`,
`POST /api/inventory/nodes/{id}/refresh` (reads the node now; 409 `node_offline`; at most one read per
5 s per node). Every node entry has `online`, `status` (`ok|partial|error|unavailable|never`),
`collected_at`, `age_seconds`, `stale`, `current`, per-section `state` (`ok|n/a|failed`) with its own
`read_at`, and the rows (`containers`, `services`, derived `compose`) with `state`, `since`, `gone`.
The chat's tool `inventory.list {query?, kind?, node?, state?, include_gone?, limit?}` reads the same
store and never asks a node; its answer lists `caveats` for nodes that are offline, stale or not read.

### Signed updates (E2-06, ARCHITECTURE D50)
Only the maintainer's signing key makes a build installable; the hub distributes, the node and the
updater verify. All of this is additive in v1.

**Manifest and signature.** `manifest.json` (signed exactly as written, read strictly: no unknown or
duplicate fields) and `manifest.sig`:

```json
{"format":1,"product":"topcoat-node","channel":"stable","version":"0.2.0","os":"linux","arch":"amd64",
 "size":12345678,"sha256":"<64 hex>","created":"2026-10-07T20:00:00Z"}
```
`manifest.sig` is one line `topcoat-sig-v1 <key id> <base64 ed25519 signature>`. The key id is the first
16 hex digits of the SHA-256 of the public key; the signed message is `topcoat-update-v1\n` followed by
the manifest bytes. A node build carries at most two public keys (`-ldflags`); one without keys is
not update-capable. Versions are `MAJOR.MINOR.PATCH` with an optional `-pre`; a pre-release is older
than its release, a development build counts as `0.0.0`. The program is at most 256 MiB.

**Config.** `config.update.channel` (`^[a-z][a-z0-9-]{0,31}$`, default `stable`; the maintainer uses
`stable` and `test`). New nodes get `node.update` in `allowed_actions`; existing ones need it added.

**Inventory.** Optional `update`: `{"capable": bool, "updater": "ok"|"stale"|"missing", "channel": …,
"key_ids": […]}`. `updater` is the age of the privileged updater's heartbeat (`ok` below 60 s).
Optional `updater_version` (since node 0.2.6): the version the running updater reports in its heartbeat;
the hub shows an updater older than the node, because such an updater may not apply a new build.

**Events** (`event` messages): `update_staging` `{channel, version}`, `update_staged` `{version, channel}`,
`update_applied` `{version}` (sent by the *new* node after the restart), `update_failed`
`{version, reason}` (a refused bundle, or a rolled-back update reported by the restarted old node).

**Action `node.update`.** Refused with `not_implemented` when the build has no key or the updater
is not running (`ok` heartbeat), `busy` while a staging runs, `invalid_args` for a bad `version`. The
node downloads in the background and stages `<data dir>/update/staging/{manifest.json, manifest.sig,
topcoat-node}` followed by `update/ready`; the updater does the rest (see D50). The node confirms
the new version in `update/confirmed.json` once the hub accepted its session. The updater's
heartbeat, marker and result are in its own directory (`--update-state-dir`, readable by the node).

**Node download endpoints** (HTTPS, same authentication as the WebSocket: a nonce from
`POST /api/node/challenge`, `Authorization: Topcoat-Ed25519 <node_id>.<nonce>.<signature>`, one nonce per
request). The hub picks channel (from the node's config), `os` and `arch` (from the node):
- `GET /api/node/update/manifest[?version=V]` → `{"manifest": "<text>", "signature": "<line>"}`
- `GET /api/node/update/binary[?version=V]` → the program (`application/octet-stream`)
- `404 {"detail": {"code": "no_release"}}` when nothing is published for that channel and platform.

**Admin endpoints** (the dev admin token only; an API key gets `403`):
- `GET /api/updates` → `{enabled, signature_check, key_ids, releases: [{channel, os, arch, version, size, sha256, created}]}`
- `PUT /api/updates/{channel}/{version}/{os}-{arch}/manifest` with body `{"manifest": "<text>", "signature": "<line>"}`
  (`204`; `400 bad_manifest|bad_signature|mismatch`, `409 exists`); the path must agree with the manifest
- `PUT /api/updates/{channel}/{version}/{os}-{arch}/binary` with the raw program and a `Content-Length`
  (`204`; `409 no_manifest`, `400 size_mismatch|hash_mismatch`, `413`)
- `DELETE /api/updates/{channel}/{version}/{os}-{arch}` (`204`, `404`)

The hub keeps the newest two releases per channel and platform. Without `TOPCOAT_UPDATE_DIR` the
channel is off; with `TOPCOAT_UPDATE_PUBKEYS` the hub also checks the signature at upload.
The GUI node summary has an `update` object: `{channel, capable, updater, key_ids, available, newer,
action_allowed}`.

### Direct model transfer (E2-07, ARCHITECTURE D51)
A model file moves from one node to another without passing the hub. The hub coordinates and never
carries the data. All of this is additive in v1.

**Flow.**
1. `POST /api/model-transfers` (admin) checks both nodes (exist, online, the action in
   `allowed_actions`, the source has the model, the target does not, the target's `disk_free_mb`
   covers size + 1 GiB when known, no other transfer on either node), stores a row (`pending`), makes a
   32-byte URL-safe token (hub memory only, never stored or logged) and dispatches `models.send` to
   the source (`preparing`).
2. The source checks the model, computes its SHA-256, makes an ephemeral self-signed certificate,
   binds the first free port of `config.transfer` and emits `transfer_ready` (`ready`).
3. The hub dispatches `models.receive` to the target. `endpoints` are `address:port` for the
   addresses of the event first, then the address the hub sees the source connect from (IPv6 as
   `[addr]:port`). `models.send` got `peer_addresses` = the target's inventory addresses plus the
   address the hub sees it connect from.
4. The target tries the endpoints in order (5 s each), pins `cert_sha256` and pulls. Both nodes
   report `transfer_progress` (`transferring`). The target's `done` ends the transfer (`done`).
5. With `move: true` the hub then dispatches `models.delete {model, confirm: true}` to the source.
   A failed delete is recorded as a warning in `error`; the status stays `done`.

**Action args** (validated by the node). `models.send`: `transfer_id`, `model` (name in `models/` or
a `model_dirs` entry), `token` (43 chars), `peer_addresses`; refused with `busy` (gaming, paused, a
transfer already open) or `invalid_args` (unknown model, split GGUF). `models.receive`:
`transfer_id`, `model`, `size`, `sha256`, `token`, `cert_sha256`, `endpoints` (1-16); refused with
`busy`, `invalid_args` (name exists) or `insufficient_disk`.

**Wire protocol.** HTTP/1.1 over TLS 1.3 on the single-use socket. `GET /model` with
`Authorization: Bearer <token>` and optional `Range: bytes=N-`; the source answers 200/206 with
`Content-Length` and `X-Topcoat-Sha256`; 401 for a wrong token, 403 for a wrong peer address, 404
otherwise. The socket accepts requests until one has delivered the file to its end, then closes (also
after `ttl_seconds`, on error, on gaming). The target writes `<name>.gguf.part`, hashes while
writing, verifies, renames, and deletes the file on a hash mismatch.

**Events** (node to hub; kinds `transfer_ready` and `transfer_progress`, only the first delivery of
a message id has an effect).
- `transfer_ready {transfer_id, model, size, sha256, port, addresses[], cert_sha256}` from the source.
- `transfer_progress {transfer_id, role: "send"|"receive", status: "running"|"done"|"failed",
  transferred_bytes, total_bytes, error_code?, error?}` from both sides, throttled to about 2 s.
  `error_code` is one of `no_route`, `hash_mismatch`, `insufficient_disk`, `timeout`, `canceled`,
  `busy`, `internal`. The hub accepts `send` events from the source and `receive` events from the
  target only, for a transfer it is coordinating.

**Inventory.** `hardware.addresses`: unicast IPs of the node (no loopback, no link-local), at most 16.
The GUI node summary exposes it as `addresses`, and `allowed_actions` from the node's config.

**Config.** `config.transfer` = `{port_min (default 47000), port_max (47099), ttl_seconds (600)}`.

**Hub REST** (admin; every row carries a `user_id` and is only read through it).
- `POST /api/model-transfers` `{source_node_id, target_node_id, model, target_name?, move?}` → 201 row.
- `GET /api/model-transfers[?node_id=]` → `{transfers: [row]}`, newest first.
- `GET /api/model-transfers/{id}` → row; `POST /api/model-transfers/{id}/cancel` → row.
- Row: `id, user_id, source_node_id, target_node_id, model, target_name, size, sha256, move, status
  (pending|preparing|ready|transferring|done|failed|canceled), error_code, error, created_at, updated_at`.
  `error_code` is a node code or `interrupted` (the hub restarted during the transfer).
- Errors: `not_found` (404), `node_offline`, `model_missing`, `model_exists`, `insufficient_disk`,
  `busy`, `not_active` (409), `not_allowed` (403), `invalid_request` (400).
- A transfer without progress from either node for 15 minutes (`TOPCOAT_MODEL_TRANSFER_TIMEOUT_SECONDS`)
  fails with `timeout`. Cancel is best effort: the hub forgets the transfer and its token and marks it
  `canceled`; the nodes close their sockets on their own (`ttl_seconds`).
- GUI stream: SSE event `model_transfer` = `{transfer: <row>, transferred_bytes?, total_bytes?}`,
  sent to the streams of the owner only.
- New nodes get `models.send` and `models.receive` in `allowed_actions`; migration 0043 adds them to
  existing nodes that allow `models.list` (tool nodes are left alone).

## Versioning
`v` is the protocol version; the schemas of each version live in `proto/v<N>/` with examples
in `proto/examples/v<N>/`. The hub supports the current and the previous version: it takes the
version from the node's `hello`, validates that session's messages against that version's
schemas and sends every message (config pushes, actions, gateway requests) with the node's own
`v`. A node with an unsupported version gets `error.code = "upgrade_required"` and stays
connected read-only. Update the hub first, then the nodes: a v1 node cannot use a hub that only
knows v0 (the hub answers `upgrade_required` in v0, which the node ignores).

**v1 (E2-01) froze the v0 draft** after stage 1 was accepted (ROADMAP test 2). Its messages
are exactly the v0 messages of that moment (`proto/v1` = `proto/v0` with `v: 1`). From now on
additive changes (new optional fields, new message types, new default actions) stay in v1;
every breaking change bumps the version and adds `proto/v2`.

**v0 nodes** (the draft) are still accepted, answered in v0, and logged with a warning and
`protocol_version` in the `connected` event. They stop being accepted with the next protocol
release (`proto/v0` is then removed): update the nodes (`topcoat-node install` again keeps the
identity).

Breaking changes inside the v0 draft (before the freeze):
- E1-01 follow-up: runner type `lmstudio` and `runner.url` removed (D9).
- E1-03: `pair_response.credential` removed; nodes authenticate with a signed nonce
  (`POST /api/node/challenge`, `Authorization: Topcoat-Ed25519 …`) instead of a bearer token;
  hub URLs are `https://`. Nodes paired before must pair again.
- E1-05: new messages `proxy_request`, `proxy_chunk`, `proxy_end`, `proxy_cancel`; heartbeat
  `loading`; error code `not_loaded`; default action `models.proxy` (all additive).
- E1-06: node state `yielded` (a hub without E1-06 rejects heartbeats that carry it); additive:
  `config.residents`, `config.pressure` (incl. `foreign_shared_trigger_mb`), heartbeat
  `foreign_vram_used_mb` / `foreign_gpu_util` / `foreign_shared_max_mb`, `model_unloaded.signal`,
  event `auto_reload_paused`.

Additive changes in v1 (no version bump):
- Inventory `models[].own` (bool): true = the file is in the node's own `<data dir>/models` (the hub may
  move or delete it), false = in a configured `model_dirs` entry (read-only for the hub).
- E2-07: actions `models.send` / `models.receive`, events `transfer_ready` / `transfer_progress`,
  `config.transfer`, inventory `hardware.addresses`, hub endpoints `/api/model-transfers*`, SSE event
  `model_transfer`.
- E2-06: action `node.update`, `config.update.channel`, inventory `update`, events `update_staging` /
  `update_staged` / `update_applied` / `update_failed`, hub endpoints `/api/node/update/*` and `/api/updates*`.
- E2-05 B / E: node state `paused`; message `proxy_status`; events `paused`, `resumed`, `sleeping`;
  error code `interrupted`; `config.yield`, `config.power`; inventory `hardware.network`; hub
  `POST /api/nodes/{id}/wake`, event `wake_sent`, SSE event `status` of the chat.
- E2-01 fixes: `config.runner.default_context_length`, `config.runner.model_context_lengths`.
- E2-05 A: `config.runner.model_backends`, `config.runner.memory_guard`, inventory
  `hardware.disk_total_mb` / `disk_free_mb`, error codes `insufficient_memory` /
  `insufficient_disk`, event `benchmark_error`, `benchmark_finished.failed_runs`.
- E3-01c: `config.runner.jinja`, `config.runner.model_jinja`; hub `GET/PUT /api/agent/settings`.
- D125 (#50): inventory `models[].projector`, `config.runner.projectors`.
- D126 (#44): `config.images`, inventory `images`.
- E3-05 (hub only): memory notes `kind`, `stage`, `expires_at`, `verify_with`, usage and feedback
  counters, status `archived`; `POST /api/memory/{id}/feedback`, `/archive`, `/api/memory/care`;
  runs of kind `care`; agent task origin `care`.
- E3-01a fixes: `config.runner.n_parallel`, `config.runner.model_n_parallel`,
  `loaded[].n_parallel`, `models.load` argument `n_parallel`, hub error code `context_full`;
  `loaded[].context_length` is the context of one slot.


## Build service (NS-01, D80)

A node paired with `--role build` (config `roles: ["build"]`, `allowed_actions: ["build.manage"]`) offers the
named steps below as capabilities with `manage: true`; the hub runs them with the action `build.manage` and
nothing else runs them. Inputs are schema-validated; there is no free-form command line. One build at a time.

| capability | input | output |
|---|---|---|
| `build.fetch` | `ref` (a commit hash, branch or tag), `source` (`release_repo` or `dev_mirror`) | `commit` (40 hex digits), `subject`, `changes` (the commit subjects since the last release, at most 4000 characters), `log` |
| `build.node_release` | `commit`, `version`, `build_id` | `artifacts` `[{name, os, arch, size, sha256}]`, `log`. The node has uploaded every artifact to the hub before it returns |
| `build.hub_image` | `commit`, `build_id` | `image` (name:tag), `digest` (`sha256:...`), `size`, `log`. The image stays on the build host; only its name and digest travel |
| `build.node_container_image` | `commit`, `version`, `build_id` | the same as `build.hub_image` for the container node |
| `build.smoke_test` | `image` | `ok` (bool), `summary`, `log` |

**Artifact upload.** `PUT /api/node/builds/{build_id}/artifacts/{name}?os=&arch=`, signed like the update
download (`Authorization` with a fresh challenge), body the file. The hub accepts it only from the node the
build was given to, while the build runs, at most 512 MiB, and records the size and SHA-256 it computed
itself; the output of `build.node_release` must list the same files with the same checksums or the build
fails. Build logs are passed through the node's secret filter and again through the hub's before they are
stored.

**Signing.** The build service never signs. The hub builds the manifests (`GET /api/releases/builds/{id}/manifests`),
the admin's browser signs `topcoat-update-v1\n` + the manifest bytes with the key file chosen on the page
(Web Crypto, Ed25519) and posts only the signature lines; the hub checks them against the configured update
keys and publishes through the update channel. Key file format: `topcoat-update-key-v1 <base64 seed>`.
