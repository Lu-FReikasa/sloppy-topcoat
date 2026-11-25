# topcoat-node

The node agent of sloppy-topcoat: a single Go binary (Windows + Linux, amd64,
no CGO; on Linux it links dynamically against glibc because NVML is loaded with purego) that connects **outbound** to the hub, reports hardware / models / state, and
executes only whitelisted, schema-validated actions. Protocol: `../docs/PROTOCOL.md`,
wire contract: `../proto/v1/*.schema.json`.

## Commands

```sh
topcoat-node install --hub https://hub.example.lan:8443 --code ABCD-1234 --ca-fingerprint sha256:<hex> [--name node-amd] [--data-dir DIR] [--force] [--no-start]
topcoat-node uninstall [--data-dir DIR] [--purge]
topcoat-node pair --hub https://hub.example.lan:8443 --code ABCD-1234 --ca-fingerprint sha256:<hex> [--name node-amd] [--data-dir DIR] [--force]
topcoat-node run  [--data-dir DIR] [--fake] [--fake-profile fake-profile.example.json] [--log-level info] [--log-file FILE]
topcoat-node version
```

- `pair` generates an ed25519 key pair, calls `POST /api/node/pair` over TLS and writes
  `node.json` (hub_url, node_id), `node.key` and the pinned hub CA `hub-ca.pem` to the data
  dir (mode 0600). Trust options: `--ca-fingerprint` (pin, as printed by the GUI), `--ca-file`
  (own CA), neither (system trust store); `--insecure-http` allows `http://` for tests only.
- Before every connect `run` signs a single-use hub nonce with `node.key` (no shared secret).
  The name defaults to the sanitized hostname.
- `run` keeps a WebSocket session to `<hub>/api/node/ws` until SIGINT/SIGTERM
  (reconnects with exponential backoff 1 s .. 60 s). `--fake` uses fake providers
  (one 16 GB AMD GPU, 32 GB RAM, three models; see `fake-profile.example.json`).
- `install` / `uninstall` register / remove a systemd unit or Windows service
  (see `../docs/INSTALL-NODE.md`).
- Data dir: `--data-dir` > `$TOPCOAT_NODE_DATA_DIR` > default
  (Windows `%ProgramData%\topcoat-node`; Linux `/var/lib/topcoat-node` as root,
  otherwise `$XDG_STATE_HOME/topcoat-node` or `~/.local/state/topcoat-node`).
  It holds `node.json`, `node.key`, `config.json` (cached hub config) and
  `events.jsonl` (events queued while the hub is unreachable, max 1000), and for real
  nodes `runtime/` (llama.cpp builds), `models/` (downloads) and `logs/`.

Dependencies (binary): `github.com/coder/websocket` (hub session), `golang.org/x/sys`
(Windows APIs: services, Toolhelp, job objects, registry, ACLs) and
`github.com/ebitengine/purego` (loads `libnvidia-ml.so.1` at runtime without CGO).

## Develop

Requires Go 1.27.1 (`go.mod` pins it; `GOTOOLCHAIN=auto` downloads it) and
golangci-lint v2.14.0 built with that Go version:

```sh
GOTOOLCHAIN=go1.27.1 go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.14.0

make vet          # go vet ./...
make test         # go test ./...
make test-race    # go test -race ./... (needs CGO + a C compiler, tests only)
make lint         # golangci-lint for linux and windows build tags
make build        # CGO_ENABLED=0 linux/amd64 + windows/amd64 into bin/
```

Tests validate every message the node produces against `../proto/v1` using
`github.com/santhosh-tekuri/jsonschema/v6`, which is imported only by tests
(`internal/schematest`) and never linked into the binary.

## Layout

```
cmd/topcoat-node/        CLI (install / uninstall / pair / run / version), real runner factory
internal/agent/          session loop, handshake, actions (incl. background download / benchmark),
                         game watcher reaction + cooldown, idle unload
internal/llamacpp/       pinned llama.cpp releases (releases.json + SHA-256), backend choice, installer
internal/hf/             Hugging Face GGUF download (resumable, SHA-256 verified)
internal/bench/          llama-bench runs, thread sweep, result parsing
internal/proc/           child processes (kill with the agent), RSS
internal/service/        systemd unit / Windows service install
internal/logfile/        rotating log file (Windows service)
internal/protocol/       wire types, canonical JSON + config_hash, UUIDv4
internal/store/          data dir files (identity, key, cached config, event queue)
internal/pairing/        pairing client
internal/backoff/        exponential backoff with jitter
internal/datadir/        data dir resolution
internal/providers/      GPUProvider, ModelRunner, ProcessWatcher, Capability, sysinfo
  runner/                llama-server runner (one process per model on 127.0.0.1)
  gpu/                   NVML (purego) + nvidia-smi, amdgpu sysfs, Windows DXGI + PDH
  sysinfo/               CPU/RAM incl. P/E-core topology
  watcher/               polling process watcher (name / folder-prefix rules)
  fake/                  fakes sharing one World (runner load raises fake VRAM use)
internal/schematest/     TEST-ONLY schema validation helper
```

## Adding a provider

Implement the interface in its package (`gpu.GPUProvider`, `runner.ModelRunner`,
`watcher.ProcessWatcher`, `capability.Capability`, `sysinfo.Provider`), keep it free of
CGO, and wire it in `cmd/topcoat-node/main.go`. Real implementations that have not been
tested on real hardware are marked `UNVERIFIED on real hardware`. A provider failing
must not crash the node: runner errors put the node into state `degraded`, GPU errors
yield an empty GPU list.
