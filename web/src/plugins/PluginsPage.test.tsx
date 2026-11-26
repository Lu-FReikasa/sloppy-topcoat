import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { createHubEvents, type HubEvents } from "../data/hubEvents";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { diffLineClass, parseManifest, PluginsPage } from "./PluginsPage";
import type { PluginDetail, PluginRow } from "./types";

let bus: HubEvents = createHubEvents();

const SHA256_HEX = 64;
const CREATED = 201;
const INVALID = 422;
const TWO = 2;

afterEach(() => {
  vi.restoreAllMocks();
  bus = createHubEvents();
});

const row = (key: string, extra: Partial<PluginRow> = {}): PluginRow => ({
  key,
  version: "1.0.0",
  enabled: true,
  builtin: false,
  title_key: null,
  updated_at: null,
  ...extra,
});

const chat = row("chat", { builtin: true, title_key: "workspace.chat" });

const board: PluginDetail = {
  ...row("board", { version: "1.1.0" }),
  versions: [
    { version: "1.1.0", sha256: "b".repeat(SHA256_HEX), installed_by: "admin", installed_at: null, manifest: {} },
    { version: "1.0.0", sha256: "a".repeat(SHA256_HEX), installed_by: "admin", installed_at: null, manifest: {} },
  ],
  changes: [{ action: "update", from_version: "1.0.0", to_version: "1.1.0", by: "admin", at: null }],
};

function setup(rows: () => PluginRow[], extra: Record<string, unknown> = {}) {
  const mock = mockFetch({
    "GET /api/plugins": () => jsonResponse({ plugins: rows() }),
    ...extra,
  });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  render(<PluginsPage api={api} events={bus} />);
  return mock;
}

describe("PluginsPage", () => {
  it("lists the plug-ins; a built-in one cannot be switched off or removed", async () => {
    setup(() => [chat, row("board")]);
    const rows = await screen.findAllByTestId("plugin-row");
    expect(rows).toHaveLength(TWO);
    const [builtin, own] = rows as [HTMLElement, HTMLElement];
    expect(within(builtin).queryByRole("button", { name: "Entfernen" })).toBeNull();
    expect(within(builtin).getByText("Eingebaut")).toBeTruthy();
    expect(within(own).getByRole("button", { name: "Entfernen" })).toBeTruthy();
    expect(within(own).getByRole("button", { name: "Abschalten" })).toBeTruthy();
  });

  it("follows the registry live: another admin's change shows without a reload", async () => {
    let rows = [chat];
    setup(() => rows);
    expect(await screen.findAllByTestId("plugin-row")).toHaveLength(1);
    rows = [chat, row("board")];
    act(() => bus.publish({ event: "ui_plugins", data: JSON.stringify({ key: "board" }) }));
    await waitFor(() => expect(screen.getAllByTestId("plugin-row")).toHaveLength(TWO));
  });

  it("installs a new id and sends a known id as a new version", async () => {
    const mock = setup(() => [chat, row("board")], {
      "POST /api/plugins": jsonResponse(row("fresh"), CREATED),
      "POST /api/plugins/board/versions": jsonResponse(row("board", { version: "2.0.0" }), CREATED),
    });
    await screen.findAllByTestId("plugin-row");
    const box = screen.getByRole("textbox", { name: "Manifest (JSON)" });
    const send = screen.getByRole("button", { name: "Installieren oder aktualisieren" });

    fireEvent.change(box, { target: { value: '{"id": "fresh", "version": "1.0.0", "title_key": "x"}' } });
    fireEvent.click(send);
    await waitFor(() => expect(mock.calls.some((c) => c.method === "POST" && c.url === "/api/plugins")).toBe(true));

    fireEvent.change(box, { target: { value: '{"id": "board", "version": "2.0.0", "title_key": "x"}' } });
    fireEvent.click(send);
    await waitFor(() =>
      expect(mock.calls.find((c) => c.url === "/api/plugins/board/versions")?.body).toEqual({
        id: "board",
        version: "2.0.0",
        title_key: "x",
      }),
    );
  });

  it("shows what the hub refused, one problem per line", async () => {
    setup(() => [chat], {
      "POST /api/plugins": jsonResponse(
        {
          detail: {
            code: "invalid_manifest",
            message: "the manifest is not valid",
            problems: ["agent.tools_ceiling: a plug-in may only narrow the tools of its program, never add: shell"],
          },
        },
        INVALID,
      ),
    });
    await screen.findAllByTestId("plugin-row");
    fireEvent.change(screen.getByRole("textbox", { name: "Manifest (JSON)" }), {
      target: { value: '{"id": "wide", "version": "1.0.0", "title_key": "x"}' },
    });
    fireEvent.click(screen.getByRole("button", { name: "Installieren oder aktualisieren" }));
    const box = await screen.findByTestId("plugin-problems");
    expect(box.textContent).toContain("never add: shell");
  });

  it("says when the text is no manifest, without asking the hub", async () => {
    const mock = setup(() => [chat]);
    await screen.findAllByTestId("plugin-row");
    fireEvent.change(screen.getByRole("textbox", { name: "Manifest (JSON)" }), { target: { value: "{nope" } });
    fireEvent.click(screen.getByRole("button", { name: "Installieren oder aktualisieren" }));
    expect((await screen.findByTestId("plugin-problems")).textContent).toContain("kein JSON-Objekt");
    expect(mock.calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("shows the versions, compares one with the version in force and rolls back", async () => {
    const mock = setup(() => [row("board", { version: "1.1.0" })], {
      "GET /api/plugins/board": board,
      "GET /api/plugins/board/diff?a=1.0.0&b=1.1.0": {
        a: "1.0.0",
        b: "1.1.0",
        lines: ["--- 1.0.0", "+++ 1.1.0", "@@ -1 +1 @@", '-  "version": "1.0.0"', '+  "version": "1.1.0"'],
      },
      "POST /api/plugins/board/rollback": row("board"),
    });
    fireEvent.click(await screen.findByRole("button", { name: "board" }));
    const versions = await screen.findAllByTestId("plugin-version");
    expect(versions).toHaveLength(TWO);
    const [inForce, older] = versions as [HTMLElement, HTMLElement];
    expect(within(inForce).getByText("(gilt)")).toBeTruthy();
    expect(within(inForce).queryByRole("button", { name: "Hierauf zurückrollen" })).toBeNull();

    fireEvent.click(within(older).getByRole("button", { name: "Vergleichen" }));
    const diff = await screen.findByTestId("plugin-diff");
    await waitFor(() => expect(diff.querySelector(".plugin-diff-add")?.textContent).toContain('"1.1.0"'));
    expect(diff.querySelector(".plugin-diff-del")?.textContent).toContain('"1.0.0"');

    fireEvent.click(within(older).getByRole("button", { name: "Hierauf zurückrollen" }));
    await waitFor(() =>
      expect(mock.calls.find((c) => c.url === "/api/plugins/board/rollback")?.body).toEqual({ version: "1.0.0" }),
    );
  });
});

describe("parseManifest", () => {
  it("reads the id of a JSON object and refuses anything else", () => {
    expect(parseManifest('{"id": "a-b"}')).toEqual({ manifest: { id: "a-b" }, id: "a-b" });
    expect(parseManifest("[1]")).toEqual({ error: "json" });
    expect(parseManifest("nope")).toEqual({ error: "json" });
    expect(parseManifest('{"version": "1.0.0"}')).toEqual({ error: "id" });
  });
});

describe("diffLineClass", () => {
  it("tells added, removed, hunk and file lines apart", () => {
    expect(diffLineClass("+++ 1.1.0")).toBe("plugin-diff-head");
    expect(diffLineClass("--- 1.0.0")).toBe("plugin-diff-head");
    expect(diffLineClass("@@ -1 +1 @@")).toBe("plugin-diff-hunk");
    expect(diffLineClass('+  "a": 1')).toBe("plugin-diff-add");
    expect(diffLineClass('-  "a": 1')).toBe("plugin-diff-del");
    expect(diffLineClass('   "a": 1')).toBe("");
  });
});
