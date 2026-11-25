# Hub image: build and install

The hub runs from a container image. The image carries the commit it was built from in the
`build` field of `GET /api/health`, so the running hub can be matched to the code.

## Build (on the machine with Docker)

```bash
scripts/build-hub-image.sh
scripts/smoke-test-hub-image.sh sloppy-topcoat-hub:local-<commit>
```

The build script refuses uncommitted changes and tags the image `sloppy-topcoat-hub:local-<commit>`.
It pushes nothing. The smoke test starts Postgres and the hub from the image, checks that the hub
migrates, serves the API and the GUI over TLS and keeps its CA across a restart; it removes what it
started.

Faster rounds while developing: `scripts/dev/deploy-overlay.sh` puts the code onto the hub host in about ten seconds
without an image (see [../docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md), D98); it is not a release. Build once before the
real build to warm the Docker layers: the second build takes seconds.

## Move the image to the hub host

Replace the placeholders with your own: `hub.example.lan` is the host, `/srv/topcoat` the
directory with the compose file and `hub.env`. The image is moved over with `docker save`:

```bash
docker save sloppy-topcoat-hub:local-<commit> | gzip > hub-<commit>.tar.gz
scp hub-<commit>.tar.gz admin@hub.example.lan:/srv/topcoat/
```

## Update (with a way back)

On the hub host, in the directory with `deploy/`:

```bash
deploy/update-hub.sh --dry-run --load hub-<commit>.tar.gz sloppy-topcoat-hub:local-<commit>
deploy/update-hub.sh           --load hub-<commit>.tar.gz sloppy-topcoat-hub:local-<commit>
```

The dry run loads the image and runs the checks, then stops. The real run, in this order and
stopping at the first problem:

1. the image exists locally;
2. the image contains the migration the database is at (a database that is ahead of an image makes
   the hub refuse to start);
3. a database dump (`topcoat-before-<image>.dump`, `pg_dump -Fc`) is written to the backup directory;
4. `TOPCOAT_IMAGE` in `hub.env` is switched and only the hub is recreated;
5. the hub must answer its health URL within `TOPCOAT_HEALTH_WAIT` seconds; if not, the previous image
   is put back (exit 1);
6. older hub images are removed; the new and the previous one stay.

Afterwards:

```bash
curl -sk https://hub.example.lan:8443/api/health
```

The answer carries `"build": "<commit>"`.

### Migrations that come with an update

The first start runs the migrations. Some cannot be reverted by a rollback of the image; the dump
from step 3 is the way back to the earlier schema.

| migration | what it does |
|---|---|
| `0071` | cannot be reverted |
| `0074_no_escalation` | drops the two (empty) escalation tables and the escalation setting |
| `0075_ui_themes` | adds the table of custom themes |

The same first start converts the stored model policy once: the per-node defaults, per-task routes
and the environment variables for the memory, review and background models become roles
(`answer`, `agent`, `background`, `check`). The hub logs a warning that names every variable it
read. Check the result on the page *Administration, Models and roles* and set an entry to `auto`
where the hub should choose by measured speed.

### By hand (without the script)

```bash
docker exec topcoat-db-1 pg_dump -U topcoat -Fc topcoat > topcoat-before-<commit>.dump
gunzip -c hub-<commit>.tar.gz | docker load
TOPCOAT_IMAGE=sloppy-topcoat-hub:local-<commit> \
  docker compose -f deploy/docker-compose.yml --env-file deploy/hub.env up -d hub
```

## Nodes

A node is updated separately: `scripts/node-version.sh --release` prints the version for a release
(from the file `VERSION`); `scripts/build-node-release.sh <version> <out dir>` (run from `node/`)
builds the binaries and `SHA256SUMS`, which are then signed and published. Until a node runs the new
version the hub asks it for a model the old way (an explicit `context_length`); the automatic context
size works on updated nodes only.

## Rollback

Point `TOPCOAT_IMAGE` back to the previous tag and restart the hub (`update-hub.sh` does this when
the new hub does not answer). The migrations are not reverted by this; restore the dump if the
earlier schema is needed:

```bash
docker exec -i topcoat-db-1 pg_restore -U topcoat -d topcoat --clean --if-exists < topcoat-before-<image>.dump
```

## Backups

`deploy/backup-hub.sh` writes the database (`pg_dump -Fc`, checked with `pg_restore --list`) and
the TLS volume (the hub CA and the key of the secret box) into the backup directory, mode 600, and
prunes by a rule: the newest 14 nightly files of each kind and the newest 5 dumps that
`update-hub.sh` writes before an update. Without the TLS volume a restored database cannot decrypt
stored tokens, and every node has to pair again.

```bash
deploy/backup-hub.sh --dry-run   # shows what it would write and remove
deploy/backup-hub.sh
```

To run it every night, install the units (replace `/srv/topcoat` and the user in the `.service`):

```bash
sudo cp deploy/topcoat-backup.service deploy/topcoat-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now topcoat-backup.timer
systemctl list-timers topcoat-backup.timer
```

Restore a dump into a fresh database: `docker exec -i topcoat-db-1 pg_restore -U topcoat -d topcoat
--clean --if-exists < topcoat-nightly-<stamp>.dump`. Copy the dump off the host as well: a backup
on the same disk does not survive the disk.

Retention of what only grows is the hub's own job (`retention.py`): node events after 90 days
(`TOPCOAT_RETENTION_NODE_EVENTS_DAYS`), the audit log of capability calls after 365 days
(`TOPCOAT_RETENTION_TOOL_CALLS_DAYS`, 0 keeps it for ever).
