import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { AgentProposal } from "../types";
import { Proposals } from "./Proposals";

function proposal(over: Partial<AgentProposal> = {}): AgentProposal {
  return {
    id: "p1",
    task_id: "t1",
    node: "vm-docker",
    capability: "docker.restart",
    input: { container: "web" },
    reason: "web restarts after an out-of-memory kill (E1)",
    effect: "stops and starts the web container",
    who_notices: "everybody using the web site, about 10 seconds",
    way_back: "nothing to undo",
    evidence: ["E1"],
    hash: "a".repeat(64),
    status: "pending",
    expires_at: "2026-10-02T16:00:00Z",
    decided_at: null,
    result: null,
    created_at: "2026-10-02T15:30:00Z",
    ...over,
  };
}

afterEach(() => {
  window.location.hash = "";
});

function setup(
  routes: Record<string, unknown>,
  proposals: AgentProposal[],
  onChanged = vi.fn(),
) {
  const mock = mockFetch(routes);
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  render(
    <Proposals
      api={api}
      question="Why does web restart?"
      proposals={proposals}
      onChanged={onChanged}
    />,
  );
  return { mock, onChanged };
}

describe("Proposals", () => {
  it("shows nothing without proposals", () => {
    const { container } = render(
      <Proposals
        api={new ApiClient({ token: "t", fetchImpl: mockFetch({}).fetch })}
        question="q"
        proposals={[]}
        onChanged={() => undefined}
      />,
    );
    expect(container.textContent).toBe("");
  });

  it("shows the exact action, the reason, who notices and the way back", () => {
    setup({}, [proposal()]);
    expect(screen.getByText("docker.restart")).toBeTruthy();
    expect(screen.getByText('{"container":"web"}')).toBeTruthy();
    expect(
      screen.getByText("web restarts after an out-of-memory kill (E1)"),
    ).toBeTruthy();
    expect(
      screen.getByText("everybody using the web site, about 10 seconds"),
    ).toBeTruthy();
    expect(screen.getByText("nothing to undo")).toBeTruthy();
  });

  it("runs the one proposal on one click and nothing else", async () => {
    const { mock, onChanged } = setup(
      {
        "POST /api/agent/proposals/p1/execute": proposal({
          status: "executed",
        }),
      },
      [proposal(), proposal({ id: "p2" })],
    );
    fireEvent.click(screen.getAllByRole("button", { name: "Ausführen" })[0]!);
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    const posts = mock.calls.filter((c) => c.method === "POST");
    expect(posts.map((c) => c.url)).toEqual([
      "/api/agent/proposals/p1/execute",
    ]);
  });

  it("refuses with one click", async () => {
    const { mock, onChanged } = setup(
      {
        "POST /api/agent/proposals/p1/refuse": proposal({ status: "refused" }),
      },
      [proposal()],
    );
    fireEvent.click(screen.getByRole("button", { name: "Ablehnen" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(mock.calls.map((c) => c.url)).toEqual([
      "/api/agent/proposals/p1/refuse",
    ]);
  });

  it("offers no button once a proposal was decided", () => {
    for (const status of [
      "executed",
      "failed",
      "refused",
      "expired",
    ] as const) {
      const { unmount } = render(
        <Proposals
          api={new ApiClient({ token: "t", fetchImpl: mockFetch({}).fetch })}
          question="q"
          proposals={[
            proposal({
              status,
              result: { ok: false, error: { message: "boom" } },
            }),
          ]}
          onChanged={() => undefined}
        />,
      );
      expect(screen.queryByRole("button", { name: "Ausführen" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Ablehnen" })).toBeNull();
      unmount();
    }
  });

  it("says why it did not work and shows an error of the hub", async () => {
    setup(
      {
        "POST /api/agent/proposals/p1/execute": jsonResponse(
          {
            detail: {
              code: "not_pending",
              message: "this proposal is expired",
            },
          },
          409,
        ),
      },
      [proposal()],
    );
    fireEvent.click(screen.getByRole("button", { name: "Ausführen" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
  });

  it("starts a read-only check after an executed proposal", async () => {
    const { mock } = setup(
      {
        "POST /api/agent/tasks": jsonResponse(
          { id: "t9", status: "queued" },
          201,
        ),
      },
      [
        proposal({
          status: "executed",
          result: { ok: true, output: { restarted: true } },
        }),
      ],
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Prüfen, ob es geholfen hat" }),
    );
    await waitFor(() => expect(window.location.hash).toBe("#/tasks/t9"));
    const body = mock.calls.find((c) => c.method === "POST")?.body as {
      prompt: string;
    };
    expect(body.prompt).toContain("docker.restart");
    expect(body.prompt).toContain("Why does web restart?");
  });
});
