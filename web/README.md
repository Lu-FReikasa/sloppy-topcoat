# Topcoat web GUI

React + Vite + TypeScript single-page app for the hub: node overview, node detail
(models, load/unload), "add node" pairing dialog, live updates via the hub's SSE stream.
UI strings are German via i18next (`src/i18n/de.json`).

## Run against a local hub

```sh
cd web
npm ci
HUB_URL=https://localhost:8443 npm run dev   # HUB_URL defaults to https://localhost:8443
```

Open the printed URL (default `http://localhost:5173`). The dev server proxies `/api/*`
to `HUB_URL`, so the browser only talks to the Vite origin. On first load the GUI asks
for the **Dev-Token** (the hub's static dev admin token); it is stored in this browser's
`localStorage` and cleared by "Abmelden" or any 401 response.

To use a hub elsewhere on your network, e.g. `HUB_URL=https://hub.example.lan:8443 npm run dev`
(the dev proxy does not verify the hub certificate; it runs on your machine only).

## Environment variables

| Variable  | Used by         | Default                 | Meaning                         |
|-----------|-----------------|-------------------------|---------------------------------|
| `HUB_URL` | `npm run dev`   | `https://localhost:8443` | Target of the `/api` dev proxy. |

## Scripts

| Script              | What it does                                       |
|---------------------|----------------------------------------------------|
| `npm run dev`       | Vite dev server with `/api` proxy                  |
| `npm run build`     | Type-check, then production build into `web/dist/` |
| `npm run typecheck` | `tsc` only                                         |
| `npm run lint`      | ESLint, zero warnings allowed                      |
| `npm test`          | Vitest (jsdom), fetch is mocked, no network        |

## Layout

- `src/api/client.ts` – REST client (bearer token, 401 handling, action error mapping)
- `src/api/sse.ts` – fetch-based SSE reader (EventSource cannot send auth headers) with
  reconnect and exponential backoff; the node list is refetched after every (re)connect
- `src/state/store.ts` – reducer for nodes, open node detail and stream events
- `src/components/` – views; routing is hash based (`#/`, `#/nodes/<id>`)
- `src/i18n/` – i18next setup and the German catalogue; add `en.json` for English
