import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { ApplicationsView, SchedulingView } from "../types";
import {
  SchedulingPage,
  cleanRows,
  problemsOf,
} from "./SchedulingPage";

afterEach(cleanup);

const node = (name: string, extra: Record<string, unknown> = {}) => ({
  id: `id-${name}`,
  name,
  state: "available" as const,
  connected: true,
  kind: "compute" as const,
  loaded: [],
  models: ["gemma-12b", "qwen-30b"],
  ...extra,
});

const VIEW: SchedulingView = {
  nodes: [
    node("node-amd", { loaded: ["gemma-12b"] }),
    node("node-laptop", { models: ["qwen-30b"] }),
    node("tools-alpha", { kind: "tools", models: [] }),
  ],
  paused_until: null,
  embedding_model: "",
  embedding_choices: ["bge-m3-FP16", "bge-m3-q8_0"],
  customised: false,
  history: [
    {
      ts: "2026-10-08T10:00:00Z",
      kind: "table",
      actor: "alice",
      detail: "research: gemma-12b on node-amd",
      ok: true,
    },
    {
      ts: "2026-10-08T09:00:00Z",
      kind: "run",
      actor: "alice",
      detail: "care: no_node_available",
      ok: false,
    },
  ],
};

const TABLE: ApplicationsView = {
  keys: ["chat", "research", "dev", "admin", "project", "check", "background", "api"],
  system_keys: ["check", "background", "api"],
  priorities: ["high", "normal", "low"],
  rows: {
    research: { models: ["gemma-12b", "qwen-30b"], nodes: ["node-amd"] },
    dev: { priority: "high" },
  },
  preview: {
    chat: { model: "gemma-12b", node: "node-amd", reason: "the hub's list" },
    research: {
      model: "gemma-12b",
      node: "node-amd",
      reason: "the first of the row research of the table that can take it now",
    },
    check: { model: null, node: null, reason: "" },
    api: { model: null, node: null, reason: "requests name their model" },
  },
};

function api(
  over: Partial<Record<keyof ApiClient, unknown>> = {},
  view: SchedulingView = VIEW,
) {
  return {
    getScheduling: vi.fn().mockResolvedValue(view),
    getApplications: vi.fn().mockResolvedValue(TABLE),
    putApplications: vi.fn().mockResolvedValue(TABLE),
    putScheduling: vi.fn().mockResolvedValue({ ...view, customised: true }),
    resetScheduling: vi.fn().mockResolvedValue(view),
    runBackgroundTask: vi.fn().mockResolvedValue({ started: true }),
    pauseBackground: vi
      .fn()
      .mockResolvedValue({ ...view, paused_until: "2026-10-08T12:00:00Z" }),
    ...over,
  } as unknown as ApiClient & {
    putApplications: ReturnType<typeof vi.fn>;
    putScheduling: ReturnType<typeof vi.fn>;
    pauseBackground: ReturnType<typeof vi.fn>;
    runBackgroundTask: ReturnType<typeof vi.fn>;
    resetScheduling: ReturnType<typeof vi.fn>;
  };
}

async function shown(a: ApiClient) {
  render(<SchedulingPage api={a} />);
  await screen.findByTestId("row-research");
}

describe("the table of programs", () => {
  it("shows a row for every program and system row with what it comes to now", async () => {
    await shown(api());
    for (const key of TABLE.keys) screen.getByTestId(`row-${key}`);
    expect(screen.getByTestId("row-research-now").textContent).toContain(
      "gemma-12b @ node-amd",
    );
    // a row that sets nothing says what the hub does then
    expect(screen.getByTestId("row-admin-empty").textContent).toContain(
      "größte Modell",
    );
    expect(screen.queryByTestId("row-research-empty")).toBeNull();
    // a priority alone decides nothing
    screen.getByTestId("row-dev-empty");
    // the system rows are named, the gateway row has no models
    within(screen.getByTestId("row-check")).getByText("Zweiter Blick");
    expect(
      within(screen.getByTestId("row-api")).queryByText("Modelle, das beste zuerst"),
    ).toBeNull();
  });

  it("adds a model and a machine and saves the whole table", async () => {
    const a = api();
    await shown(a);
    const row = within(screen.getByTestId("row-chat"));
    fireEvent.change(row.getByLabelText(/^Modell für .*Chat/), {
      target: { value: "qwen-30b" },
    });
    fireEvent.click(row.getByRole("button", { name: /Modell zu .*Chat.* hinzufügen/ }));
    fireEvent.change(row.getByLabelText(/^Zu .*Chat: Maschinen.* hinzufügen/), {
      target: { value: "node-laptop" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(a.putApplications).toHaveBeenCalledTimes(1));
    // the rows that were not touched go back as they were: a row left out would be cleared
    expect(a.putApplications.mock.calls[0]![0]).toEqual({
      chat: { models: ["qwen-30b"], nodes: ["node-laptop"] },
      research: { models: ["gemma-12b", "qwen-30b"], nodes: ["node-amd"] },
      dev: { priority: "high" },
    });
  });

  it("moves an entry by keyboard and removes one", async () => {
    const a = api();
    await shown(a);
    const row = within(screen.getByTestId("row-research"));
    fireEvent.click(
      row.getByRole("button", { name: /Eintrag 2 von .*Modelle nach oben/ }),
    );
    fireEvent.click(
      row.getByRole("button", { name: /Eintrag 1 von .*Maschinen entfernen/ }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(a.putApplications).toHaveBeenCalled());
    expect(a.putApplications.mock.calls[0]![0].research).toEqual({
      models: ["qwen-30b", "gemma-12b"],
    });
  });

  it("sets the priority of a row", async () => {
    const a = api();
    await shown(a);
    const row = within(screen.getByTestId("row-chat"));
    fireEvent.change(row.getByLabelText(/^Priorität von/), {
      target: { value: "low" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(a.putApplications).toHaveBeenCalled());
    expect(a.putApplications.mock.calls[0]![0].chat).toEqual({
      priority: "low",
    });
  });

  it("offers 'same as the answer' to the second look and not to a program", async () => {
    await shown(api());
    const check = within(screen.getByTestId("row-check"));
    expect(
      check.getByRole("option", { name: "dasselbe Modell wie die Antwort" }),
    ).toBeTruthy();
    const chat = within(screen.getByTestId("row-chat"));
    expect(
      chat.queryByRole("option", { name: "dasselbe Modell wie die Antwort" }),
    ).toBeNull();
  });

  it("warns when no machine of a row has a model", async () => {
    const a = api();
    await shown(a);
    const row = within(screen.getByTestId("row-chat"));
    fireEvent.change(row.getByLabelText(/^Modell für .*Chat/), {
      target: { value: "ghost-70b" },
    });
    fireEvent.click(row.getByRole("button", { name: /Modell zu .*Chat.* hinzufügen/ }));
    expect(row.getByText(/Modell ghost-70b/)).toBeTruthy();
  });

  it("shows the hub's refusal and keeps what was typed", async () => {
    const a = api({
      putApplications: vi
        .fn()
        .mockRejectedValue(new Error("applications.chat: node-nowhere")),
    });
    await shown(a);
    fireEvent.change(
      within(screen.getByTestId("row-chat")).getByLabelText(/^Priorität von/),
      { target: { value: "high" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toContain("node-nowhere");
    expect(
      (screen.getByRole("button", { name: "Speichern" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });

  it("discards the draft", async () => {
    await shown(api());
    const save = () =>
      screen.getByRole("button", { name: "Speichern" }) as HTMLButtonElement;
    expect(save().disabled).toBe(true);
    fireEvent.change(
      within(screen.getByTestId("row-chat")).getByLabelText(/^Priorität von/),
      { target: { value: "high" } },
    );
    expect(save().disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Verwerfen" }));
    expect(save().disabled).toBe(true);
  });
});

describe("the rules that stay", () => {
  it("saves the embedding model", async () => {
    const a = api();
    await shown(a);
    fireEvent.change(screen.getByTestId("scheduling-embedding"), {
      target: { value: "bge-m3-q8_0" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Einbettungsmodell speichern" }),
    );
    await waitFor(() => expect(a.putScheduling).toHaveBeenCalledTimes(1));
    expect(a.putScheduling.mock.calls[0]![0]).toEqual({
      embedding_model: "bge-m3-q8_0",
    });
  });

  it("pauses background work and resumes it", async () => {
    const a = api();
    await shown(a);
    fireEvent.click(screen.getByRole("button", { name: "Pause: 2 Std." }));
    await waitFor(() => expect(a.pauseBackground).toHaveBeenCalledWith(120));
  });

  it("starts a job by hand, not while paused", async () => {
    const a = api();
    await shown(a);
    fireEvent.click(screen.getByRole("button", { name: "Gedächtnis pflegen" }));
    await waitFor(() => expect(a.runBackgroundTask).toHaveBeenCalledWith("care"));
    await screen.findByText("Gestartet: Gedächtnis pflegen.");
    cleanup();
    const paused = api({}, { ...VIEW, paused_until: "2026-10-08T12:00:00Z" });
    await shown(paused);
    expect(
      (screen.getByRole("button", { name: "Chats auswerten" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("reads the history, with the change of the table in it, closed at first", async () => {
    await shown(api());
    const history = screen.getByTestId("scheduling-history") as HTMLDetailsElement;
    expect(history.open).toBe(false);
    expect(history.textContent).toContain("Tabelle der Anwendungen geändert");
    expect(history.textContent).toContain("fehlgeschlagen");
  });

  it("resets only after a confirmation, and says the table goes too", async () => {
    const a = api({}, { ...VIEW, customised: true });
    await shown(a);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const reset = screen.getByRole("button", {
      name: "Auf Hub-Konfiguration zurücksetzen",
    });
    fireEvent.click(reset);
    expect(a.resetScheduling).not.toHaveBeenCalled();
    expect(confirm.mock.calls[0]![0]).toContain("Tabelle");
    confirm.mockReturnValue(true);
    fireEvent.click(reset);
    await waitFor(() => expect(a.resetScheduling).toHaveBeenCalledTimes(1));
    confirm.mockRestore();
  });
});

describe("helpers", () => {
  it("drops a row without any setting", () => {
    expect(
      cleanRows({
        chat: { models: [], nodes: [] },
        dev: { priority: "high" },
        
        admin: {},
      }),
    ).toEqual({ dev: { priority: "high" } });
  });

  it("finds the models that none of the row's machines has", () => {
    const nodes = VIEW.nodes;
    expect(problemsOf({ models: ["gemma-12b"], nodes: ["node-laptop"] }, nodes)).toEqual([
      "gemma-12b",
    ]);
    expect(problemsOf({ models: ["gemma-12b"], nodes: ["node-laptop", "all"] }, nodes)).toEqual(
      [],
    );
    expect(problemsOf({ models: ["gemma-12b"] }, nodes)).toEqual([]);
    // the model of the answer is not a name to look for, and a tools machine has no models
    expect(problemsOf({ models: ["same_as_answer"], nodes: ["tools-alpha"] }, nodes)).toEqual([]);
  });
});
