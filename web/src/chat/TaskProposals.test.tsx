import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { AgentProposal } from "../types";
import { TaskProposals } from "./TaskProposals";

afterEach(cleanup);

const TASK = "6b7d5f12-0000-4000-8000-000000000002";

function edit(over: Partial<AgentProposal> = {}): AgentProposal {
  return {
    id: "p1",
    task_id: TASK,
    origin: "agent",
    node: "dev-node",
    capability: "repo.edit",
    input: { repo: "app", path: "VERSION", old: "0.2.0", new: "0.2.1" },
    reason: "the version is raised (E1)",
    effect: "changes one line in the scratch copy",
    who_notices: "nobody, the copy is not pushed",
    way_back: "repo.reset",
    evidence: ["E1"],
    preview: "--- VERSION\n+++ VERSION\n-0.2.0\n+0.2.1",
    hash: "a".repeat(64),
    status: "pending",
    expires_at: "2026-11-05T16:00:00Z",
    decided_at: null,
    result: null,
    created_at: "2026-11-04T15:30:00Z",
    ...over,
  };
}

function setup(list: AgentProposal[], extra: Record<string, unknown> = {}) {
  const mock = mockFetch({
    [`GET /api/agent/tasks/${TASK}/proposals`]: list,
    ...extra,
  });
  render(<TaskProposals api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} taskId={TASK} />);
  return mock;
}

describe("TaskProposals", () => {
  it("shows each proposed change with its diff and writes nothing before the click", async () => {
    const mock = setup([edit(), edit({ id: "p2", input: { repo: "app", path: "README.md" } })]);
    const cards = await screen.findAllByTestId("proposal");
    expect(cards).toHaveLength(2);
    expect(within(cards[0]!).getByText(/\+0\.2\.1/)).toBeTruthy();
    expect(mock.calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("runs one change on its button and nothing else, then shows the new state", async () => {
    const mock = setup([edit(), edit({ id: "p2" })], {
      "POST /api/agent/proposals/p1/execute": jsonResponse(edit({ status: "executed" })),
    });
    const cards = await screen.findAllByTestId("proposal");
    fireEvent.click(within(cards[0]!).getByRole("button", { name: "Ausführen" }));
    await waitFor(() => {
      const posts = mock.calls.filter((c) => c.method === "POST");
      expect(posts.map((c) => c.url)).toEqual(["/api/agent/proposals/p1/execute"]);
    });
  });

  it("shows nothing when the check made no proposal or the list cannot be read", async () => {
    setup([]);
    await waitFor(() => expect(screen.queryByTestId("task-proposals")).toBeNull());
  });
});
