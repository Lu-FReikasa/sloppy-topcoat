import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { usePreviewTheme } from "./preview";
import { LAYOUT_KINDS, type ThemeManifest } from "./theme";
import { ThemeWorkshop } from "./ThemeWorkshop";

const START: ThemeManifest = {
  key: "wire",
  name: "Wire",
  base: "terminal",
  layout: { kind: "cascade" },
  bar: { position: "bottom", items: ["programs"] },
  chrome: { buttons: ["close"], titlebar: "top" },
  motion: { style: "fade", ms: 180 },
  css: "--bg: #f0f0f0;",
};

function Shown() {
  const draft = usePreviewTheme();
  return (
    <output data-testid="shown" data-layout={draft?.layout.kind ?? ""}>
      {draft?.css ?? "none"}
    </output>
  );
}

function setup(extra: Partial<Record<string, unknown>> = {}) {
  const api = {
    workshopStart: vi.fn().mockResolvedValue({ theme: START, lint: [], from: "1.8.2.4" }),
    workshopTurn: vi.fn().mockResolvedValue({
      theme: { ...START, css: "--bg: #000000;" },
      applied: true,
      reply: "Jetzt schwarz.",
      changed: ["changed --bg"],
      notes: [],
      problems: [],
      lint: [],
      model: "m",
      calls: 1,
      ms: 900,
    }),
    workshopSave: vi.fn().mockResolvedValue({ key: "wire", stored: true, result: "r5", number: "1.8.2.5", iteration: 5 }),
    projectIterations: vi.fn().mockResolvedValue({
      task: "t1",
      title: "Theme",
      number: "1.8.2",
      revision: 3,
      requirements: [{ id: "a", text: "dunkle Grundfarbe" }],
      iterating: false,
      iterations: [],
    }),
    putProjectRequirements: vi.fn(),
    ...extra,
  };
  render(
    <>
      <ThemeWorkshop api={api as unknown as ApiClient} task="t1" base="r4" />
      <Shown />
    </>,
  );
  return api;
}

beforeEach(() => window.localStorage.clear());
afterEach(() => window.localStorage.clear());

describe("the theme workshop", () => {
  it("draws the draft live, sends a turn with the requirements and shows the change", async () => {
    const api = setup();
    await waitFor(() => expect(screen.getByTestId("shown").textContent).toBe("--bg: #f0f0f0;"));
    expect(await screen.findByText("dunkle Grundfarbe")).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "Deine Nachricht" }), { target: { value: "dunkler" } });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    await waitFor(() => expect(screen.getByTestId("shown").textContent).toBe("--bg: #000000;"));
    expect(api.workshopTurn).toHaveBeenCalledWith({
      theme: START,
      message: "dunkler",
      history: [],
      requirements: ["dunkle Grundfarbe"],
    });
    expect(screen.getByText(/Jetzt schwarz\./)).toBeTruthy();
    // back: the theme before the turn is drawn again
    fireEvent.click(screen.getByRole("button", { name: "Zurück" }));
    await waitFor(() => expect(screen.getByTestId("shown").textContent).toBe("--bg: #f0f0f0;"));
  });

  it("a colour control changes the draft without a model", async () => {
    const api = setup();
    const bg = await screen.findByLabelText("--bg");
    fireEvent.change(bg, { target: { value: "#102030" } });
    await waitFor(() => expect(screen.getByTestId("shown").textContent).toBe("--bg: #102030;"));
    expect(api.workshopTurn).not.toHaveBeenCalled();
  });

  it("offers every layout the desktop knows, nest too, and draws the one picked", async () => {
    setup();
    const layout = (await screen.findByLabelText("Anordnung")) as HTMLSelectElement;
    expect([...layout.options].map((o) => o.value).sort()).toEqual([...LAYOUT_KINDS].sort());
    fireEvent.change(layout, { target: { value: "nest" } });
    await waitFor(() => expect(screen.getByTestId("shown").dataset.layout).toBe("nest"));
  });

  it("saves as an iteration of the task, with the steps, on the version it began from", async () => {
    const api = setup();
    fireEvent.change(await screen.findByRole("textbox", { name: "Deine Nachricht" }), { target: { value: "dunkler" } });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    await screen.findByText(/Jetzt schwarz\./);
    fireEvent.click(screen.getByRole("button", { name: "Als Iteration speichern" }));
    await waitFor(() =>
      expect(api.workshopSave).toHaveBeenCalledWith({
        theme: { ...START, css: "--bg: #000000;" },
        task_id: "t1",
        based_on: "r4",
        steps: [{ message: "dunkler", reply: "Jetzt schwarz." }],
        store: true,
      }),
    );
    expect(await screen.findByText(/als Iteration 5 \(1.8.2.5\)/)).toBeTruthy();
  });

  it("does not take a turn whose patch would break the theme", async () => {
    setup({
      workshopTurn: vi.fn().mockResolvedValue({
        theme: START,
        applied: false,
        reply: "Spirale.",
        changed: [],
        notes: [],
        problems: ["layout.kind: 'spiral' is not one of …"],
        lint: [],
        model: "m",
        calls: 2,
        ms: 1,
      }),
    });
    fireEvent.change(await screen.findByRole("textbox", { name: "Deine Nachricht" }), { target: { value: "Spirale" } });
    fireEvent.click(screen.getByRole("button", { name: "Senden" }));
    expect(await screen.findByText(/Nicht übernommen/)).toBeTruthy();
    expect(screen.getByTestId("shown").textContent).toBe("--bg: #f0f0f0;");
  });
});

describe("the workshop's contrast button", () => {
  it("shows with the contrast finding and takes the hub's fixed theme", async () => {
    const fixed = { ...START, css: "--bg: #000;\n.dt-titlebar { background: var(--surface); }" };
    const api = setup({
      workshopStart: vi.fn().mockResolvedValue({
        theme: { ...START, css: "--bg: #000;\n.dt-titlebar { background: #ddd; }" },
        lint: ["css: the theme is dark (--bg #000), but these rules keep a light background of their own: .dt-titlebar (#ddd)"],
        from: null,
      }),
      workshopFixContrast: vi.fn().mockResolvedValue({ theme: fixed, changed: ["changed .dt-titlebar"], lint: [] }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Helle Flächen angleichen" }));
    await waitFor(() => expect(screen.getByTestId("shown").textContent).toBe(fixed.css));
    expect(api.workshopTurn).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Helle Flächen angleichen" })).toBeNull();
  });
});
