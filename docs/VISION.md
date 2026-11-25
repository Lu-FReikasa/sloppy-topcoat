# Vision — beyond stage 1

Recorded 2026-09-28 from the maintainer's ideas, extended 2026-10-30. This is direction, not a
commitment; it must not change the current roadmap stages without an explicit decision.

## The picture as of 2026-10-30
Topcoat is the central interface to one's whole infrastructure, and two lines run in tandem: the
**local AI platform** and the **home lab control layer**.

- **Install a small service on a machine, and manage it from Topcoat:** updates, services, Docker
  containers, restarts, with the agents helping.
- **The agent can read a lot and change little.** It builds a thorough picture of a host through
  read-only tools. Changes run only through fixed, controlled tools and the person's confirmation.
- **Agents build and tend their own knowledge base** as they work.
- **Applications stand alone.** Each is useful by itself; through the chat one can ask about it and
  start work in it.
- **Projects, Development and Administration come first.** Useful answers there are the best
  benchmark, because they affect every other system.
- **Topcoat extends itself while running.** "Design me a new theme" produces example pages or
  screenshots to discuss. A new application, its tools or a change to the code base is planned in
  Projects, then written and checked by the agent and put into use after the person's review.
- **It does not need a large lab.** One machine is a full setup; several are recommended
  (ARCHITECTURE D88).

## Core purpose
**Make it as easy as possible for anyone to build their own home lab from the hardware they
already have.** A typical start: the gaming PC as the first machine, later an old PC with
Ubuntu Server, then a mini PC as the router — each one is installed as a node and from then
on managed like every other one, from one GUI, with the AI assistant helping. The hard,
fiddly parts of running a home lab should become simple:
- installing and running services as Docker containers (choose, configure, update),
- the **DNS / reverse proxy / certificate chain** (name → DNS record → proxy host → TLS
  certificate → service) as one step instead of four tools,
- users and access (see goal 5), backups, updates.

Scope for testing now: **Windows and Ubuntu**. Design for openness from the start:
documented protocol and module contracts, no hard dependency on one distribution or vendor,
standard formats (Docker/Compose, OIDC, DNS standards), so other platforms (e.g. a router OS,
NAS systems, ARM boards) and other people's modules can be added later.

**Open source:** the licence is the AGPL-3.0 (`LICENSE`). The project is published once version 1.0
works (not at the first prototype, see ARCHITECTURE D93): the scope discussed so far, and good for
home-lab administration and local AI development. Until then the repo stays private; code and docs
are written as if they were public already (no internal names, no secrets — see CLAUDE.md).

### Appliances without a node: device adapters
Not every device can or should run the node binary. Routers and appliances are managed
**agentless**: a module talks to the device's own API, running on the hub or on a nearby
node (which then acts as the device's "proxy node"). Order:
1. **OPNsense** first (the maintainer's router): its REST API (API key + secret per user)
   covers e.g. Unbound DNS host overrides, firewall aliases, DHCP, gateways — exactly the
   pieces of the DNS chain. Agentless is also the safer default on a firewall; a native
   FreeBSD node stays optional.
2. **OpenWrt**: ubus JSON-RPC over HTTP (`/ubus`, rpcd with ACLs, session login) —
   agentless first; a tiny native node only where the device has the memory for it.
3. **FRITZ!Box**: TR-064 (SOAP) and the AHA-HTTP interface — status, hosts, WLAN, port
   forwarding, smart-home devices; the MIT-licensed Python library `fritzconnection` shows
   the scope and can serve as reference or dependency for a hub-side module.
Later candidates with the same pattern: switch/Wi-Fi controllers (e.g. Omada), NAS web
APIs, smart-home hubs.

## From "local AI manager" to "a fresh coat for the home lab"
The name is the idea: **topcoat** is a single, uniform GUI and control layer **on top of
existing, proven tools** — not a replacement for them. Comparable in spirit to HexOS on top
of TrueNAS, but for a whole home lab spread over many machines and operating systems.

Goals:
1. **Every device is a node.** Install the small node service on any machine (server, VM,
   NAS, desktop, Raspberry Pi) and it becomes manageable like every other one.
2. **One place to see everything:** per node hardware, OS, updates, disks and storage
   health, Docker containers (running/stopped, images, updates), services, network, local
   AI models.
3. **Manage through typed actions**, never a raw remote shell: restart a container, pull an
   image, create a share, add a user to a service …
4. **Building blocks → a complete package.** Well-known free projects (identity, file
   shares, Nextcloud, Immich, media, backup …) become modules that topcoat wires together
   into a coherent whole, with one design and one logic on top.
5. **Identity without the relay race.** Today: AD group → Keycloak group → app group
   (e.g. Homarr). Target: define *once* in topcoat "person X may use app Y (as role Z)";
   topcoat translates that into whatever the backends need. The existing directory/IdP stays
   the source of truth underneath; the chain becomes invisible, not removed.
6. **The AI assistant is central, not an add-on.** It uses the same typed node actions as
   the GUI to inspect, troubleshoot and manage the lab. Hard rule: **without an explicit
   confirmation by the user the AI may only read.** Every state-changing action (restart,
   install, delete, config change, user/group change …) is shown to the user with its exact
   effect and runs only after confirmation — per action, never as a blanket approval.
7. **Client devices, not only servers.** A node flavour for end-user devices (laptops,
   desktops of the household/domain) extends central management to them: inventory, update
   status of OS and installed software, remote start of updates, install/uninstall of
   programs through the platform's package managers (e.g. winget, apt, flatpak), with the
   same confirmation rule. Devices are often offline or away from home: nodes connect
   outbound (over the existing VPN mesh where needed) and catch up when they come back.

## Principles (inherited and new)
- **Thin core.** Node: single binary, tiny footprint, OS-agnostic (Linux incl. arm64,
  Windows; Unraid/TrueNAS/Proxmox via container or native package where the platform
  allows). Hub must also be able to run on small hardware (Raspberry Pi class) — revisit
  D5 (Postgres) vs. a SQLite profile for small installs when this becomes relevant.
- **Adapters, not forks.** Each integration is a module with a small, versioned contract
  (read model + typed actions + health). Upstream APIs change; the module absorbs it.
- **Read first, then act.** New integrations start read-only (overview, health), actions
  come after they proved reliable.
- **Least privilege per capability.** A node only gets the capabilities assigned to it;
  privileged actions are typed, schema-validated, audited, and need approval when they
  change state. This matters doubly once an AI can trigger them.
- **Multi-user by design** (already D8): per-person memory, permissions and audit.
- **Other people's devices:** users see what is managed on their device and why; admin
  actions on a personal device are logged and visible to its owner.

## Prior art to learn from (not to depend on)
- HexOS: simplified GUI over TrueNAS SCALE, curated app installs with automatic path/folder
  setup, cross-app linking.
- YunoHost: users + SSO + LDAP integrated into every packaged app — the closest existing
  answer to goal 5, but single-server and opinionated.
- Komodo: multi-server Docker management with a core + lightweight "periphery" agent per
  server, RBAC and OIDC — close to the node model for containers.
- Cockpit, Portainer/Dockge, Beszel (hub + agent monitoring), CasaOS/Umbrel-style app
  stores.
- For client devices: RMM/endpoint tools such as MeshCentral, Tactical RMM, Fleet/osquery
  (inventory via queries), and the Intune-style "update ring" idea.

## Big open questions (decide later, one at a time)
1. Which identity backend is the long-term source of truth for new installs (keep
   AD+Keycloak, or a lighter stack such as an LDAP-lite + OIDC provider)? How are app roles
   mapped (OIDC group claims, SCIM, per-app adapters)?
2. Module format: in-process plugins in the hub, separate containers, or node-side
   capability packs? How are modules versioned and tested against real upstream versions?
3. Which platforms get a native node first after Linux/Windows (Unraid, Proxmox host,
   TrueNAS, arm64)?
4. "Complete package" mode (opinionated fresh install) vs. "adopt my existing lab" mode —
   the maintainer's lab needs the second; the first is a later product decision.
5. Scope guard: which integrations are core, which are community modules?
6. Openness: public module API; how device-adapter credentials (router API keys, TR-064 passwords) are stored and scoped on
   the hub.
7. Client devices: how does the node behave on battery / metered networks / while the user
   works (no surprise updates), and how are package-manager differences abstracted?

## Rough order
Written before stage 4. The inventory, the confirmed container actions and the read-only identity
view are built (ARCHITECTURE D43, D45, D47). What is still ahead is in the roadmap 2.5:
production hardening with OIDC, access grants that write through to the backends, app modules and
client devices. The order of the roadmap wins over this list.
