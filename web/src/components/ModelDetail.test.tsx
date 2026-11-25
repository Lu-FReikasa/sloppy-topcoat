import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { makeNode } from "../test/fixtures";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { NodeSummary } from "../types";
import { ModelDetail } from "./ModelDetail";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const qwen = { name: "qwen-7b", size_bytes: 4_500_000_000, loaded: false, vram_used_mb: null };

const amd = makeNode({
  id: "a",
  name: "node-amd",
  models: [{ ...qwen, loaded: true, vram_used_mb: 5200 }],
});
const nvidia = makeNode({ id: "b", name: "node-nvidia", models: [qwen] });
const external = makeNode({ id: "e", name: "node-ext", models: [{ ...qwen, own: false }] });
const laptop = makeNode({
  id: "c",
  name: "node-laptop",
  connected: false,
  state: "offline",
  models: [qwen],
});
const empty = makeNode({ id: "d", name: "node-empty", models: [], disk_free_mb: 100 * 1024 });
const tool = makeNode({ id: "t", name: "node-tools", models: [], allowed_actions: ["tools.run"] });

function setup(nodes: NodeSummary[], routes: Record<string, unknown> = {}, name = "qwen-7b") {
  const mock = mockFetch({
    "GET /api/model-transfers": { transfers: [] },
    "GET /api/nodes": nodes,
    ...routes,
  });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  const onChanged = vi.fn();
  render(<ModelDetail api={api} name={name} nodes={nodes} loaded error={null} onChanged={onChanged} />);
  return { mock, onChanged };
}

const btn = (row: HTMLElement, name: string | RegExp) =>
  within(row).queryByRole("button", { name }) as HTMLButtonElement | null;

describe("ModelDetail", () => {
  it("shows the heading, size and one row per model-holding node", () => {
    setup([amd, nvidia, laptop, empty, tool]);
    expect(screen.getByRole("heading", { name: "qwen-7b" })).toBeTruthy();
    expect(screen.getByTestId("model-size").textContent).toContain("4,5 GB");
    expect(screen.queryByTestId("model-row-t")).toBeNull(); // tool-only node
    const a = screen.getByTestId("model-row-a");
    expect(within(a).getByRole("link", { name: "node-amd" }).getAttribute("href")).toBe("#/admin/nodes/a");
    expect(within(a).getByRole("img", { name: "gespeichert" })).toBeTruthy();
    expect(within(a).getByTestId("model-loaded").textContent).toContain("5.200 MB");
    expect(within(screen.getByTestId("model-row-b")).getByRole("img", { name: "nicht geladen" })).toBeTruthy();
    expect(within(screen.getByTestId("model-row-d")).getByRole("img", { name: "nicht gespeichert" })).toBeTruthy();
  });

  it("offers the right actions per state", () => {
    setup([amd, nvidia, external, laptop, empty]);
    const loaded = screen.getByTestId("model-row-a");
    expect(btn(loaded, /entladen/)).toBeTruthy();
    expect(btn(loaded, /kopieren/)).toBeTruthy();
    expect(btn(loaded, /verschieben/)!.disabled).toBe(true); // loaded: unload first
    expect(btn(loaded, /löschen/)!.disabled).toBe(true);

    const stored = screen.getByTestId("model-row-b");
    expect(btn(stored, /laden$/)!.disabled).toBe(false);
    expect(btn(stored, /verschieben/)!.disabled).toBe(false);
    expect(btn(stored, /löschen/)!.disabled).toBe(false);

    const ext = screen.getByTestId("model-row-e");
    expect(btn(ext, /kopieren/)).toBeTruthy();
    expect(btn(ext, /verschieben/)).toBeNull();
    expect(btn(ext, /löschen/)).toBeNull();

    const off = screen.getByTestId("model-row-c");
    expect(within(off).getByText(/Node offline/)).toBeTruthy();
    for (const b of within(off).getAllByRole("button")) expect((b as HTMLButtonElement).disabled).toBe(true);

    const notStored = screen.getByTestId("model-row-d");
    expect(within(notStored).getAllByRole("button")).toHaveLength(1);
    expect(btn(notStored, "qwen-7b auf node-empty kopieren")).toBeTruthy();
  });

  it("loads, unloads and reports errors", async () => {
    const { mock, onChanged } = setup([amd, nvidia], {
      "POST /api/nodes/b/actions": () => jsonResponse({ ok: true, result: {} }),
      "POST /api/nodes/a/actions": () =>
        jsonResponse({ ok: false, error: { code: "busy", message: "game running" } }),
    });
    fireEvent.click(btn(screen.getByTestId("model-row-b"), "qwen-7b auf node-nvidia laden")!);
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(mock.calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "models.load",
      args: { model: "qwen-7b" },
    });
    fireEvent.click(btn(screen.getByTestId("model-row-a"), "qwen-7b auf node-amd entladen")!);
    expect((await screen.findByRole("alert")).textContent).toContain("node-amd / qwen-7b");
  });

  it("deletes only after confirmation", async () => {
    const { mock, onChanged } = setup([nvidia], {
      "POST /api/nodes/b/actions": () => jsonResponse({ ok: true, result: {} }),
    });
    const del = btn(screen.getByTestId("model-row-b"), /löschen/)!;
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    fireEvent.click(del);
    expect(mock.calls.filter((c) => c.method === "POST")).toHaveLength(0);
    confirm.mockReturnValue(true);
    fireEvent.click(del);
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(mock.calls.find((c) => c.method === "POST")?.body).toEqual({
      name: "models.delete",
      args: { model: "qwen-7b", confirm: true },
    });
  });

  it("opens the copy dialog with this node as the source", async () => {
    setup([amd, nvidia, empty]);
    fireEvent.click(btn(screen.getByTestId("model-row-b"), /kopieren/)!);
    expect(await screen.findByText(/Von Node node-nvidia/)).toBeTruthy();
    expect(screen.getByText("Ziel-Node")).toBeTruthy();
  });

  it("copy here fixes the target and preselects the only source", async () => {
    const { mock } = setup([nvidia, empty], {
      "POST /api/model-transfers": () =>
        jsonResponse({ id: "t1", source_node_id: "b", target_node_id: "d", model: "qwen-7b" }),
    });
    fireEvent.click(btn(screen.getByTestId("model-row-d"), "qwen-7b auf node-empty kopieren")!);
    const dlg = within(await screen.findByRole("dialog"));
    const radio = (await dlg.findByLabelText(/node-nvidia/)) as HTMLInputElement;
    expect(radio.checked).toBe(true);
    expect(screen.getByText(/Auf Node node-empty/)).toBeTruthy();
    fireEvent.click(dlg.getByRole("button", { name: "Kopieren" }));
    await waitFor(() => expect(screen.queryByText("Quell-Node")).toBeNull());
    expect(mock.calls.find((c) => c.method === "POST")?.body).toEqual({
      source_node_id: "b",
      target_node_id: "d",
      model: "qwen-7b",
    });
  });

  it("copy here lets the user pick between several sources", async () => {
    setup([amd, nvidia, empty]);
    fireEvent.click(btn(screen.getByTestId("model-row-d"), "qwen-7b auf node-empty kopieren")!);
    const dlg = within(await screen.findByRole("dialog"));
    const a = (await dlg.findByLabelText(/node-amd/)) as HTMLInputElement;
    expect(a.checked).toBe(false);
    expect((dlg.getByLabelText(/node-nvidia/) as HTMLInputElement).checked).toBe(false);
  });

  it("lists the transfers of this model", async () => {
    setup([amd, nvidia], {
      "GET /api/model-transfers": {
        transfers: [
          {
            id: "t1", user_id: "u", source_node_id: "a", target_node_id: "b", model: "qwen-7b",
            target_name: null, size: 1, sha256: null, move: false, status: "done",
            error_code: null, error: null, created_at: "", updated_at: "",
          },
        ],
      },
    });
    expect(await screen.findByText("qwen-7b von node-amd nach node-nvidia")).toBeTruthy();
  });

  it("shows a friendly empty state for an unknown model", () => {
    setup([amd], {}, "nope");
    expect(screen.getByTestId("model-not-found").textContent).toContain("nope");
    expect(screen.getByRole("link", { name: "Zurück zu den Modellen" }).getAttribute("href")).toBe(
      "#/admin/models",
    );
  });
});
