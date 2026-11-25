# CPU model node in a container

A model node without a GPU, for an always-on server (for example the embedding model). It
is the same `topcoat-node` as everywhere, started with `run`; the first start pairs it with the
hub. Pairing, the CPU backend and a model transfer have run on a real Docker host (Unraid);
the compose file itself has not been run. Full steps: `docs/INSTALL-NODE.md`, "Docker container node (CPU)".

- Outbound only: the node connects to the hub; nothing is published, no port is opened.
- `llama-server` inside the container binds to the loopback.
- The pinned llama.cpp CPU build is downloaded into the data volume on first start.
- Models go to `<data volume>/models` (copy a GGUF there, or download one from the node page
  in the hub GUI).
- Updates: rebuild the image and `docker compose up -d` (a container build has no update keys,
  so the hub's signed update channel does not touch it).

## Steps

1. Hub GUI: create a pairing code (add node). Note the CA fingerprint.
2. On the server: `cp deploy/node-cpu/node.env.example deploy/node-cpu/node.env`, fill it in.
3. `docker build -f deploy/node-cpu/Dockerfile -t sloppy-topcoat-node-cpu:local .`
4. `docker compose -f deploy/node-cpu/docker-compose.yml --env-file deploy/node-cpu/node.env up -d`
5. `docker logs` shows `paired:` and `llama.cpp runtime selected ... backend=cpu`.
6. Copy the embedding GGUF into the models folder, then choose it on the Responsibilities page.

Remove the pairing code from `node.env` after the first start; it is used up by then.
