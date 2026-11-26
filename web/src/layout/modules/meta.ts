// The modules of the web build, as data (ARCHITECTURE D60). Which module opens which page,
// what it is called, whether it needs the admin role or a tool group, and how big a free-floating
// shell makes it by default. What a module *renders* is in `render.tsx`; this file has no React so
// that the tests and the hub's manifest (`proto/ui/modules.json`) can be compared with it.
import { docOfLibraryId } from "../../desktop/docs";
import type { Route } from "../../route";

export interface ModuleMeta {
  /** Dotted, stable, never reused: it is stored in layout documents. */
  id: string;
  /** i18n key of the title. */
  titleKey: string;
  kind: "page" | "widget";
  /** Needs the admin role. */
  admin: boolean;
  /** A tool group that must not be denied for the person. */
  needs?: string;
  /** The `Route["view"]` this module shows, so an address can open it. */
  view?: Route["view"];
  /** The names of the props it takes (`#/tasks/<id>` is `{id}`). */
  props?: readonly string[];
  /** Cannot be opened without them (a node page needs a node): it is not offered in "add window". */
  needsProps?: boolean;
}

const page = (
  id: string,
  titleKey: string,
  view: Route["view"],
  extra: Partial<ModuleMeta> = {},
): ModuleMeta => ({ id, titleKey, kind: "page", admin: false, view, ...extra });

export const MODULES: readonly ModuleMeta[] = [
  { id: "page.home", titleKey: "home.title", kind: "page", admin: false },
  page("page.chat", "header.chat", "chat", { props: ["id"] }),
  page("page.chat.research", "workspace.research", "chat"),
  page("page.chat.dev", "workspace.dev", "chat"),
  page("page.chat.admin", "workspace.admin", "chat"),
  page("page.chat.project", "workspace.project", "chat"),
  page("page.inbox", "inbox.title", "start"),
  { id: "page.incognito", titleKey: "incognito.title", kind: "page", admin: false },
  { id: "page.themes", titleKey: "header.themes", kind: "page", admin: true },
  // the theme workshop: a theme talked into shape, drawn live (opened from a task or the themes)
  { id: "page.theme-workshop", titleKey: "header.themeWorkshop", kind: "page", admin: true, props: ["task", "base", "key"] },
  page("page.handovers", "handover.title", "handovers", { props: ["from", "incognito", "id"] }),
  page("page.tasks", "header.tasks", "tasks", { props: ["id"], needsProps: true }),
  page("page.library", "header.library", "library", { props: ["id"] }),
  page("page.archive", "header.archive", "archive"),
  // a document of the Ablage in a window of its own (desktop/docs.ts): opened only from the list
  { id: "page.document", titleKey: "shelf.document", kind: "page", admin: false, props: ["kind", "id", "title"], needsProps: true },
  page("page.wiki", "header.wiki", "wiki", { props: ["id"] }),
  page("page.sheets", "header.sheets", "sheets", { props: ["id"] }),
  { id: "page.program", titleKey: "program.title", kind: "page", admin: false },
  page("page.memory", "header.memory", "memory"),
  page("page.knowledge", "header.knowledge", "knowledge", { needs: "notes" }),
  { id: "page.research", titleKey: "research.title", kind: "page", admin: false },
  page("page.project", "header.project", "project", { props: ["project", "node"] }),
  page("page.instructions", "header.instructions", "instructions"),
  page("page.settings", "header.settings", "settings", { props: ["section", "app", "focus"] }),
  page("page.overview", "header.overview", "overview", { admin: true }),
  page("page.node", "layout.module.node", "node", { admin: true, props: ["id"], needsProps: true }),
  page("page.models", "header.models", "models", { admin: true }),
  page("page.model", "layout.module.model", "model", { admin: true, props: ["name"], needsProps: true }),
  page("page.scheduling", "header.scheduling", "scheduling", { admin: true }),
  { id: "page.applications", titleKey: "applications.title", kind: "page", admin: true },
  { id: "page.task_kinds", titleKey: "taskKinds.title", kind: "page", admin: true },
  page("page.releases", "header.releases", "releases", { admin: true }),
  page("page.params", "header.params", "params", { admin: true }),
  page("page.programs", "header.programs", "programs", { admin: true }),
  page("page.inventory", "header.inventory", "inventory", { admin: true, needs: "host" }),
  page("page.identity", "header.identity", "identity", { admin: true, needs: "identity" }),
  page("page.repos", "header.repos", "repos", { admin: true, needs: "workshop" }),
  page("page.workshop", "header.workshop", "workshop", { admin: true, needs: "workshop" }),
  { id: "page.files", titleKey: "header.files", kind: "page", admin: true, needs: "workshop" },
  page("page.care", "header.care", "care", { admin: true }),
  { id: "page.sources", titleKey: "header.sources", kind: "page", admin: true, needs: "notes" },
  page("page.access", "header.access", "access", { admin: true }),
  {
    id: "layout.workspaces",
    titleKey: "layout.module.workspaces",
    kind: "widget",
    admin: true,
  },
];

const BY_ID = new Map(MODULES.map((m) => [m.id, m]));

export function moduleMeta(id: string): ModuleMeta | undefined {
  return BY_ID.get(id);
}

export const KNOWN_MODULES: ReadonlySet<string> = new Set(MODULES.map((m) => m.id));

type Props = Record<string, string | number | boolean | null>;

/** The module and props an address stands for, or null: the workspaces themselves, and the bare
 *  address (`#/`), which names no page and keeps the workspace the person used last. Retired
 *  addresses (bookmarks of pages that became a tab or a section) lead to their new home; an
 *  address nobody knows leads to Start, which says so (`moved`). */
export function routeToModule(route: Route): { module: string; props: Props; moved?: true } | null {
  switch (route.view) {
    case "w":
    case "start":
      return null;
    case "unknown":
      return { module: "page.home", props: {}, moved: true };
    // the menu of everything is the overview of the applications on Start
    case "more":
      return { module: "page.home", props: {} };
    case "research":
      return { module: "page.research", props: {} };
    // pages that became a tab of a view or a section of Settings (phase 4)
    case "params":
      return { module: "page.models", props: { tab: "params" } };
    case "identity":
      return { module: "layout.workspaces", props: { tab: "identity" } };
    case "memory":
      return { module: "page.settings", props: { section: "memory" } };
    case "instructions":
      return { module: "page.settings", props: { section: "assistant" } };
    case "talk":
      return { module: "page.chat", props: {} };
    case "library": {
      // an address of a document opens the document's own window
      const doc = route.id ? docOfLibraryId(route.id) : null;
      if (doc) return { module: "page.document", props: { kind: doc.kind, id: doc.id } };
      return { module: "page.library", props: {} };
    }
    case "chat":
    case "tasks":
    case "wiki":
    case "sheets":
    case "handovers":
      return { module: `page.${route.view}`, props: route.id ? { id: route.id } : {} };
    case "node":
      return { module: "page.node", props: { id: route.id } };
    case "model":
      return { module: "page.model", props: { name: route.name } };
    default: {
      const m = MODULES.find((x) => x.view === route.view);
      return m ? { module: m.id, props: {} } : null;
    }
  }
}
