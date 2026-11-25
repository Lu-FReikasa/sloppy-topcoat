import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import i18n from "../i18n";
import { fakeApi, node, project, TREE } from "../test/projectFixtures";
import { BoardView, columnAt } from "./BoardView";
import { ProjectPage } from "./ProjectPage";
import { nextSort, sortMark } from "./TableView";
import { TODAY_FILTER } from "./TodayView";
import type { ProjectFound, ProjectNode } from "./types";

afterEach(() => {
  vi.restoreAllMocks();
});
beforeEach(async () => {
  await i18n.changeLanguage("en");
});

const TASKS: ProjectNode[] = [
  node({
    id: "t1",
    title: "Write plan",
    number: "1",
    status: "todo",
    priority: 3,
    due_at: "2026-10-30T16:00:00Z",
  }),
  node({ id: "t2", title: "Review plan", number: "2", status: "in_review" }),
  node({ id: "t3", title: "Ship it", number: "3", status: "in_progress" }),
];

async function open(api: ReturnType<typeof fakeApi>) {
  render(<ProjectPage api={api as unknown as ApiClient} />);
  fireEvent.click(await screen.findByRole("button", { name: /Topcoat/ }));
  await screen.findByRole("heading", { name: "Topcoat", level: 2 });
}

const found = (n: ProjectNode): ProjectFound => ({
  ...n,
  project_title: "Topcoat",
});

describe("the sort rule of the table", () => {
  it("turns the direction on a second click and keeps the older keys behind", () => {
    expect(nextSort("", "dueDate", "asc")).toBe("dueDate:asc");
    expect(nextSort("dueDate:asc", "dueDate", "asc")).toBe("dueDate:desc");
    expect(nextSort("dueDate:desc", "dueDate", "asc")).toBe("dueDate:asc");
    expect(nextSort("dueDate:asc", "priority", "desc")).toBe(
      "priority:desc,dueDate:asc",
    );
    expect(nextSort("priority:desc,dueDate:asc", "dueDate", "asc")).toBe(
      "dueDate:asc,priority:desc",
    );
  });

  it("marks only the column it sorts by", () => {
    expect(sortMark("dueDate:desc,priority", "dueDate")).toBe("desc");
    expect(sortMark("dueDate:desc,priority", "priority")).toBe("");
    expect(sortMark("title", "title")).toBe("asc");
  });
});

describe("ProjectPage views", () => {
  it("switches to the table and asks the hub to sort when a header is clicked", async () => {
    const api = fakeApi({ project: project(), nodes: TASKS });
    api.searchProjectNodes.mockResolvedValue({
      nodes: [found(TASKS[1]!), found(TASKS[0]!)],
      total: 2,
      next_after: null,
    });
    await open(api);
    fireEvent.click(screen.getByRole("button", { name: "Table" }));
    expect(screen.getByRole("columnheader", { name: /Due/ })).toBeTruthy();
    fireEvent.click(
      within(screen.getByRole("columnheader", { name: /Priority/ })).getByRole(
        "button",
      ),
    );
    await waitFor(() =>
      expect(api.searchProjectNodes).toHaveBeenCalledWith(
        expect.objectContaining({ sort: "priority:desc", project: "p1" }),
      ),
    );
    // the order of the answer is the order shown
    await waitFor(() => {
      const rows = screen.getAllByRole("row").slice(1);
      expect(rows[0]!.textContent).toContain("Review plan");
    });
  });

  it("filters through the hub and shows its reason when the filter is wrong", async () => {
    const api = fakeApi({ project: project(), nodes: TASKS });
    api.searchProjectNodes.mockRejectedValueOnce(
      new ApiError(422, "invalid", "unknown field: colour"),
    );
    await open(api);
    fireEvent.change(screen.getByLabelText("Filter"), {
      target: { value: "colour = red" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect((await screen.findByRole("alert")).textContent).toContain(
      "unknown field: colour",
    );
    expect(api.searchProjectNodes).toHaveBeenCalledWith(
      expect.objectContaining({ filter: "colour = red", project: "p1" }),
    );
    api.searchProjectNodes.mockResolvedValue({
      nodes: [found(TASKS[2]!)],
      total: 1,
      next_after: null,
    });
    fireEvent.change(screen.getByLabelText("Filter"), {
      target: { value: "status = in_progress" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(screen.queryByText("Write plan")).toBeNull());
    expect(screen.getByText("Ship it")).toBeTruthy();
  });

  it("applies a saved view and saves the current one with the limits of the board", async () => {
    const api = fakeApi({ project: project(), nodes: TASKS });
    api.projectViews.mockResolvedValue({
      views: [
        {
          id: "v1",
          project_id: null,
          title: "In review",
          type: "board",
          filter: "status = in_review",
          sort: "",
          config: { wip: { in_review: 2 } },
          favorite: false,
          position: 1,
          builtin: true,
          revision: 1,
        },
      ],
    });
    api.searchProjectNodes.mockResolvedValue({
      nodes: [found(TASKS[1]!)],
      total: 1,
      next_after: null,
    });
    await open(api);
    fireEvent.change(screen.getByLabelText("Saved views"), {
      target: { value: "v1" },
    });
    await waitFor(() =>
      expect(api.searchProjectNodes).toHaveBeenCalledWith(
        expect.objectContaining({ filter: "status = in_review" }),
      ),
    );
    // a board now, with the limit from the view
    expect(screen.getByRole("region", { name: "In review" })).toBeTruthy();
    expect(
      (screen.getByLabelText("In review: WIP limit") as HTMLInputElement).value,
    ).toBe("2");
    fireEvent.change(screen.getByLabelText("Name of the view"), {
      target: { value: "Mine" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save this view" }));
    await waitFor(() =>
      expect(api.createProjectView).toHaveBeenCalledWith({
        title: "Mine",
        type: "board",
        filter: "status = in_review",
        sort: "",
        config: { wip: { in_review: 2 } },
        project_id: "p1",
      }),
    );
  });

  it("makes a task from the quick add line, with its labels", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    await open(api);
    fireEvent.change(screen.getByLabelText("Quick add"), {
      target: { value: "Pay the invoice *money !3 tomorrow 17:00" },
    });
    fireEvent.click(
      within(
        screen.getByLabelText("Quick add").closest("form") as HTMLElement,
      ).getByRole("button", { name: "Add" }),
    );
    await waitFor(() => expect(api.createProjectNode).toHaveBeenCalled());
    const [projectId, body] = api.createProjectNode.mock.calls[0]!;
    expect(projectId).toBe("p1");
    expect(body).toMatchObject({
      kind: "task",
      title: "Pay the invoice",
      priority: 3,
    });
    expect(body.due_at).toMatch(/T17:00:00[+-]\d\d:\d\d$/);
    await waitFor(() =>
      expect(api.createProjectLabel).toHaveBeenCalledWith({ title: "money" }),
    );
    expect(api.setProjectNodeLabels).toHaveBeenCalledWith("n-new", {
      add: ["l-money"],
    });
  });

  it("refuses a quick add line that has no title", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    await open(api);
    fireEvent.change(screen.getByLabelText("Quick add"), {
      target: { value: "*home !2 tomorrow" },
    });
    fireEvent.click(
      within(
        screen.getByLabelText("Quick add").closest("form") as HTMLElement,
      ).getByRole("button", { name: "Add" }),
    );
    expect((await screen.findByRole("alert")).textContent).toContain(
      "what the task is called",
    );
    expect(api.createProjectNode).not.toHaveBeenCalled();
  });

  it("marks a task done from the list and says so when it repeats", async () => {
    const api = fakeApi({ project: project(), nodes: TASKS });
    api.completeProjectNode.mockResolvedValue({
      ...TASKS[0]!,
      status: "todo",
      due_at: "2026-11-06T16:00:00Z",
      reopened: true,
    });
    await open(api);
    fireEvent.click(
      screen.getByRole("checkbox", { name: /Mark done: Topcoat 1/ }),
    );
    await waitFor(() =>
      expect(api.completeProjectNode).toHaveBeenCalledWith("t1", 1),
    );
    expect((await screen.findByRole("status")).textContent).toContain(
      "next one is due",
    );
  });

  it("shows what is due on the Today page, late tasks first", async () => {
    const late = node({
      id: "d1",
      title: "Overdue thing",
      number: "4",
      status: "todo",
      due_at: "2000-01-01T10:00:00Z",
    });
    const soon = node({
      id: "d2",
      title: "Later today",
      number: "5",
      status: "todo",
      due_at: "2999-01-01T10:00:00Z",
    });
    const api = fakeApi({ project: project(), nodes: [] });
    api.searchProjectNodes.mockResolvedValue({
      nodes: [found(late), found(soon)],
      total: 2,
      next_after: null,
    });
    render(<ProjectPage api={api as unknown as ApiClient} />);
    fireEvent.click(await screen.findByRole("button", { name: "Today" }));
    expect(await screen.findByText("Overdue thing")).toBeTruthy();
    expect(api.searchProjectNodes).toHaveBeenCalledWith(
      expect.objectContaining({ filter: TODAY_FILTER }),
    );
    expect(screen.getByRole("region", { name: "Overdue" })).toBeTruthy();
    expect(
      screen.getByRole("region", { name: "Due today" }).textContent,
    ).toContain("Later today");
    // a click takes the person to the node in its project
    fireEvent.click(screen.getByRole("button", { name: /Overdue thing/ }));
    await screen.findByRole("heading", { name: "Topcoat", level: 2 });
    expect(api.projectProject).toHaveBeenCalledWith("p1");
  });
});

describe("the board", () => {
  it("moves a card with the menu on it, the way for the keyboard", async () => {
    const api = fakeApi({ project: project(), nodes: TASKS });
    await open(api);
    fireEvent.click(screen.getByRole("button", { name: "Board" }));
    fireEvent.change(screen.getByLabelText("Move Topcoat 1 to"), {
      target: { value: "in_review" },
    });
    await waitFor(() =>
      expect(api.updateProjectNode).toHaveBeenCalledWith("t1", {
        revision: 1,
        status: "in_review",
      }),
    );
  });

  it("says when a column holds more than its limit, and still lets the card in", () => {
    const onMove = vi.fn();
    render(
      <BoardView
        tasks={[
          TASKS[1]!,
          node({
            id: "t4",
            title: "Another",
            number: "4",
            status: "in_review",
          }),
        ]}
        limits={{ in_review: 1 }}
        selectedId={null}
        now={new Date()}
        zone="UTC"
        onMove={onMove}
        onOpen={() => undefined}
        onLimit={() => undefined}
      />,
    );
    const column = screen.getByRole("region", { name: "In review" });
    expect(within(column).getByRole("status").textContent).toContain(
      "over the limit",
    );
    fireEvent.change(within(column).getAllByRole("combobox")[0]!, {
      target: { value: "done" },
    });
    expect(onMove).toHaveBeenCalledWith(
      expect.objectContaining({ id: "t2" }),
      "done",
    );
  });

  it("moves a card that is dragged into another column, and opens it on a plain click", () => {
    const onMove = vi.fn();
    const onOpen = vi.fn();
    render(
      <BoardView
        tasks={TASKS}
        limits={{}}
        selectedId={null}
        now={new Date()}
        zone="UTC"
        onMove={onMove}
        onOpen={onOpen}
        onLimit={() => undefined}
      />,
    );
    const card = screen.getByText("Write plan").closest("li") as HTMLElement;
    const target = screen.getByRole("region", { name: "Done" });
    // jsdom has no layout: say which element is under the pointer
    document.elementFromPoint = vi.fn(() => target);
    expect(columnAt(5, 5)).toBe("done");
    fireEvent.pointerDown(card, {
      button: 0,
      clientX: 10,
      clientY: 10,
      pointerId: 1,
    });
    fireEvent.pointerMove(card, { clientX: 60, clientY: 10, pointerId: 1 });
    fireEvent.pointerUp(card, { clientX: 60, clientY: 10, pointerId: 1 });
    expect(onMove).toHaveBeenCalledWith(
      expect.objectContaining({ id: "t1" }),
      "done",
    );
    expect(onOpen).not.toHaveBeenCalled();

    fireEvent.pointerDown(card, {
      button: 0,
      clientX: 10,
      clientY: 10,
      pointerId: 1,
    });
    fireEvent.pointerUp(card, { clientX: 11, clientY: 10, pointerId: 1 });
    expect(onOpen).toHaveBeenCalledWith("t1");
    expect(onMove).toHaveBeenCalledTimes(1);
  });

  it("does not move a card dropped on its own column", () => {
    const onMove = vi.fn();
    render(
      <BoardView
        tasks={TASKS}
        limits={{}}
        selectedId={null}
        now={new Date()}
        zone="UTC"
        onMove={onMove}
        onOpen={() => undefined}
        onLimit={() => undefined}
      />,
    );
    const card = screen.getByText("Write plan").closest("li") as HTMLElement;
    document.elementFromPoint = vi.fn(() =>
      screen.getByRole("region", { name: "To do" }),
    );
    fireEvent.pointerDown(card, {
      button: 0,
      clientX: 10,
      clientY: 10,
      pointerId: 1,
    });
    fireEvent.pointerMove(card, { clientX: 70, clientY: 10, pointerId: 1 });
    fireEvent.pointerUp(card, { clientX: 70, clientY: 10, pointerId: 1 });
    expect(onMove).not.toHaveBeenCalled();
  });
});

describe("the detail pane", () => {
  const TASK = node({
    id: "t1",
    title: "Write plan",
    number: "1",
    status: "todo",
    priority: 2,
    due_at: "2026-10-30T16:00:00Z",
    labels: [{ id: "l1", title: "money", color: null, description: "" }],
  });

  it("sends only the fields that changed, dates with an offset, and null for a cleared one", async () => {
    const api = fakeApi({ project: project(), nodes: [TASK] });
    await open(api);
    fireEvent.click(screen.getByRole("button", { name: /Write plan/ }));
    fireEvent.change(
      await screen.findByLabelText("Priority", { selector: "select" }),
      { target: { value: "4" } },
    );
    const dueField = screen.getByLabelText("Due", {
      selector: "input",
    }) as HTMLInputElement;
    expect(dueField.value).not.toBe("");
    fireEvent.click(
      within(dueField.closest("div") as HTMLElement).getByRole("button", {
        name: "Clear",
      }),
    );
    fireEvent.change(screen.getByLabelText("Estimate (minutes)"), {
      target: { value: "90" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(api.updateProjectNode).toHaveBeenCalledWith("t1", {
        revision: 1,
        due_at: null,
        priority: 4,
        estimate_minutes: 90,
      }),
    );
  });

  it("makes a repeat rule from a count and a unit", async () => {
    const api = fakeApi({ project: project(), nodes: [TASK] });
    await open(api);
    fireEvent.click(screen.getByRole("button", { name: /Write plan/ }));
    fireEvent.click(
      await screen.findByLabelText("Repeat", { selector: "input" }),
    );
    fireEvent.change(screen.getByLabelText("every", { selector: "input" }), {
      target: { value: "2" },
    });
    fireEvent.change(screen.getByLabelText("Repeat", { selector: "select" }), {
      target: { value: "month" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.updateProjectNode).toHaveBeenCalled());
    expect(api.updateProjectNode.mock.calls[0]![1]).toMatchObject({
      repeat_rule: { every: 2, unit: "month", mode: "monthly" },
    });
  });

  it("takes a label off and puts a known one on", async () => {
    const api = fakeApi({ project: project(), nodes: [TASK] });
    api.projectLabels.mockResolvedValue({
      labels: [
        { id: "l1", title: "money", color: null, description: "" },
        { id: "l2", title: "home", color: null, description: "" },
      ],
    });
    await open(api);
    fireEvent.click(screen.getByRole("button", { name: /Write plan/ }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Take money off" }),
    );
    await waitFor(() =>
      expect(api.setProjectNodeLabels).toHaveBeenCalledWith("t1", {
        remove: ["l1"],
      }),
    );
    fireEvent.change(
      screen.getByLabelText("Add label", { selector: "input" }),
      { target: { value: "Home" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Add label" }));
    await waitFor(() =>
      expect(api.setProjectNodeLabels).toHaveBeenCalledWith("t1", {
        add: ["l2"],
      }),
    );
    expect(api.createProjectLabel).not.toHaveBeenCalled(); // found by name, not made twice
  });

  it("lists relations in words and ends one", async () => {
    const api = fakeApi({ project: project(), nodes: [TASK] });
    api.projectRelations.mockResolvedValue({
      relations: [
        {
          id: "r1",
          kind: "blocked_by",
          direction: "out",
          other: {
            id: "x",
            ref: "Other 2.1",
            title: "Prepare",
            project_id: "p2",
          },
          valid_from: null,
          valid_to: null,
        },
      ],
    });
    const endProjectRelation = vi.fn(async () => ({
      id: "r1",
      valid_to: "2026-10-29T10:00:00Z",
    }));
    (
      api as unknown as { endProjectRelation: typeof endProjectRelation }
    ).endProjectRelation = endProjectRelation;
    await open(api);
    fireEvent.click(screen.getByRole("button", { name: /Write plan/ }));
    const end = await screen.findByRole("button", { name: "End: Other 2.1" });
    const line = end.closest("li")!.textContent ?? "";
    expect(line).toContain("waits for");
    expect(line).toContain("Other 2.1 Prepare");
    fireEvent.click(end);
    await waitFor(() => expect(endProjectRelation).toHaveBeenCalledWith("r1"));
  });

  it("adds a reminder before the due date, and cannot without that date", async () => {
    const withDate = fakeApi({ project: project(), nodes: [TASK] });
    const addProjectReminder = vi.fn(async () => ({
      id: "m1",
      node_id: "t1",
      at: null,
      relative_to: "due" as const,
      offset_seconds: -7200,
      fire_at: null,
      fired_at: null,
    }));
    (
      withDate as unknown as { addProjectReminder: typeof addProjectReminder }
    ).addProjectReminder = addProjectReminder;
    await open(withDate);
    fireEvent.click(screen.getByRole("button", { name: /Write plan/ }));
    fireEvent.change(
      await screen.findByLabelText("Amount (negative = before)", {
        selector: "input",
      }),
      { target: { value: "-2" } },
    );
    const units = screen.getAllByLabelText("Amount (negative = before)", {
      selector: "select",
    });
    fireEvent.change(units[0]!, { target: { value: "hours" } });
    fireEvent.click(screen.getByRole("button", { name: "Add reminder" }));
    await waitFor(() =>
      expect(addProjectReminder).toHaveBeenCalledWith("t1", {
        relative_to: "due",
        offset_seconds: -7200,
      }),
    );
  });

  it("explains why a reminder before the start date is not possible yet", async () => {
    const api = fakeApi({ project: project(), nodes: [TASK] });
    await open(api);
    fireEvent.click(screen.getByRole("button", { name: /Write plan/ }));
    fireEvent.change(
      await screen.findByLabelText("Date", { selector: "select" }),
      { target: { value: "start" } },
    );
    expect(screen.getByText(/has no start yet/)).toBeTruthy();
    expect(
      (
        screen.getByRole("button", {
          name: "Add reminder",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  });
});

describe("the page keeps working with act()", () => {
  it("shows the tree as before when no filter is set", async () => {
    const api = fakeApi({ project: project(), nodes: TREE });
    await open(api);
    await act(async () => undefined);
    expect(screen.getByText("Check")).toBeTruthy();
    expect(api.searchProjectNodes).not.toHaveBeenCalled();
  });
});
