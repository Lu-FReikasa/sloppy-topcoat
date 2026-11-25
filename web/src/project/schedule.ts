// Dates for the calendar and the Gantt chart (PPH-04, 04i): moving a task by whole days, the grid of
// a month, and the geometry of the bars. Pure: the zone is an argument, and a day is {y, m, d}.
//
// A task is moved by whole **calendar days of the person's zone**: 09:00 stays 09:00 across the change
// of summer time. Every date a task has (start, due, end) moves by the same number of days, so a
// task keeps its length.
import { dayIn, zonedIso, type Day } from "./quickAdd";
import { toInputValue } from "./time";
import type { ProjectNode } from "./types";

export type { Day };

const MS_DAY = 86_400_000;
/** Days an old, finished task may lie back and still count for the range of the Gantt chart. */
const RECENT_DAYS = 30;
/** The chart is never wider than this many days. */
export const MAX_DAYS = 730;
/** How far back a chart that is too long still looks. */
const BACK_DAYS = 60;

export const sameDay = (a: Day, b: Day): boolean => a.y === b.y && a.m === b.m && a.d === b.d;
export const dayKey = (d: Day): string => `${d.y}-${String(d.m).padStart(2, "0")}-${String(d.d).padStart(2, "0")}`;
export const parseDayKey = (text: string): Day | null => {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  return m ? { y: Number(m[1]), m: Number(m[2]), d: Number(m[3]) } : null;
};
const utc = (d: Day): number => Date.UTC(d.y, d.m - 1, d.d);

export function addDays(d: Day, n: number): Day {
  const t = new Date(utc(d) + n * MS_DAY);
  return { y: t.getUTCFullYear(), m: t.getUTCMonth() + 1, d: t.getUTCDate() };
}

/** Whole days from `a` to `b` (negative when `b` is earlier). */
export const daysBetween = (a: Day, b: Day): number => Math.round((utc(b) - utc(a)) / MS_DAY);

/** The calendar day of an instant in `zone`, or null for none. */
export function dayOf(iso: string | null | undefined, zone: string): Day | null {
  if (!iso) return null;
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? null : dayIn(at, zone);
}

/** An instant moved by whole days of `zone`, the clock time kept. */
export function shiftIso(iso: string, days: number, zone: string): string {
  const wall = toInputValue(iso, zone); // 2026-10-30T17:00 in the zone
  const day = parseDayKey(wall.slice(0, 10))!;
  const [h, min] = wall.slice(11).split(":").map(Number) as [number, number];
  return zonedIso(addDays(day, days), h, min, zone);
}

/** The dates of a task, moved by whole days: only the ones it has, all by the same number. */
export function shiftDates(
  node: Pick<ProjectNode, "start_at" | "due_at" | "end_at">,
  days: number,
  zone: string,
): { start_at?: string; due_at?: string; end_at?: string } {
  const out: { start_at?: string; due_at?: string; end_at?: string } = {};
  for (const key of ["start_at", "due_at", "end_at"] as const) {
    const value = node[key];
    if (value) out[key] = shiftIso(value, days, zone);
  }
  return out;
}

/** The day a task moved **to** `target`: by how many days its main date has to move. The main date is
 *  the due date, else the end, else the start. */
export function mainDay(node: Pick<ProjectNode, "start_at" | "due_at" | "end_at">, zone: string): Day | null {
  return dayOf(node.due_at ?? node.end_at ?? node.start_at, zone);
}

/** The patch that puts a task on `target`: its dates move together; a task with no date gets the end
 *  of that day as its due date. */
export function moveTo(
  node: Pick<ProjectNode, "start_at" | "due_at" | "end_at">,
  target: Day,
  zone: string,
): { start_at?: string; due_at?: string; end_at?: string } {
  const main = mainDay(node, zone);
  if (!main) return { due_at: zonedIso(target, 23, 59, zone) };
  return shiftDates(node, daysBetween(main, target), zone);
}

/** Monday of the week of a day. */
export function weekStart(d: Day): Day {
  const weekday = (new Date(utc(d)).getUTCDay() + 6) % 7; // 0 = Monday
  return addDays(d, -weekday);
}

/** The weeks of a month as rows of seven days, Monday first, with the days of the neighbouring months
 *  that fill the first and the last row. */
export function monthGrid(year: number, month: number): Day[][] {
  const first: Day = { y: year, m: month, d: 1 };
  let day = weekStart(first);
  const weeks: Day[][] = [];
  do {
    weeks.push(Array.from({ length: 7 }, (_, i) => addDays(day, i)));
    day = addDays(day, 7);
  } while (day.m === month || (day.y === year && day.m < month) || (day.y < year));
  return weeks;
}

export const weekOf = (d: Day): Day[] => Array.from({ length: 7 }, (_, i) => addDays(weekStart(d), i));

export function addMonths(d: Day, n: number): Day {
  const t = new Date(Date.UTC(d.y, d.m - 1 + n, 1));
  return { y: t.getUTCFullYear(), m: t.getUTCMonth() + 1, d: 1 };
}

// --- the Gantt chart -----------------------------------------------------------------------------

export interface Bar {
  id: string;
  row: number;
  kind: "task" | "summary" | "milestone" | "undated";
  /** First and last day it covers (the same for a milestone). */
  from: Day | null;
  to: Day | null;
  x: number;
  width: number;
  /** The bar lies wholly before or after the chart: it is marked at the edge instead. */
  offscreen: "before" | "after" | null;
}

export interface Arrow {
  from: string;
  to: string;
}

export interface Layout {
  start: Day;
  days: number;
  bars: Bar[];
  arrows: Arrow[];
  /** Ids a task waits for that are not in the chart (another project's, or without a date). */
  outside: Record<string, string[]>;
}

/** The chart of a project: a row per entry in the order of the tree, a bar from start to end (or due),
 *  a diamond for a milestone, a summary bar over the bars of a topic's children, and an arrow from
 *  every task a task waits for. Reports are not on it. */
export function ganttLayout(nodes: ProjectNode[], zone: string, dayWidth: number): Layout {
  const rows = nodes.filter((n) => n.kind !== "report");
  const span = (n: ProjectNode): [Day, Day] | null => {
    const from = dayOf(n.start_at ?? n.due_at ?? n.end_at, zone);
    const to = dayOf(n.end_at ?? n.due_at ?? n.start_at, zone);
    if (!from || !to) return null;
    return daysBetween(from, to) < 0 ? [to, from] : [from, to];
  };
  const byParent = new Map<string | null, ProjectNode[]>();
  for (const n of rows) byParent.set(n.parent_id, [...(byParent.get(n.parent_id) ?? []), n]);
  const summary = (n: ProjectNode): [Day, Day] | null => {
    let lo: Day | null = null;
    let hi: Day | null = null;
    const walk = (id: string) => {
      for (const c of byParent.get(id) ?? []) {
        const s = c.kind === "topic" ? null : span(c);
        if (s) {
          if (!lo || daysBetween(lo, s[0]) < 0) lo = s[0];
          if (!hi || daysBetween(hi, s[1]) > 0) hi = s[1];
        }
        walk(c.id);
      }
    };
    walk(n.id);
    return lo && hi ? [lo, hi] : null;
  };
  const spans = rows.map((n) => (n.kind === "topic" ? summary(n) : span(n)));
  const today = dayIn(new Date(), zone);
  // what sets the range: open work, and what ended lately. A task that was finished long ago stays in
  // its row but must not stretch the chart over years.
  const lately = addDays(today, -RECENT_DAYS);
  const core = spans.filter((s, i): s is [Day, Day] => {
    const n = rows[i]!;
    const open = n.status !== "done" && n.status !== "dropped";
    return s !== null && (n.kind === "topic" || open || daysBetween(lately, s[1]) >= 0);
  });
  const dated = core.length > 0 ? core : spans.filter((s): s is [Day, Day] => s !== null);
  let lo = dated.reduce<Day>((m, s) => (daysBetween(m, s[0]) < 0 ? s[0] : m), dated[0]?.[0] ?? today);
  let hi = dated.reduce<Day>((m, s) => (daysBetween(m, s[1]) > 0 ? s[1] : m), dated[0]?.[1] ?? addDays(today, 14));
  lo = addDays(lo, -3);
  hi = addDays(hi, 3);
  if (daysBetween(lo, hi) + 1 > MAX_DAYS) {
    // too long to draw: look at now, a little back and as far ahead as fits; what lies outside is
    // marked at the edge of its row
    const back = addDays(today, -BACK_DAYS);
    if (daysBetween(lo, back) > 0) lo = back;
    if (daysBetween(lo, hi) + 1 > MAX_DAYS) hi = addDays(lo, MAX_DAYS - 1);
  }
  const days = daysBetween(lo, hi) + 1;
  const bars: Bar[] = rows.map((n, row) => {
    const s = spans[row] ?? null;
    if (!s) return { id: n.id, row, kind: "undated", from: null, to: null, x: 0, width: 0, offscreen: null };
    const kind = n.kind === "topic" ? "summary" : n.is_milestone ? "milestone" : "task";
    return {
      id: n.id,
      row,
      kind,
      from: s[0],
      to: s[1],
      x: daysBetween(lo, s[0]) * dayWidth,
      width: kind === "milestone" ? dayWidth : (daysBetween(s[0], s[1]) + 1) * dayWidth,
      offscreen: daysBetween(lo, s[1]) < 0 ? "before" : daysBetween(hi, s[0]) > 0 ? "after" : null,
    };
  });
  const placed = new Set(bars.filter((b) => b.kind !== "undated").map((b) => b.id));
  const arrows: Arrow[] = [];
  const outside: Record<string, string[]> = {};
  for (const n of rows) {
    for (const dep of n.depends_on) {
      if (placed.has(dep) && placed.has(n.id)) arrows.push({ from: dep, to: n.id });
      else outside[n.id] = [...(outside[n.id] ?? []), dep];
    }
  }
  return { start: lo, days, bars, arrows, outside };
}
