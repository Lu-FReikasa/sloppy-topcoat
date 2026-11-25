# sloppy-topcoat

A self-hosted control plane for local AI across several machines in a home lab.

One **hub** holds configuration, memory, knowledge and users. Small **node** agents run on
every machine and report what they have (GPU/VRAM, downloaded models, loaded models, local
tools). A web GUI on the hub lets you see and manage all nodes, chat in parallel sessions,
run background tasks and — later — talk to the agent by voice.

Status: a private, working home-lab system (one hub, several nodes, a web desktop with chat,
research, development, administration and project planning). It runs on one production hub; real
hardware parts that were not tried yet are marked `UNVERIFIED` in the code. Try it:
[docs/QUICKSTART.md](docs/QUICKSTART.md), [docs/INSTALL-NODE.md](docs/INSTALL-NODE.md); develop:
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

- **All documentation, and which page wins: [docs/README.md](docs/README.md)**
- How the parts fit together, on one page: [docs/OVERVIEW.md](docs/OVERVIEW.md)
- The decisions and their reasons (history): [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Node ↔ hub protocol: [docs/PROTOCOL.md](docs/PROTOCOL.md); the hub's web API: [docs/HUB-API.md](docs/HUB-API.md)
- Rules for anyone (human or agent) working in this repo: [CLAUDE.md](CLAUDE.md)

## Layout

```
hub/     Python — API, orchestrator, scheduler, DB access (FastAPI, Postgres + pgvector)
node/    Go     — single static binary per machine (inventory, model runner, tool capabilities)
web/     TypeScript — React + Vite web GUI served by the hub
proto/   JSON Schemas for node ↔ hub messages (source of truth for both sides)
deploy/  docker compose files, example configs (no real hostnames, IPs or secrets)
docs/    overview, roadmap, decisions, protocol, task briefs
```

## License

GNU Affero General Public License v3.0 or later, see [LICENSE](LICENSE). If you run a modified hub for other people over a network, you must offer them the source of your modified version (AGPL section 13).
