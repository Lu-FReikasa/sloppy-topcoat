import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { BenchPanel } from "./BenchPanel";

const model = (name: string, available = true) => ({
  name,
  loaded: false,
  available,
  kinds: ["chat"],
  kinds_source: "detected" as const,
  size_mb: 4096,
});

const POOLS = {
  default_model: "big",
  nodes: [
    {
      node: "node-nvidia",
      connected: true,
      restricted: false,
      max_loaded: null,
      models: [model("big"), model("small"), model("outside", false)],
    },
  ],
};

const SUITES = [
  { key: "answer", title: "Short answers", function: "answer", version: 1, cases: 8 },
  { key: "json", title: "Structured output (JSON)", function: "extract", version: 1, cases: 6 },
];

let suggestion: unknown = null;

function setup(ratings: unknown[] = [], runs: unknown[] = []) {
  const mock = mockFetch({
    "GET /api/bench/suites": SUITES,
    "GET /api/bench/ratings": ratings,
    "GET /api/bench/runs": runs,
    "GET /api/scheduling/pools": POOLS,
    "GET /api/bench/suggestion": () => jsonResponse({ suggestion }),
    "GET /api/bench/variants": [
      { key: "", name: "default", extra: {} },
      { key: "nothink", name: "nothink", extra: { chat_template_kwargs: { enable_thinking: false } } },
    ],
    "POST /api/bench/runs": () => jsonResponse({ run_id: "r1", status: "running" }),
    "PUT /api/scheduling/model-profile": () =>
      jsonResponse({
        ...POOLS,
        nodes: [
          {
            ...POOLS.nodes[0],
            models: [{ ...model("big"), profile: { answer: "nothink" } }, model("small")],
          },
        ],
      }),
    "DELETE /api/bench/runs/r1": { stopped: true },
  });
  return { mock, api: new ApiClient({ token: "t", fetchImpl: mock.fetch }) };
}

const rating = (suite: string, score: number, current = true) => ({
  variant: "",
  model: "big",
  node: "node-nvidia",
  suite,
  function: "answer",
  version: 1,
  current,
  score,
  passed: Math.round(score * 8),
  total: 8,
  tps: 30,
  failed: score < 1 ? [{ case: "dns", why: ["must:domain"] }] : [],
  at: null,
  run_id: "r0",
});

describe("BenchPanel", () => {
  it("starts a run for the chosen model of the pool with the chosen suites", async () => {
    const { api, mock } = setup();
    render(<BenchPanel api={api} />);
    fireEvent.change(await screen.findByLabelText("Modell"), { target: { value: "small" } });
    fireEvent.click(screen.getByLabelText("Structured output (JSON)")); // switched off
    fireEvent.click(screen.getByRole("button", { name: "Bench starten" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "POST")).toBe(true));
    expect(mock.calls.find((c) => c.method === "POST")?.body).toEqual({
      model: "small",
      node: "node-nvidia",
      suites: ["answer"],
      variant: "",
    });
  });

  it("tries a setting: the chosen variant goes with the run, and a tried setting is named in the table", async () => {
    const { api, mock } = setup([{ ...rating("answer", 0.5), variant: "nothink" }]);
    render(<BenchPanel api={api} />);
    fireEvent.change(await screen.findByLabelText("Einstellung"), { target: { value: "nothink" } });
    fireEvent.click(screen.getByRole("button", { name: "Bench starten" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "POST")).toBe(true));
    expect((mock.calls.find((c) => c.method === "POST")?.body as { variant: string }).variant).toBe("nothink");
    expect((await screen.findByTestId("bench-ratings")).textContent).toContain("big@node-nvidia (nothink)");
  });

  it("offers only the models the pool allows", async () => {
    const { api } = setup();
    render(<BenchPanel api={api} />);
    const select = await screen.findByLabelText("Modell");
    const options = within(select).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["big", "small"]);
  });

  it("shows the ratings per model and suite, an old version in grey, and the failed cases", async () => {
    const { api } = setup([rating("answer", 0.75), rating("json", 1, false)]);
    render(<BenchPanel api={api} />);
    const table = await screen.findByTestId("bench-ratings");
    const answer = within(table).getByText("75 % (6/8)");
    expect(answer.getAttribute("title")).toContain("dns");
    const old = within(table).getByText("100 % (8/8)");
    expect(old.className).toContain("muted");
    expect(old.getAttribute("title")).toContain("ältere Fassung");
  });

  it("offers a better model and changes nothing by itself", async () => {
    suggestion = {
      model: "big",
      node: "node-nvidia",
      score: 0.9,
      default_model: "small",
      default_score: 0.6,
      suites: ["answer"],
    };
    const { api, mock } = setup();
    render(<BenchPanel api={api} />);
    const text = (await screen.findByTestId("bench-suggestion")).textContent ?? "";
    expect(text).toContain("big");
    expect(text).toContain("90 %");
    expect(text).toContain("60 %");
    expect(mock.calls.some((c) => c.method === "PUT")).toBe(false); // nothing changed yet
    expect(screen.queryByRole("button", { name: /Standard/ })).toBeNull(); // nothing to take: set it per application
    suggestion = null;
  });

  it("shows a running run with its progress and can stop it", async () => {
    const { api, mock } = setup([], [
      {
        run_id: "r1",
        model: "big",
        node: "node-nvidia",
        suites: ["answer"],
        status: "running",
        started_at: "2026-11-05T10:00:00+00:00",
        finished_at: null,
        progress: { suite: "answer", case: 3, of: 8 },
        results: [],
        error: null,
      },
    ]);
    render(<BenchPanel api={api} />);
    expect((await screen.findByTestId("bench-runs")).textContent).toContain("answer 3/8");
    fireEvent.click(screen.getByRole("button", { name: "Stoppen" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "DELETE")).toBe(true));
  });

  it("takes a tried setting for a kind of work, and gives it back", async () => {
    const { api, mock } = setup([
      { ...rating("answer", 0.8), variant: "" },
      { ...rating("answer", 0.95), variant: "nothink" },
    ]);
    render(<BenchPanel api={api} />);
    fireEvent.click(await screen.findByRole("button", { name: "big@node-nvidia (nothink): Nutzen für answer" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "PUT")).toBe(true));
    expect(mock.calls.find((c) => c.method === "PUT")?.body).toEqual({
      model: "big",
      function: "answer",
      variant: "nothink",
    });
    // now it is in use, and the plain row offers to give it back
    expect(await screen.findByText(/in Gebrauch für answer/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "big@node-nvidia: Schlichtes Modell für answer" }));
    await waitFor(() => expect(mock.calls.filter((c) => c.method === "PUT")).toHaveLength(2));
    expect(mock.calls.filter((c) => c.method === "PUT")[1]?.body).toEqual({
      model: "big",
      function: "answer",
      variant: null,
    });
  });
});
