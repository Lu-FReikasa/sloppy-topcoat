import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { mockFetch } from "../test/mockFetch";
import type { ReportEvidence } from "../types";
import { changedRepo, PushFromChat } from "./PushFromChat";

function ev(capability: string, repo?: string): ReportEvidence {
  return {
    ref: "E1",
    tag: "live",
    node: "dev-node",
    capability,
    input: repo ? { repo } : null,
    excerpt: "",
    truncated: false,
  };
}
const repo = (mode: string) => ({
  id: "r1",
  name: "test",
  full_name: "acme/test",
  default_branch: "main",
  mode,
  test_template: "pytest",
  token_set: true,
  state: null,
  checked_at: null,
  created_at: "2026-10-12T00:00:00Z",
});
function api(mock: ReturnType<typeof mockFetch>) {
  return new ApiClient({ token: "t", fetchImpl: mock.fetch });
}

describe("PushFromChat", () => {
  it("names the repository a check changed, and none for reads", () => {
    expect(changedRepo([ev("repo.read", "test")])).toBeNull();
    expect(
      changedRepo([ev("repo.status", "test"), ev("repo.write", "test")]),
    ).toBe("test");
  });

  it("offers the push step under a change in a test-branch repository", async () => {
    const mock = mockFetch({
      "GET /api/dev/repos": {
        repos: [repo("test-branch")],
        modes: ["read", "test-branch"],
        templates: ["none"],
      },
    });
    render(
      <PushFromChat api={api(mock)} evidence={[ev("repo.write", "test")]} />,
    );
    expect(
      await screen.findByRole("button", { name: /test.*prüfen und pushen/ }),
    ).toBeTruthy();
  });

  it("says why there is no button for a read-only repository", async () => {
    const mock = mockFetch({
      "GET /api/dev/repos": {
        repos: [repo("read")],
        modes: ["read", "test-branch"],
        templates: ["none"],
      },
    });
    render(
      <PushFromChat api={api(mock)} evidence={[ev("repo.write", "test")]} />,
    );
    await waitFor(() =>
      expect(screen.getByText(/schreibgeschützt/)).toBeTruthy(),
    );
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("shows nothing when no repository was changed", () => {
    const mock = mockFetch({});
    const { container } = render(
      <PushFromChat api={api(mock)} evidence={[ev("repo.read", "test")]} />,
    );
    expect(container.textContent).toBe("");
  });
});
