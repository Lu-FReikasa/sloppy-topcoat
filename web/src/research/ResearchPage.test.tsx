import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient, type ResearchRun } from "../api/client";
import { mockFetch } from "../test/mockFetch";
import { ResearchPage } from "./ResearchPage";

describe("ResearchPage", () => {
  it("starts a research from a question and shows how far it is and where its report is", async () => {
    let runs: ResearchRun[] = [
      { id: "r0", question: "Alt", status: "done", progress: null, error: null, report_id: "rep1" },
    ];
    const mock = mockFetch({
      "GET /api/research/runs": () => new Response(JSON.stringify({ runs }), { headers: { "Content-Type": "application/json" } }),
      "POST /api/research/runs": () => {
        const run = { id: "r1", question: "Pixel-Animationen?", status: "running", progress: "Recherche läuft: 0 von 3 Blöcken fertig. Jetzt: agent", error: null, report_id: null };
        runs = [run, ...runs];
        return new Response(JSON.stringify(run), { status: 202, headers: { "Content-Type": "application/json" } });
      },
    });
    render(<ResearchPage api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} />);
    expect(await screen.findByRole("button", { name: "Bericht öffnen" })).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Pixel-Animationen?" } });
    fireEvent.click(screen.getByRole("button", { name: "Recherche starten" }));
    expect(await screen.findByText(/0 von 3 Blöcken/)).toBeTruthy();
    await waitFor(() => expect(screen.getAllByTestId("research-run")).toHaveLength(2));
    expect(mock.calls.find((c) => c.method === "POST")?.body).toEqual({ question: "Pixel-Animationen?" });
  });
});
