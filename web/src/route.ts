import { useSyncExternalStore } from "react";

export type Route =
  | { view: "start" }
  | { view: "talk" }
  | { view: "more" }
  | { view: "archive" }
  | { view: "overview" }
  | { view: "models" }
  | { view: "model"; name: string }
  | { view: "access" }
  | { view: "repos" }
  | { view: "workshop" }
  | { view: "care" }
  | { view: "scheduling" }
  | { view: "params" }
  | { view: "programs" }
  | { view: "chat"; id?: string }
  | { view: "settings" }
  | { view: "memory" }
  | { view: "instructions" }
  | { view: "knowledge" }
  | { view: "project" }
  | { view: "inventory" }
  | { view: "releases" }
  | { view: "identity" }
  | { view: "tasks"; id?: string }
  | { view: "library"; id?: string }
  | { view: "wiki"; id?: string }
  | { view: "sheets"; id?: string }
  | { view: "handovers"; id?: string }
  | { view: "node"; id: string }
  | { view: "w"; key?: string };

/** `#/admin/…` is the admin area. Its pages still answer on their old addresses (bookmarks),
 *  see :func:`adminHash`. */
const ADMIN_PREFIX = /^\/admin(?=\/|$)/;

export function parseRoute(hash: string): Route {
  let path = hash.replace(/^#/, "");
  if (ADMIN_PREFIX.test(path)) {
    path = path.replace(ADMIN_PREFIX, "") || "/nodes";
    const inner = parseRoute(`#${path}`);
    return isAdminView(inner.view) ? inner : { view: "overview" };
  }
  const ws = /^\/w(?:\/([a-z0-9][a-z0-9-]*))?\/?$/.exec(path);
  if (ws) return ws[1] ? { view: "w", key: ws[1] } : { view: "w" };
  if (/^\/talk\/?$/.test(path)) return { view: "talk" };
  if (/^\/more\/?$/.test(path)) return { view: "more" };
  if (/^\/archive\/?$/.test(path)) return { view: "archive" };
  if (/^\/nodes\/?$/.test(path)) return { view: "overview" };
  if (/^\/models\/?$/.test(path)) return { view: "models" };
  const mo = /^\/models\/([^/]+)\/?$/.exec(path);
  if (mo?.[1]) {
    try {
      return { view: "model", name: decodeURIComponent(mo[1]) };
    } catch {
      return { view: "models" };
    }
  }
  if (/^\/access\/?$/.test(path)) return { view: "access" };
  if (/^\/repos\/?$/.test(path)) return { view: "repos" };
  if (/^\/dev\/?$/.test(path)) return { view: "workshop" };
  if (/^\/care\/?$/.test(path)) return { view: "care" };
  if (/^\/scheduling\/?$/.test(path)) return { view: "scheduling" };
  if (/^\/params\/?$/.test(path)) return { view: "params" };
  if (/^\/programs\/?$/.test(path)) return { view: "programs" };
  if (/^\/settings\/?$/.test(path)) return { view: "settings" };
  if (/^\/memory\/?$/.test(path)) return { view: "memory" };
  if (/^\/instructions\/?$/.test(path)) return { view: "instructions" };
  if (/^\/knowledge\/?$/.test(path)) return { view: "knowledge" };
  if (/^\/project\/?$/.test(path)) return { view: "project" };
  if (/^\/inventory\/?$/.test(path)) return { view: "inventory" };
  if (/^\/releases\/?$/.test(path)) return { view: "releases" };
  if (/^\/identity\/?$/.test(path)) return { view: "identity" };
  const k = /^\/tasks(?:\/([^/]+))?\/?$/.exec(path);
  if (k) {
    try {
      return k[1]
        ? { view: "tasks", id: decodeURIComponent(k[1]) }
        : { view: "tasks" };
    } catch {
      return { view: "tasks" };
    }
  }
  const l = /^\/library(?:\/([^/]+))?\/?$/.exec(path);
  if (l) {
    try {
      return l[1]
        ? { view: "library", id: decodeURIComponent(l[1]) }
        : { view: "library" };
    } catch {
      return { view: "library" };
    }
  }
  const ho = /^\/handovers(?:\/([^/]+))?\/?$/.exec(path);
  if (ho) {
    try {
      return ho[1] ? { view: "handovers", id: decodeURIComponent(ho[1]) } : { view: "handovers" };
    } catch {
      return { view: "handovers" };
    }
  }
  const sh = /^\/sheets(?:\/([^/]+))?\/?$/.exec(path);
  if (sh) {
    try {
      return sh[1] ? { view: "sheets", id: decodeURIComponent(sh[1]) } : { view: "sheets" };
    } catch {
      return { view: "sheets" };
    }
  }
  const wk = /^\/wiki(?:\/([^/]+))?\/?$/.exec(path);
  if (wk) {
    try {
      return wk[1] ? { view: "wiki", id: decodeURIComponent(wk[1]) } : { view: "wiki" };
    } catch {
      return { view: "wiki" };
    }
  }
  const c = /^\/chat(?:\/([^/]+))?\/?$/.exec(path);
  if (c) {
    try {
      return c[1]
        ? { view: "chat", id: decodeURIComponent(c[1]) }
        : { view: "chat" };
    } catch {
      return { view: "chat" };
    }
  }
  const m = /^\/nodes\/([^/]+)\/?$/.exec(path);
  const raw = m?.[1];
  if (raw) {
    try {
      return { view: "node", id: decodeURIComponent(raw) };
    } catch {
      return { view: "start" };
    }
  }
  return { view: "start" };
}

export function nodeHref(id: string): string {
  return `#/admin/nodes/${encodeURIComponent(id)}`;
}

/** The workspaces (docs/DESKTOP.md): `#/w` is the last one used, `#/w/<key>` a named one. */
export function workspaceHref(key?: string): string {
  return key ? `#/w/${encodeURIComponent(key)}` : "#/w";
}

export const overviewHref = "#/admin/nodes";

export const modelsHref = "#/admin/models";

export function modelHref(name: string): string {
  return `#/admin/models/${encodeURIComponent(name)}`;
}

export const schedulingHref = "#/admin/scheduling";

export const settingsHref = "#/settings";

export const memoryHref = "#/memory";

export const instructionsHref = "#/instructions";

export function tasksHref(id?: string): string {
  return id ? `#/tasks/${encodeURIComponent(id)}` : "#/tasks";
}

export function libraryHref(id?: string): string {
  return id ? `#/library/${encodeURIComponent(id)}` : "#/library";
}

export function sheetsHref(id?: string): string {
  return id ? `#/sheets/${encodeURIComponent(id)}` : "#/sheets";
}

export function wikiHref(id?: string): string {
  return id ? `#/wiki/${encodeURIComponent(id)}` : "#/wiki";
}

export function chatHref(id?: string): string {
  return id ? `#/chat/${encodeURIComponent(id)}` : "#/chat";
}

function subscribe(cb: () => void): () => void {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
}

export function useHashRoute(): Route {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash);
  return parseRoute(hash);
}

/** Pages of the admin area: technical, opened on purpose. Everything else is the user area. */
const ADMIN_VIEWS: ReadonlySet<Route["view"]> = new Set([
  "overview",
  "node",
  "models",
  "model",
  "access",
  "repos",
  "workshop",
  "care",
  "scheduling",
  "params",
  "programs",
  "inventory",
  "releases",
  "identity",
]);

export function isAdminView(view: Route["view"]): boolean {
  return ADMIN_VIEWS.has(view);
}

/** The `#/admin/…` address for an admin page that was opened on its old address, else null. */
export function adminHash(hash: string): string | null {
  const path = hash.replace(/^#/, "");
  if (ADMIN_PREFIX.test(path)) return null;
  const route = parseRoute(hash);
  return isAdminView(route.view)
    ? `#/admin${path.startsWith("/") ? path : `/${path}`}`
    : null;
}
