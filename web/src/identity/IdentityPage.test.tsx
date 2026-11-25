import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { mockFetch } from "../test/mockFetch";
import type { IdentitySource, IdentityView } from "../types";
import { IdentityPage } from "./IdentityPage";

function source(over: Partial<IdentitySource> = {}): IdentitySource {
  return {
    node: { id: "n1", name: "vm-id" },
    realm: "home",
    age_seconds: 20,
    cached: false,
    notes: ["Composite roles are not expanded."],
    people: [
      {
        id: "u1",
        username: "ann",
        name: "Ann Example",
        enabled: true,
        source: "directory",
        groups: ["/family/parents"],
        apps: [
          {
            app: "homarr",
            name: "Homarr",
            roles: ["user"],
            via: [{ group: "/family", member_of: "/family/parents", kind: "role" }],
          },
        ],
      },
      { id: "u2", username: "bob", name: "", enabled: false, source: "local", groups: [], apps: [] },
    ],
    groups: [
      {
        id: "g1",
        path: "/family",
        name: "family",
        members: 0,
        members_ok: true,
        realm_roles: [],
        opens: [{ app: "homarr", roles: ["user"], via_group: "/family", kind: "role" }],
      },
    ],
    apps: [
      {
        client_id: "homarr",
        name: "Homarr",
        enabled: true,
        builtin: false,
        url: "",
        roles: ["user"],
        known_app: true,
        people: 1,
        people_enabled: 1,
        groups: ["/family"],
        configured_groups: [],
      },
    ],
    ...over,
  };
}

function show(view: IdentityView, isAdmin = false, extra: Record<string, unknown> = {}) {
  const mock = mockFetch({ "GET /api/identity": view, ...extra });
  render(<IdentityPage api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} isAdmin={isAdmin} />);
  return mock;
}

describe("IdentityPage", () => {
  it("shows people, groups and apps with the age and the limit of the look", async () => {
    show({ sources: [source()], app_groups: {} });
    const card = await screen.findByTestId("identity-source");
    expect(card.textContent).toContain("vm-id");
    expect(card.textContent).toContain("vor 20 s");
    expect(screen.getByTestId("identity-note").textContent).toContain("Composite roles");
    expect(within(screen.getByTestId("identity-people")).getByText(/ann/).textContent).toContain("Ann Example");
    expect(screen.getByTestId("identity-groups").textContent).toContain("/family");
    expect(screen.getByTestId("identity-apps").textContent).toContain("Homarr");
  });

  it("explains the chain from a person to an app", async () => {
    show({ sources: [source()], app_groups: {} });
    fireEvent.click(await screen.findByRole("button", { name: /Ann Example/ }));
    const detail = screen.getByTestId("identity-person-detail");
    expect(detail.textContent).toContain("Homarr");
    expect(detail.textContent).toContain("/family/parents");
    expect(detail.textContent).toContain("das von /family erbt");
  });

  it("says plainly when a source failed instead of showing nobody", async () => {
    show({ sources: [{ node: { id: "n1", name: "vm-id" }, error: { code: "x", message: "not reachable" } }], app_groups: {} });
    expect((await screen.findByTestId("identity-error")).textContent).toContain("not reachable");
    expect(screen.queryByTestId("identity-people")).toBeNull();
  });

  it("filters people", async () => {
    show({ sources: [source()], app_groups: {} });
    await screen.findByTestId("identity-people");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "bob" } });
    expect(screen.getByTestId("identity-people").textContent).not.toContain("Ann Example");
    expect(screen.getByTestId("identity-people").textContent).toContain("bob");
  });

  it("lets only an admin note which groups an app reads, and saves the whole note", async () => {
    const view: IdentityView = { sources: [source()], app_groups: {} };
    const { calls } = show(view, true, { "PUT /api/identity/app-groups": { app_groups: { homarr: ["/family"] } } });
    fireEvent.click(await screen.findByRole("button", { name: "Gruppen, die sie liest" }));
    fireEvent.change(screen.getByLabelText("Gruppen, die sie liest"), { target: { value: "/family" } });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ app_groups: { homarr: ["/family"] } });
  });

  it("offers no editing to a person who is not an admin", async () => {
    show({ sources: [source()], app_groups: {} });
    await screen.findByTestId("identity-apps");
    expect(screen.queryByRole("button", { name: "Gruppen, die sie liest" })).toBeNull();
  });

  it("says what to do when no source is allowed", async () => {
    show({ sources: [], app_groups: {} });
    expect((await screen.findByText(/keycloak\.snapshot/)).textContent).toContain("Admin");
  });
});
