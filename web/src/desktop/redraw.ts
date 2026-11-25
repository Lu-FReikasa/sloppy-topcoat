/** When a program window must draw again (the desktop plan, P1): the stream bumps a counter of the
 *  module context on every agent step, chat message and node report, and every open window drew
 *  its whole page again for each of them. A window now draws again only when something its view
 *  reads has changed. A view that is not listed here reads everything (safe for a new page). */
import type { ModuleCtx } from "../layout/modules/render";
import type { UiBootstrap } from "../layout/types";

type Field = keyof ModuleCtx;

/** What each view reads of the module context besides `api` and `me` (layout/modules/render.tsx). */
const READS: Record<string, readonly Field[]> = {
  "page.inbox": ["inboxTick"],
  "page.handovers": ["inboxTick"],
  "page.tasks": ["tick"],
  "page.library": ["tick"],
  "page.overview": ["nodes", "nodesLoaded", "nodesError", "isAdmin"],
  "page.node": ["detail", "dispatch", "epoch"],
  "page.models": ["nodes", "nodesLoaded", "nodesError", "refreshNodes"],
  "page.model": ["nodes", "nodesLoaded", "nodesError", "refreshNodes"],
  "page.workshop": ["nodes"],
  "page.files": ["nodes"],
  "page.identity": ["isAdmin"],
  "layout.workspaces": ["isAdmin"],
  // only the api (and me)
  "page.incognito": [],
  "page.themes": [],
  "page.theme-workshop": [],
  "page.applications": [],
  "page.task_kinds": [],
  "page.chat": [],
  "page.chat.research": [],
  "page.chat.dev": [],
  "page.chat.admin": [],
  "page.chat.project": [],
  "page.wiki": [],
  "page.document": [],
  "page.archive": [],
  "page.program": [],
  "page.memory": [],
  "page.knowledge": [],
  "page.project": [],
  "page.instructions": [],
  "page.settings": [],
  "page.research": [],
  "page.home": [],
  "page.scheduling": [],
  "page.releases": [],
  "page.params": [],
  "page.programs": [],
  "page.inventory": [],
  "page.repos": [],
  "page.care": [],
  "page.access": [],
};

const ALL = Object.keys({
  api: 0,
  me: 0,
  isAdmin: 0,
  nodes: 0,
  nodesLoaded: 0,
  nodesError: 0,
  refreshNodes: 0,
  tick: 0,
  inboxTick: 0,
  epoch: 0,
  detail: 0,
  dispatch: 0,
} satisfies Record<Field, 0>) as Field[];

/** Does `module` see the same in both contexts? */
export function sameCtx(module: string, a: ModuleCtx, b: ModuleCtx): boolean {
  if (a === b) return true;
  const fields = READS[module];
  const check: readonly Field[] = fields ? ["api", "me", ...fields] : ALL;
  return check.every((f) => a[f] === b[f]);
}

/** Does `module` see the same of the interface data? The preferences change with every focus and
 *  are not read by a page; what is offered (the modules) is, and the rights page reads it all. */
export function sameBoot(module: string, a: UiBootstrap | null, b: UiBootstrap | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  if (module === "layout.workspaces") return false;
  return a.modules === b.modules && a.workspaces === b.workspaces;
}
