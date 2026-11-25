# Quickstart — try stage 1 on real machines

Copy-paste steps to run the hub on an Ubuntu Server machine (tested with 26.04), open the web
GUI and pair `topcoat-node` from a Windows and a Linux machine.

- Parts (a)–(d) below: hub + GUI, and a node in `--fake` mode (fake hardware: a 16 GB fake GPU
  and three fake models). Good for a first look; nothing touches your GPU.
- **Real nodes** (llama.cpp on the real GPU, model downloads, benchmark, service install):
  follow [INSTALL-NODE.md](INSTALL-NODE.md) once the hub runs — part (e).

The node only opens outbound connections (hub, and for real nodes GitHub and Hugging Face).

Placeholders used below — replace them with your own values:

| placeholder | meaning |
|---|---|
| `hub.example.lan` | hostname or IP of the Ubuntu machine running the hub (e.g. `10.0.0.x`) |
| `<TOKEN>` | the admin token you put into `deploy/hub.env` |
| `ABCD-1234` | the pairing code the GUI shows you |
| `sha256:<hex>` | the fingerprint of the hub's CA; the GUI puts it into the pair command |

All traffic is encrypted (HTTPS/WSS, ARCHITECTURE D11). On first start the hub creates its
own small CA and a certificate for the names in `TOPCOAT_TLS_HOSTNAMES`; nodes pin that CA
through the fingerprint in the pair command. Your browser does not know the CA: either accept
the warning once, or install the CA (link in the **Knoten hinzufügen** dialog).

---

## (a) Hub on Ubuntu Server (release channel `test`)

Nothing is built on your machine: the hub image comes from the GitHub container registry of
the repository, built by CI from every green push to `main` (channel **test**, ARCHITECTURE D123).
To build the hub from source instead, see [DEVELOPMENT.md](DEVELOPMENT.md).

### 1. Install Docker and git (once)

```sh
sudo apt update
sudo apt install -y docker.io docker-compose-v2 git python3
sudo usermod -aG docker "$USER"
# log out and back in (or: newgrp docker) so the group change applies
docker compose version        # should print "Docker Compose version v2..."
```

(Docker's own `docker-ce` packages work as well, as long as `docker compose` is available.)

### 2. Get the deploy files and log in to the registry

```sh
git clone https://github.com/Lu-FReikasa/sloppy-topcoat.git
cd sloppy-topcoat
```

The repository is private, so its images are too. Create a GitHub **personal access token
(classic)** with only the scope `read:packages` (GitHub → Settings → Developer settings →
Personal access tokens), then:

```sh
docker login ghcr.io -u <your GitHub user name>      # paste the token as password
```

Docker keeps the login in `~/.docker/config.json`; the token never goes into the repository.

### 3. Configure

```sh
cp deploy/hub.env.example deploy/hub.env
DBPW=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
sed -i "s|^TOPCOAT_DB_PASSWORD=.*|TOPCOAT_DB_PASSWORD=$DBPW|" deploy/hub.env
sed -i "s|^TOPCOAT_DEV_ADMIN_TOKEN=.*|TOPCOAT_DEV_ADMIN_TOKEN=$TOKEN|" deploy/hub.env
# Every name and IP nodes/browsers use for this machine, comma-separated:
sed -i "s|^TOPCOAT_TLS_HOSTNAMES=.*|TOPCOAT_TLS_HOSTNAMES=hub.example.lan,10.0.0.x|" deploy/hub.env
echo "Your admin token: $TOKEN"     # you need it for the GUI login
```

`deploy/hub.env` is gitignored. Never commit it.

### 4. Start

```sh
docker compose -f deploy/docker-compose.yml --env-file deploy/hub.env up -d
docker compose -f deploy/docker-compose.yml --env-file deploy/hub.env ps
docker compose -f deploy/docker-compose.yml --env-file deploy/hub.env logs hub | grep fingerprint
# INFO topcoat_hub.serve: hub CA fingerprint sha256:...
curl -s --insecure https://localhost:8443/api/health
# expected: {"status":"ok","version":"0.1.0"}
```

**Update** to the newest test build (keeps database and hub CA):

```sh
docker compose -f deploy/docker-compose.yml --env-file deploy/hub.env pull
docker compose -f deploy/docker-compose.yml --env-file deploy/hub.env up -d
```

The image tag `test` always points to the newest green build of `main`; every build is also
tagged `sha-<commit>` (set `TOPCOAT_IMAGE=ghcr.io/lu-freikasa/sloppy-topcoat-hub:sha-<commit>`
in `deploy/hub.env` to stay on one build).

If `ufw` is active, allow the hub port from your LAN:

```sh
sudo ufw allow from 10.0.0.0/24 to any port 8443 proto tcp    # adjust to your LAN range
```

The hub listens on `8443` (HTTPS: API, GUI, node connections). Postgres has no host port.

The hub CA lives in the docker volume `topcoat_hub-tls`. Keep it: `down -v` deletes it, and
then every node has to pair again. Changing `TOPCOAT_TLS_HOSTNAMES` later is fine (the hub
issues a new server certificate from the same CA at the next start).

---

## (b) Web GUI

1. Open `https://hub.example.lan:8443` in a browser on any machine in the LAN. The browser
   warns about the unknown certificate issuer: continue once, or install the hub CA (step 4).
2. Paste the dev admin token (`<TOKEN>`) → **Anmelden**.
3. You see the desktop: the programs **Chat** and **Posteingang** side by side, the taskbar at
   the bottom with a green **Live** dot (the live stream is connected).
4. Open **Verwaltung › Maschinen** (taskbar, or Ctrl+K and type "Maschinen") and click
   **Knoten hinzufügen**. The dialog shows a pairing code (valid 15 minutes, single
   use), the CA fingerprint with a download link for the CA, a choice of the node's operating
   system, an optional node name, and two ready-made commands for the downloaded binary, e.g.
   `sudo ./topcoat-node-linux-amd64 install … --ca-fingerprint sha256:<hex> --name node-nvidia`
   (service, see INSTALL-NODE.md) and
   `.\topcoat-node-windows-amd64.exe pair --hub https://hub.example.lan:8443 --code ABCD-1234 --ca-fingerprint sha256:<hex>`
   (foreground). **Kopieren** copies one.

Use `https://…:8443`. The vite dev server on `:5173` is plain HTTP; commands built there carry
`--insecure-http` and only make sense for GUI development.

---

## (c) topcoat-node on Windows and Linux (`--fake` mode)

### Get the binary

GitHub → the repository → **Actions** → the newest green run of the CI workflow on `main` →
**Artifacts** → `topcoat-node-test-<commit>` (needs a GitHub login; kept 30 days; ARCHITECTURE
D123). Unpack it: it holds `topcoat-node-windows-amd64.exe`, `topcoat-node-linux-amd64` and
`SHA256SUMS`. These builds are unsigned; signed releases reach nodes through the hub's update
channel. Check the download:

```sh
sha256sum -c SHA256SUMS --ignore-missing          # Linux: "...: OK"
```

```powershell
Get-FileHash .\topcoat-node-windows-amd64.exe -Algorithm SHA256   # compare with SHA256SUMS
```

`topcoat-node version` prints `test-<commit>`, the same commit as the hub image tag
`sha-<commit>`. Building from source: [DEVELOPMENT.md](DEVELOPMENT.md).

### Linux (e.g. `node-nvidia`)

```sh
mkdir -p ~/topcoat && cd ~/topcoat
cp /path/to/topcoat-node-linux-amd64 ./topcoat-node && chmod +x topcoat-node
./topcoat-node version
# topcoat-node 0.1.0-dev (protocol v1)   (or a git version when built with make)

./topcoat-node pair --hub https://hub.example.lan:8443 --code ABCD-1234 \
  --ca-fingerprint sha256:<hex> --name node-nvidia-fake --data-dir ~/topcoat/data
# paired: node_id=... hub=https://hub.example.lan:8443 data_dir=/home/<you>/topcoat/data

./topcoat-node run --fake --data-dir ~/topcoat/data
```

### Windows (e.g. `node-amd`), PowerShell as a normal user

```powershell
mkdir $HOME\topcoat; cd $HOME\topcoat
Copy-Item C:\path\to\topcoat-node-windows-amd64.exe .\topcoat-node.exe
Unblock-File .\topcoat-node.exe          # downloaded binaries are unsigned
.\topcoat-node.exe version

.\topcoat-node.exe pair --hub https://hub.example.lan:8443 --code ABCD-1234 `
  --ca-fingerprint sha256:<hex> --name node-amd-fake --data-dir $HOME\topcoat\data

.\topcoat-node.exe run --fake --data-dir $HOME\topcoat\data
```

Create a **new** pairing code in the GUI for every node (codes are single use). Pick a
different `--name` per node; names must be unique on the hub. Windows may ask once whether
to allow network access — the node only makes outbound connections, so "private networks"
is enough.

Notes:

- `--data-dir` holds the node identity (`node.json`, the private key `node.key`, the pinned hub
  CA `hub-ca.pem`), the cached config and the offline event queue. Without it the default is `/var/lib/topcoat-node` (root) or
  `~/.local/state/topcoat-node` on Linux, `%ProgramData%\topcoat-node` on Windows.
- Pairing again into the same data dir needs `--force`.
- Stop the node with `Ctrl+C`. It runs in the foreground; for a service use `install`
  ([INSTALL-NODE.md](INSTALL-NODE.md)).
- Optional: shape the fake hardware with `--fake-profile node/fake-profile.example.json`
  (copy and edit it: GPUs, models, VRAM per model).
- `run` **without** `--fake` uses the real providers: it detects the GPUs, downloads the
  pinned llama.cpp build into the data dir and manages real models — see
  [INSTALL-NODE.md](INSTALL-NODE.md).
- The fake node also simulates **Modell herunterladen** (any Hugging Face repo/file name, no
  real download) and **Benchmark starten** (synthetic numbers), so the GUI can be tried
  without a GPU.

---

## (d) What you should see

| step | expected |
|---|---|
| `pair` | prints `paired: node_id=…`; the GUI dialog can be closed |
| `run --fake` starts | log lines `connected to hub`, `config applied … runner=llamaserver`, `session established`; a warning that FAKE providers are in use |
| overview | within ~1 s a card per node: badge **Verfügbar**, `linux / amd64` or `windows / amd64`, VRAM bar **Fake GPU 16G** at `800 / 16.384 MB`, RAM `8,8 / 32,0 GB`, no loaded models |
| click the node | detail page: system facts, runner **Fake (Test) – verfügbar**, table with `fake-llm-3b-q8`, `fake-llm-8b-q4`, `fake-llm-14b-q4`, each with **Laden** |
| **Laden** on `fake-llm-8b-q4` | row switches to **ja** / `6.100 MB` / **Entladen**; VRAM bar jumps to `6.900 / 16.384 MB`; events **Aktion** and **Modell geladen** appear under **Letzte Ereignisse**; the overview card lists the model — all without reloading the page |
| **Entladen** | back to `800 MB`, event **Modell entladen** |
| `Ctrl+C` on the node | within a few seconds badge **Offline**, event **Getrennt** |
| start the node again | back to **Verfügbar**; loaded state is reset (the fake runner keeps nothing across restarts) |
| restart the hub (`docker compose -f deploy/docker-compose.yml --env-file deploy/hub.env restart hub`) | nodes may show **Offline** briefly, then reconnect on their own within seconds (node log: `hub session ended … retry_in=…`, then `session established`) |
| stop the hub for a while | node keeps running with its cached config and retries with growing back-off (max ~1 min) |

### Troubleshooting

| symptom | cause / fix |
|---|---|
| `pair` fails with `invalid_code` | typo, or the code belongs to another hub |
| `pair` fails with `code_expired` | older than 15 min or already used → create a new code |
| `pair` fails with `name_taken` | another node has that name → use a different `--name` |
| `pair`: connection refused / timeout | hub not reachable: check `curl --insecure https://hub.example.lan:8443/api/health` from that machine, firewall (`ufw`), the IP |
| `pair`: `certificate is valid for …, not …` | the name/IP in `--hub` is missing in `TOPCOAT_TLS_HOSTNAMES` → add it in `deploy/hub.env`, `up -d` again (same CA, new certificate), pair again |
| `pair`: `chain does not contain the CA with fingerprint …` | wrong fingerprint or another hub (or the hub lost its `hub-tls` volume) → copy the command from the GUI again |
| `pair`: `hub URL uses plain http` | use the `https://…:8443` URL from the GUI |
| GUI login says the token is invalid | token must match `TOPCOAT_DEV_ADMIN_TOKEN` in `deploy/hub.env`; after editing the file run `up -d` again |
| `docker compose pull`: `denied` / `unauthorized` | not logged in to `ghcr.io`, or the token lacks `read:packages` → `docker login ghcr.io` again |
| node never connects, log says the hub rejected the node identity (403) | the hub no longer knows this node (e.g. database reset with `down -v`) → new code, `pair … --force` |
| GUI shows **Verbindung wird wiederhergestellt** | the live stream dropped (hub restart); it reconnects automatically |

To start over: `docker compose -f deploy/docker-compose.yml --env-file deploy/hub.env
down -v` (the `-v` deletes the database and the hub CA) and delete the nodes' data dirs.

---

## (e) Real nodes

With the hub running, install `topcoat-node` as a service on each machine, download a first
model, run the benchmark and load/unload models: **[INSTALL-NODE.md](INSTALL-NODE.md)**
(sections for the Windows 11 + AMD gaming PC, the Ubuntu + NVIDIA machine and the
Windows 11 + Iris Xe laptop).

Nodes paired with an E1-01 hub keep their old config (only `models.list/load/unload`
allowed). Either pair them again, or open the node → **Konfiguration bearbeiten** and add
`"models.download"`, `"models.delete"`, `"node.benchmark"` to `allowed_actions`.
