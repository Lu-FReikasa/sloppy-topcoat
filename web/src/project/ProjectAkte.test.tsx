import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import i18n from "../i18n";
import { PreparedChanges } from "../chat/PreparedChanges";
import {
  chooseProjectAction,
  fakeApi,
  node,
  project,
} from "../test/projectFixtures";
import { ChangeSetCard } from "./ChangeSets";
import { ProjectPage } from "./ProjectPage";
import { facts, KIND_GROUPS } from "./Timeline";
import type { ProjectChange, ProjectChangeSet, ProjectEvent } from "./types";

afterEach(() => {
  vi.restoreAllMocks();
});
beforeEach(async () => {
  await i18n.changeLanguage("en");
});

const api_ = (a: ReturnType<typeof fakeApi>) => a as unknown as ApiClient;

async function open(api: ReturnType<typeof fakeApi>) {
  render(<ProjectPage api={api_(api)} />);
  fireEvent.click(await screen.findByRole("button", { name: /Topcoat/ }));
  await screen.findByRole("heading", { name: "Topcoat", level: 2 });
}

function event(seq: number, over: Partial<ProjectEvent> = {}): ProjectEvent {
  return {
    id: `e${seq}`,
    seq,
    at: "2026-10-29T10:00:00Z",
    kind: "node.created",
    label: { en: "Created", de: "Angelegt" },
    project_id: "p1",
    project_title: "Topcoat",
    node_id: null,
    node_ref: "Topcoat 1",
    node_title: "Plan",
    actor: {
      kind: "person",
      app: null,
      agent_task_id: null,
      chain_id: null,
      change_set_id: null,
    },
    group_id: null,
    payload: null,
    reason: null,
    ...over,
  };
}

describe("the timeline", () => {
  it("shows who made each line and in the person's language", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    api.projectTimeline.mockResolvedValue({
      events: [
        event(1),
        event(2, {
          kind: "node.status_changed",
          label: { en: "Status changed", de: "Status geändert" },
          actor: {
            kind: "agent",
            app: "project",
            agent_task_id: "t",
            chain_id: null,
            change_set_id: "c1",
          },
          payload: { from: "todo", to: "in_review", note: "x".repeat(200) },
        }),
      ],
      next_after: null,
    });
    await open(api);
    fireEvent.click(screen.getByRole("tab", { name: "Timeline" }));
    const line = (await screen.findByText("Status changed")).closest("li")!;
    expect(line.textContent).toContain("Agent · project");
    expect(line.textContent).toContain("via a change set");
    expect(line.textContent).toContain("from: todo");
    expect(line.textContent).not.toContain("xxxxx"); // a long text is not a fact
    expect(screen.getByText("Created")).toBeTruthy();
  });

  it("filters by group and by entry, and reads on", async () => {
    const api = fakeApi({
      project: project(),
      nodes: [node({ id: "n1", title: "Plan", number: "1" })],
    });
    api.projectTimeline.mockResolvedValueOnce({
      events: [event(1)],
      next_after: 1,
    });
    await open(api);
    fireEvent.click(screen.getByRole("tab", { name: "Timeline" }));
    await screen.findByText("Created");
    api.projectTimeline.mockResolvedValue({
      events: [event(2, { label: { en: "Changed", de: "Geändert" } })],
      next_after: null,
    });
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(await screen.findByText("Changed")).toBeTruthy();
    expect(api.projectTimeline).toHaveBeenLastCalledWith(
      "p1",
      expect.objectContaining({ after: 1 }),
    );
    expect(screen.getByText("Created")).toBeTruthy(); // the first page stays

    fireEvent.change(screen.getByLabelText("Kind"), {
      target: { value: "links" },
    });
    await waitFor(() =>
      expect(api.projectTimeline).toHaveBeenLastCalledWith(
        "p1",
        expect.objectContaining({ kinds: KIND_GROUPS.links, after: 0 }),
      ),
    );
    fireEvent.change(screen.getByLabelText("Entry"), {
      target: { value: "n1" },
    });
    await waitFor(() =>
      expect(api.projectTimeline).toHaveBeenLastCalledWith(
        "p1",
        expect.objectContaining({ node: "n1" }),
      ),
    );
  });

  it("says whether the chain of the Akte is intact", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    await open(api);
    fireEvent.click(screen.getByRole("tab", { name: "Timeline" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Check the Akte" }),
    );
    expect((await screen.findByRole("status")).textContent).toContain("intact");
    api.verifyProjectTimeline.mockResolvedValue({
      ok: false,
      problems: ["line 7 changed"],
    });
    fireEvent.click(screen.getByRole("button", { name: "Check the Akte" }));
    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toContain(
        "line 7 changed",
      ),
    );
  });

  it("knows only the kinds the hub knows, and keeps long texts out of the facts", () => {
    expect(facts(null)).toEqual([]);
    expect(
      facts({ a: 1, b: { nested: true }, c: "x".repeat(81), d: "short" }),
    ).toEqual([
      ["a", "1"],
      ["d", "short"],
    ]);
    expect(Object.values(KIND_GROUPS).flat().length).toBeGreaterThan(30);
  });
});

describe("history and the way back", () => {
  const TASK = node({ id: "t1", title: "Plan", number: "1", revision: 4 });

  it("shows what changed between two versions and restores one as a new revision", async () => {
    const api = fakeApi({ project: project(), nodes: [TASK] });
    api.projectNodeHistory.mockResolvedValue({
      versions: [
        {
          rev: 1,
          kind: "snapshot",
          event_id: "e1",
          at: "2026-10-01T10:00:00Z",
        },
        { rev: 3, kind: "diff", event_id: "e2", at: "2026-10-02T10:00:00Z" },
        { rev: 4, kind: "diff", event_id: "e3", at: "2026-10-03T10:00:00Z" },
      ],
    });
    api.projectNodeDiff.mockResolvedValue({
      from: 3,
      to: 4,
      title: ["Plan", "Plan v2"],
      body: [
        { tag: "equal", old: ["same"], new: ["same"] },
        { tag: "replace", old: ["old line"], new: ["new line"] },
      ],
    });
    await open(api);
    fireEvent.click(screen.getByRole("button", { name: /Plan/ }));
    await screen.findByRole("group", { name: "History" });
    // the last two versions are chosen
    expect(
      (screen.getByLabelText("From version") as HTMLSelectElement).value,
    ).toBe("3");
    expect(
      (screen.getByLabelText("To version") as HTMLSelectElement).value,
    ).toBe("4");
    fireEvent.click(screen.getByRole("button", { name: "Show changes" }));
    const diff = await screen.findByTestId("diff");
    expect(api.projectNodeDiff).toHaveBeenCalledWith("t1", 3, 4);
    expect(within(diff).getByText("Plan v2").tagName).toBe("INS");
    expect(diff.textContent).toContain("- old line");
    expect(diff.textContent).toContain("+ new line");

    fireEvent.change(screen.getByLabelText("From version"), {
      target: { value: "1" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Restore this version" }),
    );
    await waitFor(() =>
      expect(api.revertProjectNode).toHaveBeenCalledWith("t1", {
        revision: 4,
        to_rev: 1,
      }),
    );
    expect((await screen.findByRole("status")).textContent).toContain(
      "A new version was made",
    );
  });

  it("says so when there is only one version", async () => {
    const api = fakeApi({ project: project(), nodes: [TASK] });
    api.projectNodeHistory.mockResolvedValue({
      versions: [{ rev: 1, kind: "snapshot", event_id: null, at: null }],
    });
    await open(api);
    fireEvent.click(screen.getByRole("button", { name: /Plan/ }));
    expect(await screen.findByText("There is only one version.")).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Restore this version" }),
    ).toBeNull();
  });
});

describe("the trash", () => {
  it("restores a project and an entry", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    api.projectTrash.mockResolvedValue({
      projects: [
        project({
          id: "p9",
          title: "Old project",
          trashed_at: "2026-10-01T10:00:00Z",
        }),
      ],
      nodes: [
        {
          ...node({ id: "n9", title: "Old entry", number: "3" }),
          project_title: "Topcoat",
        },
      ],
    });
    render(<ProjectPage api={api_(api)} />);
    fireEvent.click(await screen.findByRole("button", { name: "Trash" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Restore: Old project" }),
    );
    await waitFor(() => expect(api.restoreProject).toHaveBeenCalledWith("p9"));
    fireEvent.click(
      await screen.findByRole("button", { name: "Restore: Topcoat 3" }),
    );
    await waitFor(() =>
      expect(api.restoreProjectNode).toHaveBeenCalledWith("n9"),
    );
  });

  it("deletes for good only after the title was typed, with the mode of a project", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    api.projectTrash.mockResolvedValue({
      projects: [project({ id: "p9", title: "Old project" })],
      nodes: [],
    });
    render(<ProjectPage api={api_(api)} />);
    fireEvent.click(await screen.findByRole("button", { name: "Trash" }));
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Delete for good: Old project",
      }),
    );
    const field = screen.getByLabelText(
      "Type the title to confirm: Old project",
    );
    const go = screen.getByRole("button", {
      name: "Delete for good",
    }) as HTMLButtonElement;
    expect(go.disabled).toBe(true);
    fireEvent.change(field, { target: { value: "old project" } }); // not the same
    expect(go.disabled).toBe(true);
    fireEvent.change(field, { target: { value: "Old project" } });
    fireEvent.change(screen.getByLabelText("What stays"), {
      target: { value: "remove_everything" },
    });
    expect(go.disabled).toBe(false);
    fireEvent.click(go);
    await waitFor(() =>
      expect(api.purgeProject).toHaveBeenCalledWith(
        "p9",
        "Old project",
        "remove_everything",
      ),
    );
  });

  it("deletes an entry for good, always keeping the timeline", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    api.projectTrash.mockResolvedValue({
      projects: [],
      nodes: [
        {
          ...node({ id: "n9", title: "Old entry", number: "3" }),
          project_title: "Topcoat",
        },
      ],
    });
    render(<ProjectPage api={api_(api)} />);
    fireEvent.click(await screen.findByRole("button", { name: "Trash" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Delete for good: Topcoat 3" }),
    );
    expect(screen.queryByLabelText("What stays")).toBeNull();
    fireEvent.change(
      screen.getByLabelText("Type the title to confirm: Old entry"),
      { target: { value: "Old entry" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete for good" }));
    await waitFor(() =>
      expect(api.purgeProjectNode).toHaveBeenCalledWith("n9", "Old entry"),
    );
  });

  it("moves an entry and a project to the trash from the page", async () => {
    const task = node({ id: "t1", title: "Plan", number: "1" });
    const api = fakeApi({ project: project(), nodes: [task] });
    await open(api);
    fireEvent.click(screen.getByRole("button", { name: /Plan/ }));
    await screen.findByRole("group", { name: "History" });
    // the entry's button is in its detail, the project's in the project's menu (#179)
    fireEvent.click(screen.getByRole("button", { name: "Move to trash" }));
    await waitFor(() =>
      expect(api.trashProjectNode).toHaveBeenCalledWith("t1"),
    );
    chooseProjectAction("Move to trash");
    await waitFor(() => expect(api.trashProject).toHaveBeenCalledWith("p1"));
    expect(await screen.findByText("Pick a project on the left.")).toBeTruthy(); // it left the page
  });
});

const SET: ProjectChangeSet = {
  id: "s1",
  project_id: "p1",
  task_id: null,
  title: "Plan the release",
  summary: "Three steps",
  operations: [],
  preview: [
    {
      index: 0,
      op: "create_node",
      target: "Release",
      lines: [{ kind: "node.created", node: "Topcoat 1", payload: null }],
    },
    { index: 1, op: "create_node", target: "Build", lines: [] },
    { index: 2, op: "set_status", target: "Topcoat 2", lines: [] },
  ],
  dependants: { "0": [1], "1": [] },
  status: "pending",
  applied: [],
  result: null,
  created_at: null,
  decided_at: null,
};

describe("change sets", () => {
  function card(set: ProjectChangeSet = SET) {
    const api = fakeApi({ project: project(), nodes: [] });
    const onDecided = vi.fn();
    render(
      <ChangeSetCard
        api={api_(api)}
        set={set}
        onDecided={onDecided}
        onError={vi.fn()}
      />,
    );
    return { api, onDecided };
  }
  const box = (name: RegExp) =>
    screen.getByRole("checkbox", { name }) as HTMLInputElement;

  it("applies the ticked items with one click", async () => {
    const { api, onDecided } = card();
    expect(box(/1\. Create Release/).checked).toBe(true);
    fireEvent.click(box(/3\. Set the status of/)); // untick one
    fireEvent.click(
      screen.getByRole("button", { name: "Apply the ticked ones" }),
    );
    await waitFor(() =>
      expect(api.applyProjectChangeSet).toHaveBeenCalledWith("s1", [0, 1]),
    );
    expect(onDecided).toHaveBeenCalled();
  });

  it("unticks what needs an unticked item, and says what needs it", () => {
    card();
    expect(screen.getByText(/needed by 1/)).toBeTruthy();
    fireEvent.click(box(/1\. Create Release/));
    expect(box(/2\. Create Build/).checked).toBe(false); // it needs the first one
    fireEvent.click(box(/2\. Create Build/));
    expect(box(/2\. Create Build/).checked).toBe(true);
  });

  it("does not apply an empty selection, and can be refused", async () => {
    const { api } = card();
    for (const name of [/1\. Create Release/, /3\. Set the status of/])
      fireEvent.click(box(name));
    fireEvent.click(
      screen.getByRole("button", { name: "Apply the ticked ones" }),
    );
    expect((await screen.findByRole("alert")).textContent).toContain(
      "Tick at least one",
    );
    expect(api.applyProjectChangeSet).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Refuse" }));
    await waitFor(() =>
      expect(api.refuseProjectChangeSet).toHaveBeenCalledWith("s1"),
    );
  });

  it("shows a decided set without buttons", () => {
    card({ ...SET, status: "applied", applied: [0, 1] });
    expect(
      screen.queryByRole("button", { name: "Apply the ticked ones" }),
    ).toBeNull();
    expect(
      (screen.getAllByRole("checkbox")[0] as HTMLInputElement).disabled,
    ).toBe(true);
  });

  it("shows what an operation writes, and an operation that changes nothing", () => {
    card({
      ...SET,
      operations: [
        { op: "create_node", kind: "task", title: "Recherche", body: "1. Code lesen" },
        { op: "update_node", node: "n2", body: "Neue Anleitung" },
        { op: "update_node", node: "n3", title: "Same" },
      ],
      preview: [
        { index: 0, op: "create_node", target: "1.5 Recherche", lines: [] },
        {
          index: 1,
          op: "update_node",
          target: "Entwicklung",
          ref: "Topcoat 1.7",
          change: { body: ["alt", "Neue Anleitung"], status: ["todo", "in_review"] },
          lines: [],
        },
        { index: 2, op: "update_node", target: "Same", change: {}, lines: [] },
      ],
      dependants: {},
    });
    expect(screen.getByText("1. Code lesen")).toBeTruthy(); // the text of the new task
    expect(screen.getByText("Neue Anleitung")).toBeTruthy(); // the new text, not a line kind
    expect(screen.getByText("alt")).toBeTruthy(); // the old one folded away
    expect(screen.getByText(/Topcoat 1\.7/)).toBeTruthy();
    expect(screen.getByText(/Changes nothing/)).toBeTruthy();
  });

  it("is a tab that lists only the waiting sets, the decided ones on request", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    api.projectChangeSets.mockResolvedValue({
      change_sets: [
        { ...SET, id: "s2", title: "Old one", status: "applied" },
        SET,
      ],
    });
    await open(api);
    fireEvent.click(
      await screen.findByRole("tab", { name: "Change sets (1)" }),
    );
    const shown = await screen.findAllByRole("article");
    expect(shown).toHaveLength(1);
    expect(shown[0]!.getAttribute("data-status")).toBe("pending");
    expect(within(shown[0]!).getByText("Plan the release")).toBeTruthy();
    expect(screen.queryByText("Old one")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Show decided ones \(1\)/ }));
    expect(screen.getAllByRole("article")).toHaveLength(2);
  });
});

describe("the chat card shows the change sets of its check", () => {
  it("shows a waiting set with what it writes, so there is something to click", async () => {
    const api = {
      projectChanges: vi.fn(async () => []),
      projectChangeSets: vi.fn(async () => ({
        change_sets: [
          {
            ...SET,
            operations: [{ op: "update_node", node: "n", body: "1. Code lesen" }],
            preview: [
              {
                index: 0,
                op: "update_node",
                target: "Recherche",
                ref: "Topcoat 1.5",
                change: { body: ["", "1. Code lesen"] },
                lines: [],
              },
            ],
            dependants: {},
          },
          { ...SET, id: "s2", title: "Replaced", status: "refused" },
        ],
      })),
    };
    render(<PreparedChanges api={api as unknown as ApiClient} taskId="t" />);
    expect(await screen.findByText("1. Code lesen")).toBeTruthy();
    expect(api.projectChangeSets).toHaveBeenCalledWith({ taskId: "t" });
    expect(screen.getByRole("button", { name: "Apply the ticked ones" })).toBeTruthy();
    expect(screen.queryByText("Replaced")).toBeNull(); // a replaced set is not shown
  });
});

describe("the chat card for single changes", () => {
  it("names the new tools in words, not by their capability", async () => {
    const change: ProjectChange = {
      id: "c1",
      task_id: "t",
      capability: "project.node.claim",
      input: {},
      preview: { target: "Topcoat 1", fields: { shift_days: 2 } },
      status: "pending",
      result: null,
      created_at: null,
      decided_at: null,
    };
    const api = {
      projectChanges: vi.fn(async () => [change]),
      projectChangeSets: vi.fn(async () => ({ change_sets: [] })),
    };
    render(<PreparedChanges api={api as unknown as ApiClient} taskId="t" />);
    expect(await screen.findByText("Start working on a task")).toBeTruthy();
    expect(screen.getByText("Shift by days")).toBeTruthy();
  });
});

describe("replaced reports", () => {
  it("are left out of the list until asked for, and marked when shown", async () => {
    const old = node({
      id: "r1",
      title: "Old decision",
      number: "1",
      kind: "report",
      report_kind: "decision",
      status: null,
      current: false,
    });
    const fresh = node({
      id: "r2",
      title: "New decision",
      number: "2",
      kind: "report",
      report_kind: "decision",
      status: null,
      current: true,
    });
    const api = fakeApi({ project: project(), nodes: [old, fresh] });
    await open(api);
    expect(screen.queryByText("Old decision")).toBeNull();
    expect(screen.getByText("New decision")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Show replaced reports"));
    const row = screen.getByText("Old decision").closest(".project-row")!;
    expect(row.className).toContain("is-replaced");
    expect(within(row as HTMLElement).getByText("replaced")).toBeTruthy();
  });
});
