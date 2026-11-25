import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { NodeSummary } from "../types";
import { requestShow, takeShow } from "./bus";
import { FilesPage } from "./FilesPage";

afterEach(() => {
  cleanup();
  takeShow();
  vi.restoreAllMocks();
});

const NODE = {
  id: "n1",
  name: "dev-box",
  connected: true,
  roles: ["dev"],
} as unknown as NodeSummary;

const REPO = {
  id: "r1",
  kind: "local",
  node_id: "n1",
  name: "app",
  full_name: "acme/app",
  default_branch: "main",
  mode: "test-branch",
  test_template: "pytest",
  token_set: true,
  state: null,
  checked_at: null,
  created_at: null,
};

const TEXT = Array.from({ length: 60 }, (_, i) => `line ${i + 1}`).join("\n");

function setup() {
  const reads: Record<string, unknown>[] = [];
  const mock = mockFetch({
    "GET /api/dev/repos": { repos: [REPO], modes: [], templates: [] },
    "POST /api/nodes/n1/capabilities/repo.list": () =>
      jsonResponse({
        ok: true,
        capability: "repo.list",
        output: { files: ["README.md", "src/main.py"] },
      }),
    "POST /api/nodes/n1/capabilities/repo.read": (req: unknown) => {
      reads.push(req as Record<string, unknown>);
      return jsonResponse({
        ok: true,
        capability: "repo.read",
        output: {
          text: TEXT,
          start_line: 1,
          total_lines: 60,
          more_after: false,
        },
      });
    },
  });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  render(<FilesPage api={api} nodes={[NODE]} />);
  return { mock, reads };
}

describe("FilesPage", () => {
  it("lists the files of repo.list as a tree and reads a file on click", async () => {
    const { mock } = setup();
    const readme = await screen.findByText("README.md");
    expect(screen.getAllByTestId("files-row").length).toBeGreaterThan(1);
    fireEvent.click(readme);
    expect(await screen.findByText("line 60")).toBeTruthy();
    expect(
      mock.calls.some((c) => c.url.endsWith("/capabilities/repo.read")),
    ).toBe(true);
    expect(screen.queryByTestId("files-marked")).toBeNull();
  });

  it("marks the lines a show request points at", async () => {
    setup();
    await screen.findByText("README.md");
    requestShow({ path: "README.md", from: 5, to: 7 });
    expect(await screen.findByText("line 6")).toBeTruthy();
    expect(screen.getAllByTestId("files-marked")).toHaveLength(3);
  });
});
