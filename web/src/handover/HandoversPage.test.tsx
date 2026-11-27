import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { Handover } from "../types";
import { HandoversPage } from "./HandoversPage";

function handover(over: Partial<Handover> = {}): Handover {
  return {
    id: "h1",
    from_application: "chat",
    to_application: "admin",
    conversation_id: "c1",
    target_conversation_id: null,
    task_id: null,
    dossier: {
      question: "Warum startet der Web-Container neu?",
      context: [{ role: "user", text: "er startet dauernd neu" }],
      asked: "Sieh in die Logs.",
      expected: "Kurze Zusammenfassung",
      withheld: ["die Adresse der Maschine"],
    },
    hash: "a".repeat(64),
    status: "proposed",
    result_summary: null,
    error: null,
    created_at: null,
    sent_at: null,
    finished_at: null,
    ...over,
  };
}

function hub(list: Handover[] = []) {
  let rows = list;
  const mock = mockFetch({
    "GET /api/handovers": () => jsonResponse({ handovers: rows }),
    "GET /api/conversations": () =>
      jsonResponse([{ id: "c1", application: "chat", title: "t" }]),
    "POST /api/handovers": () => jsonResponse(handover(), 201),
    "POST /api/handovers/h1/send": () => {
      rows = [handover({ status: "sent" })];
      return jsonResponse(rows[0]);
    },
    "POST /api/handovers/h1/cancel": () => {
      rows = [];
      return jsonResponse(handover({ status: "cancelled" }));
    },
  });
  return { mock, api: new ApiClient({ token: "t", fetchImpl: mock.fetch }) };
}

describe("HandoversPage", () => {
  it("shows the dossier before anything is sent, then sends exactly its hash", async () => {
    const { api, mock } = hub();
    render(<HandoversPage api={api} from="chat" />);
    fireEvent.click(await screen.findByRole("button", { name: "Admin" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Anfrage vorbereiten" }),
    );
    const dossier = await screen.findByTestId("dossier");
    expect(dossier.textContent).toContain(
      "Warum startet der Web-Container neu?",
    );
    expect(dossier.textContent).toContain("die Adresse der Maschine");
    expect(mock.calls.some((c) => c.url.endsWith("/send"))).toBe(false);
    expect(
      mock.calls.find((c) => c.method === "POST" && c.url === "/api/handovers")
        ?.body,
    ).toEqual({ to_application: "admin", conversation_id: "c1" });

    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    await waitFor(() =>
      expect(mock.calls.find((c) => c.url.endsWith("/send"))?.body).toEqual({
        hash: "a".repeat(64),
      }),
    );
    expect(await screen.findByText(/in Arbeit/)).toBeTruthy();
  });

  it("hands over from an incognito tab by the tab, never by a conversation", async () => {
    const { api, mock } = hub();
    render(<HandoversPage api={api} incognito="t9" />);
    fireEvent.click(await screen.findByRole("button", { name: "Dev" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Anfrage vorbereiten" }),
    );
    await screen.findByTestId("dossier");
    expect(
      mock.calls.find((c) => c.method === "POST" && c.url === "/api/handovers")
        ?.body,
    ).toEqual({ to_application: "dev", incognito_tab: "t9" });
    expect(mock.calls.some((c) => c.url.startsWith("/api/conversations"))).toBe(
      false,
    );
  });

  it("lists what was handed over and its answer, and offers nothing to start without a source", async () => {
    const { api } = hub([
      handover({ status: "done", result_summary: "Der Speicher war voll." }),
    ]);
    render(<HandoversPage api={api} />);
    expect(await screen.findByText(/Der Speicher war voll/)).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Anfrage vorbereiten" }),
    ).toBeNull();
  });

  it("opens the Akte of a hand-over: the request, the report with its sources, formatted", async () => {
    const mock = mockFetch({
      "GET /api/handovers": {
        handovers: [
          handover({
            status: "done",
            result_summary: "Der Speicher war voll.",
            has_file: true,
          }),
        ],
      },
      "GET /api/handovers/h1/file": {
        id: "h1",
        markdown: [
          "# Akte: Warum startet der Web-Container neu?",
          "",
          "## Bericht",
          "",
          "1. **gerade gesehen** — OOM [E1]",
          "   > „OOMKilled: true“ — E1",
          "",
          "## Quellen",
          "",
          "- **E1** · `docker.inspect`",
          "",
        ].join("\n"),
        knowledge_path: "akte-20261028-warum-11111111.md",
      },
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<HandoversPage api={api} />);
    fireEvent.click(await screen.findByRole("button", { name: "Akte öffnen" }));
    const file = await screen.findByTestId("handover-file");
    expect(file.querySelector("h1")?.textContent).toBe(
      "Akte: Warum startet der Web-Container neu?",
    );
    expect(file.querySelector("blockquote")?.textContent).toContain(
      "OOMKilled: true",
    );
    expect(file.textContent).toContain("akte-20261028-warum-11111111.md");
    expect(
      screen.getByRole("button", { name: "Herunterladen (.md)" }),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Akte schließen" }));
    expect(screen.queryByTestId("handover-file")).toBeNull();
  });

  it("can discard a dossier that was not sent", async () => {
    const { api, mock } = hub();
    render(<HandoversPage api={api} from="chat" />);
    fireEvent.click(await screen.findByRole("button", { name: "Admin" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Anfrage vorbereiten" }),
    );
    await screen.findByTestId("dossier");
    fireEvent.click(screen.getByRole("button", { name: "Verwerfen" }));
    await waitFor(() =>
      expect(mock.calls.some((c) => c.url.endsWith("/cancel"))).toBe(true),
    );
    await waitFor(() => expect(screen.queryByTestId("dossier")).toBeNull());
  });

  it("opens a dossier the assistant proposed, ready to send", async () => {
    const { api, mock } = hub([handover({ id: "h1", status: "proposed" })]);
    render(<HandoversPage api={api} id="h1" />);
    const dossier = await screen.findByTestId("dossier");
    expect(dossier.textContent).toContain(
      "Warum startet der Web-Container neu?",
    );
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    await waitFor(() =>
      expect(mock.calls.find((c) => c.url.endsWith("/send"))?.body).toEqual({
        hash: "a".repeat(64),
      }),
    );
  });
it("sends a dossier with a secret only after the explicit approval of that click", async () => {
    const { api, mock } = hub([handover({ id: "h1", status: "proposed", class: "critical" })]);
    render(<HandoversPage api={api} id="h1" />);
    const dossier = await screen.findByTestId("dossier");
    expect(dossier.textContent).toContain("kritisch/geheim");
    const send = screen.getByRole("button", { name: "Senden" }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    fireEvent.click(screen.getByTestId("approve-critical"));
    expect(send.disabled).toBe(false);
    fireEvent.click(send);
    await waitFor(() =>
      expect(mock.calls.find((c) => c.url.endsWith("/send"))?.body).toEqual({
        hash: "a".repeat(64),
        approve_critical: true,
      }),
    );
  });

  it("groups the list: waiting and running open, done and withdrawn folded, failed under done", async () => {
    const { api } = hub([
      handover({ id: "w", status: "proposed", dossier: { ...handover().dossier, question: "Frage W" } }),
      handover({ id: "r", status: "sent", dossier: { ...handover().dossier, question: "Frage R" } }),
      handover({ id: "d", status: "done", dossier: { ...handover().dossier, question: "Frage D" } }),
      handover({ id: "f", status: "failed", error: "kein Knoten", dossier: { ...handover().dossier, question: "Frage F" } }),
      handover({ id: "c", status: "cancelled", dossier: { ...handover().dossier, question: "Frage C" } }),
    ]);
    render(<HandoversPage api={api} />);
    const waiting = await screen.findByTestId("handover-group-waiting");
    expect(waiting.tagName).toBe("DIV");
    expect(waiting.textContent).toContain("Wartet auf dich");
    expect(waiting.textContent).toContain("Frage W");
    expect(screen.getByTestId("handover-group-running").textContent).toContain("Frage R");
    const done = screen.getByTestId("handover-group-done") as HTMLDetailsElement;
    expect(done.tagName).toBe("DETAILS");
    expect(done.open).toBe(false);
    expect(done.textContent).toContain("Frage D");
    expect(done.textContent).toContain("Frage F");
    expect(done.textContent).toContain("fehlgeschlagen: kein Knoten");
    const withdrawn = screen.getByTestId("handover-group-withdrawn") as HTMLDetailsElement;
    expect(withdrawn.open).toBe(false);
    expect(withdrawn.textContent).toContain("Frage C");
    // the full request is folded in every row; the file can still be opened
    const rows = screen.getAllByTestId("handover");
    for (const r of rows) {
      const full = r.querySelector("details.handover-full") as HTMLDetailsElement;
      expect(full.open).toBe(false);
      expect(full.textContent).toContain("Sieh in die Logs.");
    }
    expect(screen.getAllByRole("button", { name: "Akte öffnen" })).toHaveLength(5);
  });

  it("shows no group heading for an empty group", async () => {
    const { api } = hub([handover({ status: "done" })]);
    render(<HandoversPage api={api} />);
    await screen.findByTestId("handover-group-done");
    expect(screen.queryByTestId("handover-group-waiting")).toBeNull();
    expect(screen.queryByTestId("handover-group-withdrawn")).toBeNull();
  });
});
