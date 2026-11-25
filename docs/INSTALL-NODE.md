# Installing topcoat-node on real machines

Copy-paste steps to install the node agent as a service on the three stage-1 machines, pair
them with the hub, download a first model, run the benchmark from the GUI and load/unload
models. The hub must already run (see [QUICKSTART.md](QUICKSTART.md), part (a) and (b)).

Placeholders — replace them with your own values:

| placeholder | meaning |
|---|---|
| `hub.example.lan` | hostname or IP of the machine running the hub (e.g. `10.0.0.x`) |
| `ABCD-1234` | a pairing code from the GUI (**Knoten hinzufügen**), new one per machine, valid 15 min |

## What `install` does

`topcoat-node install --hub URL --code CODE --ca-fingerprint sha256:<hex>` pairs the node and registers a service that starts
at boot and restarts on failure:

| | Linux (Ubuntu) | Windows 11 |
|---|---|---|
| service | systemd unit `topcoat-node.service`, runs as system user `topcoat-node` (member of `video`/`render`) | Windows service `topcoat-node` running as the virtual account `NT SERVICE\topcoat-node` (member of "Performance Monitor Users"), delayed auto-start, restart after 5 s / 15 s / 60 s |
| binary | copied to `/usr/local/bin/topcoat-node` | copied to `C:\Program Files\topcoat-node\topcoat-node.exe` |
| data dir | `/var/lib/topcoat-node` (mode 0700, owned by the service user) | `C:\ProgramData\topcoat-node` (ACL: SYSTEM, Administrators and `NT SERVICE\topcoat-node` only) |
| logs | `journalctl -u topcoat-node -f` | `C:\ProgramData\topcoat-node\logs\topcoat-node.log` |

The data dir holds the node identity (`node.json`, private key `node.key`, pinned hub CA
`hub-ca.pem`), the cached config, the llama.cpp build
(`runtime\`), downloaded models (`models\`) and one log per loaded model
(`logs\llama-server-<model>.log`).

On first start the node picks the llama.cpp backend (NVIDIA → CUDA, AMD/Intel GPU → Vulkan,
no GPU → CPU), downloads the pinned **official** llama.cpp release **b11223** from GitHub and
checks it against the SHA-256 recorded in `node/internal/llamacpp/releases.json`. Until that is
done the node shows **Eingeschränkt** with the progress as runner detail; then **Verfügbar** and
an event **llama.cpp installiert**. Download sizes: Windows Vulkan 33 MB, Windows CPU 19 MB,
Linux CUDA 12.8 172 MB + 594 MB CUDA runtime libraries.

The node only opens outbound connections to the hub, GitHub (llama.cpp) and Hugging Face
(models). Each loaded model runs as its own `llama-server` process bound to `127.0.0.1`.

## Get the binary

GitHub → the repository → **Actions** → the newest green CI run on `main` → **Artifacts** →
`topcoat-node-test-<commit>` (a zip; ARCHITECTURE D123): `topcoat-node-windows-amd64.exe`,
`topcoat-node-linux-amd64` and `SHA256SUMS` (check with `sha256sum -c SHA256SUMS
--ignore-missing` or `Get-FileHash`, see [QUICKSTART.md](QUICKSTART.md) part (c)). Building from
source: [DEVELOPMENT.md](DEVELOPMENT.md).

The simplest way to get the right command: **Knoten hinzufügen** in the GUI shows the complete
`install` line with the binary name, the code, the CA fingerprint and the node name; pick the
node's operating system and enter the name first.

---

## 1. `node-amd` — Windows 11, Ryzen 7 3700X, Radeon RX 9070 XT (Vulkan)

**Prerequisite:** a current *AMD Software: Adrenalin Edition* driver (it ships the Vulkan
driver; RDNA 4 needs a 2025 or newer driver). Nothing else — no SDK, no LM Studio/Ollama.

In the GUI click **Knoten hinzufügen** and copy the code. Then open **PowerShell as
Administrator** (Start → type `powershell` → *Als Administrator ausführen*):

```powershell
cd $HOME\Downloads
Unblock-File .\topcoat-node-windows-amd64.exe        # unsigned download
.\topcoat-node-windows-amd64.exe version
.\topcoat-node-windows-amd64.exe install --hub https://hub.example.lan:8443 --code ABCD-1234 `
  --ca-fingerprint sha256:<hex> --name node-amd
```

Check:

```powershell
Get-Service topcoat-node                              # Status: Running
Get-Content "$env:ProgramData\topcoat-node\logs\topcoat-node.log" -Tail 30 -Wait
```

Expected log lines: `llama.cpp runtime selected release=b11223 backend=vulkan`, `session
established`, `llama.cpp runtime ready`. The GUI shows the node with the GPU **AMD Radeon RX
9070 XT** (16 GB VRAM bar), **Backend Vulkan**, **llama.cpp b11223**.

**Game watcher.** Open the node → **Konfiguration bearbeiten** and put your game folders
(Steam libraries) and/or executables into `watcher.rules`, then **Speichern**:

```json
"watcher": {
  "grace_seconds": 5,
  "cooldown_seconds": 60,
  "rules": [
    {"path_prefix": "C:\\Program Files (x86)\\Steam\\steamapps\\common"},
    {"path_prefix": "D:\\SteamLibrary\\steamapps\\common"},
    {"process": "eldenring.exe"}
  ]
}
```

(Backslashes are doubled in JSON.) When a matching process runs, the node unloads all models
after `grace_seconds`, shows **Spielt**, refuses loads and benchmarks, and returns to
**Verfügbar** 60 s after the last matching process ended.

**First model** (16 GB VRAM): **Modell herunterladen** →
Repo `bartowski/Qwen2.5-7B-Instruct-GGUF`, Datei `Qwen2.5-7B-Instruct-Q4_K_M.gguf` (≈ 4.7 GB).
Bigger option later: `bartowski/Qwen2.5-14B-Instruct-GGUF` / `Qwen2.5-14B-Instruct-Q4_K_M.gguf`
(≈ 9 GB).

---

## 2. `node-nvidia` — Ubuntu Server 26.04, Ryzen 5 3600X, RTX 2080 Super 8 GB (CUDA)

Tested in test 1 with Ubuntu Server 26.04 and driver 580. The steps are the same for other
NVIDIA cards (an RTX 3070 is planned).

**Prerequisite: NVIDIA driver ≥ 570** (the pinned build uses CUDA 12.8). The CUDA toolkit is
**not** needed — the node downloads the official CUDA runtime libraries (`libcudart`,
`libcublas`) together with llama.cpp.

```sh
sudo apt update
sudo apt install -y ubuntu-drivers-common
sudo ubuntu-drivers list --gpgpu          # shows e.g. nvidia-driver-580-server
sudo ubuntu-drivers install --gpgpu       # installs the recommended server driver
sudo reboot
```

After the reboot:

```sh
nvidia-smi        # must list the GPU (e.g. "NVIDIA GeForce RTX 2080 SUPER") and "CUDA Version: 12.8" or higher
```

If `nvidia-smi` says it cannot communicate with the driver and Secure Boot is on: the
installer asked for a MOK password — reboot, choose *Enroll MOK* in the blue screen and enter
it (or disable Secure Boot). If the listed driver is older than 570, install a newer one
explicitly, e.g. `sudo apt install -y nvidia-driver-580-server` (then reboot).

Install the node (new pairing code from the GUI):

```sh
cd ~/Downloads                        # wherever the binary is
chmod +x topcoat-node-linux-amd64
./topcoat-node-linux-amd64 version
sudo ./topcoat-node-linux-amd64 install --hub https://hub.example.lan:8443 --code ABCD-1234 \
  --ca-fingerprint sha256:<hex> --name node-nvidia
systemctl status topcoat-node --no-pager
journalctl -u topcoat-node -f         # Ctrl+C to leave
```

If the hub runs on this same machine, `--hub https://127.0.0.1:8443` works too (`127.0.0.1` is
always in the hub certificate).

Expected: `llama.cpp runtime selected release=b11223 backend=cuda`, after the download
`llama.cpp runtime ready`; in the GUI **Backend CUDA**, GPU **NVIDIA GeForce RTX 2080 SUPER**
with the 8 GB VRAM bar.

**First model** (8 GB VRAM): Repo `bartowski/Llama-3.2-3B-Instruct-GGUF`, Datei
`Llama-3.2-3B-Instruct-Q4_K_M.gguf` (≈ 2 GB, fast). Next:
`bartowski/Qwen2.5-7B-Instruct-GGUF` / `Qwen2.5-7B-Instruct-Q4_K_M.gguf` (≈ 4.7 GB, fits with
an 8k context).

Models placed manually (e.g. `/srv/models/*.gguf`) can be listed by adding the directory to
`runner.model_dirs` in the config; the service user needs read access. Such files are never
deleted by the node.

---

## 3. `node-laptop` — Windows 11, i7-1255U (2 P + 8 E cores), Iris Xe (CPU vs Vulkan)

**Prerequisite:** a current Intel graphics driver (Intel Arc & Iris Xe driver from Intel or the
laptop vendor) — it contains the Vulkan driver.

Install exactly like `node-amd` (elevated PowerShell), with `--name node-laptop`. The node
selects **Vulkan** by default (Iris Xe is an Intel GPU); the GUI shows the CPU as
**10 Kerne (2 P + 8 E), 12 Threads** and the Iris Xe with a small dedicated VRAM bar plus
**Geteilter Speicher** (the iGPU uses system RAM).

**First model:** Repo `Qwen/Qwen2.5-1.5B-Instruct-GGUF`, Datei
`qwen2.5-1.5b-instruct-q4_k_m.gguf` (≈ 1.1 GB). Optional:
`bartowski/Llama-3.2-3B-Instruct-GGUF` / `Llama-3.2-3B-Instruct-Q4_K_M.gguf`.

**CPU vs Vulkan.** Run a benchmark (see below). On nodes with an integrated GPU every model is
measured on Vulkan **and** on the CPU with 2, 6, 10 and 12 threads (P-cores only, P + half the
E-cores, all cores, all threads). The fastest CPU thread count is stored as `runner.threads`
automatically. If the CPU rows are faster than the Vulkan row, switch the backend: **Konfiguration
bearbeiten** → `"backend": "cpu"` in `runner` → **Speichern** (the node downloads the CPU build
and restarts its runner; loaded models are stopped).

**Big models.** An integrated GPU has no partial offload: a model loads on Vulkan only if it fits
the shared memory completely (minus a safety margin); anything bigger, e.g. a 30B, goes straight to
the CPU. The node installs the CPU build next to Vulkan in the background on such a machine and uses it
for these models (falling back to `--device none` of the Vulkan build while it is missing).

**Laptop settings** (left to you; the node does not change them):

```powershell
# Elevated PowerShell. Never sleep / hibernate on AC power, keep running with the lid closed on AC.
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
```

- Power mode: *Einstellungen → System → Netzbetrieb und Akku → Energiemodus* → *Beste Leistung*
  while plugged in.
- Battery charge limit (e.g. 80 %) for an always-plugged laptop: set it in the BIOS/UEFI or the
  vendor tool — not handled by topcoat.
- Wi-Fi power saving can drop the hub connection; the node reconnects on its own.

---

## 4. Docker host VM — tool node (Ubuntu, read-only, E2-04)

A server that runs Docker services becomes a **tool node**: no models, no GPU work, only the
read-only capabilities of docs/PROTOCOL.md "Tool nodes" (containers and their logs, compose
projects and their files, journal, systemd units, disks, RAM, load). It changes nothing on the
machine. Target size: ≤ 30 MB RAM idle. Measured on a real Docker VM (21 containers, 11 compose
projects): **16 MB RSS** (manual check T10).

```bash
# on the VM (Ubuntu), with a pairing code from the GUI; --name is the node's name in the GUI
sudo ./topcoat-node install --role tools --hub https://hub.example.lan:8443 --code ABCD-1234 \
  --ca-fingerprint sha256:<64 hex digits> --name vm-docker1
```

The node is `available` at once (no runner to wait for) and shows **Fähigkeiten** on its page
instead of models. Then three things give it something to look at:

**1. Docker — least privilege: a read-only socket proxy, not the `docker` group.** Membership in
the `docker` group is root on the machine, also for a program that only ever sends `GET` — and this
program is reachable from the hub. So the node talks to a small proxy on the **loopback** that
forwards only the read endpoints and refuses every `POST`/`PUT`/`DELETE`:

```yaml
# /opt/docker-proxy/compose.yaml — image and variable names verified on a real Docker VM
# (manual check T1); the test below is still what counts after any image update
services:
  docker-proxy:
    image: tecnativa/docker-socket-proxy
    restart: unless-stopped
    environment:
      CONTAINERS: 1      # list, inspect, logs of containers — and nothing else
      POST: 0            # no write call at all
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro   # ":ro" does NOT limit API calls; the proxy does
    ports:
      - "127.0.0.1:2375:2375"
```

Test it **before** you point the node at it — a read must work, a write must be refused:

```bash
curl -s http://127.0.0.1:2375/containers/json | head -c 200      # JSON
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:2375/containers/some-name/restart   # 403
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:2375/images/json                               # 403
```

Then tell the node where it is: GUI → node page → **Konfiguration bearbeiten** → add
`"tools": {"docker_host": "tcp://127.0.0.1:2375"}`. The node accepts only a unix socket or a
loopback address here; it never sends Docker calls over a network. (Reading the socket directly,
`"docker_host": "unix:///var/run/docker.sock"`, needs the `docker` group and is **not**
recommended.)

**2. Journal and systemd units.** `journalctl` for other units needs a group:
`sudo usermod -aG systemd-journal topcoat-node && sudo systemctl restart topcoat-node`.
`systemctl status`-type reads (`service.status`, `service.failed`) need nothing.

**3. Compose files — denied until you release a directory.** Without `compose_roots` the node reads
**no** compose or `.env` file and lists none: `compose.file` answers "no directories are released
…", and `compose.projects` shows names, services and state but no paths. Release the stack
directory (nothing else) in the node's config: GUI → node page → **Konfiguration bearbeiten**:

```json
"tools": {"docker_host": "tcp://127.0.0.1:2375", "compose_roots": ["/opt/stacks"]}
```

The node then reads, inside those directories only, files named like `compose*.yaml`,
`docker-compose*.yml`, `.env`, `*.env` (backup copies like `.env.bak-…` match the name rule too and are listed), and only
where its service user may read them. Give it read access to that directory:

`setfacl` is not installed on a minimal Ubuntu Server: `sudo apt install acl` first.

```bash
sudo setfacl -R -m u:topcoat-node:rX /opt/stacks && sudo setfacl -R -d -m u:topcoat-node:rX /opt/stacks
```

A `.env` file holds secrets: the node **masks** values whose key names a secret and anything shaped
like a token or key before an output leaves the machine, but the file is readable for the node's
user — release the directories you want the assistant to see, not more.

**Who may use it.** The admin on the GUI may run every capability. Other users (and every API
key) need a grant: node page → **Wer was nutzen darf**. Every call, allowed or refused, is listed
under **Letzte Aufrufe** and in the hub's `tool_calls` table.

**The inventory.** `inventory.snapshot` (what runs on this machine: Docker containers and systemd
services) needs nothing beyond what the steps above already give the node: it reads through the same
read-only Docker proxy and `systemctl`. The hub reads every tool node every five minutes
(`TOPCOAT_INVENTORY_INTERVAL_SECONDS`, `0` = only on demand); page **Inventar**. A user sees a node's
inventory only with a grant for `inventory.snapshot` (or `*`) on it (manual check).

### 4b. Optional: restarts after your click (E3-01b)

Off by default. When you switch it on, the assistant can *propose* restarting one container or one
systemd unit; the restart runs only after **you** click "Ausführen" on that one proposal (the GUI,
never an API key). Three things are needed, and each is its own switch:

**1. A second proxy — the only one that may write, and it can do exactly one thing.** The read
proxy above stays exactly as it is. For the write path the generic socket-proxy image is **not**
enough: its `ALLOW_RESTARTS` switch lets `stop` and `kill` through as well, and with `POST: 1`
`create` and `exec` reached the Docker daemon in the hardware test (manual check R1). So the node
binary brings its own small proxy, `topcoat-node docker-restart-proxy`: it forwards
`POST /containers/<name>/restart[?t=N]` and answers **403** to every other request (other methods,
paths, query parameters, bodies); it listens on the loopback only. Run it as its own service, under
a user of its own that may use the Docker socket and nothing else:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin --groups docker topcoat-dockerw
sudo tee /etc/systemd/system/topcoat-docker-restart-proxy.service >/dev/null <<'EOF2'
[Unit]
Description=sloppy-topcoat Docker restart proxy (POST /containers/<name>/restart only)
After=docker.service

[Service]
User=topcoat-dockerw
ExecStart=/usr/local/bin/topcoat-node docker-restart-proxy --listen 127.0.0.1:2376 --docker unix:///var/run/docker.sock
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ProtectKernelModules=true
RestrictAddressFamilies=AF_UNIX AF_INET

[Install]
WantedBy=multi-user.target
EOF2
sudo systemctl daemon-reload && sudo systemctl enable --now topcoat-docker-restart-proxy
```

(Adjust the path of the binary to where you put it.) The `docker` group is root on the machine, so
the point of this unit is the code behind it: a fixed whitelist you can read in
`node/internal/restartproxy`, tested in CI against everything that matters (create, exec, stop,
kill, start, images, escaped and dot-segment paths, extra query parameters). Test it on your
machine as well, before you point the node at it:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:2376/containers/<throwaway>/restart   # 204
for p in containers/create containers/<throwaway>/exec containers/<throwaway>/stop containers/<throwaway>/kill; do
  curl -s -o /dev/null -w "$p %{http_code}\n" -X POST http://127.0.0.1:2376/$p                        # all 403
done
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:2376/containers/json                         # 403
```

**2. Tell the node what may be restarted — nothing is the default.** GUI → node page →
**Konfiguration bearbeiten**: in `tools` add (glob patterns, exact names are better):

```json
"tools": {"docker_write_host": "tcp://127.0.0.1:2376",
          "restart_containers": ["web", "app-*"],
          "restart_units": ["nginx.service"]}
```

`restart_containers` also decides what a **stack restart** (`compose.restart`) may touch: every
running container of the project must match, otherwise the whole stack is refused and nothing is
restarted — so list the stack's containers (`"shop-*"`), and **leave the hub's own containers and
this node's proxy out of every list you do not mean to restart**.

A node with `restart_units` needs the right to restart exactly those units, e.g. a polkit rule or a
sudoers drop-in for `topcoat-node` limited to `systemctl restart nginx.service` — not a blanket
`sudo`. Without these lists the node does not offer the restart capabilities at all.

**3. Allow the action and the capabilities in the same config, then switch the hub on.** Add
`"capability.execute"` to `allowed_actions` and `"docker.restart"` / `"service.restart"` to
`capabilities` (and `"compose.restart"` for stacks); on the hub set `TOPCOAT_PROPOSALS_ENABLED=true`. The admin on the GUI may confirm;
another user needs a grant that **names** the capability (`*` does not cover restarts). Start with
one test VM and one throwaway container.

### 5. Connectors: Uptime Kuma and Omada (tool node)

A tool node can also read **Uptime Kuma** and the **Omada controller** for the assistant. Their
credentials are in **one file on the tool node** — nowhere else (not in the hub's configuration, not
in a chat). You create it; the node only reads it.

**Uptime Kuma.** *Settings → API Keys → Add API Key* (give it a name and an expiry you can live
with); copy the key once. The node asks `/metrics` with it (HTTP basic auth, the key as the
password). **Omada.** On the controller (5.12 or newer): *Settings → Platform Integration → Open
API → Add New App*, access mode **Client Credentials**, role **Viewer** (never Admin: Viewer is what
keeps this read-only even if something went wrong); note *Omada ID*, *Client ID*, *Client Secret*.

```bash
# as root on the tool node (Linux); the node's user owns the file, nobody else can read it
sudo install -m 600 -o topcoat-node -g topcoat-node /dev/null /etc/topcoat-node/connectors.json
sudoedit /etc/topcoat-node/connectors.json
```
```json
{
  "uptime_kuma": {"url": "http://10.0.0.x:3001", "api_key": "uk1_…"},
  "omada": {"url": "https://10.0.0.x:8043", "omada_id": "…", "client_id": "…",
            "client_secret": "…", "site": "Default", "ca_file": "/etc/topcoat-node/omada-ca.pem"}
}
```
Either section may be left out. **`api_key` is optional:** an Uptime Kuma that runs without its own
login (its settings say *API keys are disabled because authentication is disabled*) serves
`/metrics` openly. Leave `api_key` out (or empty) and the node sends **no** `Authorization` header;
then release `/metrics` in the reverse proxy in front of Kuma for the tool node's address only (and
nothing else), e.g. `{"uptime_kuma": {"url": "https://kuma.example.lan"}}`. If Kuma does ask for a
login the capability says so and names `uptime_kuma.api_key`. A controller with its own certificate: put its certificate (PEM)
in `ca_file`; `"insecure_skip_verify": true` switches the check off for that section (only on a
LAN you control). Then `sudo systemctl restart topcoat-node`; the node log says *connectors
configured*. **If the file is readable by others, is a link or belongs to someone else, the node
refuses it** and logs why (`chmod 600`, `chown topcoat-node`). On Windows the file is
`%ProgramData%\topcoat-node\connectors.json`; give it an ACL for the service account and the
administrators only (the permission bits mean nothing there; this part is UNVERIFIED).

**Keycloak (identity view, E3-12):** in Keycloak create a client in your realm, *Client
authentication* on, *Service accounts roles* on, no login flows. Under *Service account roles* add
only these roles of the `realm-management` client: `view-users`, `query-users`, `query-groups`,
`view-clients`, `query-clients`. Put its secret in the section
`"keycloak": {"url": "https://10.0.0.x:8443", "realm": "home", "client_id": "topcoat-reader",
"client_secret": "…"}` (a Keycloak behind a path such as `/auth` needs it in the URL). Restart the
node; it offers `keycloak.snapshot` and `keycloak.user`, read-only.

Then open the node's page, switch the new capabilities on (`kuma.monitors`, `omada.*`, `keycloak.*`; new tool
nodes have them on) and grant them to the users who may use them (*Wer was nutzen darf*). The file
can hold a different path via *Konfiguration bearbeiten*: `"tools": {"connectors_file": "/path"}`.

### 6. Knowledge folders: Markdown and text notes (tool node, E2-03)

The knowledge base reads your notes **through a tool node**, from directories you release — none by
default. Release the folders (nothing else) in the node's config: GUI → node page →
**Konfiguration bearbeiten**:

```json
"tools": {"docs_roots": ["/srv/notes"], "docs_exclude": ["private-*"]}
```

Only with a released directory does the node offer `docs.list` and `docs.read` (switch them on in the
node's page if it is an older one). The node lists and reads only `.md`, `.markdown` and `.txt`
files, never follows a link, never leaves the directory, and answers a file the exclude list names
like a file that does not exist. **The exclude list cannot be switched off**: a file or folder whose
*name* matches `*SENSIBEL*`, `*ZUGANGSDATEN*`, `*.key`, `*.pem`, `*.env`, `.env*`, `*password*`,
`*passwort*`, `*secret*`, `*credential*`, `*.kdbx`, `id_rsa*`, `id_ed25519*` or `.git` (any segment of
the path, upper or lower case) is not listed, not read, not counted by name; `docs_exclude` only adds
globs. The node's user needs read access to the folder (`setfacl` as in step 3). Files larger than
1 MiB are not indexed. Then add the source in the GUI under **Wissen** (admin). Do not release a
folder that holds credentials under ordinary names — the list works on names, and what a note says
inside is only filtered by the secret rules (chunks that look like a password, key or token are not
indexed; the GUI shows which files had such chunks, never their content).

### 7. Web search and page reading (tool node, E3-04)

`web.search` asks a **SearXNG** instance you run (on the LAN, next to the node); `web.fetch` reads
one public page. Both are off until you configure them (node page → **Konfiguration bearbeiten**):

```json
"tools": {"searxng_url": "http://10.0.0.x:8080", "web_fetch": true}
```

- **SearXNG must answer JSON:** in its `settings.yml` set `search: { formats: [html, json] }` and
  allow the node's address (SearXNG's `server.limiter` / `botdetection` may answer 403 to an
  unknown client). A 403 or an HTML answer is reported by the capability with that hint. The URL
  comes from the config only; a model can never aim `web.search` somewhere else.
- **`web.fetch` is the one capability that makes the node call an address a model chose**, so it is
  opt-in (`web_fetch: true`) and built defensively: http/https only, ports 80/443/8080/8443, no
  user name in the URL, **no private, loopback, link-local, carrier-grade-NAT or reserved address**
  (checked on every answer of the name lookup and again when the connection is made, for each
  redirect too, at most 3), text only (HTML, plain, JSON, XML), at most 1 MiB read and 20000
  characters returned, secrets in the text masked like a log.
- **Nothing private goes out, checked on the node too:** `web.search` and `web.fetch` refuse (error
  code `private_data`, before anything is sent) an input with an IP or MAC address, an e-mail
  address, a secret, an internal-looking domain (`.lan`, `.local`, `.internal` …) or a name of the
  operator: the node's own host name and domain and the search domains of its resolver are known
  to it; add your other hosts, users and domains with `"private_names": ["alpha", "alice",
  "example.lan"]` in `tools` (whole words, at least 3 characters). Refused calls are in the hub's
  audit log without the value.
- **Nothing private goes out:** the hub refuses a query or URL that contains an IP or MAC address,
  an e-mail address, a secret, the name of one of your nodes or users, a private domain name or a
  domain from `TOPCOAT_REDACT_DOMAINS` (the same detection as the redaction layer for Claude,
  ARCHITECTURE D33). The model is told and can ask again about the public thing only.
- Results are **untrusted web content**: fenced as data like every tool output; what a model takes
  from them is tagged `[web]` with the source URL in the evidence.
### 8. Collecting checks: `health.check`, `logs.search`, `certs.expiry`, `net.check` (tool node, E3-04)

Four read-only capabilities that answer in **one call** what took six (a check with six calls was
about 100 s):

- **`health.check`** and **`logs.search`** need nothing but what the node already reads (Docker
  API, `journalctl`, `systemctl`, `/proc`): failed units, containers that are unhealthy, restarting,
  killed for memory or exited with an error, file systems over a threshold (default 80 %), RAM, swap,
  load and the error lines of the last hour per container and per unit — compact (a few KB), a part
  that cannot be read is listed under `unavailable` instead of failing the call; and a text or
  regular expression searched in the last lines of every container's log and in the journal, with a
  time range, per-source counts and at most 200 lines (bounded: 2000 lines per container, 3000
  journal lines — the answer says what was scanned).
- **`certs.expiry`** and **`net.check`** look at **hosts you list in the node's config** — a caller
  never names a host, so a model cannot turn the node into a port scanner. Without a list the
  capability is not offered. `net.check` pings with **unprivileged ICMP datagram sockets** (no `ping`
  program, no capability on the binary, no shell): the node's service account must be allowed to open
  them. Check with `sysctl net.ipv4.ping_group_range` (`1 0` = nobody; a Debian/Ubuntu default may
  be `0 2147483647`). If the service account's group is outside the range, allow it, e.g. for every
  group (a `/etc/sysctl.d/90-ping.conf` file with `net.ipv4.ping_group_range = 0 2147483647`, then
  `sudo sysctl --system`) or just for the node's group (`... = 995 995`, the account's numeric GID from
  `id topcoat-node`). Without the right the capability does not fail: the ping rows say *ping not
  possible (permissions)* and the result lists them under `not_checked` — it is no statement about the
  host; the TCP port checks still run.

```json
"tools": {
  "cert_targets": ["proxy.example.lan", "10.0.0.5:8443"],
  "net_targets": [{"name": "gateway", "host": "10.0.0.1", "ping": true, "ports": [53, 443]}],
  "dns_names": ["status.example.lan", "example.org"]
}
```
`certs.expiry` reads the certificate of each target (also an invalid one), reports subject, issuer,
expiry, days remaining and whether the chain is valid for the name, soonest first, and flags what
expires within `warn_days` (default 21). `net.check` resolves the names with the node's resolver, pings
(3 echo requests) and TCP-connects to the ports; problems are listed first. In `health.check` identical
error messages are summed up (numbers, addresses and ids ignored): each unit or container shows its
three most frequent messages with their counts; a journal read stops at 500 lines, then the count is
given as *at least*.

### 9. Read-only shell (tool node, `shell.read`)

For what the fixed capabilities do not cover (look into a config file, `ss`, `ps`, `find`, `grep`
in `/var/log`): the assistant may run **one shell command** on the server — as an operating-system
user that can only read. Off until you switch it on, per node. Docker state stays with
`docker.*`; the shell user is not in the `docker` group (that is root).

**Why "read-only" is the OS's job, not a command list.** A list of allowed commands is either too
short (the assistant cannot look) or too long (`find -exec`, `awk system()` … run anything). So the
node does not filter commands. It hands the command line to a systemd *socket unit* that runs it as
`topcoat-ro`: no sudo, no `docker`/`sudo`/`shadow` group, no write permission anywhere
(`ProtectSystem=strict`), no network (`RestrictAddressFamilies=AF_UNIX AF_NETLINK`: nothing can be
sent out, which also stops a log line that talks the assistant into "curl this"), no `/home`,
`/root`, node data or SSH host keys, 256 MB / 64 tasks, killed after 70 s. A command that tries to
change something fails at the operating system. The node (with `NoNewPrivileges`) stays
unprivileged: it only connects to a socket that only its group may open. Sudo is not used.

**Set up (root, once per server — the script is idempotent):**

```bash
sudo bash setup-readshell.sh              # user, socket, units
sudo bash setup-readshell.sh audit        # what could the read user still read? READ THE LIST
sudo bash setup-readshell.sh status
```

Options (environment variables): `NODE_GROUP` (default `topcoat-node`), `READ_GROUPS` (default
`systemd-journal`; `adm` adds `/var/log/*`), `HIDE_PATHS="/srv/compose /opt/app"` (directories the
shell cannot see at all). Get the script from `deploy/readshell/setup-readshell.sh`.

**What the read user can still read — look at it before you switch it on.** "Read-only" does not
mean "harmless to read": anything world-readable is readable. The `audit` run lists files named like
credentials and readable text files that look like they hold one (names only, never contents):
typically `.env` files of compose projects, application configs with passwords, world-readable
backups. Fix each: `chmod o-r FILE` or put the directory in `HIDE_PATHS`. The node's secret filter
masks most credentials in the output as well, but it is the second line of defence. Processes
and their command lines are visible (`ps`), the journal too (group `systemd-journal`).

**Switch it on (GUI, node page → config; or `PUT /api/nodes/{id}/config`):** in `tools` add
`"shell_socket": "/run/topcoat-readshell.sock"`, and add `"shell.read"` to the node's
`capabilities`. Admins may use it then; every other user needs a grant that **names** it
(`"*"` does not cover it). Every call is audited on the hub (command and output cut); the node
logs the command in its journal.

**Way back:** remove `"shell.read"` from the capabilities (nothing can run it any more), then
`sudo bash setup-readshell.sh uninstall` (removes units and wrapper; `userdel topcoat-ro` removes
the user). UNVERIFIED on real hardware: the units were written for systemd 249+ and tested in the
cloud only against the protocol (manual check).

### 10. Windows read capabilities (`windows.*`)

For troubleshooting a Windows PC (a node with a model runner too, e.g. `node-amd`): the assistant can
read settings and state — OS and updates, power plan and sleep states, event log, services, drivers
and problem devices, disks, network, startup items, installed programs, Defender, top processes.
Off until you switch it on, per node.

**How it differs from `shell.read`.** There is no free shell on Windows: the OS has no
`systemd`-style sandbox, and PowerShell cannot be made read-only by a command list. Instead the node
contains eleven fixed PowerShell scripts (`windows.system`, `.power`, `.events`, `.services`,
`.devices`, `.storage`, `.network`, `.startup`, `.programs`, `.defender`, `.processes`). A caller
only picks one and gives a few validated parameters (log name, time span, a service name …); they
reach the script as environment variables, never as script text. The scripts use only `Get-*`
cmdlets and `powercfg` with read switches (a test enforces the verbs). The node starts the local
`powershell.exe` itself, as its own service account: **no listening port, no WinRM, no SSH.**

**What these scripts do not read:** the Security log, arbitrary registry paths, environment
variables, process command lines. **File contents are not read by these scripts either.** Since D89
files can be read by a separate, opt-in capability that is not a script: `files.read` inside folders you
release one by one, see section 10a. User profiles and browser data stay closed there too. Output goes
through the same secret mask as every other capability and is cut at 64 KiB.

**Set up (elevated PowerShell on the PC, once):** the service account is a low-rights virtual
account. For the event log it needs the builtin group *Event Log Readers* (the name is localized,
so it is looked up by SID):

```powershell
$g = (New-Object Security.Principal.SecurityIdentifier 'S-1-5-32-573').Translate([Security.Principal.NTAccount]).Value.Split('\')[1]
net localgroup "$g" "NT SERVICE\topcoat-node" /add
Restart-Service topcoat-node
```

Everything else works with the rights the account already has. If a section lacks a right, the call
still answers and that section holds `{"error": "…"}` — that tells you what to grant, nothing is
guessed.

**Switch it on (GUI: the node's page, "Windows checks", one checkbox for all eleven; or the config
editor / `PUT /api/nodes/{id}/config`):** the checkbox adds the `windows.*` names to the node's
`capabilities` and makes sure `capability.invoke` is in its `allowed_actions`. By hand: add the
names you want to `capabilities`, and, for a node that is not a tool node, `capability.invoke` to
`allowed_actions`. A compute node (for example `node-amd`) shows its capabilities table on its page
once any capability is switched on. A Windows node build offers them in its inventory
without any other setting; they run only once they are in `capabilities`. Admins may
use them then; every other user needs a grant that **names** them (`"*"` does not cover them).
Every call is audited on the hub.

**Way back:** remove the `windows.*` names from `capabilities` ;
`net localgroup "$g" "NT SERVICE\topcoat-node" /delete`. UNVERIFIED on real hardware: the scripts
were written and tested in the cloud against a fake PowerShell only (manual check).

### 10a. Reading files of a host: `files.list`, `files.read` (HOST-01, D89)

For troubleshooting: the assistant reads logs and configuration files, on Windows and on Linux. It
reads only. This is **not** a shell and not PowerShell: the node opens the file itself, in Go, so
nothing a caller writes is ever executed. Off until you release a folder.

**Release folders (GUI: the node's page, "Reading files on this machine").** Folders are per node and
stored in `config.tools.read_roots`; saving them also switches `files.list` and `files.read` on for the
node (and `capability.invoke` if it was missing). With no folder the node does not offer the two
capabilities at all. The page suggests Topcoat's own log folder (`C:\ProgramData\topcoat-node\logs`,
Linux `/var/lib/topcoat-node/logs`) and nothing else; a suggestion only fills the
input, nothing is released until you save. The node's own `config.json`, `node.json` and
`connectors*` stay closed even if you release the data folder itself. A user's profile folder (`C:\Users\<name>`, `/home/<name>`)
is not suggested and stays closed unless you add one of its folders on purpose. The same by hand: a
PUT of the node config with `"tools": {"read_roots": ["C:\\ProgramData\\topcoat-node\\logs"]}` and
`files.list`, `files.read` in `capabilities`.

**What it does with a path.** It resolves it to the real place (links, junctions, Windows short names)
and refuses it unless that place is inside a released folder; it checks again after the file is open,
and a link that points out of the folder is refused. Refused outright: `..`, relative paths, UNC
(`\\server\share`) and device paths (`\\?\`, `\\.\`), alternate data streams (`file:stream`),
reserved names (`CON`, `NUL`, `COM1` …), short (8.3) names, binary files and UTF-16 files. On Windows
case does not matter. **Names that mark a secret are refused even inside a released folder:**
`*.key`, `*.pem`, `*.pfx`, `*.p12`, `*.kdbx`, `.env` and `.env.*`, `*credential*`, `*secret*`,
`*password*`, `id_rsa*` and the other SSH key names, `connectors*` (the node's own credentials file),
`.ssh`, `.aws`, `.kube`, `.docker`, browser profiles (`User Data`, Firefox and Chrome folders,
`Login Data`, `Cookies` …), `Protect` and `Crypto` (Windows keys). The list cannot be shortened by
configuration. What passes goes through the same secret mask as everything else (masked **before** a
`grep` or a `tail` looks at it, so searching for a password finds nothing) and is cut: a page of at
most 48 KiB, a `tail` of at most 500 lines from the last 256 KiB.

**Rights.** The node reads as its own service account and gets no new rights. If that account cannot
read a file, the answer says "permission denied for the account the node runs as". **What to grant is
your decision.** On Windows the virtual account `NT SERVICE\topcoat-node` can read its own data folder
and whatever *Users* may read; give it read access to one more folder only if you want that folder
readable, for example:

```powershell
icacls "D:\Logs\app" /grant "NT SERVICE\topcoat-node:(OI)(CI)R"
```

On Linux the service user needs read access to the folder (group membership or an ACL). Do not release
a folder you would not show the assistant.

**Who may use it.** Admins on the GUI may; every other user needs a grant that **names** `files.read` /
`files.list` for the node (`"*"` does not cover them, like `shell.read` and `windows.*`). The assistant
treats everything it reads as data, never as instructions. Every call is audited in `tool_calls`.

**Way back.** Remove the folders on the node's page (or `read_roots` and the two names in
`capabilities`); the node stops offering the capabilities at the next config push.

**UNVERIFIED on real hardware.** Written and tested in the cloud on Linux. The Windows rules for path
strings are tested there; what Windows itself does (junctions, short names, case, streams) was not
seen. Steps for `node-amd`:

1. Install a node build with this change; on the node's page save `C:\ProgramData\topcoat-node\logs`.
2. In the GUI (node page, run a capability) or through the assistant: `files.list` without a path
   (names the released folders), `files.list` on the log folder, `files.read` of `topcoat-node.log`
   with `tail: 20`, then with `grep: "error"`.
3. Make a junction out of the folder and expect a refusal:
   `mkdir D:\x; cmd /c mklink /J C:\ProgramData\topcoat-node\logs\out D:\x`, then `files.list` on
   `...\logs\out` must be refused and the junction must not appear in the listing.
4. Expect refusals for: `C:\ProgramData\topcoat-node\node.key`, `...\logs\topcoat-node.log:stream`,
   `\\?\C:\ProgramData\topcoat-node\logs\topcoat-node.log`, `...\logs\NUL`, the 8.3 name of the log
   file (`dir /x` shows it) and the same path in other case (this one must work).
5. Read a file the service account cannot read; the answer must say permission denied.
6. Run the Go tests that only run on Windows: `cd node; go test ./internal/tools -run WindowsFiles -v`.
7. Switch on the Windows checks on the same page and run each `windows.*` once (section 10).

## 11. Dev node — the dev workshop (E6-01, D52) — UNVERIFIED on real hardware

A dev node is a tool node that holds clones of the repositories you registered under
"Repositories" in the admin area, and lets the assistant read, search and change them in a scratch
copy and run their tests. It never merges, signs or uploads anything.

1. Pair it as `--role dev` (`topcoat-node install --hub … --code … --name dev-<host> --role dev`).
   It needs `git` (2.31 or newer; the token is passed through the environment of the git process).
   The rootless Docker daemon of step 3 belongs to its own account, and only that account can reach
   its socket: install with `--service-user <account>` (Linux) and the service runs as that
   existing account instead of the `topcoat-node` user (the installer never creates the account and
   never changes its groups; the data dir is handed to it). The updater still runs as root.
2. In the node's page switch the capabilities on (the defaults are all on) and set
   `config.tools.workshop` if the defaults do not fit: `dir`, `allowed_hosts` (default only
   `github.com`), `max_repo_mb`.
3. For `repo.test`, give the workshop **a Docker or Podman daemon of its own** (rootless, for a
   dedicated user), not the daemon that runs your services, and pull the images once by hand:

   ```json
   "workshop": {
     "runner": {
       "type": "docker", "docker_host": "unix:///run/user/1500/docker.sock",
       "images": {"make-check": "topcoat-ws-build:1", "pytest": "python:3.12-slim"},
       "network": "workshop-net", "network_templates": ["pytest", "npm-test"],
       "memory_mb": 2048, "cpu_milli": 2000, "timeout_seconds": 600
     }
   }
   ```

   The `pytest` template for the hub's own repository needs an image with PostgreSQL and the hub's
   dependencies; it also holds Node and the packages of `web/`, so the same run ends with the web
   checks (lint, typecheck, tests). It is built on the dev node, as the dedicated user, from the
   repository root (build context: `hub/pyproject.toml`, `hub/uv.lock`, `hub/README.md`,
   `web/package.json`, `web/package-lock.json`, `deploy/workshop/`) and has to be rebuilt when
   `hub/uv.lock` or `web/package-lock.json` changes:

   ```sh
   docker build -f deploy/workshop/pytest/Dockerfile -t topcoat-pytest:2 .
   ```

   Then name it in `images` (`"pytest": "topcoat-pytest:2"`). For other repositories a plain
   `python:3.12-slim` is enough.

   `repo.test_start` starts the same run in the background (`repo.test_status` shows it,
   `repo.test_cancel` stops it). It ends a run that printed nothing new for `idle_seconds`
   (default 180) or that hit `timeout_seconds` (default 1800 for a background run, 600 for
   `repo.test`); a long run is never cut because a call timed out.

   `network` is a network of that daemon which your firewall limits to the package sources; a
   template not listed in `network_templates` runs with no network at all. If the node itself runs
   in a container, `host_dir` is the clone directory as the daemon sees it.
4. Register the repository (Repositories, mode `test-branch` to let the assistant change it) and
   ask in the chat; the group "workshop" appears next to the other tools.
5. Optional, builds of the node (`repo.build_node`): add `"build-node": "golang:1.27"` to `images`
   (pull it once by hand) and `"build-node"` to `network_templates` (Go downloads its modules). The
   assistant then cross-builds Linux and Windows binaries from the worktree and keeps them,
   **unsigned**, under `<workshop dir>/out/<repo>/<version>/` on the dev node together with
   `SHA256SUMS` (`repo.build_list` shows them). Copy them to your PC, sign them there
   The same way, the browser of the workshop (`browser.*`, E6-02): build the image once from the repository
   root (`docker build -f deploy/workshop/browser/Dockerfile -t topcoat-workshop-browser:1 .`) and add
   `"browser": "topcoat-workshop-browser:1"` to `images`. It needs no `network_templates` entry: the
   browser container never has a network. The assistant can then open the built GUI of a registered
   repository, resize it, operate it, check its layout by measurement and judge animations as data; see
   `deploy/workshop/browser/README.md`.
   (`topcoat-sign`) and upload them yourself; nothing in the workshop signs or uploads.

## Direct model transfer (E2-07, D51)

A node can send a model file straight to another node (hub: "Copy to..." / "Move to..."). The hub
only coordinates; the bytes never pass through it.

- **What opens when.** Nothing listens on a node by default. Only after the hub sends `models.send`
  (and the action is in the node's `allowed_actions`) does the **source** node open one TCP port, with
  TLS 1.3 and an ephemeral self-signed certificate that the target pins. The port closes after the file
  was delivered once, on an error, when a game starts, or after `config.transfer.ttl_seconds` without
  progress (default 600). The **target** node only connects outward, it opens no port.
- **Who may connect.** One peer: the address list and the one-time token the hub gives the source. The
  socket serves exactly one file (`GET /model`) from `models/` or a `model_dirs` entry; split GGUFs
  are refused. The token is never logged.
- **Port range.** The first free port of `config.transfer.port_min` .. `port_max` (default
  `47000`-`47099`). Both nodes of a transfer must be able to reach each other's subnet (routed
  networks and VPNs work; there is no relay and no NAT hole punching). If no address of the source
  answers, the transfer ends with `no_route`.
- **Windows firewall.** `topcoat-node install` adds the inbound rule `topcoat-node-transfer` (best
  effort, **UNVERIFIED on real hardware**): the program, profile *Private* and *Domain*, remote address
  *LocalSubnet*, TCP `47000-47099`. `uninstall` removes it. Another range in `config.transfer`, or
  nodes in different subnets, need the rule changed by hand (elevated PowerShell):

  ```powershell
  netsh advfirewall firewall delete rule name=topcoat-node-transfer
  netsh advfirewall firewall add rule name=topcoat-node-transfer dir=in action=allow enable=yes program="C:\Program Files\topcoat-node\topcoat-node.exe" profile=private,domain remoteip=10.0.0.0/24 protocol=TCP localport=47000-47099
  ```

  Without the rule only receiving works; sending then fails with `no_route` on the target.
- **Linux.** The installer changes no firewall rules and only prints a note. If `ufw` or `firewalld`
  is active on a node that should send models, allow the range from your network, for example
  `ufw allow from 10.0.0.0/24 to any port 47000:47099 proto tcp`.
- **Disk.** The target refuses a transfer unless file size + 1 GiB are free on the model drive
  (`insufficient_disk`). An interrupted transfer continues from `<name>.gguf.part` the next time.

## Benchmark from the GUI

1. Open the node (click its card in **Übersicht**).
2. Section **Benchmark** → **Benchmark starten**.
3. Tick one or more downloaded models. **Schnell** = one repetition and only two thread counts
   (about a minute per model instead of several).
4. **Starten**. The section shows *Benchmark läuft …*; results arrive live in the table
   (**Modell**, **Backend**, **Threads**, **GPU-Layer**, **Prompt t/s (pp512)**,
   **Generierung t/s (tg128)**), the fastest generation per model is marked with ★.
5. At the end the event **Benchmark beendet** appears; on CPU sweeps also
   **Konfiguration geändert** (threads).

A benchmark is refused while a game runs and is aborted (**Benchmark beendet**, error
`aborted: game detected`) if a game starts. Unload big models first on 8 GB cards — the
benchmark needs VRAM for the model it measures.

## Load / unload

In the node detail, **Modelle**: **Laden** starts a `llama-server` for that model (the first
load of a big model can take a while — the button waits up to the hub's action timeout, the
load itself continues on the node); **Entladen** stops it. Loaded models show VRAM, RAM,
backend and the loopback port. Models are also unloaded after `idle_ttl_seconds` (default
1800 s) without use and when a game starts. **Löschen** removes a downloaded (not loaded)
model file.

## Update / uninstall

Update: download the new binary and run the same `install` command without `--code` (the node
keeps its pairing — including the hub address it was paired with, see "Moving the hub"; the
service is replaced and restarted).

On Windows `install` first stops the service, ends the input helper's task and **every other
`topcoat-node.exe` process** (the helper runs from the same file in your session; a node started
from a shell too — it lists them), waits until they are gone, moves the old program file aside
(`topcoat-node.exe.old`, deleted at the next update) and puts the new one in place; at the end it
starts the service and the helper's task again. So an update from a remote shell works while
someone is logged on. If a process cannot be ended, `install` names its pid and stops before
touching the file.

```sh
sudo ./topcoat-node-linux-amd64 uninstall            # keep data (node key, models)
sudo ./topcoat-node-linux-amd64 uninstall --purge    # also delete /var/lib/topcoat-node and the user
```

```powershell
& "C:\Program Files\topcoat-node\topcoat-node.exe" uninstall            # elevated
& "C:\Program Files\topcoat-node\topcoat-node.exe" uninstall --purge    # also deletes C:\ProgramData\topcoat-node
```

After `--purge`, delete the node in the hub database or pair again with a new name (the old
name stays taken).

## Updates through the hub (signed update channel, E2-06)

After the first installation a node can update itself from the GUI. Nothing is installed unless it
was signed with your key on your PC; the hub only hands builds out. Background: ARCHITECTURE D50,
brief E2-06-update-channel.

**One-time setup on the build machine (the maintainer's PC):**

```sh
cd node
go build -o topcoat-sign ./cmd/topcoat-sign        # GOOS=windows for topcoat-sign.exe
./topcoat-sign keygen --out ~/.topcoat/release.key # prints the public key; back up release.key offline
```

**Build a node with the public key built in** (without it the node reports "cannot verify updates"):

```sh
PUB=$(cat ~/.topcoat/release.key.pub)
GOOS=linux GOARCH=amd64 go build -ldflags "-X topcoat/node/internal/version.Version=$(scripts/node-version.sh --release) \
  -X topcoat/node/internal/version.UpdateKeys=$PUB" \
  -o topcoat-node-linux-amd64 ./cmd/topcoat-node
```
The version comes from one place, the `VERSION` file at the root of the repository: `scripts/node-version.sh --release`
prints it (what is signed and published); without `--release` it prints `<VERSION>-dev.<commit>`, which the update
channel counts as older than the release. Never invent another number for a build that other machines run.
(PowerShell: set `$env:GOOS`/`$env:GOARCH` and put the same `-ldflags` string in quotes. In `node/`,
`make build VERSION=0.2.0 UPDATE_KEYS=$PUB` does the same, and `make build-sign` builds `topcoat-sign`.)
Up to two keys may be given, comma-separated (main and reserve).

**Hub:** set `TOPCOAT_UPDATE_DIR` (a volume, see `deploy/docker-compose.yml`) and optionally
`TOPCOAT_UPDATE_PUBKEYS` (the same public key; then the hub refuses uploads that nobody signed).

**Publish a build** (the admin token is the hub's `TOPCOAT_DEV_ADMIN_TOKEN`, read from the environment,
never from a flag):

```sh
export TOPCOAT_ADMIN_TOKEN=...
./topcoat-sign publish --key ~/.topcoat/release.key --binary topcoat-node-linux-amd64 \
  --version 0.2.0 --os linux --arch amd64 --channel stable \
  --hub https://hub.example.lan:8443 --ca-fingerprint sha256:<hub CA fingerprint>
```
`sign` and `upload` do the two halves separately; `verify --pub <key> --dir <dir>` checks a bundle.
Publish one build per platform (`windows`/`amd64`, `linux`/`amd64`).

**First installation of an update-capable build — by hand, once per node** (`install` now registers the
node service **and** the updater service `topcoat-node-updater`; Linux: a root systemd unit, Windows:
a LocalSystem service). Existing nodes: run the usual `install` from the new build, see "Update /
uninstall". Check afterwards: the node page shows the card *Update* without a hint that the updater is missing.

**Every later update:** node page → *Update* → **Auf x.y.z aktualisieren**. The node fetches and
verifies the build, the updater replaces the program, restarts the node and waits up to two minutes
for the new version to report back; otherwise it restores the old program. Result and reason show in
the node's events. For an existing node the action must be allowed first (the card has a button;
it adds `node.update` to `allowed_actions`). The channel is `config.update.channel` (`stable` or `test`).

Keep in mind: the updater runs with admin rights but trusts only the keys inside the program file and
what it verified itself. Losing the only key means one more manual install on every node, which is
why a reserve key exists. A build without a key (a development build) cannot be updated this way.

## Switching an existing Windows node to the virtual account (D17)
Nodes installed before D17 run as LocalSystem. Download the new binary and run the install
command again in an elevated PowerShell — without `--code` it keeps the node's identity:

```powershell
Unblock-File .\topcoat-node-windows-amd64.exe
.\topcoat-node-windows-amd64.exe install
Get-CimInstance Win32_Service -Filter "Name='topcoat-node'" | Select-Object StartName   # NT SERVICE\topcoat-node
```

The installer replaces the service, adds the account to "Performance Monitor Users" and
grants it the data dir. `uninstall` removes the group membership and the account's ACL entry
again (`--purge` deletes the data dir as before).

## Energy (yielding to local use, sleep, Wake-on-LAN)
Everything here is optional and set per node (node page in the GUI: **Consideration for local
use**, **Energy profile**; config sections `yield` / `power`, see docs/PROTOCOL.md).

**Yielding to local use ("ducken").** `llama-server` and `llama-bench` always run at the lowest
process priority. On a node **without a dedicated GPU** (the laptop) the node also freezes its
models while someone uses keyboard or mouse, other programs use the CPU heavily, or the machine
runs on battery; they continue at the same place afterwards. With a dedicated GPU freezing is off
(`yield.enabled: true` turns it on).
- *Windows service:* the service account `NT SERVICE\topcoat-node` may not ask Windows for the
  console session's last input, so `topcoat-node install` also registers the scheduled task
  **`topcoat-node-input`**. It starts `topcoat-node input-helper` **at logon of any user, only while
  that user is logged on, with that user's rights** (no elevation, no stored password — it does not
  need a domain password and works where S4U fails). The helper answers the service over a local
  named pipe that only the service account may open (no network port). After an install log off
  and on again — or run `schtasks /Run /TN topcoat-node-input` — so it runs in your session; the
  installer tries to start it for the users logged on already. Without it the node log says "no
  input helper answers in the console session" once and the node works on CPU load and battery
  only. `uninstall` ends and deletes the task. Its own log: `%LOCALAPPDATA%\topcoat-node\input-helper.log`.
- *Linux:* keyboard/mouse come from systemd-logind's idle hint (`loginctl`); a server without a
  desktop session sees no input and works on CPU load and battery only.

**Wake-on-LAN.** The node reports the MAC addresses of its *physical* network cards (wired
first); the hub keeps them while the node is off. **Aufwecken** on the node page (or
`POST /api/nodes/<id>/wake`) sends the magic packet. For the machine to react:
1. BIOS/UEFI: enable *Wake on LAN* / *Power on by PCI-E*.
2. Windows: Device Manager → network card → Advanced: *Wake on Magic Packet* = Enabled; Power
   Management: *Allow this device to wake the computer* + *Only allow a magic packet*; to wake
   from "off" also disable Fast Startup.
3. Linux: `sudo ethtool -s <eth0> wol g` (persistent: NetworkManager
   `nmcli connection modify <connection> 802-3-ethernet.wake-on-lan magic`).
4. The hub must get the packet into the LAN: in Docker use `network_mode: host` or
   `TOPCOAT_WOL_BROADCAST=<the LAN's broadcast address>` (default 255.255.255.255).

**Sleep.** The action `node.sleep` is not among the default permissions: add it to
`allowed_actions` in the config (then **In den Ruhezustand versetzen** appears on the node page),
or set `power.sleep_after_idle_seconds` so that the machine puts itself to sleep after that long
without work (nothing loaded, no resident, nobody at the machine). The node unloads its models
first. The service account needs the right to suspend — both **unverified** on real hardware:
- Linux: a polkit rule, e.g. `/etc/polkit-1/rules.d/50-topcoat-suspend.rules`:
  `polkit.addRule(function(a, s) { if (a.id == "org.freedesktop.login1.suspend" && s.user == "topcoat-node") { return polkit.Result.YES; } });`
- Windows: the service account needs `SeShutdownPrivilege`; otherwise the error is in the node log
  and an `error` event.
A sleeping node is *offline* for the hub; only **Aufwecken** brings it back. Waking by the gateway
when a model lives only on a sleeping node is not built yet.

**Energy profile.** `balanced` (default) and `saver` make the node look at the hardware less often
while nothing is loaded (factor 3 and 6; `saver` also reports less often); `performance` never
slows down. A node with a model in memory stays responsive whatever the profile.

## Moving the hub (to another machine or address)
A node stores the hub address in `node.json`; `install --hub <new>` on an already paired node
**keeps the old address** (it only pairs when there is no identity yet). To move the hub:

1. Move the hub **with its database and its `tls` directory**, so the hub keeps its CA and the
   nodes stay known. Put the new name or IP into `TOPCOAT_TLS_HOSTNAMES` so the hub certificate
   is valid for it (the CA stays the same, the certificate is re-issued for the new names).
2. On every node, run (Linux with `sudo`, Windows in an elevated PowerShell):

```sh
sudo ./topcoat-node-linux-amd64 set-hub --hub https://hub-new.example.lan:8443
```
```powershell
& "C:\Program Files\topcoat-node\topcoat-node.exe" set-hub --hub https://hub-new.example.lan:8443
```

`set-hub` first connects to the new address and checks that (a) its certificate verifies against
the **CA this node pinned when it paired** (the fingerprint is printed) and (b) it answers like a
topcoat hub (`GET /api/health`). Only then does it write `node.json` (node id, key and pinned CA
stay; the file keeps its owner, so the service account can still read it) and restart the
service. If the certificate comes from another CA, nothing is changed and the message says so:
move the hub with its old `tls` directory, or pair the node again (`--purge`, new name). Options:
`--data-dir DIR` (default as for `install`), `--no-restart` (only write the file; restart the
service yourself). Without an installed service (a node started by hand) it saves the address and
tells you to restart `topcoat-node run`.

A hub that announces its new address to the nodes itself (signed, same CA only) is not built:
the hub is unreachable for a node under the old name anyway, so the command on each node is the
honest way. UNVERIFIED on real hardware: the service restart (systemd / Windows service manager);
the check and the file handling are tested.

## Docker container node (CPU)

For an always-on Docker host without a GPU (for example a NAS-style server): the same
`topcoat-node`, packaged as a container (`deploy/node-cpu/`). It pairs on the first start, runs
CPU-only and serves a small model such as the embedding model (bge-m3 Q8_0, ~0.6 GB).

- Outbound only: the node connects to the hub and publishes no port; `llama-server` inside the
  container binds to the loopback.
- The pinned llama.cpp CPU build (SHA-256 checked) is downloaded into the data folder on the
  first start; the image does not contain it.
- Identity, key, llama.cpp build and models live in the data folder (mounted at `/data`), so the
  container is disposable. Models go to `<data folder>/models`; download them from the node page
  in the hub GUI.
- A container build has no update keys: the hub's signed update channel does not touch it. To
  update, rebuild the image and replace the container; the data folder keeps the identity.

Placeholders below: hub `https://hub.example.lan:8443`, data folder `/mnt/pool/topcoat-node`.

1. Hub GUI: add a node, note the one-time code and the **CA fingerprint** (valid 15 minutes).
   The fingerprint must be exactly `sha256:` plus 64 hex digits; the node refuses anything else.
2. Build the image on the host and put the settings in a file outside the repository:

   ```bash
   docker build -f deploy/node-cpu/Dockerfile -t sloppy-topcoat-node-cpu:local .
   cp deploy/node-cpu/node.env.example /root/node.env    # fill in hub URL, code, fingerprint, name
   ```

3. Start it (plain `docker`, no compose needed):

   ```bash
   docker run -d --name topcoat-node-embed --init --restart unless-stopped \
     --memory 4g --cpus 3 --env-file /root/node.env -e TZ=Europe/Berlin \
     -v /mnt/pool/topcoat-node:/data sloppy-topcoat-node-cpu:local
   docker logs -f topcoat-node-embed
   ```

   Expected: `paired: node_id=...`, then `llama.cpp runtime selected ... backend=cpu`.
4. Delete `/root/node.env` afterwards (the code is used up). A failed first start loops through
   `--restart`; fix the file, then `docker rm -f topcoat-node-embed` and run step 3 again.
5. Download the model from the node page (the transfer goes node to node, see below) or copy the
   GGUF into `<data folder>/models`, then choose it on the **Responsibilities** page.

Model transfers between nodes use TCP `47000`-`47099` on the **source** node. The firewall rule
that `install` creates on Windows allows `LocalSubnet` only; when the container host is in another
subnet, add that subnet to the rule (`Set-NetFirewallRule -DisplayName topcoat-node-transfer
-RemoteAddress LocalSubnet,10.0.0.0/24`), or copy the file by hand.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| runner detail `llama.cpp … install failed: … HTTP 403/404` or timeout | no access to `github.com` / `objects.githubusercontent.com` from that machine (proxy, firewall). The node retries every minute. |
| runner detail `… SHA-256 mismatch` | download corrupted or changed upstream; the node refuses to use it. Report it — do not bypass. |
| Linux CUDA: model load fails, log `CUDA driver version is insufficient` | NVIDIA driver older than 570 → section 2. |
| Linux Vulkan backend (only if configured): `libvulkan.so.1: cannot open shared object file` | `sudo apt install -y libvulkan1 mesa-vulkan-drivers` |
| Windows: log line `GPU memory counters are not readable …` or `no GPU detected; using the CPU backend` | the service account is not in "Performance Monitor Users" (install adds it). Check with `net localgroup "Performance Monitor Users"` (the group name is localized, e.g. *Leistungsüberwachungsbenutzer*); run `topcoat-node install` again (elevated) to fix it. |
| Windows: `path_prefix` rule never matches, `game detected` without `path` | an agent older than D17 running as a virtual account; update the node and run `install` again. |
| Windows: model load fails immediately, `logs\llama-server-<model>.log` shows no Vulkan device | see *Vulkan in the Windows service* below; workaround: run the node in the foreground as your user. |
| download `failed` with `HTTP 401/403` | gated or private repo; the node has no Hugging Face token yet. Use a public GGUF repo. |
| download `failed` with `split GGUF` | multi-part models (`-00001-of-0000N.gguf`) are not supported yet — pick a single-file quant. |
| model load `not ready after 10m0s` | disk too slow or model too big; check `logs\llama-server-<model>.log` in the data dir. |
| node stays **Spielt** | a watched process is still running (see the log line `game detected process=…`); remove over-broad `path_prefix` rules. |

### Vulkan in the Windows service

The Windows service runs as the virtual account `NT SERVICE\topcoat-node` in session 0 (D17).
**Verified in test 1** on `node-amd` (RX 9070 XT, AMD driver): models load on Vulkan in the
service, as LocalSystem (A4) and as the virtual account with "Performance Monitor Users" (A7). Still unverified for the Intel
driver (`node-laptop`, manual check P-rows). If loads fail on Vulkan in the service but work in
the foreground, run the node as your user instead:

```powershell
& "C:\Program Files\topcoat-node\topcoat-node.exe" uninstall          # elevated
& "C:\Program Files\topcoat-node\topcoat-node.exe" pair --hub https://hub.example.lan:8443 --code ABCD-1234 --ca-fingerprint sha256:<hex> --name node-amd --data-dir $HOME\topcoat
& "C:\Program Files\topcoat-node\topcoat-node.exe" run --data-dir $HOME\topcoat
```

(needs a new pairing code and name, or `--force` into the old data dir), and report the result.
