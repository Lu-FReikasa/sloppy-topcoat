import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { mockFetch } from "../test/mockFetch";
import type { ChatReview } from "../types";
import { ReviewNote } from "./ReviewNote";

const review = (over: Partial<ChatReview> = {}): ChatReview => ({
  id: "r1",
  conversation_id: "c1",
  message_id: 7,
  verdict: "error",
  findings: [
    {
      claim: "Plex läuft auf node-nvidia",
      reason: "laut Notiz auf node-amd",
      evidence: { note_id: "n1", text: "Plex läuft auf node-amd" },
    },
  ],
  reviewer_model: "qwen",
  author_model: "gemma",
  author_node: "node-amd",
  seen: false,
  created_at: null,
  ...over,
});

describe("ReviewNote", () => {
  it("shows an error with its evidence and can be marked as seen", async () => {
    const mock = mockFetch({
      "POST /api/reviews/r1/seen": new Response(null, { status: 204 }),
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<ReviewNote review={review()} api={api} />);
    expect(screen.getByText("Nachprüfung: Fehler gefunden")).toBeTruthy();
    expect(
      screen.getByText(/Gedächtnis sagt: Plex läuft auf node-amd/),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Gesehen" }));
    await waitFor(() => expect(screen.queryByRole("button")).toBeNull());
    expect(mock.calls.some((c) => c.url === "/api/reviews/r1/seen")).toBe(true);
  });

  it("says when the check was made by the model that wrote the answer", () => {
    const same = review({ reviewer_model: "gemma", same_model: true });
    render(<ReviewNote review={same} />);
    expect(
      screen.getByText(
        /geprüft von gemma, dem Modell, das die Antwort geschrieben hat/,
      ),
    ).toBeTruthy();
  });

  it("names only the model when another one checked", () => {
    render(<ReviewNote review={review({ same_model: false })} />);
    expect(screen.getByText("geprüft von qwen")).toBeTruthy();
  });

  it("calls a finding without evidence only a hint", () => {
    const hint = review({
      verdict: "hint",
      findings: [{ claim: "Port 32400", reason: "unsicher", evidence: null }],
    });
    render(<ReviewNote review={hint} />);
    expect(
      screen.getByText("Nachprüfung: etwas sieht falsch aus"),
    ).toBeTruthy();
    expect(screen.queryByText(/Gedächtnis sagt/)).toBeNull();
  });
});
