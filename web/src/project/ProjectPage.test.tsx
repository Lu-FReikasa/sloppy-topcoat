import {
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
import { ProjectPage, visibleNodes } from "./ProjectPage";

afterEach(() => {
  vi.restoreAllMocks();
});

async function openProject(api: ReturnType<typeof fakeApi>) {
  render(<ProjectPage api={api as unknown as ApiClient} />);
  fireEvent.click(await screen.findByRole("button", { name: /Topcoat/ }));
  await screen.findByRole("heading", { name: "Topcoat", level: 2 });
}

describe("ProjectPage", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("lists the projects and shows the numbered tree of the selected one", async () => {
    const api = fakeApi({ project: project(), nodes: TREE });
    await openProject(api);
    expect(screen.getByText("1.1")).toBeTruthy();
    expect(screen.getByText("Check")).toBeTruthy();
    expect(screen.getByText("Which node?")).toBeTruthy();
  });

  it("hides the contents of a collapsed topic", async () => {
    const api = fakeApi({ project: project(), nodes: TREE });
    await openProject(api);
    fireEvent.click(screen.getByRole("button", { name: "Hide the contents" }));
    expect(screen.queryByText("Check")).toBeNull();
    expect(screen.getByText("Repo")).toBeTruthy();
  });

  it("creates a project with a title and a description", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    render(<ProjectPage api={api as unknown as ApiClient} />);
    await screen.findByRole("button", { name: /Topcoat/ });
    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "Home-Lab" },
    });
    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Operations" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(api.createProject).toHaveBeenCalledWith({
        title: "Home-Lab",
        description: "Operations",
      }),
    );
  });

  it("saves the grants of the project through the scope editor", async () => {
    const api = fakeApi({ project: project({ scope: {} }), nodes: [] });
    await openProject(api);
    fireEvent.click(
      screen.getByRole("button", { name: "Grants for the agent" }),
    );
    fireEvent.change(screen.getByLabelText("dev request_report"), {
      target: { value: "free" },
    });
    fireEvent.change(screen.getByLabelText("dev create_branch"), {
      target: { value: "confirm" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save grants" }));
    await waitFor(() =>
      expect(api.setProjectScope).toHaveBeenCalledWith("p1", {
        revision: 1,
        scope: { dev: { request_report: "free", create_branch: "confirm" } },
      }),
    );
  });

  it("lets the person confirm a request that awaits confirmation", async () => {
    const request = node({
      id: "r1",
      title: "Branch for the trace tool",
      number: "3",
      kind: "report",
      report_kind: "request",
      status: null,
      message: {
        type: "project.request",
        from: "project",
        to: "dev",
        chain_id: "c1",
        in_reply_to: null,
        round: 1,
        state: "awaiting_click",
        task_id: null,
        payload: { topic: "Branch for the trace tool" },
        schema_version: 1,
        trust: "own",
        stats: null,
        verification: null,
      },
    });
    const api = fakeApi({ project: project(), nodes: [request] });
    await openProject(api);
    fireEvent.click(
      screen.getByRole("button", { name: /Branch for the trace tool/ }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));
    await waitFor(() =>
      expect(api.decideProjectRequest).toHaveBeenCalledWith("r1", true),
    );
  });

  it("opens a result as it reads, with a theme in it as a card to try", async () => {
    const theme = {
      key: "xp-luna", name: "XP Luna", base: "schreibtisch", layout: { kind: "cascade" },
      bar: { position: "bottom", items: ["start", "programs", "clock"] },
      chrome: { buttons: ["minimise", "maximise", "close"], titlebar: "top" },
      motion: { style: "fade", ms: 150 }, css: "--accent: #245EDC;",
    };
    const result = node({
      id: "r2",
      title: "Ergebnis: XP Theme",
      number: "1.7.1",
      kind: "report",
      report_kind: "document",
      status: null,
      body: ["**Stand:** in Prüfung", "", "```json", JSON.stringify(theme, null, 2), "```"].join(
        "\n",
      ),
    });
    const api = fakeApi({ project: project(), nodes: [result] });
    await openProject(api);
    fireEvent.click(screen.getByRole("button", { name: /Ergebnis: XP Theme/ }));
    expect(await screen.findByTestId("theme-card")).toBeTruthy();
  });

  it("answers a question on the page", async () => {
    const api = fakeApi({ project: project(), nodes: TREE });
    await openProject(api);
    fireEvent.click(screen.getByRole("button", { name: /Which node/ }));
    const detail = screen.getByLabelText("Answer", { selector: "input" });
    fireEvent.change(detail, { target: { value: "node-amd" } });
    fireEvent.click(
      within(detail.closest("form") as HTMLElement).getByRole("button", {
        name: "Answer",
      }),
    );
    await waitFor(() =>
      expect(api.answerProjectQuestion).toHaveBeenCalledWith("q1", "node-amd"),
    );
  });

  it("shows the conflict message when the revision is stale", async () => {
    const api = fakeApi({ project: project(), nodes: TREE });
    api.updateProject.mockRejectedValueOnce(
      new ApiError(409, "conflict", "stale"),
    );
    await openProject(api);
    fireEvent.click(screen.getByRole("button", { name: "Archive project" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("alert").textContent ?? "").toContain(
      "changed meanwhile",
    );
  });
});

describe("visibleNodes", () => {
  it("gives each node its depth and leaves out the contents of collapsed nodes", () => {
    const shown = visibleNodes(TREE, new Set(["n1"])).map((v) => v.node.id);
    expect(shown).toEqual(["n1", "q1"]);
    const all = visibleNodes(TREE, new Set());
    expect(all.map((v) => v.depth)).toEqual([0, 1, 0]);
    expect(all[0]?.hasChildren).toBe(true);
  });
});
