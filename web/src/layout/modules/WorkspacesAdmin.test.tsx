import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../../api/client";
import { AGENT, bootstrap, workspace } from "../../test/layoutFixtures";
import { jsonResponse, mockFetch } from "../../test/mockFetch";
import type { UiProfileView, UiTemplateView, UiUserView } from "../types";
import { WorkspacesAdmin } from "./WorkspacesAdmin";

const tpl = (key: string, over: Partial<UiTemplateView> = {}): UiTemplateView => ({
  key,
  title: key[0]?.toUpperCase() + key.slice(1),
  title_key: null,
  icon: "",
  position: 10,
  modules: ["page.chat", "page.library"],
  agent: { ...AGENT, tool_scope: { ...AGENT.tool_scope } },
  requires_role: null,
  builtin: false,
  rev: 2,
  ...over,
});

const TEMPLATES = [tpl("talk", { builtin: true, rev: 1, title_key: "workspace.research" }), tpl("room")];
const PROFILES: UiProfileView[] = [
  { name: "default", description: "", default: true, builtin: true, grants: [{ kind: "workspace", key: "talk", effect: "allow" }] },
  { name: "guest", description: "", default: false, builtin: true, grants: [{ kind: "tool", key: "shell", effect: "deny" }] },
  { name: "family", description: "The family", default: false, builtin: false, grants: [] },
];
const USERS: UiUserView[] = [
  { id: "u1", name: "dev-admin", role: "admin", profile: null },
  { id: "u2", name: "user-b", role: "user", profile: "guest" },
];

function setup(routes: Record<string, unknown> = {}) {
  const mock = mockFetch({
    "GET /api/ui/templates": { templates: TEMPLATES },
    "GET /api/ui/profiles": { profiles: PROFILES },
    "GET /api/ui/users": { users: USERS },
    ...routes,
  });
  const api = new ApiClient({ token: "tok", fetchImpl: mock.fetch });
  const boot = bootstrap([workspace("talk", ["page.chat"])]);
  render(<WorkspacesAdmin api={api} boot={boot} />);
  return mock;
}

const sent = (mock: ReturnType<typeof setup>, method: string, url: string) =>
  mock.calls.find((c) => c.method === method && c.url === url);

describe("WorkspacesAdmin", () => {
  it("shows one half when asked: the applications with their profiles, or the people", async () => {
    const mock = mockFetch({
      "GET /api/ui/templates": { templates: TEMPLATES },
      "GET /api/ui/profiles": { profiles: PROFILES },
      "GET /api/ui/users": { users: USERS },
    });
    const api = new ApiClient({ token: "tok", fetchImpl: mock.fetch });
    const boot = bootstrap([workspace("talk", ["page.chat"])]);
    const apps = render(<WorkspacesAdmin api={api} boot={boot} part="apps" />);
    expect(await screen.findByText("Room")).toBeTruthy();
    expect(screen.getByText("family", { selector: "strong" })).toBeTruthy();
    expect(screen.queryByLabelText("Profil von user-b")).toBeNull();
    apps.unmount();
    render(<WorkspacesAdmin api={api} boot={boot} part="people" />);
    expect(await screen.findByLabelText("Profil von user-b")).toBeTruthy();
    expect(screen.queryByText("Room")).toBeNull();
  });

  it("lists the workspaces, the profiles and the people", async () => {
    setup();
    expect(await screen.findByText("Recherche")).toBeTruthy(); // the built-in title through its key
    expect(screen.getByText("eingebaut")).toBeTruthy();
    expect(screen.getByText("Room")).toBeTruthy();
    expect((within(screen.getByText("Recherche").closest("tr") as HTMLElement).getByRole("button", { name: "Bearbeiten" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("guest", { selector: "strong" })).toBeTruthy();
    expect(screen.getByText("family", { selector: "strong" })).toBeTruthy();
    expect((screen.getByLabelText("Profil von user-b") as HTMLSelectElement).value).toBe("guest");
  });

  it("creates a workspace with the windows and the tool groups that were chosen", async () => {
    const mock = setup({ "PUT /api/ui/templates/office": tpl("office") });
    fireEvent.click(await screen.findByRole("button", { name: "Neue Anwendung" }));
    const form = screen.getByRole("form", { name: "Neue Anwendung" });
    fireEvent.change(within(form).getByLabelText("Schlüssel"), { target: { value: "office" } });
    fireEvent.change(within(form).getByLabelText("Anwendung"), { target: { value: "Büro" } });
    fireEvent.click(within(form).getByLabelText("Chat"));
    fireEvent.click(within(form).getByLabelText("Ablage"));
    fireEvent.click(within(form).getByLabelText("nur Admin"));
    fireEvent.click(within(form).getByLabelText("Alle Werkzeuggruppen"));
    fireEvent.click(within(form).getByLabelText("Dokumente"));
    fireEvent.click(within(form).getByLabelText("E-Mail-Entwürfe"));
    fireEvent.change(within(form).getByLabelText("Internet"), { target: { value: "off" } });
    fireEvent.change(within(form).getByLabelText("Modell"), { target: { value: "deep" } });
    fireEvent.click(within(form).getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(sent(mock, "PUT", "/api/ui/templates/office")).toBeDefined());
    expect(sent(mock, "PUT", "/api/ui/templates/office")?.body).toEqual({
      title: "Büro",
      icon: "",
      position: 50,
      modules: ["page.chat", "page.library"],
      requires_role: "admin",
      agent: { tool_scope: { nodes: null, groups: ["docs", "mail"], internet: false, memory: null }, persona: null, model_role: "deep" },
    });
    expect(await screen.findByText("Gespeichert.")).toBeTruthy();
    expect(screen.queryByRole("form", { name: "Neue Anwendung" })).toBeNull();
  });

  it("edits a workspace of its own and keeps its key", async () => {
    const mock = setup({ "PUT /api/ui/templates/room": tpl("room") });
    const row = (await screen.findByText("Room")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Bearbeiten" }));
    const form = screen.getByRole("form", { name: "Anwendung bearbeiten" });
    expect((within(form).getByLabelText("Schlüssel") as HTMLInputElement).disabled).toBe(true);
    fireEvent.change(within(form).getByLabelText("Anwendung"), { target: { value: "Raum" } });
    fireEvent.click(within(form).getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(sent(mock, "PUT", "/api/ui/templates/room")).toBeDefined());
    expect(sent(mock, "PUT", "/api/ui/templates/room")?.body).toMatchObject({ title: "Raum", modules: ["page.chat", "page.library"] });
  });

  it("copies a workspace under a new key", async () => {
    const mock = setup({ "POST /api/ui/templates/talk/duplicate": tpl("talk-2") });
    const row = (await screen.findByText("Recherche")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Kopieren" }));
    expect((screen.getByLabelText("Kopie von talk als") as HTMLInputElement).value).toBe("talk-2");
    fireEvent.click(screen.getAllByRole("button", { name: "Kopieren" }).at(-1) as HTMLElement);
    await waitFor(() => expect(sent(mock, "POST", "/api/ui/templates/talk/duplicate")?.body).toEqual({ key: "talk-2" }));
  });

  it("asks before it deletes a workspace, and cannot delete a built-in one", async () => {
    const mock = setup({ "DELETE /api/ui/templates/room": { deleted: "room" } });
    const builtin = (await screen.findByText("Recherche")).closest("tr") as HTMLElement;
    expect((within(builtin).getByRole("button", { name: "Löschen" }) as HTMLButtonElement).disabled).toBe(true);
    const row = screen.getByText("Room").closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Löschen" }));
    expect(sent(mock, "DELETE", "/api/ui/templates/room")).toBeUndefined();
    const dialog = screen.getByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Löschen" }));
    await waitFor(() => expect(sent(mock, "DELETE", "/api/ui/templates/room")).toBeDefined());
  });

  it("creates a profile with the workspaces it offers and the tool groups it takes away", async () => {
    const mock = setup({ "PUT /api/ui/profiles/kids": { ...PROFILES[2], name: "kids" } });
    fireEvent.click(await screen.findByRole("button", { name: "Neues Profil" }));
    const form = screen.getByRole("form", { name: "Neues Profil" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "kids" } });
    fireEvent.change(within(form).getByLabelText("Recherche"), { target: { value: "allow" } });
    fireEvent.click(within(form).getByLabelText("Wegnehmen: Lese-Shell"));
    fireEvent.click(within(form).getByLabelText("Wegnehmen: Internet"));
    fireEvent.click(within(form).getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(sent(mock, "PUT", "/api/ui/profiles/kids")).toBeDefined());
    expect(sent(mock, "PUT", "/api/ui/profiles/kids")?.body).toEqual({
      description: "",
      default: false,
      grants: [
        { kind: "workspace", key: "talk", effect: "allow" },
        { kind: "tool", key: "shell", effect: "deny" },
        { kind: "tool", key: "web", effect: "deny" },
      ],
    });
  });

  it("shows what a profile has and keeps it when saved again", async () => {
    const mock = setup({ "PUT /api/ui/profiles/guest": PROFILES[1] });
    const item = (await screen.findByText("guest", { selector: "strong" })).closest("li") as HTMLElement;
    fireEvent.click(within(item).getByRole("button", { name: "Bearbeiten" }));
    const form = screen.getByRole("form", { name: "Profil bearbeiten" });
    fireEvent.click(within(form).getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(sent(mock, "PUT", "/api/ui/profiles/guest")).toBeDefined());
    expect(sent(mock, "PUT", "/api/ui/profiles/guest")?.body).toMatchObject({ grants: [{ kind: "tool", key: "shell", effect: "deny" }] });
  });

  it("does not delete the built-in or the default profile", async () => {
    setup();
    const guest = (await screen.findByText("guest", { selector: "strong" })).closest("li") as HTMLElement;
    expect((within(guest).getByRole("button", { name: "Löschen" }) as HTMLButtonElement).disabled).toBe(true);
    const family = screen.getByText("family", { selector: "strong" }).closest("li") as HTMLElement;
    expect((within(family).getByRole("button", { name: "Löschen" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("gives a person another profile", async () => {
    const mock = setup({ "PUT /api/ui/users/u2/profile": { user: "u2", profile: "family" } });
    fireEvent.change(await screen.findByLabelText("Profil von user-b"), { target: { value: "family" } });
    await waitFor(() => expect(sent(mock, "PUT", "/api/ui/users/u2/profile")?.body).toEqual({ profile: "family" }));
  });

  it("says what the hub refused", async () => {
    setup({ "PUT /api/ui/templates/office": () => jsonResponse({ detail: { code: "modules", message: "unknown module page.nope" } }, 422) });
    fireEvent.click(await screen.findByRole("button", { name: "Neue Anwendung" }));
    const form = screen.getByRole("form", { name: "Neue Anwendung" });
    fireEvent.change(within(form).getByLabelText("Schlüssel"), { target: { value: "office" } });
    fireEvent.change(within(form).getByLabelText("Anwendung"), { target: { value: "Büro" } });
    fireEvent.click(within(form).getByRole("button", { name: "Speichern" }));
    expect((await screen.findByRole("alert")).textContent).toBe("unknown module page.nope");
    expect(screen.getByRole("form", { name: "Neue Anwendung" })).toBeTruthy(); // the form stays
  });

  it("says when the hub cannot be asked", async () => {
    setup({ "GET /api/ui/templates": () => jsonResponse({ detail: "down" }, 503) });
    expect(await screen.findByRole("alert")).toBeTruthy();
  });
});
