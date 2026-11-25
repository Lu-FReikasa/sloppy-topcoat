import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import { afterEach, describe, expect, it } from "vitest";
import i18n from "../i18n";
import type { ActivityItem } from "../types";
import { ActivityTimeline, minSteps, progressOf, stepsAfter, summarize } from "./ActivityTimeline";
import { chatReducer, initialChatState, upsertActivity } from "./state";

afterEach(cleanup);

let next = 0;
function item(over: Partial<ActivityItem>): ActivityItem {
  next += 1;
  return {
    id: next,
    kind: "tool",
    status: "ok",
    title: "",
    detail: null,
    data: {},
    at: "2026-10-21T10:00:00.000Z",
    ms: 120,
    ...over,
  };
}

function show(items: ActivityItem[], live: boolean) {
  return render(
    <I18nextProvider i18n={i18n}>
      <ActivityTimeline items={items} live={live} />
    </I18nextProvider>,
  );
}

describe("summarize", () => {
  it("counts steps without plan lines and notes, failures and the running item", () => {
    const running = item({ status: "running", title: "docker.ps @ node-a", ms: null });
    const s = summarize([
      item({ kind: "plan", status: "info" }),
      item({ status: "failed", detail: "capability_disabled" }),
      item({}),
      running,
    ]);
    expect(s.steps).toBe(3);
    expect(s.failed).toBe(1);
    expect(s.current).toBe(running);
    expect(s.ms).toBe(360);
  });
});

describe("ActivityTimeline", () => {
  it("shows nothing for an answer without activity", () => {
    show([], false);
    expect(screen.queryByTestId("activity")).toBeNull();
  });

  it("names what runs right now while the answer is made, and stays open", () => {
    show(
      [
        item({ kind: "thinking", detail: "first look at the logs" }),
        item({ status: "running", title: "docker.logs @ node-a", ms: null }),
      ],
      true,
    );
    const box = screen.getByTestId("activity") as HTMLDetailsElement;
    expect(box.open).toBe(true);
    expect(within(box).getByText(/Arbeitet: Aufruf · docker.logs @ node-a/)).toBeTruthy();
  });

  it("shows a failed call in red at once, with the reason in words", () => {
    show(
      [
        item({ title: "inventory.snapshot @ node-b", status: "failed", detail: "capability_disabled" }),
        item({ title: "docker.ps @ node-a", status: "running", ms: null }),
      ],
      true,
    );
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("fehlgeschlagen: auf dieser Maschine nicht eingeschaltet");
    expect(screen.getByText("bisher 1 fehlgeschlagen")).toBeTruthy();
  });

  it("falls back to the raw code for a reason it has no words for", () => {
    show([item({ status: "failed", detail: "weird_code" })], false);
    expect(screen.getByRole("alert").textContent).toContain("fehlgeschlagen: weird_code");
  });

  it("is closed after the answer, also when something failed: the summary line says so", () => {
    const { unmount } = show([item({ title: "docker.ps @ node-a" })], false);
    expect((screen.getByTestId("activity") as HTMLDetailsElement).open).toBe(false);
    expect(screen.getByText(/Was geschah — Schritte: 1/)).toBeTruthy();
    unmount();
    show([item({ status: "failed", detail: "timeout" })], false);
    expect((screen.getByTestId("activity") as HTMLDetailsElement).open).toBe(false);
    expect(screen.getByText(/Schritte: 1, fehlgeschlagen: 1/)).toBeTruthy();
  });

  it("keeps a thought and a lead-in readable instead of throwing them away", () => {
    show(
      [
        item({ kind: "lead_in", status: "info", detail: "Ich schaue nach." }),
        item({ kind: "thinking", detail: "erst die Logs\nund dann den Speicher" }),
      ],
      false,
    );
    expect(screen.getAllByText("Ich schaue nach.").length).toBeGreaterThan(0);
    const pre = screen.getAllByText(/erst die Logs/);
    expect(pre.some((e) => e.tagName === "PRE")).toBe(true);
  });
});

describe("the chain in the chat state", () => {
  it("adds an item and replaces it by id when it changes", () => {
    const a = item({ status: "running", ms: null });
    const list = upsertActivity(undefined, a);
    const done = { ...a, status: "ok" as const, ms: 50 };
    expect(upsertActivity(list, done)).toEqual([done]);
    expect(upsertActivity(list, item({})).length).toBe(2);
  });

  it("takes activity events into the stream of the conversation", () => {
    let state = chatReducer(initialChatState, {
      type: "stream_start",
      id: "c1",
      userMessage: null,
    });
    const a = item({ kind: "thinking", status: "running", ms: null });
    state = chatReducer(state, {
      type: "stream_event",
      id: "c1",
      event: { type: "activity", op: "add", item: a },
    });
    state = chatReducer(state, {
      type: "stream_event",
      id: "c1",
      event: { type: "activity", op: "update", item: { ...a, status: "ok", ms: 30 } },
    });
    expect(state.streams.c1?.activity).toEqual([{ ...a, status: "ok", ms: 30 }]);
  });
});

describe("a note of the second look at a step", () => {
  it("is shown as a line of its own and is not counted as a step", () => {
    const items = [
      item({ title: "repo.read" }),
      item({
        kind: "step_check",
        status: "info",
        title: "repo.read",
        detail: "the file is cut after 400 lines",
        ms: null,
      }),
    ];
    expect(summarize(items).steps).toBe(1);
    show(items, false);
    expect(screen.getByTestId("activity-step_check").textContent).toContain(
      "the file is cut after 400 lines",
    );
  });
});

describe("the order card of a check", () => {
  it("shows the machine, the groups, the period and the form of the result", () => {
    show(
      [
        item({
          kind: "check",
          status: "running",
          data: {
            order: {
              target: "node-amd",
              tools: ["host", "topcoat"],
              goal: "Audio problems on node-amd",
              result: "table",
              window: "14d",
              corrections: [],
              mode: "read",
            },
          },
        }),
      ],
      true,
    );
    const card = screen.getByTestId("order-card");
    expect(card.textContent).toContain("Audio problems on node-amd");
    expect(screen.getByTestId("order-target").textContent).toContain("node-amd");
    expect(card.textContent).toContain("14d");
    expect(card.textContent).toMatch(/host/i);
    expect(screen.getByTestId("order-mode").textContent).toMatch(/lesen|read/);
  });

  it("shows a corrected order's notes and leaves an old check without an order alone", () => {
    show(
      [
        item({
          kind: "check",
          data: {
            order: {
              target: "any",
              tools: [],
              goal: "",
              result: "diagnosis",
              window: "24h",
              corrections: ["window 'forever' is not a duration; using 24h"],
              mode: "read",
            },
          },
        }),
      ],
      false,
    );
    expect(screen.getByText(/using 24h/)).toBeTruthy();
    cleanup();
    show([item({ kind: "check", data: { task_id: "x" } })], false);
    expect(screen.queryByTestId("order-card")).toBeNull();
  });
});

describe("the steps of a check", () => {
  it("counts the calls after the check up to the next one", () => {
    const items = [
      item({ kind: "check", data: {} }),
      item({ kind: "tool" }),
      item({ kind: "tool" }),
      item({ kind: "check", data: {} }),
      item({ kind: "tool" }),
    ];
    expect(stepsAfter(items, 0)).toBe(2);
    expect(stepsAfter(items, 3)).toBe(1);
  });
});

describe("the progress of a check", () => {
  const lookup = {
    target: "any",
    tools: ["notes", "web"],
    goal: "Wann haben sich McKellen und Stewart kennengelernt?",
    result: "diagnosis",
    window: "24h",
    corrections: [],
    mode: "read",
  };

  it("starts at the least steps of the order and grows by one past it", () => {
    expect(minSteps(lookup)).toBe(4); // notes 1, web 2 (search and page), the answer
    expect(progressOf(0, 4, true, false)).toEqual({ step: 1, of: 4 });
    expect(progressOf(3, 4, true, true)).toEqual({ step: 3, of: 4 });
    expect(progressOf(3, 4, true, false)).toEqual({ step: 4, of: 4 });
    expect(progressOf(4, 4, true, true)).toEqual({ step: 4, of: 4 });
    expect(progressOf(4, 4, true, false)).toEqual({ step: 5, of: 5 }); // one more than planned
    expect(progressOf(7, 4, false, false)).toEqual({ step: 7, of: 7 });
    // with the run's limit and the usual count of earlier checks (2026-11-13): both count the answer
    expect(progressOf(5, 4, true, false, { max_calls: 16, typical_calls: 6 })).toEqual({
      step: 6,
      of: 6,
      typical: 7,
      max: 17,
    });
    expect(progressOf(1, 4, true, false, { max_calls: 3, typical_calls: 9 }).typical).toBe(4);
  });

  it("shows a look-up as such, with a bar, the order once and only the last steps", () => {
    const items = [
      item({ kind: "check", status: "running", data: { order: lookup } }),
      ...[1, 2, 3, 4].map((n) => item({ title: `web.search ${n}` })),
      item({ title: "web.fetch @ tools", status: "running", ms: null }),
    ];
    show(items, true);
    expect(screen.getByTestId("activity-progress").textContent).toBe("Schritt 5 von mind. 5");
    expect(screen.getByRole("progressbar")).toBeTruthy();
    expect(screen.getAllByTestId("order-card")).toHaveLength(1);
    expect(screen.getByText(/Arbeitet: Aufruf · web.fetch/)).toBeTruthy();
    // three steps in the list, the rest on request
    expect(screen.getAllByTestId("activity-tool")).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: /Alle Schritte zeigen \(6\)/ }));
    expect(screen.getAllByTestId("activity-tool")).toHaveLength(5);
    expect(screen.getByTestId("activity-check").textContent).toContain("Sucht nach");
  });
});
