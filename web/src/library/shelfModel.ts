// The Ablage as one list (2026-11-13): research reports, the reports of tasks and the conversations,
// each with its application and whether a person or the hub's own work (a project run, a block
// program) made it. Pure, so the rules are tested without a page.
import { applicationOf } from "../chat/applications";
import type { DocKind } from "../desktop/docs";
import type { Artifact, Conversation, Report, ReportStatus } from "../types";

export type ShelfKind = DocKind | "chat";
export type KindFilter = "all" | "documents" | "chats";

export interface ShelfItem {
  key: string;
  kind: ShelfKind;
  id: string;
  title: string;
  /** null: not known (an artifact of a task without an application). */
  application: string | null;
  updated: string | null;
  unread: boolean;
  /** Made by the hub's own work, not asked for by the person. */
  auto: boolean;
  status?: ReportStatus;
  version?: number;
}

/** One list, newest first. A task that filed a research report and (before 2026-11-13) a library
 *  copy of it as well is listed once, as the report. */
export function shelfItems(
  reports: Report[],
  artifacts: Artifact[],
  chats: Conversation[],
): ShelfItem[] {
  const reportTasks = new Set(reports.map((r) => r.task_id).filter(Boolean));
  const out: ShelfItem[] = [
    ...reports.map<ShelfItem>((r) => ({
      key: `r:${r.id}`,
      kind: "report",
      id: r.id,
      title: r.title,
      application: r.application,
      updated: r.updated_at,
      unread: r.unread,
      auto: r.auto ?? false,
      status: r.status,
      version: r.current_version,
    })),
    ...artifacts
      .filter((a) => !(a.task_id && reportTasks.has(a.task_id)))
      .map<ShelfItem>((a) => ({
        key: `a:${a.id}`,
        kind: "artifact",
        id: a.id,
        title: a.title,
        application: a.application ?? null,
        updated: a.updated_at,
        unread: a.unread,
        auto: a.auto ?? false,
      })),
    ...chats.map<ShelfItem>((c) => ({
      key: `c:${c.id}`,
      kind: "chat",
      id: c.id,
      title: c.title,
      application: applicationOf(c),
      updated: c.updated_at,
      unread: false,
      auto: false,
    })),
  ];
  return out.sort((a, b) => (b.updated ?? "").localeCompare(a.updated ?? ""));
}

export interface ShelfFilter {
  app: string; // "all" or an application
  kind: KindFilter;
  auto: boolean; // show what the hub's own work made
  query: string;
}

export function matches(item: ShelfItem, f: ShelfFilter): boolean {
  if (f.app !== "all" && item.application !== f.app) return false;
  if (f.kind === "documents" && item.kind === "chat") return false;
  if (f.kind === "chats" && item.kind !== "chat") return false;
  if (item.auto && !f.auto) return false;
  const q = f.query.trim().toLowerCase();
  return !q || item.title.toLowerCase().includes(q);
}

/** The applications the list holds (for the filter), with how many entries each, in the given order
 *  first. Entries without an application are counted under none. */
export function appCounts(items: ShelfItem[], order: readonly string[]): { application: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const i of items) if (i.application) counts.set(i.application, (counts.get(i.application) ?? 0) + 1);
  const known = order.filter((a) => counts.has(a));
  const others = [...counts.keys()].filter((a) => !order.includes(a)).sort();
  return [...known, ...others].map((application) => ({ application, count: counts.get(application) ?? 0 }));
}
