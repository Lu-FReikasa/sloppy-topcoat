import { render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { ActivityView } from "../types";
import { ActivityPanel, duration, useActivity } from "./ActivityPanel";

const VIEW: ActivityView = {
  nodes: [
    {
      name: "node-laptop",
      state: "available",
      work: [{ node: "node-laptop", model: "qwen-30b", kind: "digest_summary", seconds: 12 }],
    },
    { name: "node-amd", state: "busy_gaming", work: [] },
  ],
  roles: [
    { key: "chat", model: "gemma", node: null },
    { key: "digest", model: "qwen-30b", node: "node-laptop" },
    { key: "checker", model: null, node: null },
  ],
  background_nodes: ["node-laptop"],
  digest: { running: 1, waiting: 2 },
};

describe("ActivityPanel (standing tasks, until they move to Models)", () => {
  it("is a collapsed section with the standing tasks and the background note", () => {
    render(<ActivityPanel view={VIEW} />);
    const panel = screen.getByTestId("activity-panel") as HTMLDetailsElement;
    expect(panel.tagName).toBe("DETAILS");
    expect(panel.open).toBe(false);
    expect(screen.getByTestId("activity-role-digest").textContent).toContain("qwen-30b @ node-laptop");
    expect(within(screen.getByTestId("activity-role-checker")).getByText("automatisch")).toBeTruthy();
    expect(panel.textContent).toContain("node-laptop");
  });

  it("keeps a one-line link to the responsibilities outside the collapsed section", () => {
    render(<ActivityPanel view={VIEW} />);
    const link = screen.getByRole("link", { name: "Zuständigkeiten ändern" });
    expect(link.closest("details")).toBeNull();
    expect(link.getAttribute("href")).toBe("#/admin/scheduling");
  });

  it("does not repeat the nodes: that is the table's job", () => {
    render(<ActivityPanel view={VIEW} />);
    expect(screen.queryByTestId("activity-node-node-amd")).toBeNull();
  });
});

describe("useActivity", () => {
  it("reads the activity of the hub", async () => {
    const api = { getActivity: vi.fn().mockResolvedValue(VIEW) } as unknown as ApiClient;
    const { result } = renderHook(() => useActivity(api));
    await waitFor(() => expect(result.current.view).toEqual(VIEW));
    expect(result.current.failed).toBe(false);
  });

  it("says when the read failed, and reads nothing without a client", async () => {
    const api = { getActivity: vi.fn().mockRejectedValue(new Error("x")) } as unknown as ApiClient;
    const { result } = renderHook(() => useActivity(api));
    await waitFor(() => expect(result.current.failed).toBe(true));
    expect(result.current.view).toBeNull();
    const none = renderHook(() => useActivity(undefined));
    expect(none.result.current).toEqual({ view: null, failed: false });
  });
});

describe("duration", () => {
  it("writes seconds, then minutes", () => {
    expect(duration(12)).toBe("12 s");
    expect(duration(600)).toBe("10 min");
  });
});
