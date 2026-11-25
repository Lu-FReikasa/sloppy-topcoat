// The programs as the desktop draws them (docs/DESKTOP.md): their title, their colour, the views
// their navigation offers.
import { APPLICATION_HUE } from "../chat/applications";
import { moduleMeta } from "../layout/modules/meta";
import type { UiModuleInfo, Workspace } from "../layout/types";
import type { ProgramInfo } from "./state";

type T = (key: string, o?: Record<string, unknown>) => string;

/** A fixed colour per built-in program (the same tag in the inbox); any other gets one from its key. */
const HUES: Record<string, number> = {
  ...APPLICATION_HUE,
  inbox: 45,
  files: 40,
  incognito: 320,
  settings: 160,
};

export function programHue(key: string): number {
  const fixed = HUES[key];
  if (fixed !== undefined) return fixed;
  let h = 0;
  for (const c of key) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}

/** A three-letter code, as a display names its page (the control room shows it). */
const CODES: Record<string, string> = {
  inbox: "INB",
  chat: "CHT",
  research: "RES",
  dev: "DEV",
  files: "FIL",
  incognito: "INC",
  admin: "ADM",
  settings: "SET",
};

export function programCode(key: string): string {
  return CODES[key] ?? key.replace(/[^a-z0-9]/gi, "").slice(0, 3).toUpperCase();
}

/** The paper a program is written on in the desk look (docs/DESKTOP.md, section 4). */
const PAPERS: Record<string, string> = {
  inbox: "legal",
  chat: "plain",
  research: "card",
  dev: "ledger",
  files: "plain",
  incognito: "plain",
  admin: "carbon",
  settings: "green",
};

export function programPaper(key: string): string {
  return PAPERS[key] ?? "plain";
}

/** How far a slip lies askew in the desk look: a small fixed angle per program (degrees). */
export function programTilt(key: string): number {
  let h = 0;
  for (const c of key) h = (h * 17 + c.charCodeAt(0)) % 997;
  return Math.round(((h % 13) - 6) * 10) / 100; // -0.6 .. 0.6
}

export function programTitle(w: Workspace, t: T): string {
  return w.title_key ? t(w.title_key, { defaultValue: w.title }) : w.title;
}

/** The views of a program its navigation lists: known to this build, allowed for the person, and
 *  not one that only a link can open (a node, a model). */
export function navViews(w: Workspace, modules: Map<string, UiModuleInfo>): string[] {
  return w.modules.filter((id) => {
    const meta = moduleMeta(id);
    return meta !== undefined && !meta.needsProps && (modules.get(id)?.allowed ?? false);
  });
}

export function programInfo(w: Workspace, modules: Map<string, UiModuleInfo>): ProgramInfo {
  const views = navViews(w, modules);
  const first = w.open?.[0] && views.includes(w.open[0]) ? w.open[0] : (views[0] ?? w.modules[0] ?? "");
  return { key: w.key, modules: w.modules, first };
}

export function viewTitle(module: string, t: T): string {
  const meta = moduleMeta(module);
  return meta ? t(meta.titleKey) : module;
}
