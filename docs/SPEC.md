# Specification

Goals, constraints and non-goals. How the system works today is in [OVERVIEW.md](OVERVIEW.md), what is
done and open in the roadmap.

## Problem
Local AI in a home lab is spread over machines with very different capabilities: a gaming PC
with a strong GPU that is often busy with games, a second PC with a smaller GPU, and servers
without GPUs that run the actual home services. Existing tools manage one machine each
(LM Studio, Ollama, llama-swap) or offer a chat UI on top of one endpoint. Nothing gives one
place to see and control all of it, share memory and knowledge across it, and run agents
that use local tools on each machine safely.

## Goals
1. **One overview**: which nodes are online, their hardware, VRAM/RAM usage, downloaded
   models, loaded models, available local tools.
2. **Model management across nodes**: download, load, unload, idle-unload, and automatic
   unload when a game starts on a node (VRAM given back to the game).
3. **Central state on the hub**: configuration, users, memory, knowledge index, chat
   history, task history. Nodes hold no configuration of their own beyond a cached copy.
4. **Everyday GUI** (inspired by Newelle): parallel chats, background tasks with progress,
   model/node picker, knowledge search, later voice.
5. **Agents with verification**: research locally first (knowledge base, web search, local
   tools), mark every claim as `[live]` / `[doc]` / `[assumption]` and verify the answer against
   evidence before presenting it. Nothing is sent to an external LLM.
6. **Tool nodes**: servers contribute local, whitelisted capabilities (read logs, service
   status, disk usage …) instead of the hub holding SSH keys to every machine.
7. **Voice**: wake word, natural turn-taking, the conversation stays responsive while
   background tasks run.
8. **Multi-user ready**: designed for several people from day one (OIDC login, per-user
   memory and permissions), even though the first GUI only serves one admin.
9. **From one machine upwards**: one machine with one node and one model is a full setup,
   including the checks (ARCHITECTURE D88). Several nodes are the recommended setup because they
   are faster; nothing may require them. No home lab is needed to get real use from it.
10. **Standalone applications, chat on top**: every application is usable by itself; the chat can
    ask about it and start work in it. New applications, themes and tools can be planned, written,
    checked and, after the person's review, put into use from inside Topcoat (see VISION.md).

## Non-goals (for now)
- External LLM APIs: the hub calls none (the escalation idea was dropped, ARCHITECTURE D33).
- Splitting one model across several machines (network is too slow to be worth it).
- Kubernetes or any general-purpose cluster scheduler.
- Training or fine-tuning models.
- Mobile apps (the web GUI should be responsive; a PWA may come much later).
- Public internet exposure of the hub.

## Environment of the maintainer (anonymised, one example, not a requirement)
| Role | Hardware | OS | Notes |
|---|---|---|---|
| `node-amd` | Ryzen 7 3700X, RX 9070 XT 16 GB | Windows 11 | Gaming PC. Main GPU. Mic/speakers of the user. No CUDA (AMD via Vulkan). |
| `node-nvidia` | Ryzen 5 3600X, RTX 2080 Super 8 GB (an RTX 3070 8 GB is available), 16 GB RAM | Ubuntu Server 26.04 | Second PC, the NVIDIA GPU node. |
| `node-laptop` | i7-1255U (2 P + 8 E cores), Iris Xe iGPU, 32 GB LPDDR5-5200 | Windows 11 | CPU/iGPU only, background work. |
| hub | a VM on the always-on Unraid host (Docker) | Linux | Shared with family services, so the hub itself does **no model inference**; a CPU-only model node may run as a container on the same host. |
| other servers | small VMs, 4–8 GB | Ubuntu / Debian | Future tool nodes only. |

## Constraints
- The gaming PC must never lose game performance to AI: VRAM is released when a game runs.
- The hub must run without any GPU node online (chat falls back or queues; overview still works).
- Hub resource budget: ≤ 1 GB RAM for hub + DB + web in normal operation.
- Node agent budget: ≤ 30 MB RAM idle, single binary, Windows + Linux (amd64).
- Everything restarts on its own after a reboot (services/containers with restart policy).
- Home-lab friendly: installation = one binary + one pairing code per node.

## Users and roles (target)
- `admin` — everything, including infrastructure tools and node management.
- `user` — chats, own memory, own tasks, models allowed by admin, no infrastructure tools.
- Identity via OIDC with the existing Keycloak — this is the core login in production; a
  static dev admin token until then (stage 5).
- The admin can technically read all data. This must be stated in the GUI for other users.
