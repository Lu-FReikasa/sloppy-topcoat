import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { INTENT_EVENT, takeIntent } from "../layout/intent";
import { IterationPanel } from "./IterationPanel";
import type { ProjectIterations, ProjectNode } from "./types";

const task = {
  id: "t1",
  kind: "task",
  number: "1.8.2",
  status: "in_review",
  revision: 7,
  requirements: [{ id: "r1", text: "dunkle Grundfarbe", from: 2 }],
} as unknown as ProjectNode;
const v3 = { id: "v3", kind: "report", number: "1.8.2.3", parent_id: "t1", revision: 1 } as unknown as ProjectNode;

const DATA: ProjectIterations = {
  task: "t1",
  requirements: [{ id: "r1", text: "dunkle Grundfarbe", from: 2 }],
  iterating: false,
  iterations: [
    { id: "v1", number: "1.8.2.1", iteration: 1, based_on: null, feedback: null, theme: true, created_at: null },
    { id: "v3", number: "1.8.2.3", iteration: 3, based_on: "1.8.2.2", feedback: "kein Wackeln", theme: true, created_at: null },
  ],
};

function setup(node: ProjectNode) {
  const api = {
    projectIterations: vi.fn().mockResolvedValue(DATA),
    iterateProjectNode: vi.fn().mockResolvedValue({ task: "t1", run: "x" }),
    putProjectRequirements: vi.fn().mockResolvedValue(task),
  };
  const changed = vi.fn();
  render(
    <IterationPanel api={api as unknown as ApiClient} node={node} task={task} onChanged={changed} onError={vi.fn()} />,
  );
  return { api, changed };
}

describe("iterations of a task", () => {
  it("lists the versions with what they continued and keeps the requirements", async () => {
    setup(task);
    expect(await screen.findByText("v3")).toBeTruthy();
    expect(screen.getByText(/← 1.8.2.2/)).toBeTruthy();
    expect(screen.getByText(/dunkle Grundfarbe/)).toBeTruthy();
  });

  it("starts the next iteration from a result with its feedback, kept as a requirement", async () => {
    const { api, changed } = setup(v3);
    expect(await screen.findByText("Iteration 3")).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: /Rückmeldung für die nächste Iteration/ }), {
      target: { value: "  Hintergrund schwarz  " },
    });
    fireEvent.click(screen.getByRole("button", { name: /Nächste Iteration aus dieser Fassung/ }));
    await waitFor(() =>
      expect(api.iterateProjectNode).toHaveBeenCalledWith("t1", {
        based_on: "v3",
        feedback: "Hintergrund schwarz",
        keep: true,
      }),
    );
    expect(changed).toHaveBeenCalled();
  });

  it("strikes out a requirement with the task's revision", async () => {
    const { api } = setup(task);
    fireEvent.click(await screen.findByRole("button", { name: /Streichen: dunkle Grundfarbe/ }));
    await waitFor(() => expect(api.putProjectRequirements).toHaveBeenCalledWith("t1", { revision: 7, items: [] }));
  });

  it("opens a theme result in the workshop", async () => {
    setup(v3);
    const heard = vi.fn();
    window.addEventListener(INTENT_EVENT, heard);
    fireEvent.click(await screen.findByRole("button", { name: /Theme-Werkstatt/ }));
    expect(heard).toHaveBeenCalled();
    expect(takeIntent()).toEqual({ module: "page.theme-workshop", props: { task: "t1", base: "v3" } });
    window.removeEventListener(INTENT_EVENT, heard);
  });
});
