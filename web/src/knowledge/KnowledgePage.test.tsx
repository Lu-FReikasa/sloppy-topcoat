import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { KnowledgeSource, Me } from "../types";
import { KnowledgePage } from "./KnowledgePage";

const ADMIN: Me = { id: "a", name: "admin", role: "admin" } as Me;
const USER: Me = { id: "u", name: "anna", role: "user" } as Me;
const SOURCE: KnowledgeSource = {
  id: "s1",
  name: "Notes",
  visibility: "shared",
  kind: "node",
  own: true,
  node: "vm-docker1",
  root: "",
  status: "ready",
  error: null,
  files: 12,
  chunks: 80,
  embedded: 75,
  excluded: 3,
  blocked: 1,
  indexed_at: "2026-10-03T10:00:00Z",
  created_at: null,
};
const STATUS = {
  embedder: "hashing-v1-384",
  transitional: true,
  exclude: ["*SENSIBEL*", "*.key"],
};

function show(routes: Record<string, unknown>, me: Me = ADMIN, mode: "user" | "care" = "user") {
  const mock = mockFetch({
    "GET /api/knowledge/sources": [SOURCE],
    "GET /api/knowledge/status": STATUS,
    ...routes,
  });
  render(<KnowledgePage api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} me={me} mode={mode} />);
  return mock;
}

describe("KnowledgePage", () => {
  it("lists the sources with counts, what was kept out, and the exclude list", async () => {
    show({}, ADMIN, "care");
    const card = await screen.findByTestId("knowledge-source");
    expect(card.textContent).toContain("Notes");
    expect(card.textContent).toMatch(/12 Dateien, 80 Abschnitte/);
    expect(card.textContent).toMatch(/3 Einträge durch die Ausschlussliste, 1 Abschnitte/);
    expect((await screen.findByTestId("knowledge-exclude")).textContent).toContain("*SENSIBEL*");
  });

  it("shows how many passages carry a vector, and re-indexes the source", async () => {
    const mock = show(
      {
        "GET /api/knowledge/sources": [SOURCE],
        "POST /api/knowledge/sources/s1/reindex": { ...SOURCE, status: "indexing" },
      },
      ADMIN,
      "care",
    );
    const card = await screen.findByTestId("knowledge-source");
    expect(card.textContent).toMatch(/75 von 80 Abschnitten mit Vektor/);
    expect(card.textContent).toContain("bereit");
    fireEvent.click(within(card).getByRole("button", { name: "Neu indexieren" }));
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/knowledge/sources/s1/reindex")).toBe(true),
    );
  });

  it("keeps the management out of everyday use, even for the admin", async () => {
    show({});
    await screen.findByTestId("knowledge-source");
    expect(screen.getByTestId("knowledge-transitional")).toBeTruthy();
    expect(screen.queryByTestId("knowledge-add")).toBeNull();
    expect(screen.queryByTestId("knowledge-exclude")).toBeNull();
    expect(screen.queryByRole("button", { name: "Entfernen" })).toBeNull();
  });

  it("searches and shows the hits with file and heading", async () => {
    const mock = show({
      "GET /api/knowledge/search?q=backup": {
        query: "backup",
        hits: [
          {
            source: "Notes",
            path: "README.md",
            heading: "Home lab > Backups",
            text: "The nightly backup runs at 03:00.",
            mtime: 1,
            score: 0.03,
          },
        ],
      },
    });
    const box = await screen.findByTestId("knowledge-search");
    fireEvent.change(within(box).getByLabelText("Notizen durchsuchen"), {
      target: { value: "backup" },
    });
    fireEvent.click(within(box).getByRole("button", { name: "Suchen" }));
    const hit = await screen.findByTestId("knowledge-hit");
    expect(hit.textContent).toContain("README.md");
    expect(hit.textContent).toContain("Home lab > Backups");
    expect(hit.textContent).toContain("03:00");
    expect(mock.calls.some((c) => c.url === "/api/knowledge/search?q=backup")).toBe(true);
  });

  it("says when nothing was found", async () => {
    show({ "GET /api/knowledge/search?q=zzz": { query: "zzz", hits: [] } });
    const box = await screen.findByTestId("knowledge-search");
    fireEvent.change(within(box).getByLabelText("Notizen durchsuchen"), {
      target: { value: "zzz" },
    });
    fireEvent.click(within(box).getByRole("button", { name: "Suchen" }));
    expect(await screen.findByText("Nichts gefunden.")).toBeTruthy();
  });

  it("shows which files the secret filter held back, without any content", async () => {
    show({
      "GET /api/knowledge/sources/s1": {
        ...SOURCE,
        blocked_files: [{ path: "ops.md", rules: ["password"] }],
      },
    });
    await screen.findByTestId("knowledge-source");
    fireEvent.click(screen.getByRole("button", { name: "Welche Dateien?" }));
    expect((await screen.findByTestId("knowledge-blocked")).textContent).toBe("ops.md — password");
  });

  it("lets the admin add, re-index and remove a source", async () => {
    const mock = show({
      "POST /api/knowledge/sources": { ...SOURCE, id: "s2", name: "Lab", status: "new" },
      "POST /api/knowledge/sources/s1/reindex": { ...SOURCE, status: "indexing" },
      "DELETE /api/knowledge/sources/s1": () => new Response(null, { status: 204 }),
    }, ADMIN, "care");
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const form = await screen.findByTestId("knowledge-add");
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Lab" } });
    fireEvent.change(within(form).getByLabelText("Tool-Node"), { target: { value: "vm-docker1" } });
    fireEvent.click(within(form).getByRole("button", { name: "Hinzufügen und indexieren" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "POST")).toBe(true));
    expect(
      mock.calls.find((c) => c.url === "/api/knowledge/sources" && c.method === "POST")?.body,
    ).toEqual({ name: "Lab", node: "vm-docker1", root: "", visibility: "shared" });
    fireEvent.click(screen.getAllByRole("button", { name: "Neu indexieren" })[0]!);
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/knowledge/sources/s1/reindex")).toBe(true),
    );
    fireEvent.click(screen.getAllByRole("button", { name: "Entfernen" })[0]!);
    await waitFor(() =>
      expect(
        mock.calls.some((c) => c.method === "DELETE" && c.url === "/api/knowledge/sources/s1"),
      ).toBe(true),
    );
  });

  it("shows a refused folder in words", async () => {
    show({
      "POST /api/knowledge/sources": () =>
        jsonResponse({ detail: { code: "root_refused", message: "raw text" } }, 422),
    }, ADMIN, "care");
    const form = await screen.findByTestId("knowledge-add");
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Lab" } });
    fireEvent.change(within(form).getByLabelText("Tool-Node"), { target: { value: "vm-docker1" } });
    fireEvent.click(within(form).getByRole("button", { name: "Hinzufügen und indexieren" }));
    expect(await screen.findByText("Der Node gibt diesen Ordner nicht frei.")).toBeTruthy();
  });

  it("offers no management to a user, and hides where the files are for a shared source of another", async () => {
    show(
      { "GET /api/knowledge/sources": [{ ...SOURCE, own: false, root: null, error: null }] },
      USER,
    );
    await screen.findByTestId("knowledge-source");
    expect(screen.queryByTestId("knowledge-add")).toBeNull();
    expect(screen.queryByRole("button", { name: "Neu indexieren" })).toBeNull();
    expect(screen.getByTestId("knowledge-source").textContent).not.toContain("vm-docker1");
  });

  it("uploads files into a new source, one request per file", async () => {
    const mock = show({
      "POST /api/knowledge/uploads": { ...SOURCE, id: "u1", kind: "upload", name: "Handbook" },
    }, ADMIN, "care");
    const form = await screen.findByTestId("knowledge-upload");
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Handbook" } });
    const a = new File(["# A\n\nfirst note about the router"], "a.md", { type: "text/markdown" });
    const b = new File(["second note"], "b.txt", { type: "text/plain" });
    fireEvent.change(within(form).getByLabelText("Dateien"), { target: { files: [a, b] } });
    fireEvent.click(within(form).getByRole("button", { name: "Hochladen" }));
    await waitFor(() =>
      expect(mock.calls.filter((c) => c.url === "/api/knowledge/uploads")).toHaveLength(2),
    );
    const calls = mock.calls.filter((c) => c.url === "/api/knowledge/uploads");
    expect(calls[0]?.body).toMatchObject({ name: "Handbook", filename: "a.md" });
    expect(calls[1]?.body).toMatchObject({ source_id: "u1", filename: "b.txt" });
  });

  it("offers no upload to a user who is not the admin", async () => {
    show({}, USER);
    await screen.findByTestId("knowledge-source");
    expect(screen.queryByTestId("knowledge-upload")).toBeNull();
  });
});
