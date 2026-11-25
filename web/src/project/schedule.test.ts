import { describe, expect, it } from "vitest";
import {
  MAX_DAYS,
  addDays,
  addMonths,
  daysBetween,
  ganttLayout,
  monthGrid,
  moveTo,
  shiftDates,
  shiftIso,
  weekOf,
  weekStart,
} from "./schedule";
import type { ProjectNode } from "./types";

const BERLIN = "Europe/Berlin";
/** An instant some days from today, at noon, so the test does not depend on the date it runs. */
function addIso(days: number): string {
  const t = new Date(Date.now() + days * 86_400_000);
  return `${t.toISOString().slice(0, 10)}T12:00:00+01:00`;
}

describe("moving dates by whole days", () => {
  it("keeps the clock time over the change of summer time", () => {
    // the clocks go back on 2026-10-25: 09:00 stays 09:00 on the other side
    expect(shiftIso("2026-10-24T09:00:00+02:00", 2, BERLIN)).toBe("2026-10-26T09:00:00+01:00");
    expect(shiftIso("2026-10-26T09:00:00+01:00", -2, BERLIN)).toBe("2026-10-24T09:00:00+02:00");
    expect(shiftIso("2026-03-28T09:00:00+01:00", 1, BERLIN)).toBe("2026-03-29T09:00:00+02:00");
  });

  it("moves every date a task has by the same number of days and leaves the others out", () => {
    expect(
      shiftDates({ start_at: "2026-10-29T08:00:00+01:00", due_at: "2026-10-31T17:00:00+01:00", end_at: null }, 3, BERLIN),
    ).toEqual({ start_at: "2026-11-01T08:00:00+01:00", due_at: "2026-11-03T17:00:00+01:00" });
    expect(shiftDates({ start_at: null, due_at: null, end_at: null }, 3, BERLIN)).toEqual({});
  });

  it("puts a task on a day by its main date, and gives an undated one the end of that day", () => {
    const task = { start_at: "2026-10-29T08:00:00+01:00", due_at: "2026-10-31T17:00:00+01:00", end_at: null };
    expect(moveTo(task, { y: 2026, m: 11, d: 3 }, BERLIN)).toEqual({
      start_at: "2026-11-01T08:00:00+01:00",
      due_at: "2026-11-03T17:00:00+01:00",
    });
    expect(moveTo({ start_at: null, due_at: null, end_at: null }, { y: 2026, m: 11, d: 3 }, BERLIN)).toEqual({
      due_at: "2026-11-03T23:59:00+01:00",
    });
  });

  it("counts days across month and year ends", () => {
    expect(addDays({ y: 2026, m: 12, d: 30 }, 3)).toEqual({ y: 2027, m: 1, d: 2 });
    expect(daysBetween({ y: 2026, m: 2, d: 27 }, { y: 2026, m: 3, d: 2 })).toBe(3);
    expect(daysBetween({ y: 2026, m: 3, d: 2 }, { y: 2026, m: 2, d: 27 })).toBe(-3);
  });
});

describe("the grid of a month", () => {
  it("has full weeks, Monday first, with the days of the months around", () => {
    const weeks = monthGrid(2026, 10); // 1 October 2026 is a Thursday
    expect(weeks[0]![0]).toEqual({ y: 2026, m: 9, d: 28 });
    expect(weeks[0]![3]).toEqual({ y: 2026, m: 10, d: 1 });
    expect(weeks.at(-1)!.at(-1)).toEqual({ y: 2026, m: 11, d: 1 });
    expect(weeks.every((w) => w.length === 7)).toBe(true);
    expect(weeks).toHaveLength(5);
  });

  it("covers February of a year that starts the month on a Monday in four weeks", () => {
    expect(monthGrid(2027, 2)).toHaveLength(4); // 1 Feb 2027 is a Monday, 28 days
  });

  it("finds the week of a day and moves by months", () => {
    expect(weekStart({ y: 2026, m: 10, d: 29 })).toEqual({ y: 2026, m: 10, d: 26 });
    expect(weekOf({ y: 2026, m: 10, d: 29 }).map((d) => d.d)).toEqual([26, 27, 28, 29, 30, 31, 1]);
    expect(addMonths({ y: 2026, m: 12, d: 15 }, 1)).toEqual({ y: 2027, m: 1, d: 1 });
    expect(addMonths({ y: 2026, m: 1, d: 15 }, -1)).toEqual({ y: 2025, m: 12, d: 1 });
  });
});

function task(id: string, over: Partial<ProjectNode> = {}): ProjectNode {
  return {
    id,
    project_id: "p1",
    ref: `P ${id}`,
    number: id,
    parent_id: null,
    kind: "task",
    report_kind: null,
    title: id,
    body: "",
    status: "todo",
    depends_on: [],
    answer: null,
    answered_at: null,
    revision: 1,
    updated_at: null,
    ...over,
  };
}

describe("the Gantt layout", () => {
  const nodes = [
    task("1", { kind: "topic", status: null }),
    task("1.1", { parent_id: "1", start_at: "2026-11-02T09:00:00+01:00", end_at: "2026-11-04T17:00:00+01:00" }),
    task("1.2", { parent_id: "1", due_at: "2026-11-09T17:00:00+01:00", depends_on: ["1.1", "elsewhere"] }),
    task("2", { is_milestone: true, due_at: "2026-11-10T12:00:00+01:00" }),
    task("3"),
    task("r", { kind: "report", report_kind: "note", status: null }),
  ];
  const layout = ganttLayout(nodes, BERLIN, 20);
  const bar = (id: string) => layout.bars.find((b) => b.id === id)!;

  it("has a row for each entry but the reports", () => {
    expect(layout.bars.map((b) => b.id)).toEqual(["1", "1.1", "1.2", "2", "3"]);
  });

  it("draws a task from its start to its end, and a task with a due date only on that day", () => {
    expect(bar("1.1").kind).toBe("task");
    expect(bar("1.1").width).toBe(3 * 20); // 2, 3 and 4 November
    expect(bar("1.2").width).toBe(20);
    expect(bar("1.2").x - bar("1.1").x).toBe(7 * 20);
  });

  it("covers the bars of a topic's children with a summary bar", () => {
    const summary = bar("1");
    expect(summary.kind).toBe("summary");
    expect(summary.from).toEqual({ y: 2026, m: 11, d: 2 });
    expect(summary.to).toEqual({ y: 2026, m: 11, d: 9 });
  });

  it("makes a diamond of a milestone and leaves an undated task without a bar", () => {
    expect(bar("2").kind).toBe("milestone");
    expect(bar("2").width).toBe(20);
    expect(bar("3").kind).toBe("undated");
  });

  it("draws an arrow from what a task waits for, and counts what is not on the chart", () => {
    expect(layout.arrows).toEqual([{ from: "1.1", to: "1.2" }]);
    expect(layout.outside).toEqual({ "1.2": ["elsewhere"] });
  });

  it("starts a little before the first bar and ends a little after the last", () => {
    expect(layout.start).toEqual({ y: 2026, m: 10, d: 30 });
    expect(bar("1.1").x).toBe(3 * 20);
    expect(layout.days).toBe(daysBetween(layout.start, { y: 2026, m: 11, d: 13 }) + 1);
  });
});

describe("the range of the Gantt chart", () => {
  it("is not stretched by a task that was finished long ago", () => {
    const old = task("old", { status: "done", start_at: "2019-01-01T09:00:00+01:00", end_at: "2019-01-05T17:00:00+01:00" });
    const soon = task("soon", { start_at: "2026-11-02T09:00:00+01:00", end_at: "2026-11-04T17:00:00+01:00" });
    const layout = ganttLayout([old, soon], BERLIN, 24);
    expect(layout.days).toBeLessThan(40);
    expect(layout.bars.find((b) => b.id === "old")!.x).toBeLessThan(0); // still has its row, off to the left
  });

  it("is never wider than two years", () => {
    const a = task("a", { due_at: "2026-11-02T09:00:00+01:00" });
    const b = task("b", { due_at: "2031-11-02T09:00:00+01:00" });
    expect(ganttLayout([a, b], BERLIN, 12).days).toBe(MAX_DAYS);
  });

  it("looks at now when an open task from years ago would make it too long, and marks the old one", () => {
    const ancient = task("ancient", { due_at: "2019-01-05T17:00:00+01:00" }); // still open
    const soon = task("soon", { due_at: addIso(5) });
    const layout = ganttLayout([ancient, soon], BERLIN, 24);
    expect(layout.days).toBeLessThanOrEqual(MAX_DAYS);
    expect(layout.bars.find((b) => b.id === "ancient")!.offscreen).toBe("before");
    expect(layout.bars.find((b) => b.id === "soon")!.offscreen).toBeNull();
    expect(layout.bars.find((b) => b.id === "soon")!.x).toBeGreaterThanOrEqual(0);
  });

  it("falls back to every date when only finished tasks have any", () => {
    const only = task("only", { status: "done", due_at: "2019-01-05T17:00:00+01:00" });
    const layout = ganttLayout([only], BERLIN, 24);
    expect(layout.days).toBeLessThan(15);
    expect(layout.bars[0]!.x).toBe(3 * 24);
  });
});
