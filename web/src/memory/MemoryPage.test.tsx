import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { MemoryItem, MemoryOverview, MemoryQuestion } from "../types";
import { MemoryPage } from "./MemoryPage";

afterEach(() => {
  vi.restoreAllMocks();
});

function item(overrides: Partial<MemoryItem> & { id: string; text: string }): MemoryItem {
  return {
    status: "active",
    evidence: "doc",
    confidence: 0.9,
    source_type: "chat",
    source_ref: { conversation_id: "c1", message_id: 5 },
    version: 1,
    confirmed_count: 0,
    last_confirmed_at: null,
    disputed_with: null,
    superseded_by: null,
    embedder: "hashing-v1-384",
    created_at: "2026-09-01T10:00:00Z",
    updated_at: "2026-09-01T10:00:00Z",
    ...overrides,
  };
}

function overview(items: MemoryItem[], extra: Partial<MemoryOverview> = {}): MemoryOverview {
  const counts = { active: 0, disputed: 0, superseded: 0, deleted: 0, archived: 0 };
  for (const i of items) counts[i.status] += 1;
  return {
    items,
    counts,
    blocked: 0,
    enabled: true,
    embedder: { id: "hashing-v1-384", transitional: true },
    running: false,
    runs: [],
    questions: [],
    ...extra,
  };
}

function setup(routes: Record<string, unknown>) {
  const mock = mockFetch(routes);
  render(<MemoryPage api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} />);
  return mock;
}

const HAMBURG = item({ id: "a", text: "Wohnt in Hamburg" });

describe("MemoryPage", () => {
  it("lists the notes with tag, status, version and a link to the conversation", async () => {
    setup({ "GET /api/memory?application=core": overview([HAMBURG]) });
    const card = await screen.findByTestId("memory-item");
    expect(card.textContent).toContain("Wohnt in Hamburg");
    expect(card.textContent).toContain("[doc]");
    expect(card.textContent).toContain("Aktiv");
    expect(card.textContent).toContain("Sicherheit 90 %");
    expect(card.textContent).toContain("Version 1");
    expect(
      within(card).getByRole("link", { name: "aus einem Chat" }).getAttribute("href"),
    ).toBe("#/chat/c1");
    expect(screen.getByTestId("transitional-note")).toBeTruthy();
  });

  it("shows how often a note was confirmed without a new version", async () => {
    setup({
      "GET /api/memory?application=core": overview([
        item({
          id: "a",
          text: "Wohnt in Hamburg",
          confirmed_count: 3,
          last_confirmed_at: "2026-09-20T10:00:00Z",
        }),
        item({ id: "b", text: "Ist Tischler" }),
      ]),
    });
    const cards = await screen.findAllByTestId("memory-item");
    expect(within(cards[0] as HTMLElement).getByTestId("confirmed").textContent).toContain(
      "3× bestätigt (zuletzt",
    );
    expect(cards[0]?.textContent).toContain("Version 1");
    expect(within(cards[1] as HTMLElement).queryByTestId("confirmed")).toBeNull();
  });

  it("searches and filters on the server", async () => {
    const mock = setup({
      "GET /api/memory?application=core": overview([HAMBURG]),
      "GET /api/memory?q=hamburg&application=core": overview([HAMBURG]),
      "GET /api/memory?status=deleted&application=core": overview([]),
      "GET /api/memory?evidence=assumption&application=core": overview([]),
    });
    await screen.findByTestId("memory-item");
    fireEvent.change(screen.getByLabelText("Notizen durchsuchen"), {
      target: { value: "hamburg" },
    });
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/memory?q=hamburg&application=core")).toBe(true),
    );
    fireEvent.change(screen.getByLabelText("Notizen durchsuchen"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "deleted" } });
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/memory?status=deleted&application=core")).toBe(true),
    );
    expect(await screen.findByText("Keine Notiz passt.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Beleg"), { target: { value: "assumption" } });
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/memory?evidence=assumption&application=core")).toBe(true),
    );
  });

  it("edits a note, deletes it and restores a deleted one", async () => {
    const mock = setup({
      "GET /api/memory?application=core": overview([
        HAMBURG,
        item({ id: "d", text: "Alte Notiz", status: "deleted" }),
      ]),
      "PATCH /api/memory/a": item({ id: "a", text: "Wohnt in Altona", version: 2 }),
      "DELETE /api/memory/a": item({ id: "a", text: "x", status: "deleted" }),
      "POST /api/memory/d/restore": item({ id: "d", text: "Alte Notiz" }),
    });
    const cards = await screen.findAllByTestId("memory-item");
    const first = cards[0] as HTMLElement;
    fireEvent.click(within(first).getByRole("button", { name: "Bearbeiten" }));
    fireEvent.change(within(first).getByLabelText("Text der Notiz"), {
      target: { value: "Wohnt in Altona" },
    });
    fireEvent.click(within(first).getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(mock.calls.find((c) => c.method === "PATCH")?.body).toEqual({ text: "Wohnt in Altona" });
    fireEvent.click(within(first).getByRole("button", { name: "Löschen" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "DELETE")).toBe(true));
    fireEvent.click(
      within(cards[1] as HTMLElement).getByRole("button", { name: "Wiederherstellen" }),
    );
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/memory/d/restore")).toBe(true),
    );
  });

  it("shows the history with reasons and reverts to an earlier version", async () => {
    const current = item({ id: "a", text: "Wohnt in Altona", version: 3 });
    const mock = setup({
      "GET /api/memory?application=core": overview([current]),
      "GET /api/memory/a": {
        ...current,
        versions: [
          {
            version: 3,
            text: "Wohnt in Altona",
            status: "active",
            evidence: "doc",
            confidence: 0.9,
            reason: "edited by the user",
            actor: "user",
            created_at: "2026-09-03T10:00:00Z",
          },
          {
            version: 2,
            text: "Wohnt in Hamburg-Nord",
            status: "active",
            evidence: "doc",
            confidence: 0.9,
            reason: "consolidation: more precise",
            actor: "consolidation",
            created_at: "2026-09-02T10:00:00Z",
          },
          {
            version: 1,
            text: "Wohnt in Hamburg",
            status: "active",
            evidence: "doc",
            confidence: 0.9,
            reason: "remembered from conversation",
            actor: "chat",
            created_at: "2026-09-01T10:00:00Z",
          },
        ],
      },
      "POST /api/memory/a/revert": item({ id: "a", text: "Wohnt in Hamburg", version: 4 }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Verlauf" }));
    const history = await screen.findByTestId("history");
    expect(history.textContent).toContain("consolidation: more precise");
    expect(history.textContent).toContain("remembered from conversation");
    expect(within(history).queryByRole("button", { name: "Auf v3 zurücksetzen" })).toBeNull();
    fireEvent.click(within(history).getByRole("button", { name: "Auf v1 zurücksetzen" }));
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/memory/a/revert")).toBe(true),
    );
    expect(mock.calls.find((c) => c.url === "/api/memory/a/revert")?.body).toEqual({ version: 1 });
  });

  it("shows a disputed pair and lets the user decide", async () => {
    const berlin = item({
      id: "b",
      text: "Wohnt in Berlin",
      status: "disputed",
      disputed_with: "h",
    });
    const hamburg = item({
      id: "h",
      text: "Wohnt in Hamburg",
      status: "disputed",
      disputed_with: "b",
    });
    const mock = setup({
      "GET /api/memory?application=core": overview([berlin, hamburg]),
      "POST /api/memory/h/resolve": item({ id: "h", text: "Wohnt in Hamburg" }),
    });
    const disputes = await screen.findAllByTestId("dispute");
    expect(disputes).toHaveLength(2);
    expect(disputes[0]?.textContent).toContain("Widerspricht: „Wohnt in Hamburg“");
    fireEvent.click(
      within(disputes[1] as HTMLElement).getByRole("button", { name: "Diese behalten" }),
    );
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/memory/h/resolve")).toBe(true),
    );
    expect(mock.calls.find((c) => c.url === "/api/memory/h/resolve")?.body).toEqual({
      keep: "this",
    });
  });

  it("suggests the newer note of a dispute", async () => {
    const older = item({
      id: "o",
      text: "Wohnt in Hamburg",
      status: "disputed",
      disputed_with: "n",
      created_at: "2026-09-01T10:00:00Z",
    });
    const newer = item({
      id: "n",
      text: "Wohnt in Berlin",
      status: "disputed",
      disputed_with: "o",
      created_at: "2026-09-10T10:00:00Z",
    });
    setup({ "GET /api/memory?application=core": overview([newer, older]) });
    const disputes = await screen.findAllByTestId("dispute");
    const [forNewer, forOlder] = disputes as [HTMLElement, HTMLElement];
    expect(within(forNewer).getByTestId("suggested-newer").textContent).toContain("neuere Notiz");
    expect(within(forNewer).getByRole("button", { name: "Diese behalten" }).className).toContain(
      "primary",
    );
    expect(within(forOlder).queryByTestId("suggested-newer")).toBeNull();
    expect(
      within(forOlder).getByRole("button", { name: "Die andere behalten (neuer, Vorschlag)" })
        .className,
    ).toContain("primary");
    expect(
      within(forOlder).getByRole("button", { name: "Diese behalten" }).className,
    ).not.toContain("primary");
  });

  it("forgets everything only after confirmation and can purge deleted notes", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValue(true);
    const mock = setup({
      "GET /api/memory?application=core": overview([HAMBURG, item({ id: "d", text: "weg", status: "deleted" })]),
      "POST /api/memory/forget-all": { forgotten: 2 },
      "POST /api/memory/purge-deleted": { purged: 1 },
    });
    const forget = await screen.findByRole("button", { name: "Alles vergessen" });
    fireEvent.click(forget);
    expect(mock.calls.some((c) => c.url === "/api/memory/forget-all")).toBe(false);
    fireEvent.click(forget);
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/memory/forget-all")).toBe(true),
    );
    expect(
      await screen.findByText("Alle Notizen wurden gelöscht (wiederherstellbar)."),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Gelöschte Notizen endgültig entfernen" }));
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/memory/purge-deleted")).toBe(true),
    );
    expect(confirm).toHaveBeenCalledTimes(3);
  });

  it("starts a consolidation, explains a refusal and shows the last run", async () => {
    const run = {
      id: "r1",
      trigger: "manual",
      status: "ok",
      started_at: "2026-09-29T10:00:00Z",
      finished_at: "2026-09-29T10:01:00Z",
      stats: { merged: 1, disputed: 2, corrected: 3 },
    };
    let refuse = true;
    const mock = setup({
      "GET /api/memory?application=core": overview([HAMBURG], { runs: [run] as MemoryOverview["runs"] }),
      "POST /api/memory/consolidate": () =>
        refuse
          ? jsonResponse({ detail: { code: "no_node_available", message: "english" } }, 409)
          : jsonResponse({ started: true }, 202),
    });
    const button = await screen.findByRole("button", { name: "Jetzt aufräumen" });
    expect(screen.getByTestId("run-summary").textContent).toContain(
      "zusammengeführt 1, geklärt 0, widersprüchlich 2, korrigiert 3, Fragen 0",
    );
    fireEvent.click(button);
    expect(await screen.findByText(/Kein Node ist verfügbar/)).toBeTruthy();
    refuse = false;
    fireEvent.click(button);
    await waitFor(() => expect(mock.calls.filter((c) => c.method === "POST")).toHaveLength(2));
  });

  it("says when memory is off and shows blocked candidates", async () => {
    setup({ "GET /api/memory?application=core": overview([], { enabled: false, blocked: 3 }) });
    expect(await screen.findByText(/Das Gedächtnis ist ausgeschaltet/)).toBeTruthy();
    expect(screen.getByTestId("blocked-total").textContent).toContain("3 Kandidaten");
    expect(screen.getByText(/Noch nichts gemerkt/)).toBeTruthy();
  });

  it("does not show the transitional note with a real embedder", async () => {
    setup({
      "GET /api/memory?application=core": overview([HAMBURG], {
        embedder: { id: "gateway:bge-m3", transitional: false },
      }),
    });
    await screen.findByTestId("memory-item");
    expect(screen.queryByTestId("transitional-note")).toBeNull();
  });
  it("puts the open questions on top, answers one and reloads", async () => {
    const question: MemoryQuestion = {
      id: "q1",
      topic: "residence",
      origin: "consolidation",
      status: "open",
      candidates: [
        { id: "a", text: "Lebt in Hamburg", at: null, confirmed_count: 0, last_confirmed_at: null },
        { id: "b", text: "Lebt in Berlin", at: null, confirmed_count: 0, last_confirmed_at: null },
      ],
      resolution: null,
      created_at: null,
      answered_at: null,
    };
    let answered = false;
    const mock = setup({
      "GET /api/memory?application=core": () =>
        jsonResponse(overview([HAMBURG], { questions: answered ? [] : [question] })),
      "POST /api/memory/questions/q1/answer": () => {
        answered = true;
        return jsonResponse({ ok: true });
      },
    });
    const card = await screen.findByTestId("question-card");
    fireEvent.click(within(card).getByRole("button", { name: "„Lebt in Hamburg“ stimmt" }));
    await waitFor(() => expect(screen.queryByTestId("question-card")).toBeNull());
    expect(mock.calls.find((c) => c.method === "POST")?.body).toEqual({ note_id: "a" });
  });

  it("shows what the tidy-up settled and asked, and what a change is based on", async () => {
    const mock = setup({
      "GET /api/memory?application=core": overview([HAMBURG], {
        runs: [
          {
            id: "r1",
            trigger: "manual",
            status: "ok",
            stats: { merged: 1, resolved: 2, disputed: 0, corrected: 0, questions: 3 },
            started_at: "2026-09-29T10:00:00Z",
            finished_at: "2026-09-29T10:01:00Z",
          },
        ],
      }),
      "GET /api/memory/a": {
        ...HAMBURG,
        versions: [
          {
            version: 1,
            text: "Wohnt in Hamburg",
            status: "superseded",
            evidence: "doc",
            confidence: 0.9,
            reason: "the user corrected it",
            actor: "chat",
            source: { conversation_id: "c9", cue: "tatsächlich" },
            created_at: "2026-09-29T10:00:00Z",
          },
        ],
      },
    });
    const summary = await screen.findByTestId("run-summary");
    expect(summary.textContent).toContain("geklärt 2");
    expect(summary.textContent).toContain("Fragen 3");
    fireEvent.click(await screen.findByRole("button", { name: "Verlauf" }));
    const source = await screen.findByTestId("version-source");
    expect(source.textContent).toContain("Hinweis: „tatsächlich“");
    expect(within(source).getByRole("link").getAttribute("href")).toBe("#/chat/c9");
    expect(mock.calls.length).toBeGreaterThan(0);
  });
});
