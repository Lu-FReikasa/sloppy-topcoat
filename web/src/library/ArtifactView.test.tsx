import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { Artifact } from "../types";
import { ArtifactView } from "./ArtifactView";

afterEach(() => {
  window.sessionStorage.clear();
  window.location.hash = "";
});

const art = (over: Partial<Artifact> = {}): Artifact => ({
  id: "a1",
  kind: "report",
  title: "Warum startet web neu?",
  task_id: "t1",
  conversation_id: null,
  unread: true,
  created_at: "2026-10-04T08:00:00Z",
  updated_at: "2026-10-04T08:00:30Z",
  ...over,
});

describe("ArtifactView", () => {
  it("opens a report, marks it read and can ask about it in a new chat", async () => {
    const mock = mockFetch({
      "GET /api/artifacts": jsonResponse({ artifacts: [art()], unread: 1 }),
      "GET /api/artifacts/a1": jsonResponse({
        ...art(),
        body: "**web** hat zu wenig Speicher.",
      }),
      "POST /api/artifacts/a1/read": new Response(null, { status: 204 }),
      "POST /api/conversations": jsonResponse(
        { id: "c9", title: "", messages: [] },
        201,
      ),
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<ArtifactView api={api} id="a1" />);
    expect(await screen.findByTestId("library-open")).toBeTruthy();
    expect(
      screen.getAllByText("Warum startet web neu?").length,
    ).toBeGreaterThan(0);
    await waitFor(() =>
      expect(
        mock.calls.some((c) => c.url.endsWith("/api/artifacts/a1/read")),
      ).toBe(true),
    );
    fireEvent.click(screen.getByRole("button", { name: "Dazu fragen" }));
    await waitFor(() => expect(window.location.hash).toBe("#/chat/c9"));
    expect(window.sessionStorage.getItem("topcoat.pendingPrompt")).toContain(
      "zu wenig Speicher",
    );
  });
});
