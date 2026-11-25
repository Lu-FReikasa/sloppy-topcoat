import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import i18n from "../i18n";
import { fakeApi, node, project } from "../test/projectFixtures";
import { CalendarView, dayAt } from "./CalendarView";
import { GanttView } from "./GanttView";
import { ProjectPage } from "./ProjectPage";
import { dayIn } from "./quickAdd";
import { dayKey, monthGrid } from "./schedule";

afterEach(() => {
  vi.restoreAllMocks();
});
beforeEach(async () => {
  await i18n.changeLanguage("en");
});

const ZONE = "Europe/Berlin";
const today = dayIn(new Date(), ZONE);
const isoAt = (daysFromToday: number, hh = "09:00") => {
  const t = new Date(Date.UTC(today.y, today.m - 1, today.d + daysFromToday));
  return `${t.getUTCFullYear()}-${String(t.getUTCMonth() + 1).padStart(2, "0")}-${String(t.getUTCDate()).padStart(2, "0")}T${hh}:00+01:00`;
};

describe("the grid of December and January", () => {
  it("ends where the month ends, over the turn of the year", () => {
    const dec = monthGrid(2026, 12);
    expect(dec.at(-1)!.at(-1)).toEqual({ y: 2027, m: 1, d: 3 });
    const jan = monthGrid(2027, 1);
    expect(jan[0]![0]).toEqual({ y: 2026, m: 12, d: 28 });
    expect(jan.at(-1)!.at(-1)).toEqual({ y: 2027, m: 1, d: 31 });
  });
});

describe("the calendar", () => {
  const A = node({
    id: "a",
    title: "Write plan",
    number: "1",
    due_at: isoAt(0),
    start_at: isoAt(-2, "08:00"),
  });
  const B = node({ id: "b", title: "No date yet", number: "2" });

  function show(onMove = vi.fn(), onOpen = vi.fn()) {
    render(
      <CalendarView
        tasks={[A, B]}
        zone={ZONE}
        selectedId={null}
        onOpen={onOpen}
        onMove={onMove}
      />,
    );
    return { onMove, onOpen };
  }

  it("puts a task on its due day, marks the day it starts, and lists a task without a date apart", () => {
    show();
    const due = document.querySelector(
      `[data-day="${dayKey(today)}"]`,
    ) as HTMLElement;
    expect(within(due).getByText("Write plan")).toBeTruthy();
    const startDay = new Date(Date.UTC(today.y, today.m - 1, today.d - 2));
    const key = `${startDay.getUTCFullYear()}-${String(startDay.getUTCMonth() + 1).padStart(2, "0")}-${String(startDay.getUTCDate()).padStart(2, "0")}`;
    const start = document.querySelector(`[data-day="${key}"]`);
    if (start) expect(start.textContent).toContain("▸ Write plan");
    expect(
      within(screen.getByRole("region", { name: "Without a date" })).getByText(
        "No date yet",
      ),
    ).toBeTruthy();
  });

  it("moves all dates of a task with Alt and an arrow key", () => {
    const { onMove } = show();
    const button = within(
      document.querySelector(`[data-day="${dayKey(today)}"]`) as HTMLElement,
    ).getByRole("button", {
      name: "Write plan",
    });
    fireEvent.keyDown(button, { key: "ArrowRight", altKey: true });
    expect(onMove).toHaveBeenLastCalledWith(
      expect.objectContaining({ id: "a" }),
      expect.objectContaining({
        due_at: expect.stringMatching(/T09:00:00/),
        start_at: expect.stringMatching(/T08:00:00/),
      }),
    );
    fireEvent.keyDown(button, { key: "ArrowDown", altKey: true });
    expect(onMove).toHaveBeenCalledTimes(2);
    fireEvent.keyDown(button, { key: "ArrowRight" }); // without Alt nothing moves
    expect(onMove).toHaveBeenCalledTimes(2);
  });

  it("moves a dragged task to the day it is dropped on, and opens it on a plain click", () => {
    const { onMove, onOpen } = show();
    const task = screen.getByText("Write plan").closest("li") as HTMLElement;
    const target = document.querySelector(
      `[data-day="${dayKey({ ...today, d: today.d })}"]`,
    ) as HTMLElement;
    const other = Array.from(document.querySelectorAll("[data-day]")).find(
      (c) => c !== target && !c.className.includes("is-outside"),
    ) as HTMLElement;
    document.elementFromPoint = vi.fn(() => other);
    expect(dayAt(1, 1)).not.toBeNull();
    fireEvent.pointerDown(task, {
      button: 0,
      clientX: 5,
      clientY: 5,
      pointerId: 1,
    });
    fireEvent.pointerMove(task, { clientX: 80, clientY: 5, pointerId: 1 });
    fireEvent.pointerUp(task, { clientX: 80, clientY: 5, pointerId: 1 });
    expect(onMove).toHaveBeenCalledTimes(1);
    expect(onMove.mock.calls[0]![0]).toMatchObject({ id: "a" });
    fireEvent.pointerDown(task, {
      button: 0,
      clientX: 5,
      clientY: 5,
      pointerId: 1,
    });
    fireEvent.pointerUp(task, { clientX: 6, clientY: 5, pointerId: 1 });
    expect(onOpen).toHaveBeenCalledWith("a");
  });

  it("gives a task without a date the end of the day it is dropped on", () => {
    const { onMove } = show();
    const task = screen.getByText("No date yet").closest("li") as HTMLElement;
    const cell = document.querySelector(
      `[data-day="${dayKey(today)}"]`,
    ) as HTMLElement;
    document.elementFromPoint = vi.fn(() => cell);
    fireEvent.pointerDown(task, {
      button: 0,
      clientX: 5,
      clientY: 5,
      pointerId: 1,
    });
    fireEvent.pointerMove(task, { clientX: 60, clientY: 5, pointerId: 1 });
    fireEvent.pointerUp(task, { clientX: 60, clientY: 5, pointerId: 1 });
    expect(onMove).toHaveBeenCalledWith(expect.objectContaining({ id: "b" }), {
      due_at: expect.stringMatching(/T23:59:00/),
    });
  });

  it("goes to the next month and back to today, and to a week", () => {
    show();
    const title = () => document.querySelector("[aria-live]")!.textContent;
    const here = title();
    expect(
      document.querySelectorAll("[data-day]").length,
    ).toBeGreaterThanOrEqual(28);
    fireEvent.click(screen.getByRole("button", { name: "Later" }));
    expect(title()).not.toBe(here);
    fireEvent.click(screen.getByRole("button", { name: "Earlier" }));
    expect(title()).toBe(here);
    fireEvent.click(screen.getByRole("button", { name: "Later" }));
    fireEvent.click(screen.getByRole("button", { name: "Today" }));
    expect(title()).toBe(here);
    fireEvent.click(screen.getByRole("button", { name: "Week" }));
    expect(document.querySelectorAll("[data-day]")).toHaveLength(7);
    fireEvent.click(screen.getByRole("button", { name: "Later" })); // a week now, not a month
    expect(document.querySelector(`[data-day="${dayKey(today)}"]`)).toBeNull();
  });
});

describe("the Gantt chart", () => {
  const nodes = [
    node({ id: "t", title: "Phase", number: "1", kind: "topic", status: null }),
    node({
      id: "a",
      title: "Design",
      number: "1.1",
      parent_id: "t",
      start_at: "2026-11-02T09:00:00+01:00",
      end_at: "2026-11-04T17:00:00+01:00",
    }),
    node({
      id: "b",
      title: "Build",
      number: "1.2",
      parent_id: "t",
      start_at: "2026-11-05T09:00:00+01:00",
      end_at: "2026-11-09T17:00:00+01:00",
      depends_on: ["a", "far"],
    }),
    node({
      id: "m",
      title: "Ship",
      number: "2",
      is_milestone: true,
      due_at: "2026-11-10T12:00:00+01:00",
    }),
    node({ id: "u", title: "Later", number: "3" }),
  ];

  function show(onMove = vi.fn(), onOpen = vi.fn()) {
    render(
      <GanttView
        nodes={nodes}
        zone={ZONE}
        selectedId={null}
        onOpen={onOpen}
        onMove={onMove}
      />,
    );
    return { onMove, onOpen };
  }
  const bar = (name: RegExp) => screen.getByRole("button", { name });

  it("draws a row for each entry, an arrow for what a task waits for and says what is off the chart", () => {
    show();
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
    expect(
      bar(/Topcoat 1\.1 Design, .*2.* – .*4/).getAttribute("class"),
    ).toContain("gantt-task");
    expect(bar(/Topcoat 1 Phase/).getAttribute("class")).toContain(
      "gantt-summary",
    );
    expect(bar(/Topcoat 2 Ship/).getAttribute("class")).toContain(
      "gantt-milestone",
    );
    expect(document.querySelectorAll(".gantt-arrow")).toHaveLength(1);
    expect(
      screen.getByText(/1 tasks wait for something that is not on this chart/),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: /Topcoat 3 Later/ }),
    ).toBeNull(); // no date, no bar
  });

  it("moves a task by a day, or by a week with Shift, from the keyboard", () => {
    const { onMove } = show();
    fireEvent.keyDown(bar(/1\.1 Design/), { key: "ArrowRight" });
    expect(onMove).toHaveBeenLastCalledWith(
      expect.objectContaining({ id: "a" }),
      {
        start_at: "2026-11-03T09:00:00+01:00",
        end_at: "2026-11-05T17:00:00+01:00",
      },
    );
    fireEvent.keyDown(bar(/1\.1 Design/), { key: "ArrowLeft", shiftKey: true });
    expect(onMove).toHaveBeenLastCalledWith(
      expect.objectContaining({ id: "a" }),
      {
        start_at: "2026-10-26T09:00:00+01:00",
        end_at: "2026-10-28T17:00:00+01:00",
      },
    );
  });

  it("does not move a summary bar: a topic has no dates of its own", () => {
    const { onMove } = show();
    fireEvent.keyDown(bar(/Topcoat 1 Phase/), { key: "ArrowRight" });
    expect(onMove).not.toHaveBeenCalled();
  });

  it("moves a dragged bar by whole days, and opens it on a click or Enter", () => {
    const { onMove, onOpen } = show();
    const b = bar(/1\.2 Build/);
    fireEvent.pointerDown(b, { button: 0, clientX: 100, pointerId: 1 });
    fireEvent.pointerMove(b, { clientX: 100 + 24 * 2 + 5, pointerId: 1 });
    fireEvent.pointerUp(b, { clientX: 100 + 24 * 2 + 5, pointerId: 1 });
    expect(onMove).toHaveBeenCalledWith(expect.objectContaining({ id: "b" }), {
      start_at: "2026-11-07T09:00:00+01:00",
      end_at: "2026-11-11T17:00:00+01:00",
    });
    fireEvent.pointerDown(b, { button: 0, clientX: 100, pointerId: 1 });
    fireEvent.pointerUp(b, { clientX: 101, pointerId: 1 });
    expect(onOpen).toHaveBeenCalledWith("b");
    fireEvent.keyDown(b, { key: "Enter" });
    expect(onOpen).toHaveBeenCalledTimes(2);
  });

  it("zooms", () => {
    show();
    const svg = document.querySelector("svg")!;
    const before = Number(svg.getAttribute("width"));
    fireEvent.change(screen.getByLabelText("Zoom"), {
      target: { value: "48" },
    });
    expect(Number(document.querySelector("svg")!.getAttribute("width"))).toBe(
      before * 2,
    );
  });
});

describe("the page", () => {
  it("shows the calendar and the Gantt chart and writes a move with the revision it read", async () => {
    const task = node({
      id: "t1",
      title: "Plan",
      number: "1",
      due_at: isoAt(0),
      revision: 3,
    });
    const api = fakeApi({ project: project(), nodes: [task] });
    render(<ProjectPage api={api as unknown as ApiClient} />);
    fireEvent.click(await screen.findByRole("button", { name: /Topcoat/ }));
    await screen.findByRole("heading", { name: "Topcoat", level: 2 });
    fireEvent.click(screen.getByRole("button", { name: "Calendar" }));
    const button = within(
      document.querySelector(`[data-day="${dayKey(today)}"]`) as HTMLElement,
    ).getByRole("button", { name: "Plan" });
    fireEvent.keyDown(button, { key: "ArrowRight", altKey: true });
    await waitFor(() =>
      expect(api.updateProjectNode).toHaveBeenCalledWith(
        "t1",
        expect.objectContaining({ revision: 3 }),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Gantt" }));
    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Gantt" })).toBeTruthy(),
    );
    fireEvent.keyDown(screen.getByRole("button", { name: /Topcoat 1 Plan/ }), {
      key: "ArrowLeft",
    });
    await waitFor(() => expect(api.updateProjectNode).toHaveBeenCalledTimes(2));
  });
});
