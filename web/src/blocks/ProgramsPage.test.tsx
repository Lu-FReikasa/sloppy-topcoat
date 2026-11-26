import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { ProgramsPage, childStacks, summary } from "./ProgramsPage";

const PROGRAM = {
  name: "research_report",
  title: "Research report",
  inputs: { question: "text" },
  stack: [
    { b: "agent", prompt: "{{question}}", out: "draft" },
    { b: "check", in: "draft", pick: { other_than: ["draft"], exclusive: true }, out: "v" },
    {
      b: "repeat_until",
      cond: "v.ok",
      max: 3,
      stack: [{ b: "revise", draft: "draft", issues: "v", out: "draft" }],
    },
  ],
};

const CATALOGUE = {
  types: ["text", "texts", "verdict", "compare", "bool"],
  blocks: {},
  compare_modes: ["pick", "merge", "flag"],
  ranks: ["best", "fastest", "largest", "loaded", "rated"],
  suites: ["answer"],
  hooks: ["claims", "repeat_guard", "second_reader", "stall_guard", "step_check"],
  hook_booleans: ["step_check", "claims", "second_reader"],
  hook_numbers: { repeat_guard: [1, 20], stall_guard: [0, 50] },
  applications: ["research", "dev"],
  depths: ["quick", "default", "thorough"],
};


function setup() {
  const mock = mockFetch({
    "GET /api/blocks/programs": [
      { name: "research_report", title: "Research report", inputs: PROGRAM.inputs, program: PROGRAM, builtin: true },
    ],
    "GET /api/blocks/catalogue": CATALOGUE,
    "POST /api/blocks/validate": { ok: true },
    "PUT /api/blocks/programs/my_copy": { saved: "my_copy" },
    "POST /api/blocks/runs": () =>
      jsonResponse({ id: "r1", program: "x", status: "ok", error: "", reason: "", notes: [], filed: [], variables: { draft: "text" }, authors: { draft: "big" }, steps: [{ event: "step", seq: 0, path: "stack[0]", b: "agent", state: "done", preview: "text" }] }),
    "GET /api/blocks/runs/r1": () => jsonResponse({}),
  });
  return { mock, api: new ApiClient({ token: "t", fetchImpl: mock.fetch }) };
}

describe("ProgramsPage helpers", () => {
  it("shows what a C-block holds and what a block does", () => {
    const loop = PROGRAM.stack[2] as never;
    expect(childStacks(loop).map((c) => c.label)).toEqual(["do"]);
    expect(summary(PROGRAM.stack[1] as never)).toContain("pick other_than=draft exclusive=true");
    expect(summary(PROGRAM.stack[2] as never)).toContain("max 3");
    expect(
      summary({ b: "agent", prompt: "q", out: "d", hooks: { step_check: true, claims: false, repeat_guard: 2 } }),
    ).toContain("hooks +step_check -claims repeat_guard=2");
  });
});

describe("ProgramsPage", () => {
  it("draws the program as blocks and offers a copy to save", async () => {
    const { api, mock } = setup();
    render(<ProgramsPage api={api} />);
    expect(await screen.findAllByTestId("block-agent")).toHaveLength(1);
    expect(screen.getAllByTestId("block-revise")).toHaveLength(1); // inside the loop
    // the shipped program's title in the GUI's language, by its name (issue #11)
    expect(screen.getByRole("button", { name: "Recherchebericht mit Überarbeitung" })).toBeTruthy();
    const saveAs = screen.getByLabelText("Speichern als") as HTMLInputElement;
    expect(saveAs.value).toBe("research_report_copy"); // a program that ships is not overwritten
    fireEvent.change(saveAs, { target: { value: "my_copy" } });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "PUT")).toBe(true));
    expect((mock.calls.find((c) => c.method === "PUT")?.body as { program: { name: string } }).program.name).toBe("my_copy");
  });

  it("says when the source is not valid JSON", async () => {
    const { api } = setup();
    render(<ProgramsPage api={api} />);
    const source = (await screen.findByLabelText("Quelltext")) as HTMLTextAreaElement;
    fireEvent.change(source, { target: { value: "{ not json" } });
    fireEvent.click(screen.getByRole("button", { name: "Prüfen" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
  });

  it("starts a run with the inputs and shows its steps and variables", async () => {
    const { api, mock } = setup();
    render(<ProgramsPage api={api} />);
    fireEvent.change(await screen.findByLabelText("question"), { target: { value: "why?" } });
    fireEvent.click(screen.getByRole("button", { name: "Starten" }));
    expect(await screen.findByTestId("block-run")).toBeTruthy();
    expect(mock.calls.find((c) => c.url.endsWith("/api/blocks/runs") && c.method === "POST")?.body).toMatchObject({
      inputs: { question: "why?" },
    });
    expect(screen.getByText(/stack\[0\] agent/)).toBeTruthy();
    expect(screen.getByText(/draft \(big\)/)).toBeTruthy();
  });

  // --- the editor ---------------------------------------------------------------------------

  const source = async () =>
    JSON.parse(((await screen.findByLabelText("Quelltext")) as HTMLTextAreaElement).value) as {
      stack: { b: string; [key: string]: unknown }[];
    };

  it("adds a block after the selected one, with fields that already pass", async () => {
    const { api } = setup();
    render(<ProgramsPage api={api} />);
    fireEvent.click((await screen.findAllByTestId("block-agent"))[0]!);
    fireEvent.click(screen.getByRole("button", { name: "Block hinzufügen: ask" }));
    const stack = (await source()).stack;
    expect(stack.map((b) => b.b)).toEqual(["agent", "ask", "check", "repeat_until"]);
    expect(stack[1]).toMatchObject({ prompt: "{{draft}}", out: "answer" });
  });

  it("moves a block with the keyboard and refuses a move that reads too early", async () => {
    const { api } = setup();
    render(<ProgramsPage api={api} />);
    const check = (await screen.findByTestId("block-check")) as HTMLElement;
    check.focus();
    // the check up, before the agent that writes `draft`: refused, and says why
    fireEvent.keyDown(check, { key: "ArrowUp", altKey: true });
    expect(await screen.findByText(/Nicht ausgeführt: .*draft is not defined before this block/)).toBeTruthy();
    expect((await source()).stack.map((b) => b.b)).toEqual(["agent", "check", "repeat_until"]);
    // the agent down is refused as well; a duplicate is fine and Undo takes it back
    fireEvent.keyDown(check, { key: "d", ctrlKey: true });
    expect((await source()).stack.map((b) => b.b)).toEqual(["agent", "check", "check", "repeat_until"]);
    fireEvent.click(screen.getByRole("button", { name: "Rückgängig" }));
    expect((await source()).stack.map((b) => b.b)).toEqual(["agent", "check", "repeat_until"]);
  });

  it("removes a block, refuses to remove one that a later block reads, and puts one into the loop", async () => {
    const { api } = setup();
    render(<ProgramsPage api={api} />);
    // the loop reads the verdict of the check: the check cannot go
    fireEvent.click(await screen.findByTestId("block-check"));
    fireEvent.click(screen.getByRole("button", { name: "Entfernen" }));
    expect(await screen.findByText(/Nicht ausgeführt: .*v is not a bool, verdict or compare/)).toBeTruthy();
    expect((await source()).stack.map((b) => b.b)).toEqual(["agent", "check", "repeat_until"]);
    // the loop itself can go, and Undo brings it back
    fireEvent.click(screen.getAllByTestId("block-repeat_until")[0]!);
    fireEvent.click(screen.getByRole("button", { name: "Entfernen" }));
    expect((await source()).stack.map((b) => b.b)).toEqual(["agent", "check"]);
    fireEvent.click(screen.getByRole("button", { name: "Rückgängig" }));
    expect((await source()).stack.map((b) => b.b)).toEqual(["agent", "check", "repeat_until"]);
    // the agent holds no blocks: the check cannot be put into it, and the page says so
    fireEvent.click(screen.getByTestId("block-check"));
    fireEvent.click(screen.getByRole("button", { name: "In den Block davor" }));
    expect(await screen.findByText(/holds no blocks/)).toBeTruthy();
  });

  it("changes a field of the selected block, and offers only variables of the right type", async () => {
    const { api } = setup();
    render(<ProgramsPage api={api} />);
    fireEvent.click(await screen.findByTestId("block-check"));
    const reads = within(screen.getByTestId("block-fields")).getByLabelText("Liest") as HTMLSelectElement;
    expect([...reads.options].map((o) => o.value)).toEqual(["question", "draft"]); // texts, no verdict
    fireEvent.change(reads, { target: { value: "question" } });
    expect((await source()).stack[1]?.in).toBe("question");
  });

  it("sets the hooks of an agent block as the frame says, on or off", async () => {
    const { api } = setup();
    render(<ProgramsPage api={api} />);
    fireEvent.click(await screen.findByTestId("block-agent"));
    const claims = screen.getByLabelText("Regeln für die Behauptungen der Antwort") as HTMLSelectElement;
    fireEvent.change(claims, { target: { value: "false" } });
    fireEvent.change(screen.getByLabelText("So oft derselbe Aufruf beendet den Lauf"), { target: { value: "4" } });
    expect((await source()).stack[0]?.hooks).toEqual({ claims: false, repeat_guard: 4 });
    fireEvent.change(claims, { target: { value: "" } });
    expect((await source()).stack[0]?.hooks).toEqual({ repeat_guard: 4 });
  });

  it("drops a block from the palette between two blocks, and moves one by dragging", async () => {
    const { api } = setup();
    render(<ProgramsPage api={api} />);
    await screen.findAllByTestId("block-agent");
    const slots = screen.getAllByTestId("block-slot");
    const data: Record<string, string> = {};
    const transfer = {
      setData: (k: string, v: string) => (data[k] = v),
      getData: (k: string) => data[k] ?? "",
    };
    const palette = screen.getByRole("button", { name: "Block hinzufügen: set" });
    fireEvent.dragStart(palette, { dataTransfer: transfer });
    // slot 1 is after the first top-level block: between the agent and the check
    fireEvent.drop(slots[1]!, { dataTransfer: transfer });
    expect((await source()).stack.map((b) => b.b)).toEqual(["agent", "set", "check", "repeat_until"]);
    // drag the new set above the agent: it reads `draft`, which the agent writes: refused
    const set = screen.getByTestId("block-set");
    fireEvent.dragStart(set, { dataTransfer: transfer });
    fireEvent.drop(screen.getAllByTestId("block-slot")[0]!, { dataTransfer: transfer });
    expect(await screen.findByText(/Nicht ausgeführt: .*draft}} is not defined before this block/)).toBeTruthy();
    expect((await source()).stack.map((b) => b.b)).toEqual(["agent", "set", "check", "repeat_until"]);
    // below the check it is fine (slot 3 in document order: after the check)
    fireEvent.dragStart(set, { dataTransfer: transfer });
    fireEvent.drop(screen.getAllByTestId("block-slot")[3]!, { dataTransfer: transfer });
    expect((await source()).stack.map((b) => b.b)).toEqual(["agent", "check", "set", "repeat_until"]);
  });
});
