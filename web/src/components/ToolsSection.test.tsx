import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { makeDetail } from "../test/fixtures";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { NodeCapabilities, ToolCallRow } from "../types";
import { ToolsSection } from "./ToolsSection";

const caps: NodeCapabilities = {
  node: { id: "n1", name: "vm-docker", roles: ["tools"] },
  offered: [
    {
      name: "docker.ps",
      description: "Containers of this host",
      read_only: true,
      input_schema: { properties: { all: { type: "boolean" } } },
    },
    {
      name: "docker.logs",
      description: "Log lines of one container",
      read_only: true,
      input_schema: { required: ["container"], properties: { container: { type: "string" } } },
    },
    {
      name: "docker.restart",
      description: "Restart a container",
      read_only: false,
      input_schema: { required: ["container"], properties: { container: { type: "string" } } },
    },
  ],
  enabled: ["docker.ps", "docker.logs", "docker.restart"],
  usable: ["docker.ps", "docker.logs"],
  grants: [],
};

const call: ToolCallRow = {
  id: 7,
  user_id: "u1",
  user: "admin",
  node: "vm-docker",
  capability: "docker.ps",
  input: {},
  via: "gui",
  ok: true,
  code: null,
  error: null,
  output_bytes: 123,
  output_excerpt: '{"total":1}',
  duration_ms: 5,
  created_at: "2026-09-30T08:00:00Z",
};

function setup(routes: Record<string, unknown> = {}) {
  const mock = mockFetch({
    "GET /api/nodes/n1/capabilities": caps,
    "GET /api/tool-calls?limit=25&node_id=n1": [call],
    "GET /api/users": jsonResponse({ detail: { code: "forbidden" } }, 403),
    ...routes,
  });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  const node = makeDetail({ id: "n1", name: "vm-docker", connected: true, state: "available" });
  render(<ToolsSection api={api} node={node} />);
  return mock;
}

describe("ToolsSection", () => {
  it("lists the capabilities with their state for the user", async () => {
    setup();
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Containers of this host")).toBeTruthy();
    const rows = within(table).getAllByRole("row");
    const restart = rows.find((r) => r.textContent?.includes("docker.restart"));
    expect(restart?.textContent).toContain("nur nach Bestätigung");
    const ps = rows.find((r) => r.textContent?.includes("docker.ps"));
    expect(ps?.textContent).toContain("darfst du nutzen");
    // a capability that changes state can not even be picked
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual(["docker.ps", "docker.logs"]);
  });

  it("runs a capability with the typed input and shows the output", async () => {
    const mock = setup({
      "POST /api/nodes/n1/capabilities/docker.logs": {
        ok: true,
        call_id: 8,
        output: { container: "web", lines: 2, log: "line one\nline two" },
        bytes: 99,
        duration_ms: 12,
      },
    });
    await screen.findByRole("table");
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "docker.logs" } });
    const box = screen.getByLabelText("Eingabe (JSON)") as HTMLTextAreaElement;
    expect(JSON.parse(box.value)).toEqual({ container: "" }); // the template of the required fields
    fireEvent.change(box, { target: { value: '{"container":"web"}' } });
    fireEvent.click(screen.getByRole("button", { name: "Ausführen" }));
    expect(await screen.findByText(/line one/)).toBeTruthy();
    expect(screen.getByText(/Ergebnis \(99 Bytes, 12 ms\)/)).toBeTruthy();
    const post = mock.calls.find((c) => c.method === "POST");
    expect(post?.body).toEqual({ input: { container: "web" } });
  });

  it("does not send input that is not a JSON object", async () => {
    const mock = setup();
    await screen.findByRole("table");
    fireEvent.change(screen.getByLabelText("Eingabe (JSON)"), { target: { value: "[1" } });
    fireEvent.click(screen.getByRole("button", { name: "Ausführen" }));
    expect(await screen.findByText("Die Eingabe muss ein JSON-Objekt sein.")).toBeTruthy();
    expect(mock.calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("explains a refusal in words", async () => {
    setup({
      "POST /api/nodes/n1/capabilities/docker.ps": jsonResponse(
        { detail: { code: "not_granted", message: "you may not use docker.ps on this node" } },
        403,
      ),
    });
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("button", { name: "Ausführen" }));
    expect(await screen.findByText(/darfst du auf diesem Node nicht nutzen/)).toBeTruthy();
  });

  it("shows the recent calls with their details", async () => {
    setup();
    await screen.findByRole("table");
    expect(await screen.findByText("ok, 123 Bytes")).toBeTruthy();
    expect(screen.getByText('{"total":1}')).toBeTruthy();
  });

  it("says so when nothing may be run", async () => {
    setup({ "GET /api/nodes/n1/capabilities": { ...caps, usable: [] } });
    expect(await screen.findByText(/keine Fähigkeit zum Ausführen/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Ausführen" })).toBeNull();
  });

  it("lets an admin grant capabilities per user", async () => {
    const mock = setup({
      "GET /api/users": [
        { id: "u1", name: "admin", role: "admin" },
        { id: "u2", name: "anna", role: "user" },
      ],
      "GET /api/nodes/n1/grants": [{ user_id: "u2", user: "anna", capabilities: ["docker.ps"] }],
      "PUT /api/nodes/n1/grants/u2": { user_id: "u2", capabilities: ["docker.logs", "docker.ps"] },
    });
    const box = await screen.findByRole("checkbox", { name: "anna: docker.logs" });
    expect(
      (screen.getByRole("checkbox", { name: "anna: docker.ps" }) as HTMLInputElement).checked,
    ).toBe(true);
    expect((box as HTMLInputElement).checked).toBe(false);
    expect(screen.queryByRole("checkbox", { name: /admin:/ })).toBeNull(); // admins need no grant
    fireEvent.click(box);
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await screen.findByRole("button", { name: "Gespeichert" });
    const put = mock.calls.find((c) => c.method === "PUT");
    expect(put?.body).toEqual({ capabilities: ["docker.logs", "docker.ps"] });
  });

  it("shows no grants section to a user who may not manage them", async () => {
    setup();
    await screen.findByRole("table");
    await waitFor(() => expect(screen.queryByText("Wer was nutzen darf")).toBeNull());
  });
});
