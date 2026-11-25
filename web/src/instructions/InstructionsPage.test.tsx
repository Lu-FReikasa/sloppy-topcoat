import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { CoreOverview } from "../types";
import { BUTLER, core, coreItem, library, persona, proposal } from "./fixtures";
import { InstructionsPage } from "./InstructionsPage";

afterEach(() => {
  vi.restoreAllMocks();
});

function setup(
  overview: CoreOverview,
  extra: Record<string, unknown> = {},
  lib = library(),
  mode: "user" | "care" = "user",
) {
  const mock = mockFetch({
    "GET /api/core?context=16384": overview,
    "GET /api/personas": lib,
    "GET /api/personas/stats": [],
    ...extra,
  });
  render(<InstructionsPage api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} mode={mode} />);
  return mock;
}

describe("InstructionsPage", () => {
  it("shows the slices of the core with the budget and the preview", async () => {
    setup(core());
    expect(await screen.findByRole("heading", { name: "Anweisungen" })).toBeTruthy();
    expect(screen.getByText("Nenne immer zuerst das Ergebnis.")).toBeTruthy();
    expect(screen.getByText("Wohnt in Hamburg")).toBeTruthy();
    expect(screen.getByText("Zwei Etagen")).toBeTruthy();
    // the technical parts are not in everyday use: they are in the admin area
    expect(screen.queryByTestId("budget-total")).toBeNull();
    expect(screen.queryByText("Stick to evidence.")).toBeNull();
    expect(screen.getByTestId("core-preview").textContent).toContain("System rules (fixed)");
    expect(screen.getByRole("heading", { name: "Stimme (Persona)" })).toBeTruthy();
  });

  it("shows the latest chats as key terms, newest first, and nothing without chats", async () => {
    setup(
      core({
        recent: [
          {
            id: "c1",
            title: "Backups",
            day: "2026-11-03",
            statements: ["Backup-Job: läuft nachts um 3 Uhr", "Aufbewahrung: noch offen"],
          },
        ],
      }),
    );
    const chat = await screen.findByTestId("recent-chat");
    expect(within(chat).getByText("Backups")).toBeTruthy();
    expect(within(chat).getByText("Backup-Job: läuft nachts um 3 Uhr")).toBeTruthy();
    expect(within(chat).getByText("Aufbewahrung: noch offen")).toBeTruthy();
  });

  it("shows no section for the latest chats when there are none", async () => {
    setup(core());
    await screen.findByRole("heading", { name: "Anweisungen" });
    expect(screen.queryByTestId("recent-chat")).toBeNull();
  });

  it("shows the budget and the fixed rules in the care form", async () => {
    setup(core(), {}, library(), "care");
    expect(await screen.findByText("Stick to evidence.")).toBeTruthy();
    expect(screen.getByTestId("budget-total").textContent).toContain("420 von 1000");
    for (const part of ["system", "persona", "rules", "profile", "household", "recent"]) {
      expect(screen.getByTestId(`budget-${part}`)).toBeTruthy();
    }
    expect(screen.queryByTestId("core-preview")).toBeNull();
  });

  it("asks the hub for the budget of another context", async () => {
    const mock = setup(
      core(),
      {
        "GET /api/core?context=4096": core({ budget: { ...core().budget, total: 500 } }),
      },
      library(),
      "care",
    );
    await screen.findByTestId("budget-total");
    fireEvent.change(screen.getByLabelText("Kontext des Modells"), { target: { value: "4096" } });
    await waitFor(() =>
      expect(screen.getByTestId("budget-total").textContent).toContain("von 500"),
    );
    expect(screen.getByTestId("budget-total").textContent).toContain("kleiner Kern");
    expect(mock.calls.some((c) => c.url === "/api/core?context=4096")).toBe(true);
  });

  it("is read-only for a user without admin rights where the household and the rules are concerned", async () => {
    setup(core({ admin: false }));
    await screen.findByText("Zwei Etagen");
    const household = screen.getByRole("region", { name: "Haushalt" });
    expect(within(household).queryByRole("button", { name: "Bearbeiten" })).toBeNull();
    expect(within(household).queryByRole("button", { name: "Hinzufügen" })).toBeNull();
    // their own entries are editable
    const rules = screen.getByRole("region", { name: "Deine Regeln und Stile" });
    expect(within(rules).getByRole("button", { name: "Bearbeiten" })).toBeTruthy();
  });

  it("shows the fixed rules read-only to a user without admin rights", async () => {
    setup(core({ admin: false }), {}, library(), "care");
    const system = await screen.findByRole("region", { name: "Feste Regeln" });
    expect(within(system).queryByRole("button", { name: "Bearbeiten" })).toBeNull();
    expect(within(system).getByText(/nur ein Admin ändert sie/)).toBeTruthy();
  });

  it("lets an admin change the household", async () => {
    const mock = setup(core({ admin: true }), {
      "POST /api/core/household/items": coreItem({ id: "h2", text: "Neu", scope: "household" }),
      "PUT /api/core/system/en": { version: 2, text: "Always evidence." },
    });
    await screen.findByText("Zwei Etagen");
    const household = screen.getByRole("region", { name: "Haushalt" });
    fireEvent.change(within(household).getByLabelText("Neuer Eintrag in Haushalt"), {
      target: { value: "Der Keller ist trocken." },
    });
    fireEvent.click(within(household).getByRole("button", { name: "Hinzufügen" }));
    await waitFor(() =>
      expect(
        mock.calls.find((c) => c.method === "POST" && c.url.endsWith("/household/items"))?.body,
      ).toEqual({
        text: "Der Keller ist trocken.",
      }),
    );
  });

  it("lets an admin change the fixed rules", async () => {
    const mock = setup(
      core({ admin: true }),
      { "PUT /api/core/system/en": { version: 2, text: "Always evidence." } },
      library(),
      "care",
    );
    await screen.findByText("Stick to evidence.");
    const system = screen.getByRole("region", { name: "Feste Regeln" });
    fireEvent.click(within(system).getAllByRole("button", { name: "Bearbeiten" })[0]!);
    fireEvent.change(within(system).getByRole("textbox"), {
      target: { value: "Always evidence." },
    });
    fireEvent.click(within(system).getByRole("button", { name: "Speichern" }));
    await waitFor(() =>
      expect(
        mock.calls.find((c) => c.method === "PUT" && c.url === "/api/core/system/en")?.body,
      ).toEqual({
        text: "Always evidence.",
      }),
    );
  });

  it("adds a style with an example and edits and pins an entry", async () => {
    const mock = setup(core(), {
      "POST /api/core/items": coreItem({ id: "n", text: "x", kind: "style" }),
      "PATCH /api/core/items/r1": coreItem({ id: "r1", text: "Nenne zuerst das Ergebnis." }),
    });
    await screen.findByText("Nenne immer zuerst das Ergebnis.");
    const rules = screen.getByRole("region", { name: "Deine Regeln und Stile" });
    fireEvent.change(within(rules).getByLabelText("Art"), { target: { value: "style" } });
    fireEvent.change(within(rules).getByLabelText("Neuer Eintrag in Deine Regeln und Stile"), {
      target: { value: "Antworte knapp." },
    });
    fireEvent.change(within(rules).getByLabelText("Beispiel"), {
      target: { value: "Läuft. CPU hoch." },
    });
    fireEvent.click(within(rules).getByRole("button", { name: "Hinzufügen" }));
    await waitFor(() =>
      expect(
        mock.calls.find((c) => c.method === "POST" && c.url === "/api/core/items")?.body,
      ).toEqual({
        kind: "style",
        text: "Antworte knapp.",
        example: "Läuft. CPU hoch.",
      }),
    );
    const entry = within(rules).getAllByTestId("core-item")[0]!;
    fireEvent.click(within(entry).getByRole("button", { name: "Bearbeiten" }));
    fireEvent.change(within(entry).getByLabelText("Text"), {
      target: { value: "Nenne zuerst das Ergebnis." },
    });
    fireEvent.click(within(rules).getByRole("button", { name: "Speichern" }));
    await waitFor(() =>
      expect(
        mock.calls.find((c) => c.method === "PATCH" && c.url === "/api/core/items/r1")?.body,
      ).toEqual({
        text: "Nenne zuerst das Ergebnis.",
      }),
    );
    // pinning a fact into the core is the user's own click
    const profile = screen.getByRole("region", { name: "Profil" });
    fireEvent.click(within(profile).getByLabelText("Im Kern festhalten"));
    await waitFor(() =>
      expect(
        mock.calls
          .filter((c) => c.method === "PATCH" && c.url === "/api/core/items/p1")
          .map((c) => c.body),
      ).toEqual([{ core: true }]),
    );
  });

  it("shows the history of an entry with an undo to an earlier version", async () => {
    const versions = [
      {
        version: 2,
        text: "neu",
        reason: "changed",
        actor: "user",
        created_at: "2026-10-02T10:00:00Z",
      },
      {
        version: 1,
        text: "alt",
        reason: "first",
        actor: "user",
        created_at: "2026-10-01T10:00:00Z",
      },
    ];
    const mock = setup(core({ rules: [coreItem({ id: "r1", text: "neu", version: 2 })] }), {
      "GET /api/memory/r1": { ...coreItem({ id: "r1", text: "neu", version: 2 }), versions },
      "POST /api/memory/r1/revert": coreItem({ id: "r1", text: "alt" }),
    });
    await screen.findByText("neu");
    fireEvent.click(screen.getAllByRole("button", { name: "Verlauf" })[0]!);
    const history = await screen.findByTestId("item-history");
    expect(history.textContent).toContain("alt");
    fireEvent.click(within(history).getByRole("button", { name: "Zurück zu Version 1" }));
    await waitFor(() =>
      expect(mock.calls.find((c) => c.url === "/api/memory/r1/revert")?.body).toEqual({
        version: 1,
      }),
    );
    expect(within(history).queryByRole("button", { name: "Zurück zu Version 2" })).toBeNull();
  });

  it("removes an entry and offers to undo it", async () => {
    const mock = setup(core(), {
      "DELETE /api/core/items/r1": coreItem({ id: "r1", text: "x", status: "deleted" }),
      "POST /api/memory/r1/restore": coreItem({ id: "r1", text: "x" }),
    });
    await screen.findByText("Nenne immer zuerst das Ergebnis.");
    fireEvent.click(screen.getByRole("button", { name: "Entfernen" }));
    const removed = await screen.findByTestId("core-item-removed");
    fireEvent.click(within(removed).getByRole("button", { name: "Rückgängig" }));
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/memory/r1/restore")).toBe(true),
    );
  });

  it("works through the proposals: accept, edit and accept, reject", async () => {
    const mock = setup(
      core({
        proposals: [
          proposal({ id: "a" }),
          proposal({
            id: "b",
            kind: "style",
            text: "Kurz und trocken.",
            example: "Läuft.",
            evidence: { quote: "bitte trocken" },
          }),
          proposal({ id: "c", text: "Benutze Tabellen.", evidence: {} }),
        ],
      }),
      {
        "POST /api/core/proposals/a/accept": { proposal: proposal({ id: "a" }), item: null },
        "POST /api/core/proposals/b/accept": { proposal: proposal({ id: "b" }), item: null },
        "POST /api/core/proposals/c/reject": proposal({ id: "c", status: "rejected" }),
      },
    );
    const cards = await screen.findAllByTestId("proposal");
    expect(cards).toHaveLength(3);
    expect(screen.getByTestId("proposal-count").textContent).toBe("3");
    expect(cards[0]!.textContent).toContain("antworte bitte immer knapp");
    expect(within(cards[1]!).getByTestId("proposal-example").textContent).toContain("Läuft.");
    fireEvent.click(within(cards[0]!).getByRole("button", { name: "Annehmen" }));
    await waitFor(() =>
      expect(
        mock.calls.find((c) => c.url === "/api/core/proposals/a/accept")?.body,
      ).toBeUndefined(),
    );
    fireEvent.click(within(cards[1]!).getByRole("button", { name: "Erst bearbeiten" }));
    fireEvent.change(within(cards[1]!).getAllByLabelText("Text")[0]!, {
      target: { value: "Kurz, trocken." },
    });
    fireEvent.click(within(cards[1]!).getByRole("button", { name: "Annehmen" }));
    await waitFor(() =>
      expect(mock.calls.find((c) => c.url === "/api/core/proposals/b/accept")?.body).toEqual({
        text: "Kurz, trocken.",
        example: "Läuft.",
      }),
    );
    fireEvent.click(within(cards[2]!).getByRole("button", { name: "Ablehnen" }));
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url === "/api/core/proposals/c/reject")).toBe(true),
    );
  });

  it("offers Markdown to copy for a proposal about a note file and writes nothing itself", async () => {
    const write = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: write },
      configurable: true,
    });
    const mock = setup(
      core({
        proposals: [
          proposal({
            id: "f",
            action: "kb_edit",
            kind: "operational",
            text: "node-amd: llama-server is stopped",
            reason: "care: a live check shows something else (E1)",
            evidence: {
              markdown: "<!-- steckbriefe/node-amd.md -->\nnode-amd: llama-server is stopped\n",
              path: "steckbriefe/node-amd.md",
            },
          }),
        ],
      }),
      { "POST /api/core/proposals/f/accept": { proposal: proposal({ id: "f" }), item: null } },
    );
    const card = await screen.findByTestId("proposal");
    expect(card.textContent).toContain("Notiz-Datei evtl. veraltet");
    expect(within(card).getByTestId("proposal-markdown").textContent).toContain(
      "steckbriefe/node-amd.md",
    );
    fireEvent.click(within(card).getByRole("button", { name: "Markdown kopieren" }));
    await waitFor(() =>
      expect(write).toHaveBeenCalledWith(expect.stringContaining("llama-server is stopped")),
    );
    expect(within(card).queryByRole("button", { name: "Erst bearbeiten" })).toBeNull();
    expect(mock.calls.filter((c) => c.method === "PUT")).toEqual([]);
  });

  it("works with personas: choose the default, see the count, the cut-off note and the editor", async () => {
    const own = persona({
      id: "o1",
      name: "Eigener Text",
      key: "legacy",
      truncated: true,
      components: { free_text: "Reime." },
    });
    const mock = setup(
      core(),
      {
        "PUT /api/personas/default": { default_id: "t-butler" },
        "GET /api/personas/stats": [
          {
            persona_id: "t-butler",
            name: "Butler",
            key: "butler",
            answers: 5,
            helped: 3,
            unfit: 1,
          },
        ],
      },
      library({ own: [own], default_id: "o1" }),
    );
    await screen.findByText("Stimme (Persona)");
    const section = screen.getByRole("region", { name: "Stimme (Persona)" });
    expect(within(section).getAllByTestId("persona-card")).toHaveLength(3);
    expect(within(section).getByTestId("persona-default").closest("li")?.textContent).toContain(
      "Eigener Text",
    );
    expect(within(section).getByTestId("persona-truncated").textContent).toContain("400");
    await waitFor(() =>
      expect(within(section).getByTestId("persona-stat").textContent).toContain(
        "5 Antworten · hat geholfen 3 · unpassend 1",
      ),
    );
    fireEvent.change(within(section).getByLabelText("Persona für neue Chats"), {
      target: { value: "t-butler" },
    });
    await waitFor(() =>
      expect(
        mock.calls.find((c) => c.method === "PUT" && c.url === "/api/personas/default")?.body,
      ).toEqual({
        persona_id: "t-butler",
      }),
    );
    // a template is not editable for a user who is no admin; their own is
    const cards = within(section).getAllByTestId("persona-card");
    expect(within(cards[0]!).queryByRole("button", { name: "Bearbeiten" })).toBeNull();
    fireEvent.click(within(cards[2]!).getByRole("button", { name: "Bearbeiten" }));
    expect(screen.getByRole("form", { name: "Persona bearbeiten" })).toBeTruthy();
    expect(
      within(section).queryByRole("button", { name: "Neue Vorlage (alle Nutzer)" }),
    ).toBeNull();
  });

  it("builds a personal persona from blocks", async () => {
    const mock = setup(core(), { "POST /api/personas": persona({ id: "n", name: "Navigator" }) });
    await screen.findByText("Stimme (Persona)");
    fireEvent.click(screen.getByRole("button", { name: "Neue Persona aus Bausteinen" }));
    const form = screen.getByRole("form", { name: "Persona bearbeiten" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Navigator" } });
    fireEvent.change(within(form).getByLabelText("Rolle (Deutsch)"), {
      target: { value: "Du bist mein Navigator." },
    });
    fireEvent.change(within(form).getByLabelText("Humor"), { target: { value: "0" } });
    fireEvent.click(within(form).getByRole("button", { name: "Speichern" }));
    await waitFor(() => {
      const call = mock.calls.find((c) => c.method === "POST" && c.url === "/api/personas");
      expect(call?.body).toMatchObject({
        name: "Navigator",
        scope: "user",
        components: {
          role: { de: "Du bist mein Navigator." },
          humor: 0,
          address: "du",
          transparency: true,
        },
      });
    });
  });

  it("an admin can also write a shared template", async () => {
    const mock = setup(core({ admin: true }), { "POST /api/personas": BUTLER });
    await screen.findByText("Stimme (Persona)");
    fireEvent.click(screen.getByRole("button", { name: "Neue Vorlage (alle Nutzer)" }));
    const form = screen.getByRole("form", { name: "Persona bearbeiten" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Lotse" } });
    fireEvent.change(within(form).getByLabelText("Rolle (Englisch)"), {
      target: { value: "You are my pilot." },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Speichern" }));
    await waitFor(() =>
      expect(
        mock.calls.find((c) => c.method === "POST" && c.url === "/api/personas")?.body,
      ).toMatchObject({
        scope: "household",
      }),
    );
  });

  it("shows an error from the hub", async () => {
    setup(core(), {
      "POST /api/core/items": jsonResponse(
        { detail: { code: "secret_blocked", message: "looks like a secret" } },
        422,
      ),
    });
    await screen.findByText("Nenne immer zuerst das Ergebnis.");
    const rules = screen.getByRole("region", { name: "Deine Regeln und Stile" });
    fireEvent.change(within(rules).getByLabelText("Neuer Eintrag in Deine Regeln und Stile"), {
      target: { value: "Passwort" },
    });
    fireEvent.click(within(rules).getByRole("button", { name: "Hinzufügen" }));
    expect((await screen.findByRole("alert")).textContent).toBeTruthy();
  });
});
