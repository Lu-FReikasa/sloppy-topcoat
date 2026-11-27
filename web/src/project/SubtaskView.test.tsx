import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { createHubEvents } from "../data/hubEvents";
import i18n from "../i18n";
import { LargeTaskPanel, nextKey, stagesOf, SubtaskView } from "./SubtaskView";
import type { LargeTaskTick, LargeTaskView, Subtask } from "./types";

const REFUSED = 422;
/** The first load and two more while a sub-task runs. */
const LOADS_WITH_TWO_POLLS = 3;
/** A matcher for text that contains `part`, wherever the element's text is cut. */
const having = (part: string) => (_: string, el: Element | null) =>
  !!el && el.children.length === 0 && (el.textContent ?? "").includes(part);

function sub(over: Partial<Subtask> & { id: string }): Subtask {
  return {
    node_id: `n-${over.id}`,
    number: `3.2.1.${over.id.slice(1)}`,
    title: `Sub-task ${over.id}`,
    goal: "",
    layer: "api",
    files: [],
    new_files: [],
    accept: [],
    depends_on: [],
    state: "waiting",
    status: "todo",
    result: null,
    next: "startet, wenn die Abhängigkeiten fertig sind",
    ...over,
  };
}

// the API shape agreed in #33 (from #32), as the hub answers it
function view(subtasks: Subtask[], over: Partial<LargeTaskView["large_task"]> = {}): LargeTaskView {
  const states = subtasks.map((s) => s.state);
  const rules: [boolean, LargeTaskView["large_task"]["state"]][] = [
    [subtasks.length === 0, "planning"],
    [states.every((s) => s === "done"), "done"],
    [states.includes("blocked"), "blocked"],
  ];
  const state = rules.find(([when]) => when)?.[1] ?? "running";
  return {
    large_task: {
      id: "lt",
      project_id: "p1",
      project: "Topcoat",
      number: "3.2.1",
      title: "Paging",
      branch: "topcoat/v-3-2-1",
      state,
      scope: null,
      ...over,
    },
    subtasks,
    rollout: { ready: state === "done" },
  };
}

const PLAN = [
  sub({ id: "s1", layer: "store", state: "done", status: "done", next: "fertig", files: ["hub/src/x.py"] }),
  sub({
    id: "s2",
    depends_on: ["s1"],
    state: "running",
    status: "in_progress",
    next: "läuft",
    result: { id: "f1", state: "testing", summary: "adds the route", error: null },
  }),
  sub({ id: "s3", layer: "research", depends_on: ["s1"], state: "failed", next: "nächster Entwurf" }),
  sub({ id: "s4", layer: "gui", depends_on: ["s2", "s3"] }),
];

function fakeApi(first: LargeTaskView) {
  return {
    largeTask: vi.fn(async (_id: string) => first),
    decomposeLargeTask: vi.fn(async (_id: string) => ({
      filed: [{}, {}],
      change_set: null as string | null,
      summary: "",
      model: "m",
      large_task: view(PLAN),
    })),
    giveWorkshopScope: vi.fn(async (_id: string) => ({ mandate: {} as never })),
    tickLargeTask: vi.fn(async (_id: string) => ({
      tick: { started: ["s4"], replanned: [], blocked: [], stopped: "", done: false } as LargeTaskTick,
      large_task: first,
    })),
    revokeProjectMandate: vi.fn(async (_id: string) => ({ mandate: {} as never })),
  };
}

describe("stagesOf", () => {
  it("puts a sub-task one step after the last of those it depends on", () => {
    const stages = stagesOf(PLAN).map((stage) => stage.map((s) => s.id));
    expect(stages).toEqual([["s1"], ["s2", "s3"], ["s4"]]);
  });

  it("treats unknown ids and loops as no dependency instead of hanging", () => {
    const loop = [sub({ id: "s1", depends_on: ["s2", "gone"] }), sub({ id: "s2", depends_on: ["s1"] })];
    expect(stagesOf(loop).flat().map((s) => s.id).sort()).toEqual(["s1", "s2"]);
    expect(stagesOf([])).toEqual([]);
  });
});

describe("nextKey", () => {
  it("knows the hub's phrases and leaves others to be shown as they are", () => {
    expect(nextKey("wartet auf dich")).toBe("largeTask.next.you");
    expect(nextKey("etwas Neues")).toBeNull();
  });
});

describe("SubtaskView", () => {
  it("shows each sub-task with its state, what it waits for, its result and next step (English)", async () => {
    await i18n.changeLanguage("en");
    render(<SubtaskView api={fakeApi(view(PLAN))} nodeId="lt" />);
    const cards = await screen.findAllByTestId("subtask");
    expect(cards).toHaveLength(PLAN.length);
    expect(screen.getByText("topcoat/v-3-2-1")).toBeTruthy();
    expect(screen.getByText("1 of 4 done")).toBeTruthy();
    expect(within(cards[1]!).getByText("running", { selector: ".badge" })).toBeTruthy();
    expect(within(cards[1]!).getByText(having("Result: testing · adds the route"))).toBeTruthy();
    expect(within(cards[3]!).getByText(having("after 3.2.1.2, 3.2.1.3"))).toBeTruthy();
    expect(within(cards[3]!).getByText(having("Next: starts when the sub-tasks before it are done"))).toBeTruthy();
    expect(screen.getByText("Step 3")).toBeTruthy();
    // the hub's German phrase is not shown in English
    expect(screen.queryByText(having("startet, wenn"))).toBeNull();
  });

  it("splits a task in planning and says what the hub refused, every problem at once", async () => {
    const api = fakeApi(view([]));
    api.decomposeLargeTask.mockRejectedValueOnce(
      new ApiError(REFUSED, "refused", "refused", undefined, { problems: ["s2: file x missing", "loop s1 -> s3"] }),
    );
    render(<SubtaskView api={api} nodeId="lt" />);
    fireEvent.click(await screen.findByRole("button", { name: "In Teilaufgaben zerlegen" }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("s2: file x missing")).toBeTruthy();
    expect(within(alert).getByText("loop s1 -> s3")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "In Teilaufgaben zerlegen" }));
    expect(await screen.findByText("2 Teilaufgaben angelegt.")).toBeTruthy();
    expect(screen.getAllByTestId("subtask")).toHaveLength(PLAN.length);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says when a split waits as a change set", async () => {
    const api = fakeApi(view([]));
    api.decomposeLargeTask.mockResolvedValueOnce({
      filed: [],
      change_set: "cs1",
      summary: "",
      model: "m",
      large_task: view([]),
    });
    render(<SubtaskView api={api} nodeId="lt" />);
    fireEvent.click(await screen.findByRole("button", { name: "In Teilaufgaben zerlegen" }));
    expect(await screen.findByText(having("wartet unter Änderungssätze"))).toBeTruthy();
  });

  it("shows the whole workshop scope before the click and asks the hub for nothing else", async () => {
    const api = fakeApi(view(PLAN));
    render(<SubtaskView api={api} nodeId="lt" />);
    const card = await screen.findByTestId("workshop-scope");
    expect(within(card).getByText(having("genau dem Zweig topcoat/v-3-2-1"))).toBeTruthy();
    expect(within(card).getByText(having("Gilt 14 Tage"))).toBeTruthy();
    // every action and exclusion of the hub's own card text (vorhaben.scope_text) is on the card
    expect(within(card).getByText(having("Theme- und Persona-Entwürfe ändern"))).toBeTruthy();
    expect(within(card).getByText(having("topcoat/live und main"))).toBeTruthy();
    fireEvent.click(within(card).getByRole("button", { name: "Werkstatt-Umfang erteilen" }));
    await waitFor(() => expect(api.giveWorkshopScope).toHaveBeenCalledWith("lt"));
    await waitFor(() => expect(api.largeTask.mock.calls.length).toBeGreaterThan(1));
  });

  it("offers to revoke an active scope and hides the card when nothing left writes", async () => {
    const scope = { id: "m7", state: "active" as const, expires_at: "2026-12-10T10:00:00+00:00" };
    const api = fakeApi(view(PLAN, { scope }));
    const { unmount } = render(<SubtaskView api={api} nodeId="lt" />);
    fireEvent.click(await screen.findByRole("button", { name: "Werkstatt-Umfang widerrufen" }));
    await waitFor(() => expect(api.revokeProjectMandate).toHaveBeenCalledWith("m7"));
    unmount();

    const reading = [sub({ id: "s1", layer: "research", state: "running" })];
    render(<SubtaskView api={fakeApi(view(reading))} nodeId="lt" />);
    await screen.findAllByTestId("subtask");
    expect(screen.queryByTestId("workshop-scope")).toBeNull();
  });

  it("goes on one step and says what started, by the sub-tasks' numbers", async () => {
    const api = fakeApi(view(PLAN));
    render(<SubtaskView api={api} nodeId="lt" />);
    fireEvent.click(await screen.findByRole("button", { name: "Weiter" }));
    expect(await screen.findByText("Gestartet: 3.2.1.4.")).toBeTruthy();
  });

  it("says why nothing started when the hub held a writer back (its reason is a string)", async () => {
    const api = fakeApi(view(PLAN));
    api.tickLargeTask.mockResolvedValueOnce({
      tick: {
        started: ["s3"],
        replanned: [],
        blocked: [],
        stopped: "no active workshop scope for topcoat/v-3-2-1",
        done: false,
      },
      large_task: view(PLAN),
    });
    render(<SubtaskView api={api} nodeId="lt" />);
    fireEvent.click(await screen.findByRole("button", { name: "Weiter" }));
    expect(
      await screen.findByText(having("Gestartet: 3.2.1.3. Nichts, was Dateien ändert, wurde gestartet")),
    ).toBeTruthy();
  });

  it("offers the roll-out once every sub-task is done, still without a click to give", async () => {
    const done = PLAN.map((s) => ({ ...s, state: "done" as const, next: "fertig" }));
    render(<SubtaskView api={fakeApi(view(done))} nodeId="lt" />);
    const rollout = await screen.findByTestId("large-task-rollout");
    expect((within(rollout).getByRole("button") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "Weiter" })).toBeNull();
  });

  it("reloads on a change to its own project only", async () => {
    const bus = createHubEvents();
    const api = fakeApi(view(PLAN.map((s) => ({ ...s, state: "waiting" as const }))));
    render(<SubtaskView api={api} nodeId="lt" events={bus} />);
    await screen.findAllByTestId("subtask");
    act(() => bus.publish({ event: "project_changed", data: JSON.stringify({ project_id: "p2" }) }));
    expect(api.largeTask).toHaveBeenCalledTimes(1);
    act(() => bus.publish({ event: "project_changed", data: JSON.stringify({ project_id: "p1" }) }));
    await waitFor(() => expect(api.largeTask.mock.calls.length).toBeGreaterThan(1));
  });

  it("looks again while a sub-task runs", async () => {
    const api = fakeApi(view(PLAN));
    render(<SubtaskView api={api} nodeId="lt" pollMs={20} />);
    await screen.findAllByTestId("subtask");
    await waitFor(() => expect(api.largeTask.mock.calls.length).toBeGreaterThanOrEqual(LOADS_WITH_TWO_POLLS));
  });
});

describe("LargeTaskPanel", () => {
  it("loads nothing for a plain task until it is unfolded", async () => {
    const api = fakeApi(view([]));
    const { container } = render(<LargeTaskPanel api={api} nodeId="t1" open={false} />);
    expect(api.largeTask).not.toHaveBeenCalled();
    const details = container.querySelector("details")!;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    await waitFor(() => expect(api.largeTask).toHaveBeenCalledWith("t1"));
  });

  it("is open on a task with sub-tasks", async () => {
    const api = fakeApi(view(PLAN));
    render(<LargeTaskPanel api={api} nodeId="lt" open />);
    expect(await screen.findAllByTestId("subtask")).toHaveLength(PLAN.length);
  });
});
