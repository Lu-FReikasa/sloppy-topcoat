import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { NodeSummary } from "../types";
import { DevWorkshop } from "./DevWorkshop";

afterEach(() => {
  vi.restoreAllMocks();
});

const repo = (over: object) => ({
  id: "r1",
  name: "app",
  full_name: "acme/app",
  default_branch: "trunk",
  mode: "test-branch",
  test_template: "pytest",
  token_set: true,
  state: null,
  checked_at: null,
  created_at: null,
  ...over,
});

const NODE = {
  id: "n1",
  name: "dev-box",
  connected: true,
  roles: ["dev"],
} as unknown as NodeSummary;

const JOB = "0123456789abcdef01234567";

const cap = (name: string, output: unknown) => ({
  [`POST /api/nodes/n1/capabilities/${name}`]: () =>
    jsonResponse({ ok: true, capability: name, output }),
});

function api(mock: ReturnType<typeof mockFetch>) {
  return new ApiClient({ token: "t", fetchImpl: mock.fetch });
}

describe("DevWorkshop", () => {
  it("shows the state and the diff, and runs the tests with the repository's name", async () => {
    const mock = mockFetch({
      "GET /api/dev/repos": { repos: [repo({})], modes: [], templates: [] },
      ...cap("repo.status", {
        branch: "topcoat/fix",
        head: "abc123",
        last_commit: "first",
        changed: ["M a.py"],
      }),
      ...cap("repo.diff", {
        diff: "diff --git a/a.py b/a.py\n+x = 1\n",
        masked: 0,
      }),
      ...cap("repo.test_start", { repo: "app", job_id: JOB, state: "running" }),
      ...cap("repo.test_status", {
        repo: "app",
        job_id: JOB,
        state: "passed",
        running: false,
        passed: true,
        exit_code: 0,
        log: "3 passed",
      }),
    });
    render(<DevWorkshop api={api(mock)} nodes={[NODE]} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Stand und Änderung" }),
    );
    expect(await screen.findByText(/\+x = 1/)).toBeTruthy();
    expect(screen.getByText(/topcoat\/fix/)).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: "Tests laufen lassen" }),
    );
    expect(await screen.findByText("Tests bestanden.")).toBeTruthy();
    expect(screen.getByText("3 passed")).toBeTruthy();
    const start = mock.calls.find((c) => c.url.endsWith("/repo.test_start"));
    expect(start?.body).toEqual({ input: { repo: "app" } });
    const ask = mock.calls.find((c) => c.url.endsWith("/repo.test_status"));
    expect(ask?.body).toEqual({
      input: { repo: "app", job_id: JOB, wait_seconds: 10 },
    });
    // a test run is never one synchronous call: that one is cut off after seconds
    expect(mock.calls.some((c) => c.url.endsWith("/repo.test"))).toBe(false);
  });

  it("keeps asking while the run goes on and can cancel it", async () => {
    let asked = 0;
    const mock = mockFetch({
      "GET /api/dev/repos": { repos: [repo({})], modes: [], templates: [] },
      ...cap("repo.test_start", { repo: "app", job_id: JOB, state: "running" }),
      "POST /api/nodes/n1/capabilities/repo.test_status": () => {
        asked += 1;
        return jsonResponse({
          ok: true,
          output:
            asked < 3
              ? {
                  state: "running",
                  running: true,
                  elapsed_ms: 4000,
                  log: "collecting …",
                }
              : {
                  state: "canceled",
                  running: false,
                  passed: false,
                  exit_code: -1,
                  log: "",
                },
        });
      },
      ...cap("repo.test_cancel", { state: "canceled" }),
    });
    render(<DevWorkshop api={api(mock)} nodes={[NODE]} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Tests laufen lassen" }),
    );
    expect(await screen.findByText(/laufen seit 4 s/)).toBeTruthy();
    expect(screen.getByText("collecting …")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Lauf abbrechen" }));
    expect(await screen.findByText("Der Lauf wurde abgebrochen.")).toBeTruthy();
    const cancel = mock.calls.find((c) => c.url.endsWith("/repo.test_cancel"));
    expect(cancel?.body).toEqual({ input: { repo: "app", job_id: JOB } });
    expect(asked).toBeGreaterThanOrEqual(3);
  });

  it("builds with a version and says the files are unsigned; a read-only repository has no change buttons", async () => {
    const mock = mockFetch({
      "GET /api/dev/repos": {
        repos: [repo({}), repo({ id: "r2", name: "docs", mode: "read" })],
        modes: [],
        templates: [],
      },
      ...cap("repo.build_node", {
        built: true,
        folder: "out/app/0.1.6-dev1",
        artefacts: [
          {
            name: "topcoat-node-linux-amd64",
            bytes: 2048,
            sha256: "a".repeat(64),
          },
        ],
      }),
    });
    render(<DevWorkshop api={api(mock)} nodes={[NODE]} />);
    await screen.findAllByTestId("workshop-repo");
    expect(
      screen.getAllByRole("button", { name: "Tests laufen lassen" }),
    ).toHaveLength(1);
    fireEvent.change(screen.getByLabelText(/Version/), {
      target: { value: "0.1.6-dev1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Node bauen" }));
    expect(await screen.findByText(/topcoat-node-linux-amd64/)).toBeTruthy();
    expect(screen.getByText(/Unsigniert: Dateien/)).toBeTruthy();
    const call = mock.calls.find((c) => c.url.endsWith("/repo.build_node"));
    expect(call?.body).toEqual({
      input: { repo: "app", version: "0.1.6-dev1" },
    });
  });

  it("says so when there is no dev node and does not offer to run anything offline", async () => {
    const mock = mockFetch({
      "GET /api/dev/repos": { repos: [repo({})], modes: [], templates: [] },
    });
    render(
      <DevWorkshop api={api(mock)} nodes={[{ ...NODE, connected: false }]} />,
    );
    expect(await screen.findByText(/offline/)).toBeTruthy();
    await waitFor(() =>
      expect(
        screen
          .getByRole("button", { name: "Abrufen" })
          .hasAttribute("disabled"),
      ).toBe(true),
    );
  });
});
