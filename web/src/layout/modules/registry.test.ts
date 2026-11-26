import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import de from "../../i18n/de.json";
import en from "../../i18n/en.json";
import { isAdminView, parseRoute, type Route } from "../../route";
import { KNOWN_MODULES, MODULES, moduleMeta, routeToModule } from "./meta";

interface ManifestModule {
  id: string;
  kind: string;
  admin: boolean;
  needs?: string;
}
const manifest = JSON.parse(readFileSync(resolve(process.cwd(), "../proto/ui/modules.json"), "utf8")) as {
  modules: ManifestModule[];
};

function lookup(catalogue: unknown, key: string): unknown {
  return key.split(".").reduce<unknown>((o, k) => (o && typeof o === "object" ? (o as Record<string, unknown>)[k] : undefined), catalogue);
}

describe("the module registry and the hub's list of modules agree", () => {
  it("lists the same ids", () => {
    expect(MODULES.map((m) => m.id).sort()).toEqual(manifest.modules.map((m) => m.id).sort());
    expect(new Set(MODULES.map((m) => m.id)).size).toBe(MODULES.length);
  });

  it("agrees on kind, admin and the tool group a module needs", () => {
    for (const m of manifest.modules) {
      const meta = moduleMeta(m.id);
      expect(meta, m.id).toBeDefined();
      expect(meta?.kind, m.id).toBe(m.kind);
      expect(meta?.admin, m.id).toBe(m.admin);
      expect(meta?.needs ?? null, m.id).toBe(m.needs ?? null);
    }
    expect(KNOWN_MODULES.size).toBe(manifest.modules.length);
  });

  it("has a title in both catalogues for every module", () => {
    for (const m of MODULES) {
      for (const [name, cat] of [["en", en], ["de", de]] as const) {
        expect(typeof lookup(cat, m.titleKey), `${name}: ${m.titleKey}`).toBe("string");
      }
    }
  });
});

// `Record<Route["view"], …>` fails to compile when a page is added to the routes and not listed here,
// which is the point: a new page needs a module (or an explicit "none").
const SAMPLE: Record<Route["view"], Route> = {
  start: { view: "start" },
  talk: { view: "talk" },
  more: { view: "more" },
  archive: { view: "archive" },
  overview: { view: "overview" },
  models: { view: "models" },
  model: { view: "model", name: "m" },
  access: { view: "access" },
  repos: { view: "repos" },
  workshop: { view: "workshop" },
  care: { view: "care" },
  scheduling: { view: "scheduling" },
  params: { view: "params" },
  programs: { view: "programs" },
  chat: { view: "chat", id: "c1" },
  settings: { view: "settings" },
  memory: { view: "memory" },
  instructions: { view: "instructions" },
  knowledge: { view: "knowledge" },
  project: { view: "project" },
  inventory: { view: "inventory" },
  releases: { view: "releases" },
  identity: { view: "identity" },
  tasks: { view: "tasks", id: "t1" },
  library: { view: "library", id: "l1" },
  wiki: { view: "wiki", id: "p1" },
  sheets: { view: "sheets", id: "s1" },
  handovers: { view: "handovers", id: "h1" },
  node: { view: "node", id: "n1" },
  w: { view: "w", key: "talk" },
  research: { view: "research" },
  unknown: { view: "unknown" },
};

describe("every address of a page (old bookmarks, links in pages) leads to a view", () => {
  it("maps each route to a module, except the bare address and the workspaces themselves", () => {
    for (const [view, route] of Object.entries(SAMPLE)) {
      const target = routeToModule(route);
      if (view === "start" || view === "w") expect(target, view).toBeNull();
      else {
        expect(target, view).not.toBeNull();
        expect(moduleMeta(target?.module ?? ""), view).toBeDefined();
      }
    }
  });

  it("keeps the admin pages admin-only, and nothing else", () => {
    for (const [view, route] of Object.entries(SAMPLE)) {
      const target = routeToModule(route);
      if (!target) continue;
      expect(moduleMeta(target.module)?.admin, view).toBe(isAdminView(route.view));
    }
  });

  it("carries the id or name of the address as props", () => {
    expect(routeToModule({ view: "chat", id: "c1" })).toEqual({ module: "page.chat", props: { id: "c1" } });
    expect(routeToModule({ view: "chat" })).toEqual({ module: "page.chat", props: {} });
    expect(routeToModule({ view: "tasks", id: "t1" })).toEqual({ module: "page.tasks", props: { id: "t1" } });
    expect(routeToModule({ view: "node", id: "n1" })).toEqual({ module: "page.node", props: { id: "n1" } });
    expect(routeToModule({ view: "model", name: "m" })).toEqual({ module: "page.model", props: { name: "m" } });
  });

  it("opens the module for an old admin address too", () => {
    expect(routeToModule(parseRoute("#/admin/nodes"))?.module).toBe("page.overview");
    expect(routeToModule(parseRoute("#/dev"))?.module).toBe("page.workshop");
    expect(routeToModule(parseRoute("#/admin/nodes/abc"))).toEqual({ module: "page.node", props: { id: "abc" } });
  });

  it("does not offer a module that cannot open without props", () => {
    expect(MODULES.filter((m) => m.needsProps).map((m) => m.id).sort()).toEqual(["page.document", "page.model", "page.node", "page.tasks"]);
  });
});

describe("the workspace address", () => {
  it("is #/w or #/w/<key>", () => {
    expect(parseRoute("#/w")).toEqual({ view: "w" });
    expect(parseRoute("#/w/")).toEqual({ view: "w" });
    expect(parseRoute("#/w/talk")).toEqual({ view: "w", key: "talk" });
    expect(parseRoute("#/w/my-room-2")).toEqual({ view: "w", key: "my-room-2" });
  });

  it("is not an admin page and ignores what is not a key", () => {
    expect(isAdminView("w")).toBe(false);
    expect(parseRoute("#/w/Bad Key")).toEqual({ view: "unknown" });
    expect(parseRoute("#/wx")).toEqual({ view: "unknown" });
  });
});
