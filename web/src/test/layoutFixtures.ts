// Test data for the desktop tests: a hub bootstrap and small helpers. Not shipped (tests import it).
import type { UiBootstrap, UiModuleInfo, Workspace } from "../layout/types";

export const AGENT = {
  tool_scope: { nodes: null, groups: null, internet: null },
  persona: null,
  model_role: null,
} as const;

export function workspace(key: string, modules: string[], over: Partial<Workspace> = {}): Workspace {
  return {
    key,
    title: key[0]?.toUpperCase() + key.slice(1),
    title_key: null,
    icon: "",
    position: 10,
    modules,
    agent: { ...AGENT, tool_scope: { ...AGENT.tool_scope } },
    requires_role: null,
    // an admin's own program: its title stays as given. A shipped one (builtin) without a
    // title_key would read `workspace.<key>` from the catalogue (issue #181).
    builtin: false,
    rev: 1,
    ...over,
  };
}

export function moduleInfo(id: string, over: Partial<UiModuleInfo> = {}): UiModuleInfo {
  return { id, kind: id.startsWith("layout.") ? "widget" : "page", admin: false, needs: null, allowed: true, ...over };
}

export function bootstrap(workspaces: Workspace[], over: Partial<UiBootstrap> = {}): UiBootstrap {
  const ids = new Set(workspaces.flatMap((w) => w.modules));
  for (const m of ["page.library", "page.memory", "page.archive", "page.tasks"]) ids.add(m);
  return {
    v: 1,
    access: { admin: true, profile: null, denied_tools: [] },
    prefs: {
      hue: 140,
      look: "terminal",
      desktop: { open: workspaces.slice(0, 1).map((w) => w.key), minimised: [], focus: workspaces[0]?.key ?? null, maximised: null, views: {} },
    },
    modules: [...ids].map((id) => moduleInfo(id)),
    workspaces,
    ...over,
  };
}

/** jsdom has no matchMedia: say whether the screen is narrow (one program at a time) or wide. */
export function setNarrow(narrow: boolean): void {
  window.matchMedia = ((query: string) => ({
    matches: narrow && query.includes("max-width"),
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}
