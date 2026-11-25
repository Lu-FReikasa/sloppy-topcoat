# The build service (NS-01, D80)

A node can have a *build service*: a second process under its own OS user that builds releases from a
pinned commit. The hub's page **Administration → Releases** starts a build (a click of the signed-in
admin), shows commit, changes and checksums, and publishes only with a signature made in the admin's
browser. Nothing on the build machine can sign.

Status: the pieces are tested with fakes (`node/internal/servicebus`, `buildsvc`, `buildcap`,
`node/internal/agent`, `hub/tests/test_releases.py`, `web/src/releases`). **UNVERIFIED on real
hardware**: the systemd unit, rootless Docker, the real repositories, and the smoke test of the
hub image on that host.

## Set up

1. Pair a node with `--role build` (`topcoat-node pair --role build ...`; on the hub the node gets
   `allowed_actions: ["build.manage"]` and the five `build.*` capabilities).
2. Create the user, its directory and rootless Docker (see the comments in `topcoat-build.service`).
3. Start the node with the service's socket: `topcoat-node run --build-sockets /run/topcoat-node
   --build-user topcoat-build --build-work /srv/topcoat/build`. The node creates one socket per
   service, owned by that user, and knows the caller by the kernel's word about its user id.
4. Fill in `topcoat-build.service` (sources, the hub's public update key) and enable it.
5. On the hub, set `TOPCOAT_UPDATE_DIR` and `TOPCOAT_UPDATE_PUBKEYS` (the public half of the key the
   admin signs with; the private half stays on the admin's PC and is only ever read in the browser).

## Where the code comes from

Two options, chosen per build on the page: `release_repo` (a release repository of its own) and
`dev_mirror` (a mirror of the development repository). Both are configured on the build host; the
hub only picks the name. A build always starts from a fresh clone at the exact commit, never from a
workshop folder.

## What the service can do

Exactly five named steps (`build.fetch`, `build.node_release`, `build.hub_image`,
`build.node_container_image`, `build.smoke_test`), one at a time, with strictly checked inputs. It may
ask the node for one thing: to upload a finished file below its own directory. See
`docs/PROTOCOL.md`, "Build service".
