import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import type { ProjectChange } from "../project/types";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { PreparedChanges } from "./PreparedChanges";

afterEach(cleanup);

const TASK = "6b7d5f12-0000-4000-8000-000000000001";

function change(over: Partial<ProjectChange> = {}): ProjectChange {
  return {
    id: "c1",
    task_id: TASK,
    capability: "project.node.update",
    input: {},
    preview: { target: "Topcoat 1.3", fields: { status: "done" } },
    status: "pending",
    result: null,
    created_at: null,
    decided_at: null,
    ...over,
  };
}

function setup(list: ProjectChange[], extra: Record<string, unknown> = {}) {
  const mock = mockFetch({
    ...extra,
    [`GET /api/project/changes?task_id=${TASK}`]: list,
    "POST /api/project/changes/c1/apply": jsonResponse(
      change({ status: "applied" }),
    ),
    "POST /api/project/changes/c1/refuse": jsonResponse(
      change({ status: "refused" }),
    ),
  });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  render(<PreparedChanges api={api} taskId={TASK} />);
  return mock;
}

describe("PreparedChanges", () => {
  it("shows a batch card with its tasks in order and waits for the click", async () => {
    const mock = setup([
      change({
        capability: "project.batch.propose",
        preview: {
          target: "Topcoat-Hub Abgleich",
          fields: { tasks: ["1.1 Looks prüfen", "1.2 Repo prüfen"], max_runs: 3 },
        },
      }),
    ]);
    const card = await screen.findByTestId("batch-card");
    const items = Array.from(card.querySelectorAll("ol.batch-tasks li")).map((li) => li.textContent);
    expect(items).toEqual(["1.1 Looks prüfen", "1.2 Repo prüfen"]);
    expect(card.textContent).toContain("Läufe: 0 von 3");
    expect(card.querySelector("button")?.textContent).toBe("Batch starten");
    expect(mock.calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("shows the state of each task of a started batch, with the reason of a blocked one", async () => {
    const started = change({
      capability: "project.batch.propose",
      status: "applied",
      result: { started: true, batch: { id: "b1" } },
    });
    const state = {
      id: "b1",
      project_id: "p1",
      node_ids: ["n1", "n2"],
      max_runs: 3,
      runs: 1,
      status: "stopped",
      stop_reason: "no task can start: its blockers are not done",
      created_at: null,
      stopped_at: null,
      tasks: [
        {
          node_id: "n1",
          number: "1.1",
          title: "Wiki",
          status: "blocked",
          reason: "row 1 has no value",
          waits_for: [],
        },
        {
          node_id: "n2",
          number: "1.2",
          title: "Repo",
          status: "todo",
          reason: null,
          waits_for: [{ number: "1.1", title: "Wiki", status: "blocked" }],
        },
      ],
    };
    setup([started], { "GET /api/project/batches/b1": state });
    const card = await screen.findByTestId("batch-card");
    expect(await screen.findByText("1.1 Wiki")).toBeTruthy();
    expect(card.textContent).toContain("fehlerhaft");
    expect(card.textContent).toContain("Grund: row 1 has no value");
    expect(card.textContent).toContain("angehalten");
    expect(card.querySelector("button")).toBeNull();
  });

  it("shows what was prepared and changes nothing until the click", async () => {
    const mock = setup([change()]);
    const card = await screen.findByTestId("prepared-changes");
    expect(card.textContent).toContain("Eintrag ändern");
    expect(card.textContent).toContain("Topcoat 1.3");
    expect(card.textContent).toContain("done");
    expect(card.textContent).toContain("Noch wurde nichts geändert");
    expect(mock.calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("applies one change with its button and shows the new state", async () => {
    const mock = setup([change()]);
    fireEvent.click(await screen.findByRole("button", { name: "Übernehmen" }));
    await waitFor(() =>
      expect(screen.getByTestId("change-status").textContent).toBe(
        "übernommen",
      ),
    );
    expect(
      mock.calls.some(
        (c) => c.method === "POST" && c.url.endsWith("/c1/apply"),
      ),
    ).toBe(true);
    expect(screen.queryByRole("button", { name: "Übernehmen" })).toBeNull();
  });

  it("discards a change without applying it", async () => {
    const mock = setup([change()]);
    fireEvent.click(await screen.findByRole("button", { name: "Verwerfen" }));
    await waitFor(() =>
      expect(screen.getByTestId("change-status").textContent).toBe("verworfen"),
    );
    expect(mock.calls.some((c) => c.url.endsWith("/apply"))).toBe(false);
  });

  it("says why a change failed and offers no button for a decided one", async () => {
    setup([
      change({
        status: "failed",
        result: {
          error: "conflict",
          message: "the item changed since it was read",
        },
      }),
    ]);
    await screen.findByText("the item changed since it was read");
    expect(screen.queryByRole("button", { name: "Übernehmen" })).toBeNull();
  });

  it("shows nothing when the check prepared nothing", async () => {
    setup([]);
    await waitFor(() =>
      expect(screen.queryByTestId("prepared-changes")).toBeNull(),
    );
  });

  // live 2026-11-17: three cards of "Art task, Titel …, Darunter 44c2095b-…"
  it("groups new entries under the entry they go under, by number and title", async () => {
    const parent = { ref: "Topcoat 3.1", title: "Imagetools im Chat" };
    const create = (id: string, title: string, body?: string) =>
      change({
        id,
        capability: "project.node.create",
        preview: {
          target: "Topcoat",
          parent,
          fields: {
            kind: "task",
            title,
            parent_id: "44c2095b-811b-4889-9606-2715022cea5c",
            ...(body ? { body } : {}),
          },
        },
      });
    const mock = setup([
      create("c1", "Recherche: Bildverständnis", "Zielanwendung: dev\n- [ ] Code lesen"),
      create("c2", "Dev: Bildverständnis"),
    ], {
      "POST /api/project/changes/c2/apply": jsonResponse(change({ id: "c2", status: "applied" })),
    });
    const group = await screen.findByTestId("change-group");
    expect(group.textContent).toContain("Neu unter Topcoat 3.1 · Imagetools im Chat");
    expect(screen.getAllByTestId("change-title").map((x) => x.textContent)).toEqual([
      "Recherche: Bildverständnis",
      "Dev: Bildverständnis",
    ]);
    expect(screen.getByTestId("change-body").textContent).toContain("- [ ] Code lesen");
    expect(screen.getAllByTestId("change-nobody")).toHaveLength(1);
    // no ids and no "task" for a person to decode
    expect(group.textContent).not.toContain("44c2095b");
    expect(group.textContent).not.toContain("Art");
    fireEvent.click(screen.getByRole("button", { name: "Alle übernehmen (2)" }));
    await waitFor(() =>
      expect(mock.calls.filter((c) => c.method === "POST").map((c) => c.url)).toEqual([
        "/api/project/changes/c1/apply",
        "/api/project/changes/c2/apply",
      ]),
    );
  });
});
