/**
 * The tabs of the node page (issue #176) and the order of its model list, as plain functions.
 */
import type { ModelInfo } from "../types";

export type NodeTab = "models" | "history" | "rights" | "maintenance";

const ALL_TABS: readonly NodeTab[] = ["models", "history", "rights", "maintenance"];

/** A tool or dev node holds no models: no Models tab, and its page opens on what it offers. */
export function nodeTabs(isTools: boolean): NodeTab[] {
  return isTools ? ALL_TABS.filter((x) => x !== "models") : [...ALL_TABS];
}

export function defaultTab(isTools: boolean): NodeTab {
  return isTools ? "rights" : "models";
}

/** The tab the person last opened on a node page, when this node has it; else the default. */
export function pickTab(stored: string | null, isTools: boolean): NodeTab {
  const tabs = nodeTabs(isTools);
  return tabs.find((x) => x === stored) ?? defaultTab(isTools);
}

/** localStorage key: the choice belongs to this browser profile (see the PR of #176). */
export const TAB_KEY = "topcoat.node.tab";

export function rememberedTab(): string | null {
  try {
    return window.localStorage.getItem(TAB_KEY);
  } catch {
    return null;
  }
}

export function rememberTab(tab: NodeTab): void {
  try {
    window.localStorage.setItem(TAB_KEY, tab);
  } catch {
    /* not remembered; the tab still holds for this page */
  }
}

/**
 * The model list in two parts: what is loaded or kept ready (residents, the house model among
 * them) first, always shown; the rest after it, narrowed by the filter text.
 */
export function splitModels(
  models: readonly ModelInfo[],
  residents: ReadonlySet<string>,
  filter: string,
): { top: ModelInfo[]; rest: ModelInfo[]; hidden: number } {
  const first = (m: ModelInfo) => m.loaded || residents.has(m.name);
  // loaded before kept-ready; the node's order holds inside each group
  const top = [...models.filter((m) => m.loaded), ...models.filter((m) => !m.loaded && first(m))];
  const others = models.filter((m) => !first(m));
  const needle = filter.trim().toLowerCase();
  const rest = needle ? others.filter((m) => m.name.toLowerCase().includes(needle)) : others;
  return { top, rest, hidden: others.length - rest.length };
}

/** History lists show the newest rows up to this count until "show more". */
export const HISTORY_SHOWN = 10;
