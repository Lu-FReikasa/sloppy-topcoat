import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { ChatMessage } from "../types";
import { HandoffCard } from "./HandoffCard";

const message = {
  id: 5,
  role: "assistant",
  content: "Das gebe ich als Aufgabe ab. Das Ergebnis liegt danach in der Ablage.",
  kind: "handoff",
  status: "complete",
  agent_task_id: "t1",
} as unknown as ChatMessage;

const task = (over: Record<string, unknown> = {}) => ({ id: "t1", status: "queued", ...over });

describe("HandoffCard", () => {
  it("shows the sentence and the link to the task, with nothing to start by hand", async () => {
    const mock = mockFetch({ "GET /api/agent/tasks/t1": jsonResponse(task()) });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<HandoffCard message={message} api={api} />);
    expect(await screen.findByText(/als Aufgabe ab/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Aufgabe öffnen" })).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("says that the task runs, and points to the library once it is done", async () => {
    const mock = mockFetch({ "GET /api/agent/tasks/t1": jsonResponse(task({ status: "running" })) });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    const { rerender } = render(<HandoffCard message={message} api={api} />);
    expect(await screen.findByText("Die Aufgabe läuft.")).toBeTruthy();
    const done = mockFetch({ "GET /api/agent/tasks/t1": jsonResponse(task({ status: "done" })) });
    rerender(
      <HandoffCard message={{ ...message, agent_task_id: "t1" }} api={new ApiClient({ token: "t", fetchImpl: done.fetch })} />,
    );
    expect(await screen.findByText(/Fertig/)).toBeTruthy();
  });
});
