import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { DevRepos } from "./DevRepos";

afterEach(() => {
  vi.restoreAllMocks();
});

const REPO = {
  id: "r1",
  name: "app",
  full_name: "acme/app",
  default_branch: "trunk",
  mode: "read",
  test_template: "make-check",
  token_set: true,
  state: {
    open_prs: 2,
    branches: 3,
    test_branches: ["topcoat/try-it"],
    private: true,
  },
  checked_at: "2026-10-09T01:00:00Z",
  created_at: "2026-10-09T01:00:00Z",
};
const LIST = {
  repos: [REPO],
  modes: ["read", "test-branch"],
  templates: ["none", "make-check"],
};

function api(mock: ReturnType<typeof mockFetch>) {
  return new ApiClient({ token: "t", fetchImpl: mock.fetch });
}

describe("DevRepos", () => {
  it("lists registered repositories with what the last look saw, and no token", async () => {
    const mock = mockFetch({ "GET /api/dev/repos": LIST });
    const { container } = render(<DevRepos api={api(mock)} />);
    expect(await screen.findByText("acme/app")).toBeTruthy();
    expect(screen.getByText("trunk")).toBeTruthy();
    expect(screen.getByText(/topcoat\/try-it/)).toBeTruthy();
    expect(container.querySelector("input[type=password]")).toBeTruthy();
    expect(container.textContent).not.toContain("github_pat_");
  });

  it("edits the test command and sends the token only when typed", async () => {
    const mock = mockFetch({
      "GET /api/dev/repos": LIST,
      "PATCH /api/dev/repos/r1": () => jsonResponse(REPO),
    });
    render(<DevRepos api={api(mock)} />);
    await screen.findByText("acme/app");
    fireEvent.click(screen.getByRole("button", { name: "Bearbeiten" }));
    const form = await screen.findByTestId("repo-edit");
    const selects = form.querySelectorAll("select");
    // an external repository is only read (D76): its form offers no mode
    expect(selects).toHaveLength(1);
    fireEvent.change(selects[0]!, { target: { value: "none" } });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() =>
      expect(mock.calls.some((c) => c.method === "PATCH")).toBe(true),
    );
    expect(mock.calls.find((c) => c.method === "PATCH")?.body).toEqual({
      mode: "read",
      test_template: "none",
    });
    await waitFor(() => expect(screen.queryByTestId("repo-edit")).toBeNull());
  });

  it("adds a repository with the typed values and clears the token field", async () => {
    const mock = mockFetch({
      "GET /api/dev/repos": {
        repos: [],
        modes: ["read", "test-branch"],
        templates: ["none"],
      },
      "POST /api/dev/repos": () => jsonResponse(REPO, 201),
    });
    const { container } = render(<DevRepos api={api(mock)} />);
    await screen.findByText(/Noch kein Repository/);
    const inputs = container.querySelectorAll("input");
    fireEvent.change(inputs[0]!, { target: { value: "app" } });
    fireEvent.change(inputs[1]!, {
      target: { value: "https://github.com/acme/app" },
    });
    fireEvent.change(inputs[2]!, { target: { value: "github_pat_secret" } });
    fireEvent.click(
      screen.getByRole("button", { name: "Hinzufügen und prüfen" }),
    );
    await waitFor(() =>
      expect(mock.calls.some((c) => c.method === "POST")).toBe(true),
    );
    expect(mock.calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "app",
      repo: "https://github.com/acme/app",
      token: "github_pat_secret",
      mode: "read",
      test_template: "none",
    });
    await waitFor(() =>
      expect(
        (container.querySelectorAll("input")[2] as HTMLInputElement).value,
      ).toBe(""),
    );
  });

  it("shows the hub's refusal in words", async () => {
    const mock = mockFetch({
      "GET /api/dev/repos": { repos: [], modes: ["read"], templates: ["none"] },
      "POST /api/dev/repos": () =>
        jsonResponse(
          {
            detail: {
              code: "not_found",
              message: "GitHub does not know this path",
            },
          },
          422,
        ),
    });
    const { container } = render(<DevRepos api={api(mock)} />);
    await screen.findByText(/Noch kein Repository/);
    const inputs = container.querySelectorAll("input");
    fireEvent.change(inputs[0]!, { target: { value: "x" } });
    fireEvent.change(inputs[1]!, { target: { value: "acme/missing" } });
    fireEvent.change(inputs[2]!, { target: { value: "tok" } });
    fireEvent.click(
      screen.getByRole("button", { name: "Hinzufügen und prüfen" }),
    );
    expect(await screen.findByRole("alert")).toBeTruthy();
  });

  it("looks again and removes after confirmation", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const mock = mockFetch({
      "GET /api/dev/repos": LIST,
      "POST /api/dev/repos/r1/refresh": () => jsonResponse(REPO),
      "DELETE /api/dev/repos/r1": () => new Response(null, { status: 204 }),
    });
    render(<DevRepos api={api(mock)} />);
    await screen.findByText("acme/app");
    fireEvent.click(screen.getByRole("button", { name: "Neu prüfen" }));
    await waitFor(() =>
      expect(
        mock.calls.some((c) => c.url.endsWith("/api/dev/repos/r1/refresh")),
      ).toBe(true),
    );
    fireEvent.click(screen.getByRole("button", { name: "Entfernen" }));
    await waitFor(() =>
      expect(mock.calls.some((c) => c.method === "DELETE")).toBe(true),
    );
  });

  it("shows the diff in a proposal first and offers the push only on a test-branch repository", async () => {
    const card = {
      id: "p1",
      task_id: null,
      origin: "manual",
      node: "dev-node",
      capability: "repo.push",
      input: { repo: "app", branch: "topcoat/fix" },
      reason: "r",
      effect: "e",
      who_notices: "w",
      way_back: "b",
      evidence: [],
      preview: "diff --git a/a.txt b/a.txt\n+hello\n",
      hash: "h",
      status: "pending",
      expires_at: null,
      decided_at: null,
      result: null,
      created_at: null,
    };
    const mock = mockFetch({
      "GET /api/dev/repos": {
        ...LIST,
        repos: [REPO, { ...REPO, id: "r2", name: "tb", mode: "test-branch" }],
      },
      "POST /api/dev/repos/r2/push-proposal": () => jsonResponse(card),
    });
    render(<DevRepos api={api(mock)} />);
    await screen.findAllByText("acme/app");
    // only the test-branch repository has the button
    const pushButtons = screen.getAllByRole("button", { name: /pushen/ });
    expect(pushButtons).toHaveLength(1);
    fireEvent.click(pushButtons[0]!);
    fireEvent.change(screen.getByLabelText("Commit-Nachricht"), {
      target: { value: "Fix paging" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Änderung zeigen/ }));
    expect(await screen.findByText(/\+hello/)).toBeTruthy();
    const call = mock.calls.find((c) => c.url.endsWith("/push-proposal"));
    expect(call?.body).toEqual({ message: "Fix paging" });
    // nothing was executed by showing it
    expect(mock.calls.some((c) => c.url.endsWith("/execute"))).toBe(false);
  });
});

describe("local and external repositories (D76)", () => {
  const LOCAL = {
    ...REPO,
    id: "l1",
    name: "app-local",
    full_name: "",
    kind: "local",
    node_id: "n1",
    node: "dev-alpha",
    upstream_id: "r1",
    default_branch: "trunk",
    mode: "test-branch",
    state: null,
  };
  const BOTH = {
    ...LIST,
    repos: [{ ...REPO, kind: "external", clones: ["l1"] }, LOCAL],
    nodes: [{ id: "n1", name: "dev-alpha", online: true }],
  };
  const BRANCHES = {
    repo: "app-local",
    default_branch: "trunk",
    branches: [
      { name: "trunk", default: true, work: false, ahead: 0, behind: 0 },
      {
        name: "topcoat/fix",
        default: false,
        work: true,
        ahead: 2,
        behind: 0,
        subject: "Fix paging",
      },
    ],
  };

  it("shows local and external repositories apart, each with what belongs to it", async () => {
    const mock = mockFetch({ "GET /api/dev/repos": BOTH });
    render(<DevRepos api={api(mock)} />);
    const local = await screen.findByTestId("local-repos");
    const external = screen.getByTestId("external-repos");
    expect(local.textContent).toContain("app-local");
    expect(local.textContent).toContain("dev-alpha");
    expect(screen.getByTestId("clone-of").textContent).toContain("acme/app");
    expect(external.textContent).toContain("acme/app");
    expect(screen.getByTestId("clones").textContent).toContain("app-local");
    expect(
      screen.getByRole("button", { name: "Lokalen Klon anlegen" }),
    ).toBeTruthy();
  });

  it("lists the branches under their repository and takes one into the default branch", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const mock = mockFetch({
      "GET /api/dev/repos": BOTH,
      "GET /api/dev/repos/l1/branches": BRANCHES,
      "POST /api/dev/repos/l1/merge": { merged: true, into: "trunk" },
      "POST /api/dev/repos/l1/branches": () =>
        jsonResponse({ created: true }, 201),
    });
    render(<DevRepos api={api(mock)} />);
    fireEvent.click(await screen.findByRole("button", { name: "Branches" }));
    const rows = await screen.findAllByTestId("repo-branch");
    expect(rows.map((r) => r.querySelector("code")?.textContent)).toEqual([
      "app-local/trunk",
      "app-local/topcoat/fix",
    ]);
    fireEvent.click(
      screen.getByRole("button", { name: "In trunk übernehmen" }),
    );
    await waitFor(() =>
      expect(
        mock.calls.find((c) => c.url === "/api/dev/repos/l1/merge")?.body,
      ).toEqual({
        branch: "topcoat/fix",
      }),
    );
    expect(
      await screen.findByText("topcoat/fix wurde in trunk übernommen."),
    ).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/Neuer Branch in app-local/), {
      target: { value: "feature" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Lokalen Branch anlegen" }),
    );
    await waitFor(() =>
      expect(
        mock.calls.find(
          (c) => c.url === "/api/dev/repos/l1/branches" && c.method === "POST",
        )?.body,
      ).toEqual({ name: "feature" }),
    );
  });

  it("makes a local clone of an external repository and a new local one", async () => {
    const mock = mockFetch({
      "GET /api/dev/repos": BOTH,
      "POST /api/dev/repos/r1/clone": () => jsonResponse(LOCAL, 201),
      "POST /api/dev/repos/local": () => jsonResponse(LOCAL, 201),
    });
    render(<DevRepos api={api(mock)} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Lokalen Klon anlegen" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Klonen" }));
    await waitFor(() =>
      expect(
        mock.calls.find((c) => c.url === "/api/dev/repos/r1/clone")?.body,
      ).toEqual({
        name: "app-local",
      }),
    );
    const form = screen.getByRole("form", { name: "Neues lokales Repository" });
    fireEvent.change(form.querySelector("input")!, {
      target: { value: "notes" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Anlegen" }));
    await waitFor(() =>
      expect(
        mock.calls.find((c) => c.url === "/api/dev/repos/local")?.body,
      ).toEqual({
        name: "notes",
        branch: "main",
        test_template: "none",
      }),
    );
  });
});
