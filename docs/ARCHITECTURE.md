# Architecture

> The current state on one page is [OVERVIEW.md](OVERVIEW.md). This file is the **history of the
> decisions and their reasons** (D1 to D104); where a row says it was removed or replaced, that row
> is no longer how the hub works. D85 and D90 to D92 are decided but **not built yet** (D93 is a rule for the project); D86 is built in part
> (SCH-03 steps 1 to 3, D94 and D95; the table that gives the queue its lists is D85), D87 and D88 only in part (SCH-02); until the briefs are
> merged, D78 and the rows they replace still describe the running hub. D89 (host reading) is built.

```
                         ┌──────────────────────── hub (container) ────────────────────────┐
 browser / desktop  ───► │ web GUI ─► API (FastAPI) ─► orchestrator ─► tool router          │
                         │                 │              │                                 │
                         │                 ▼              ▼                                 │
                         │        Postgres + pgvector   model gateway (OpenAI-compatible)   │
                         │                 ▲              │                                 │
                         │        node registry ◄── WebSocket (nodes connect outbound) ─────┼──┐
                         └──────────────────────────────────────────────────────────────────┘  │
                                                                                               │
   node-amd (Windows)            node-nvidia (Linux)             tool nodes (servers)          │
   ├ inventory (DXGI/ADLX)       ├ inventory (NVML)              ├ inventory (CPU/RAM/disk)    │
   ├ runner: llama-server        ├ runner: llama-server          └ capabilities (whitelist) ◄──┘
   │   (Vulkan, node-managed)    │   (CUDA, node-managed)
   ├ game watcher → unload       ├ STT/TTS service (stage 4)
   └ audio I/O (stage 4)         └ capabilities
```

## Components

### Hub (Python 3.12, FastAPI, Postgres 16 + pgvector)
- **API**: REST for the GUI, WebSocket for GUI live updates, WebSocket endpoint for nodes.
- **Node registry**: pairing, identity (per-node ed25519 key, D12), heartbeat, last-seen,
  desired vs. actual state per node.
- **TLS** (D11): the hub serves HTTPS/WSS itself — with a certificate from its own small CA
  (created on first start) or with a certificate the admin provides — or sits behind a
  reverse proxy that terminates TLS.
- **Model gateway** (D18): one OpenAI-compatible endpoint (`/v1/chat/completions`,
  `/v1/completions`, `/v1/embeddings`, `/v1/models`). Resolves a requested model to a node that
  has it (loaded preferred, nodes that are gaming or yielded skipped), asks the node to load
  it if needed, proxies the stream through the node session. Fallback order configurable
  (`TOPCOAT_GATEWAY_NODE_ORDER`). External LLMs are added in stage 3 behind the redaction
  layer.
- **Orchestrator** (stage 2–3): chat sessions, background tasks (async, resumable, cancelable),
  tool calls, verification pipeline.
- **Knowledge & memory** (stage 2): indexes a read-only Markdown knowledge base (hybrid search:
  Postgres full-text + pgvector embeddings), plus a per-user agent memory in the DB.
- **Scheduler**: time windows (e.g. "no heavy jobs 00:00–08:00 on the first Tuesday"),
  recurring tasks for tool nodes.

### Node (Go, single static binary, Windows + Linux amd64)
- Connects outbound to the hub, authenticates, sends inventory + heartbeats, receives actions.
- Caches the last config it received, keeps working (e.g. idle-unload, game watcher, VRAM
  pressure handling) when the hub is unreachable, and replays queued events when it returns.
- **Providers** (interfaces, each with a fake for tests):
  - `GPUProvider` — list GPUs, VRAM total/used. NVIDIA on Linux via NVML (dynamic load with
    purego, no CGO; `nvidia-smi` fallback), AMD on Linux via sysfs, all vendors on Windows via
    DXGI (adapters, totals) + the system-wide PDH counters `GPU Adapter Memory` (usage;
    `QueryVideoMemoryInfo` only reports the calling process). iGPUs report shared memory
    separately. For VRAM pressure (D16) it also reports per GPU what the node's own model
    processes use and how busy other programs keep the GPU: PDH `GPU Process Memory` /
    `GPU Engine` on Windows, NVML process lists on Linux NVIDIA, amdgpu `fdinfo` on Linux AMD.
  - `ModelRunner` — list/download/load/unload models, report loaded models and their VRAM.
    Single implementation `llamaserver`: the node supervises `llama-server` processes
    (llama-swap style), GGUF files in a node-local model directory, downloads directly from
    Hugging Face with checksum verification. The node manages its own pinned llama.cpp build
    per platform/backend (Windows + AMD: Vulkan; Linux + NVIDIA: CUDA; CPU fallback),
    fetched from the official llama.cpp releases and verified by hash. **No third-party
    model apps (LM Studio, Ollama …) are required or supported as runners.**
  - `ProcessWatcher` — detects configured game executables / GPU-heavy processes → triggers
    `unload_all` with a grace period. While a rule matches, nothing is loaded automatically;
    afterwards resident models come back through pressure handling (D16).
- **Pressure controller** (D16): samples VRAM (and optionally RAM) every few seconds, unloads
  models when other programs need the memory and reloads the hub-listed resident models when
  it is free again.
  - `Capability` — named local tools with JSON-Schema input, e.g. `service.status`,
    `logs.tail`, `disk.usage`. Assigned per node by the hub. No generic shell.
- Runs as a Windows service (virtual account `NT SERVICE\topcoat-node`, D17) / systemd unit
  (system user `topcoat-node`), restarting on failure.

### Web GUI (React + Vite + TypeScript)
- Served by the hub. Responsive layout (desktop first, usable on phone later).
- Stage 1 views: node overview, node detail (GPU/VRAM, models, loaded state, actions),
  model catalogue across nodes, event log, API access (keys).
- Stage 2 views (E2-01): chat (conversation list with search, parallel streaming answers,
  Markdown with code highlighting, model/node picker, stop/regenerate/copy) and settings
  (default model, system prompt, temperature, form of address, memory on/off; the admin also
  sets the hub default model).
- Stage 2 views (E2-02): memory (search and filter the assistant's notes, history with reasons,
  edit, revert, delete/restore, decide disputes, "tidy up now", forget everything) and
  "remembered: …" lines with undo under answers.
- UI strings through i18n: English is the default, German is fully supported; the language
  is stored per user (fallback: browser language).
- A thin desktop shell (Tauri: tray icon, global hotkey, overlay) comes in stage 5.

## Key decisions

One file per decision in [decisions/](decisions): the decision and the reason it was taken. The numbers
never change and are quoted in code and docs. A decision that says it was removed or replaced is history.

| # | Decision |
|---|---|
| [D1](decisions/D01.md) | Own lean orchestrator, no embedded agent framework |
| [D2](decisions/D02.md) | Hub in Python, node in Go |
| [D3](decisions/D03.md) | Nodes connect outbound over WebSocket |
| [D4](decisions/D04.md) | Whitelisted capabilities instead of remote shell |
| [D5](decisions/D05.md) | Postgres + pgvector instead of SQLite |
| [D6](decisions/D06.md) | Hub is a container |
| [D7](decisions/D07.md) | No inference on the hub host |
| [D8](decisions/D08.md) | Multi-user data model from day one, single-admin GUI first |
| [D9](decisions/D09.md) | Self-contained |
| [D10](decisions/D10.md) | External LLM calls only through the redaction layer |
| [D11](decisions/D11.md) | TLS from the start |
| [D12](decisions/D12.md) | Node authentication by signature |
| [D13](decisions/D13.md) | Release channels |
| [D14](decisions/D14.md) | When a hub version adds default permissions |
| [D15](decisions/D15.md) | The assistant only reads without explicit confirmation |
| [D16](decisions/D16.md) | Nodes give GPU memory back on their own when other programs need it |
| [D17](decisions/D17.md) | The Windows service runs as a virtual account |
| [D18](decisions/D18.md) | Model gateway |
| [D19](decisions/D19.md) | The assistant keeps its own memory |
| [D20](decisions/D20.md) | Strict per-user isolation of everything personal |
| [D21](decisions/D21.md) | Chat |
| [D22](decisions/D22.md) | Memory notes |
| [D23](decisions/D23.md) | Roles instead of device names |
| [D24](decisions/D24.md) | Local use comes first |
| [D25](decisions/D25.md) | A durable, per-user queue for deep tasks |
| [D26](decisions/D26.md) | Follow-up checks of an answer |
| [D27](decisions/D27.md) | Node resource guards and prompt order |
| [D28](decisions/D28.md) | Energy |
| [D29](decisions/D29.md) | Tool nodes |
| [D30](decisions/D30.md) | The agent loop |
| [D31](decisions/D31.md) | The chat uses the agent loop |
| [D32](decisions/D32.md) | Connectors |
| [D33](decisions/D33.md) | Escalation to an external LLM (removed) |
| [D34](decisions/D34.md) | The knowledge base reads notes through a tool node |
| [D35](decisions/D35.md) | Web access is a read-only capability of a tool node |
| [D36](decisions/D36.md) | The internet is a switch of its own in the chat, not one of the tool groups |
| [D37](decisions/D37.md) | The assistant tends its own memory |
| [D38](decisions/D38.md) | One memory place with a core that stands first |
| [D39](decisions/D39.md) | An answer belongs to the hub, not to the browser |
| [D40](decisions/D40.md) | Times in the chat |
| [D41](decisions/D41.md) | Message kinds |
| [D42](decisions/D42.md) | Honesty by mechanism, not by prompt |
| [D43](decisions/D43.md) | The agent proposes, the person confirms one click at a time |
| [D44](decisions/D44.md) | The coordinator |
| [D45](decisions/D45.md) | The inventory |
| [D46](decisions/D46.md) | Stacks and the inventory's buttons |
| [D47](decisions/D47.md) | The identity view |
| [D48](decisions/D48.md) | The assistant may learn rules and styles itself, in a narrow way |
| [D49](decisions/D49.md) | GitHub as a read-only tool of the assistant |
| [D50](decisions/D50.md) | A signed update channel for the nodes |
| [D51](decisions/D51.md) | Models move directly from node to node |
| [D52](decisions/D52.md) | The dev workshop |
| [D53](decisions/D53.md) | Sections, incognito and a short tool list in the chat |
| [D54](decisions/D54.md) | A section is a chat of its own |
| [D55](decisions/D55.md) | One way to connect a repository |
| [D56](decisions/D56.md) | One record of what the hub does for an answer |
| [D57](decisions/D57.md) | Announced, not done |
| [D58](decisions/D58.md) | Parameters an admin changes while the hub runs |
| [D59](decisions/D59.md) | Flows |
| [D60](decisions/D60.md) | The interface is data drawn by a shell |
| [D61](decisions/D61.md) | One interface, applications and the inbox |
| [D62](decisions/D62.md) | Memory belongs to an application or is shared |
| [D63](decisions/D63.md) | The incognito folder |
| [D64](decisions/D64.md) | A node comes online |
| [D65](decisions/D65.md) | Hand-over between applications |
| [D66](decisions/D66.md) | Effort is a variable of the run |
| [D67](decisions/D67.md) | The chat policy |
| [D68](decisions/D68.md) | Marks and the regular re-check |
| [D69](decisions/D69.md) | The tools of an application are a ceiling |
| [D70](decisions/D70.md) | The desktop replaces the shells and the looks |
| [D71](decisions/D71.md) | Nachlese and emoji answers are policy nodes too |
| [D72](decisions/D72.md) | The chat model may propose a hand-over |
| [D73](decisions/D73.md) | A second look |
| [D74](decisions/D74.md) | A memory per program, a tag tree, a frame per program |
| [D75](decisions/D75.md) | The hub's own work as background flows |
| [D76](decisions/D76.md) | Local repositories in the dev workshop |
| [D77](decisions/D77.md) | One running summary per chat |
| [D78](decisions/D78.md) | Model roles |
| [D79](decisions/D79.md) | The node sizes the context of a load |
| [D80](decisions/D80.md) | A node is a machine |
| [D81](decisions/D81.md) | Applications talk to each other in typed messages |
| [D82](decisions/D82.md) | A reviewed result becomes part of the Project's Akte |
| [D83](decisions/D83.md) | Approvals follow the scope of the request, not a single attempt |
| [D84](decisions/D84.md) | The review loop has no hard limit, and its load is observed |
| [D85](decisions/D85.md) | One table decides model and node |
| [D86](decisions/D86.md) | One queue in the hub, the node is chosen when there is room |
| [D87](decisions/D87.md) | A check needs a fresh context, not a second node |
| [D88](decisions/D88.md) | One machine is a full setup |
| [D89](decisions/D89.md) | The agent may read the host thoroughly, and reads files only from folders named per node |
| [D90](decisions/D90.md) | Speed decides on its own, quality only suggests |
| [D91](decisions/D91.md) | The decision log is for administrators |
| [D92](decisions/D92.md) | Model quality is rated continuously from the second look, with a floor as default |
| [D93](decisions/D93.md) | Version 1.0 is the goal, and the project is published with it |
| [D94](decisions/D94.md) | A node's queue is as large as the node says, and the node is chosen before the prompt, without holding a place |
| [D95](decisions/D95.md) | The record of model requests holds no content and nothing of incognito work |
| [D96](decisions/D96.md) | A tool description reaches the model whole where it is the only teacher |
| [D97](decisions/D97.md) | Thinking is a setting per model family and quality level, and an everyday answer does not think |
| [D98](decisions/D98.md) | Development may go live through an overlay; a release is an image |
| [D100](decisions/D100.md) | A new chat reads the key terms of the latest chats' running summaries; the hand-over note (episode) is removed |
| [D101](decisions/D101.md) | Research runs by three frames (quick, default, thorough) with no fixed limits and a progress guard; its own prompt |
| [D102](decisions/D102.md) | A thorough research is a report: a versioned document in the Ablage, Markdown with blocks as source, HTML as view |
| [D99](decisions/D99.md) | A change to a registered repository is confirmed one by one; the assistant proposes it, it never makes it (changes D52 for file edits) |
| [D103](decisions/D103.md) | The fix of an issue is confirmed as one set, with its whole change shown (amends D99) |
| [D104](decisions/D104.md) | A tested fix reaches the hub through a spool on the hub host, not through the hub |
| [D108](decisions/D108.md) | An issue's investigation may try a guess (one test file, one run) under the person's probe mandate (amends D99, uses D83) |

## Failure behaviour
- Hub down: nodes keep their cached config, keep idle-unload, game watcher and pressure
  handling running, queue events. GUI is unavailable.
- GPU node down: overview marks it offline; gateway falls back or queues; running tasks on it
  fail visibly and can be retried.
- Game starts on `node-amd`: node unloads its models, reports `busy_gaming`; gateway routes
  elsewhere until the node reports `available` again.
- Another program needs the VRAM (no rule matches): node unloads models (non-residents first,
  then lowest priority, then least recently used) until enough is free, reports `yielded`;
  gateway treats the models as unavailable. Residents come back when the GPU has been free for
  `pressure.reload_after_seconds`; a model pushed out 3 times in 30 minutes waits 60 minutes.
- Measurement unavailable (driver without per-process data): the node falls back to
  system-wide free VRAM; without per-process data it cannot see demotion, unmet demand or
  foreign GPU load and reloads on free memory alone. Unmet demand is measured on Windows only
  (PDH); on Linux NVIDIA an allocation that does not fit fails in the other program instead.
