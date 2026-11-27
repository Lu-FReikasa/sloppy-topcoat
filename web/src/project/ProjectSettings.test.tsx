import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import i18n from "../i18n";
import {
  chooseProjectAction,
  fakeApi,
  node,
  project,
} from "../test/projectFixtures";
import * as download from "./download";
import { ProjectPage } from "./ProjectPage";
import type { ProjectTemplate } from "./types";

afterEach(() => {
  vi.restoreAllMocks();
});
beforeEach(async () => {
  await i18n.changeLanguage("en");
});

const as = (a: ReturnType<typeof fakeApi>) => a as unknown as ApiClient;

describe("reminders and time", () => {
  it("saves quiet hours and the hour of the daily list", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    render(<ProjectPage api={as(api)} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Reminders and time" }),
    );
    const start = (await screen.findByLabelText(
      "Quiet from",
    )) as HTMLInputElement;
    fireEvent.change(start, { target: { value: "22:00" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Save" })[1]!);
    expect((await screen.findByRole("alert")).textContent).toContain(
      "a start and an end",
    );
    expect(api.putProjectNotificationSettings).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("until"), {
      target: { value: "07:00" },
    });
    fireEvent.change(screen.getByLabelText("Daily list of overdue tasks at"), {
      target: { value: "8" },
    });
    fireEvent.click(screen.getAllByRole("button", { name: "Save" })[1]!);
    await waitFor(() =>
      expect(api.putProjectNotificationSettings).toHaveBeenCalledWith({
        quiet_start: "22:00",
        quiet_end: "07:00",
        digest_hour: 8,
      }),
    );
    expect(await screen.findByText("Saved.")).toBeTruthy();
  });

  it("stores the time zone the person chose, or none to follow the hub again", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    render(<ProjectPage api={as(api)} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Reminders and time" }),
    );
    const field = await screen.findByLabelText("Time zone");
    expect((field as HTMLInputElement).placeholder).toContain(
      "Follow the hub (Europe/Berlin)",
    );
    fireEvent.change(field, { target: { value: "Asia/Tokyo" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Save" })[0]!);
    await waitFor(() =>
      expect(api.putMyTimezone).toHaveBeenCalledWith("Asia/Tokyo"),
    );
    expect(await screen.findByText("Time zone saved.")).toBeTruthy();
    fireEvent.change(field, { target: { value: "" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Save" })[0]!);
    await waitFor(() =>
      expect(api.putMyTimezone).toHaveBeenLastCalledWith(null),
    );
  });

  it("says when the hub does not know the zone", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    api.putMyTimezone.mockRejectedValueOnce(new ApiError(422, "invalid", "no"));
    render(<ProjectPage api={as(api)} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Reminders and time" }),
    );
    fireEvent.change(await screen.findByLabelText("Time zone"), {
      target: { value: "Mars/Olympus" },
    });
    fireEvent.click(screen.getAllByRole("button", { name: "Save" })[0]!);
    expect(
      await screen.findByText("That is not a time zone the hub knows."),
    ).toBeTruthy();
  });

  it("shows the dates in the zone of the person, not in the browser's", async () => {
    const task = node({
      id: "t1",
      title: "Plan",
      number: "1",
      due_at: "2026-10-30T16:00:00Z",
    });
    const api = fakeApi({ project: project(), nodes: [task] });
    api.me.mockResolvedValue({
      id: "u1",
      name: "Lu",
      role: "admin",
      timezone: "Asia/Tokyo",
      effective_timezone: "Asia/Tokyo",
      hub_timezone: "Europe/Berlin",
    });
    render(<ProjectPage api={as(api)} />);
    fireEvent.click(await screen.findByRole("button", { name: /Topcoat/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Plan/ }));
    const due = (await screen.findByLabelText("Due", {
      selector: "input",
    })) as HTMLInputElement;
    await waitFor(() => expect(due.value).toBe("2026-10-31T01:00")); // 16:00 UTC is 01:00 the next day in Tokyo
  });
});

describe("export", () => {
  it("hands the file the hub made to the browser", async () => {
    const save = vi
      .spyOn(download, "saveBlob")
      .mockImplementation(() => undefined);
    const api = fakeApi({ project: project(), nodes: [] });
    render(<ProjectPage api={as(api)} />);
    fireEvent.click(await screen.findByRole("button", { name: /Topcoat/ }));
    await screen.findByRole("button", { name: "Project actions" });
    chooseProjectAction("Export (JSON)");
    await waitFor(() =>
      expect(api.downloadProjectExport).toHaveBeenCalledWith("p1", "json"),
    );
    expect(save).toHaveBeenCalledWith(expect.any(Blob), "project-topcoat.json");
    chooseProjectAction("Export (Markdown, zip)");
    await waitFor(() =>
      expect(api.downloadProjectExport).toHaveBeenCalledWith("p1", "markdown"),
    );
    expect(save).toHaveBeenLastCalledWith(
      expect.any(Blob),
      "project-topcoat.zip",
    );
  });

  it("says when the export did not work", async () => {
    vi.spyOn(download, "saveBlob").mockImplementation(() => undefined);
    const api = fakeApi({ project: project(), nodes: [] });
    api.downloadProjectExport.mockRejectedValueOnce(
      new ApiError(404, "not_found", "gone"),
    );
    render(<ProjectPage api={as(api)} />);
    fireEvent.click(await screen.findByRole("button", { name: /Topcoat/ }));
    await screen.findByRole("button", { name: "Project actions" });
    chooseProjectAction("Export (JSON)");
    expect(await screen.findByRole("alert")).toBeTruthy();
  });
});

const TEMPLATE: ProjectTemplate = {
  id: "tpl1",
  name: "Release",
  description: "The steps of a release",
  nodes: 12,
  has_dates: true,
  created_at: null,
};

describe("templates", () => {
  it("saves a project as a template", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    render(<ProjectPage api={as(api)} />);
    fireEvent.click(await screen.findByRole("button", { name: /Topcoat/ }));
    await screen.findByRole("button", { name: "Project actions" });
    chooseProjectAction("Save as template");
    fireEvent.change(screen.getByLabelText("Name of the template"), {
      target: { value: "Release" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save as template" }));
    await waitFor(() =>
      expect(api.saveProjectTemplate).toHaveBeenCalledWith({
        project_id: "p1",
        name: "Release",
      }),
    );
  });

  it("lists the templates, makes a project from one with a start, and deletes one", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    api.projectTemplates.mockResolvedValue({ templates: [TEMPLATE] });
    render(<ProjectPage api={as(api)} />);
    fireEvent.click(await screen.findByRole("button", { name: "Templates" }));
    expect(await screen.findByText("12 entries · with dates")).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", {
        name: "New project from this template: Release",
      }),
    );
    fireEvent.change(screen.getByLabelText("Title of the new project"), {
      target: { value: "Release 2" },
    });
    fireEvent.change(screen.getByLabelText("The earliest date falls on"), {
      target: { value: "2026-11-02T09:00" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create the project" }));
    await waitFor(() =>
      expect(api.createProjectFromTemplate).toHaveBeenCalledWith("tpl1", {
        title: "Release 2",
        start_at: "2026-11-02T09:00:00+01:00",
      }),
    );
    // the new project opens
    await waitFor(() => expect(api.projectProject).toHaveBeenCalledWith("p2"));
  });

  it("makes a project without dates when no start is given, and can delete a template", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    api.projectTemplates.mockResolvedValue({ templates: [TEMPLATE] });
    render(<ProjectPage api={as(api)} />);
    fireEvent.click(await screen.findByRole("button", { name: "Templates" }));
    fireEvent.click(
      await screen.findByRole("button", {
        name: "New project from this template: Release",
      }),
    );
    fireEvent.change(screen.getByLabelText("Title of the new project"), {
      target: { value: "Bare" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create the project" }));
    await waitFor(() =>
      expect(api.createProjectFromTemplate).toHaveBeenCalledWith("tpl1", {
        title: "Bare",
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Templates" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Delete template: Release" }),
    );
    await waitFor(() =>
      expect(api.deleteProjectTemplate).toHaveBeenCalledWith("tpl1"),
    );
    expect(await screen.findByText("Template deleted.")).toBeTruthy();
  });

  it("says that there is none yet", async () => {
    const api = fakeApi({ project: project(), nodes: [] });
    render(<ProjectPage api={as(api)} />);
    fireEvent.click(await screen.findByRole("button", { name: "Templates" }));
    expect(await screen.findByText(/No templates yet/)).toBeTruthy();
  });
});

describe("opened from the inbox", () => {
  it("lands in the project, on the task a reminder is about", async () => {
    const task = node({ id: "t1", title: "Pay the invoice", number: "1" });
    const api = fakeApi({ project: project(), nodes: [task] });
    render(<ProjectPage api={as(api)} project="p1" node="t1" />);
    expect(
      await screen.findByRole("heading", { name: "Topcoat", level: 2 }),
    ).toBeTruthy();
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: /Pay the invoice/, level: 3 }),
      ).toBeTruthy(),
    );
  });
});
