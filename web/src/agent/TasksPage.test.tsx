import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { AgentTask, AgentTaskDetail } from "../types";
import { TasksPage } from "./TasksPage";

function task(over: Partial<AgentTask> = {}): AgentTask {
  return {
    id: "t1",
    title: "Why does web restart?",
    prompt: "Why does web restart?",
    model: null,
    via: "gui",
    status: "done",
    stop_reason: null,
    error: null,
    summary: "The web container ran out of memory.",
    diagnosis: null,
    step_count: 5,
    tool_calls: 2,
    runs: 1,
    created_at: "2026-09-30T08:00:00Z",
    started_at: "2026-09-30T08:00:01Z",
    finished_at: "2026-09-30T08:00:30Z",
    updated_at: "2026-09-30T08:00:30Z",
    ...over,
  };
}

function detail(over: Partial<AgentTaskDetail> = {}): AgentTaskDetail {
  return {
    ...task(),
    diagnosis: {
      summary: "The web container ran out of memory.",
      findings: [
        {
          claim: "web was killed for memory",
          tag: "live",
          evidence: ["E1"],
          confirm_with: "",
        },
        {
          claim: "the limit is too low",
          tag: "assumption",
          evidence: [],
          confirm_with: "docker.inspect web",
          downgraded: true,
        },
      ],
      next: "Raise the memory limit.",
      notes: [
        "a claim tagged live cited no live evidence and became an assumption",
      ],
    },
    steps: [
      {
        seq: 1,
        kind: "assistant",
        data: {
          content:
            '{"action":"tool","node":"vm-docker","capability":"docker.ps","input":{},"reason":"list them"}',
        },
        created_at: "2026-09-30T08:00:02Z",
      },
      {
        seq: 2,
        kind: "observation",
        data: { kind: "tool", text: "[E1] live · docker.ps …", evidence: "E1" },
        created_at: "2026-09-30T08:00:03Z",
      },
      {
        seq: 3,
        kind: "observation",
        data: {
          kind: "error",
          text: "[no evidence] not_granted: …",
          evidence: null,
        },
        created_at: "2026-09-30T08:00:04Z",
      },
      {
        seq: 4,
        kind: "assistant",
        data: { content: '{"action":"finish","summary":"x"}' },
        created_at: null,
      },
    ],
    evidence: [
      {
        ref: "E1",
        tag: "live",
        source: {
          node: "vm-docker",
          capability: "docker.ps",
          input: {},
          at: "2026-09-30T08:00:03Z",
        },
        excerpt: '{"containers":[{"name":"web","oom_killed":true}]}',
        bytes: 2048,
        created_at: null,
      },
    ],
    ...over,
  };
}

afterEach(() => {
  window.location.hash = "";
});

describe("TaskView", () => {
  function open(d: AgentTaskDetail, extra: Record<string, unknown> = {}) {
    const mock = mockFetch({
      [`GET /api/agent/tasks/${d.id}`]: d,
      "GET /api/agent/tasks": [],
      ...extra,
    });
    render(
      <TasksPage
        api={new ApiClient({ token: "t", fetchImpl: mock.fetch })}
        taskId={d.id}
        tick={0}
      />,
    );
    return mock;
  }

  it("shows the diagnosis with tagged findings, the evidence and the steps", async () => {
    open(detail());
    expect(
      await screen.findByText("The web container ran out of memory.", {
        selector: "p",
      }),
    ).toBeTruthy();
    const live = screen
      .getByText("web was killed for memory")
      .closest("li") as HTMLElement;
    expect(within(live).getByText("[live]")).toBeTruthy();
    expect(within(live).getByRole("link", { name: /E1/ })).toBeTruthy();
    const guess = screen
      .getByText("the limit is too low")
      .closest("li") as HTMLElement;
    expect(within(guess).getByText("[Annahme]")).toBeTruthy();
    expect(
      within(guess).getByText(/als live bezeichnet, aber ohne Beleg/),
    ).toBeTruthy();
    expect(
      within(guess).getByText(/prüfen mit: docker.inspect web/),
    ).toBeTruthy();
    expect(screen.getByText("Raise the memory limit.")).toBeTruthy();
    // evidence and steps
    expect(
      screen.getByText(/docker.ps auf vm-docker/, { selector: "summary" }),
    ).toBeTruthy();
    expect(screen.getByText("fragt docker.ps auf vm-docker")).toBeTruthy();
    expect(screen.getByText("list them")).toBeTruthy();
    expect(screen.getByText("Ergebnis E1")).toBeTruthy();
    expect(screen.getByText("abgelehnt / fehlgeschlagen")).toBeTruthy();
    expect(screen.getByText("schreibt die Diagnose")).toBeTruthy();
    // a finished task offers neither Cancel nor Continue
    expect(screen.queryByRole("button", { name: "Abbrechen" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Weiter" })).toBeNull();
  });

  it("says when a live finding contradicts a note and that the note should be updated", async () => {
    const d = detail();
    d.diagnosis!.findings = [
      {
        claim: "the proxy listens on 8443, not 443",
        tag: "live",
        evidence: ["E1", "E2"],
        confirm_with: "",
        contradicts: ["E2"],
      },
      {
        claim: "web was killed for memory",
        tag: "live",
        evidence: ["E1"],
        confirm_with: "",
      },
    ];
    open(d);
    const item = (await screen.findByText(/the proxy listens on 8443/)).closest(
      "li",
    ) as HTMLElement;
    expect(within(item).getByTestId("contradicts").textContent).toBe(
      "widerspricht den Notizen E2 – die Notiz sollte aktualisiert werden",
    );
    const other = screen
      .getByText("web was killed for memory")
      .closest("li") as HTMLElement;
    expect(within(other).queryByTestId("contradicts")).toBeNull();
  });

  it("cancels a running task", async () => {
    const running = detail({
      status: "running",
      diagnosis: null,
      summary: null,
      steps: [],
      evidence: [],
    });
    const mock = open(running, {
      "POST /api/agent/tasks/t1/cancel": task({ status: "canceled" }),
    });
    expect(await screen.findByText("Der Agent arbeitet …")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Abbrechen" }));
    await waitFor(() =>
      expect(
        mock.calls.some((c) => c.url === "/api/agent/tasks/t1/cancel"),
      ).toBe(true),
    );
  });

  it("continues a stopped task and says why it stopped", async () => {
    const stopped = detail({ status: "canceled", stop_reason: "canceled" });
    const mock = open(stopped, {
      "POST /api/agent/tasks/t1/continue": task({ status: "queued" }),
    });
    expect(await screen.findByText("von dir abgebrochen")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Weiter" }));
    await waitFor(() =>
      expect(
        mock.calls.some((c) => c.url === "/api/agent/tasks/t1/continue"),
      ).toBe(true),
    );
  });

  it("shows a failure with its reason", async () => {
    open(
      detail({
        status: "failed",
        stop_reason: "bad_format",
        error: "no valid action",
        diagnosis: null,
      }),
    );
    expect(
      await screen.findByText(
        /Die Aufgabe ist fehlgeschlagen: no valid action/,
      ),
    ).toBeTruthy();
    expect(
      screen.getByText("das Modell antwortete nicht in brauchbarer Form"),
    ).toBeTruthy();
  });

  it("names a missing model's real reason (RAM) instead of a generic one", async () => {
    open(
      detail({
        status: "failed",
        stop_reason: "insufficient_memory",
        error: "no node could serve 'big': node-laptop: not enough free RAM",
        diagnosis: null,
      }),
    );
    expect(
      await screen.findByText(
        "das Modell konnte nicht geladen werden: zu wenig freier RAM",
      ),
    ).toBeTruthy();
    expect(screen.getByText(/not enough free RAM/)).toBeTruthy();
  });

  it("does not show another user's task: 404 is just an error line", async () => {
    const mock = mockFetch({
      "GET /api/agent/tasks/zz": jsonResponse(
        { detail: { code: "not_found" } },
        404,
      ),
    });
    render(
      <TasksPage
        api={new ApiClient({ token: "t", fetchImpl: mock.fetch })}
        taskId="zz"
        tick={0}
      />,
    );
    expect(await screen.findByText("Nicht gefunden.")).toBeTruthy();
  });
});

describe("TaskView of a coordinator", () => {
  it("shows the checks of a coordinator and the machine of each finding", async () => {
    const mock = mockFetch({
      "GET /api/agent/tasks/t1": detail({
        kind: "coordinator",
        children: [
          {
            id: "k1",
            node: "vm-a",
            question: "q",
            status: "done",
            stop_reason: null,
            summary: "vm-a is fine.",
            step_count: 3,
            tool_calls: 1,
          },
        ],
        diagnosis: {
          summary: "merged",
          findings: [
            {
              claim: "web runs",
              tag: "live",
              evidence: ["E1"],
              confirm_with: "",
              node: "vm-a",
            },
          ],
          next: "",
          notes: [],
        },
      }),
    });
    render(
      <TasksPage
        api={new ApiClient({ token: "t", fetchImpl: mock.fetch })}
        tick={0}
        taskId="t1"
      />,
    );
    const checks = await screen.findByTestId("checks");
    expect(within(checks).getByText("vm-a")).toBeTruthy();
    expect(
      within(checks).getByRole("link", { name: "öffnen" }).getAttribute("href"),
    ).toBe("#/tasks/k1");
    expect(screen.getByText("vm-a:")).toBeTruthy();
  });
});
