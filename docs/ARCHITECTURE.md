# Architecture

> The current state on one page is [OVERVIEW.md](OVERVIEW.md). This file describes the components and
> lists every decision with its reason, sorted by topic (D1 to D122). A decision that no longer holds
> as written says so at the top of its file and in the column *Status* below; its text is kept as history.

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
  it if needed, proxies the stream through the node session. Which model and node serve a
  piece of work: pools and house models (D115, D117) and the table of applications (D85). The
  hub calls no external LLM (D33).
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
- A desktop of programs (Inbox, Chat, Research, Development, Administration, Projects,
  Incognito, Settings) with a taskbar, a command palette and keys, drawn by a theme (D70, D106,
  [THEMES.md](THEMES.md)). The pages of each program are described in [PLATFORM.md](PLATFORM.md).
- UI strings through i18n: English is the default, German is fully supported; the language
  is stored per user (fallback: browser language).
- A desktop client (Tauri 2: free windows per program, a global key, tray icon, an overlay per
  screen) wraps the same web GUI (D106, [DESKTOP.md](DESKTOP.md)).

## Key decisions

One file per decision in [decisions/](decisions): the decision and the reason it was taken. The numbers never change and
are quoted in code and docs. Each file that is no longer current says so at its top (**Status**), with the date and
what replaces or amends it; the text below the status is kept as it was decided.

| Status | Meaning |
|---|---|
| Current | holds as written |
| Amended | holds; the status line names what changed since |
| Replaced / Replaced in part | history; follow the link in the file |
| Removed | the feature is gone |
| Decided, not built | decided, waiting for its plan step |
| Under review / Open question | conflicts with the current state or goal; the maintainer decides |

D109, D110 and D112 to D114 are reserved for the open decisions of the plan of 2026-11-24 (decomposition into
tasks, workshop scope, secretary, Theme v2, theme images). D115 and D116 restore two decisions of 2026-11-04 whose
numbers were reused by mistake on 2026-11-17.

### Foundations and security

What Topcoat is built on and what it never does. These rules are the baseline of `CLAUDE.md`.

| # | Decision | Status |
|---|---|---|
| [D1](decisions/D01.md) | Own lean orchestrator, no embedded agent framework | Current |
| [D2](decisions/D02.md) | Hub in Python, node in Go | Current |
| [D5](decisions/D05.md) | Postgres + pgvector instead of SQLite | Current |
| [D6](decisions/D06.md) | Hub is a container | Amended |
| [D7](decisions/D07.md) | No inference on the hub host | Open question |
| [D8](decisions/D08.md) | Multi-user data model from day one, single-admin GUI first | Current |
| [D9](decisions/D09.md) | Self-contained | Current |
| [D3](decisions/D03.md) | Nodes connect outbound over WebSocket | Current |
| [D4](decisions/D04.md) | Whitelisted capabilities instead of remote shell | Current |
| [D11](decisions/D11.md) | TLS from the start | Current |
| [D12](decisions/D12.md) | Node authentication by signature | Current |
| [D20](decisions/D20.md) | Strict per-user isolation of everything personal | Current |
| [D15](decisions/D15.md) | The assistant only reads without explicit confirmation | Amended |
| [D43](decisions/D43.md) | The agent proposes, the person confirms one click at a time | Current |
| [D83](decisions/D83.md) | Approvals follow the scope of the request, not a single attempt | Current |
| [D42](decisions/D42.md) | Honesty by mechanism, not by prompt | Current |
| [D10](decisions/D10.md) | External LLM calls only through the redaction layer | Replaced |
| [D33](decisions/D33.md) | Escalation to an external LLM (removed) | Current |
| [D93](decisions/D93.md) | Version 1.0 is the goal, and the project is published with it | Replaced |
| [D122](decisions/D122.md) | The repository is public before version 1.0 | Current |

### Nodes and hardware

How a machine joins, protects its owner's use of it and updates itself.

| # | Decision | Status |
|---|---|---|
| [D80](decisions/D80.md) | A node is a machine | Open question |
| [D14](decisions/D14.md) | When a hub version adds default permissions | Current |
| [D16](decisions/D16.md) | Nodes give GPU memory back on their own when other programs need it | Current |
| [D17](decisions/D17.md) | The Windows service runs as a virtual account | Current |
| [D24](decisions/D24.md) | Local use comes first | Current |
| [D27](decisions/D27.md) | Node resource guards and prompt order | Current |
| [D28](decisions/D28.md) | Energy | Current |
| [D79](decisions/D79.md) | The context of a load is the smallest the work needs; growth is a node's cap | Current |
| [D50](decisions/D50.md) | A signed update channel for the nodes | Open question |
| [D51](decisions/D51.md) | Models move directly from node to node | Current |
| [D64](decisions/D64.md) | A node comes online | Current |

### Models, queue and checks

Which model and which node do a piece of work, in which order, and who checks it.

| # | Decision | Status |
|---|---|---|
| [D18](decisions/D18.md) | Model gateway | Amended |
| [D23](decisions/D23.md) | Roles instead of device names | Replaced |
| [D78](decisions/D78.md) | Model roles | Replaced |
| [D85](decisions/D85.md) | One table decides model and node | Replaced in part |
| [D115](decisions/D115.md) | Pools per node, functions per program, block programs instead of flows | Current |
| [D117](decisions/D117.md) | House models: each node keeps one model for all text work | Current |
| [D86](decisions/D86.md) | One queue in the hub, the node is chosen when there is room | Current |
| [D94](decisions/D94.md) | A node's queue is as large as the node says, and the node is chosen before the prompt, without holding a place | Current |
| [D95](decisions/D95.md) | The record of model requests holds no content and nothing of incognito work | Current |
| [D91](decisions/D91.md) | The decision log is for administrators | Current |
| [D97](decisions/D97.md) | Thinking is a setting per model family and quality level, and an everyday answer does not think | Amended |
| [D87](decisions/D87.md) | A check needs a fresh context, not a second node | Amended |
| [D121](decisions/D121.md) | A check comes from another model family where there is one | Current |
| [D88](decisions/D88.md) | One machine is a full setup | Current |
| [D90](decisions/D90.md) | Speed decides on its own, quality only suggests | Under review |
| [D92](decisions/D92.md) | Model quality is rated continuously from the second look, with a floor as default | Under review |

### Applications and how they work together

The programs (Chat, Research, Development, Administration, Projects), their rights, contracts, block programs and messages.

| # | Decision | Status |
|---|---|---|
| [D61](decisions/D61.md) | One interface, applications and the inbox | Amended |
| [D69](decisions/D69.md) | The tools of an application are a ceiling | Amended |
| [D116](decisions/D116.md) | One application profile: run, prompt and rights of a program in one place | Current |
| [D118](decisions/D118.md) | Each application has a contract | Current |
| [D105](decisions/D105.md) | Applications as plug-ins: data in the hub, code only as a node of its own | Under review |
| [D59](decisions/D59.md) | Flows | Removed |
| [D66](decisions/D66.md) | Effort is a variable of the run | Amended |
| [D67](decisions/D67.md) | The chat policy | Replaced |
| [D71](decisions/D71.md) | Nachlese and emoji answers are policy nodes too | Replaced |
| [D75](decisions/D75.md) | The hub's own work as background flows | Amended |
| [D81](decisions/D81.md) | Applications talk to each other in typed messages | Current |
| [D82](decisions/D82.md) | A reviewed result becomes part of the Project's Akte | Current |
| [D84](decisions/D84.md) | The review loop has no hard limit, and its load is observed | Current |
| [D65](decisions/D65.md) | Hand-over between applications | Under review |
| [D72](decisions/D72.md) | The chat model may propose a hand-over | Current |
| [D63](decisions/D63.md) | The incognito folder | Current |
| [D111](decisions/D111.md) | The inbox has no marks, no board and no re-check | Decided, not built |
| [D68](decisions/D68.md) | Marks and the regular re-check | Replaced |

### Chat and answers

How one answer is made, stored, shown and continued.

| # | Decision | Status |
|---|---|---|
| [D21](decisions/D21.md) | Chat | Amended |
| [D39](decisions/D39.md) | An answer belongs to the hub, not to the browser | Current |
| [D40](decisions/D40.md) | Times in the chat | Current |
| [D41](decisions/D41.md) | Message kinds | Current |
| [D56](decisions/D56.md) | One record of what the hub does for an answer | Current |
| [D57](decisions/D57.md) | Announced, not done | Current |
| [D26](decisions/D26.md) | Follow-up checks of an answer | Replaced |
| [D53](decisions/D53.md) | Sections, incognito and a short tool list in the chat | Replaced |
| [D54](decisions/D54.md) | A section is a chat of its own | Amended |
| [D77](decisions/D77.md) | One running summary per chat | Current |
| [D100](decisions/D100.md) | A new chat reads the key terms of the latest chats; there is no hand-over note | Current |
| [D96](decisions/D96.md) | A tool description reaches the model whole where it is the only teacher | Current |

### Agents and the home lab

Read-only looks at the servers, the evidence behind a finding, and the one-click path to a change.

| # | Decision | Status |
|---|---|---|
| [D29](decisions/D29.md) | Tool nodes | Current |
| [D30](decisions/D30.md) | The agent loop | Amended |
| [D31](decisions/D31.md) | The chat uses the agent loop | Under review |
| [D44](decisions/D44.md) | The coordinator | Current |
| [D45](decisions/D45.md) | The inventory | Current |
| [D46](decisions/D46.md) | Stacks and the inventory's buttons | Current |
| [D47](decisions/D47.md) | The identity view | Current |
| [D32](decisions/D32.md) | Connectors | Current |
| [D35](decisions/D35.md) | Web access is a read-only capability of a tool node | Current |
| [D36](decisions/D36.md) | The internet is a switch of its own in the chat, not one of the tool groups | Current |
| [D89](decisions/D89.md) | The agent may read the host thoroughly, and reads files only from folders named per node | Current |
| [D25](decisions/D25.md) | A durable, per-user queue for deep tasks | Replaced |

### Memory and knowledge

What the assistant keeps about a person and what it reads from notes, reports and the wiki.

| # | Decision | Status |
|---|---|---|
| [D19](decisions/D19.md) | The assistant keeps its own memory | Under review |
| [D22](decisions/D22.md) | Memory notes | Under review |
| [D37](decisions/D37.md) | The assistant tends its own memory | Under review |
| [D38](decisions/D38.md) | One memory place with a core that stands first | Amended |
| [D48](decisions/D48.md) | The assistant may learn rules and styles itself, in a narrow way | Current |
| [D62](decisions/D62.md) | Memory belongs to an application or is shared | Replaced |
| [D74](decisions/D74.md) | A memory per program, a tag tree, a frame per program | Current |
| [D34](decisions/D34.md) | The knowledge base reads notes through a tool node | Amended |
| [D107](decisions/D107.md) | Topcoat has its own wiki; Outline is removed | Current |

### Research and documents

Research runs and the reports they end in.

| # | Decision | Status |
|---|---|---|
| [D101](decisions/D101.md) | Research is run by three frames, not by the limits of a server check | Amended |
| [D102](decisions/D102.md) | A thorough research is a report: a versioned document in the Ablage, Markdown as source, HTML as view | Current |

### Interface, desktop and themes

How the GUI is drawn: the desktop, the desktop client and themes as data.

| # | Decision | Status |
|---|---|---|
| [D60](decisions/D60.md) | The interface is data drawn by a shell | Under review |
| [D70](decisions/D70.md) | The desktop replaces the shells and the looks | Replaced |
| [D73](decisions/D73.md) | A second look | Replaced |
| [D106](decisions/D106.md) | A desktop client with free windows; themes decide behaviour as data | Current |

### Development, workshop and releases

How code changes are made, confirmed, tested and rolled out, and how releases are built.

| # | Decision | Status |
|---|---|---|
| [D52](decisions/D52.md) | The dev workshop | Amended |
| [D55](decisions/D55.md) | One way to connect a repository | Current |
| [D49](decisions/D49.md) | GitHub as a read-only tool of the assistant | Amended |
| [D76](decisions/D76.md) | Local repositories in the dev workshop | Current |
| [D99](decisions/D99.md) | A change to a registered repository is confirmed one by one | Amended |
| [D103](decisions/D103.md) | The fix of an issue is confirmed as one set, with its whole change shown | Amended |
| [D108](decisions/D108.md) | An issue's investigation may try a guess, under the person's probe mandate | Current |
| [D119](decisions/D119.md) | A model's answer has a form the server enforces, and a plan names symbols, not lines | Current |
| [D120](decisions/D120.md) | The same failure twice stops the automatic drafts | Current |
| [D104](decisions/D104.md) | A tested fix reaches the hub through a spool on the hub host, not through the hub | Amended |
| [D98](decisions/D98.md) | Development may go live through an overlay; a release is an image | Current |
| [D58](decisions/D58.md) | Parameters an admin changes while the hub runs | Current |
| [D13](decisions/D13.md) | Release channels | Under review |

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
