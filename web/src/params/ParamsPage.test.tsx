import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import i18n from "../i18n";
import type { ParamView, ParamsView } from "../types";
import { ParamsPage, asText, parseInput } from "./ParamsPage";

afterEach(cleanup);

function param(over: Partial<ParamView>): ParamView {
  return {
    key: "agent_max_turns",
    group: "agent",
    kind: "int",
    text: "Model turns one check may take.",
    unit: "",
    low: 1,
    high: 200,
    choices: [],
    default: 30,
    value: 30,
    overridden: false,
    ...over,
  };
}

const VIEW: ParamsView = {
  groups: ["agent", "chat"],
  params: [
    param({}),
    param({ key: "agent_tool_mode", kind: "enum", choices: ["auto", "native", "json"], value: "auto", default: "auto", low: null, high: null }),
    param({ key: "review_enabled", group: "chat", kind: "bool", value: true, default: true, low: null, high: null }),
    param({ key: "chat_sampling", group: "chat", kind: "json", value: { qwen: { temperature: 0.4 } }, default: {}, overridden: true, low: null, high: null }),
  ],
};

function setup(over: Partial<Record<keyof ApiClient, unknown>> = {}) {
  const api = {
    params: vi.fn().mockResolvedValue(VIEW),
    setParam: vi.fn().mockResolvedValue({ key: "x", value: 1 }),
    resetParam: vi.fn().mockResolvedValue({ key: "x", value: 1 }),
    paramHistory: vi.fn().mockResolvedValue([]),
    getChatPolicy: vi.fn().mockResolvedValue({ policy: {}, levels: {}, numbers: {} }),
    putChatPolicy: vi.fn().mockResolvedValue({ policy: {}, levels: {}, numbers: {} }),
    exportParams: vi.fn().mockResolvedValue({ version: 1, values: { agent_max_turns: 7 } }),
    importParams: vi.fn().mockResolvedValue({ applied: ["agent_max_turns"] }),
    ...over,
  };
  render(
    <I18nextProvider i18n={i18n}>
      <ParamsPage api={api as unknown as ApiClient} />
    </I18nextProvider>,
  );
  return api;
}

describe("the helpers", () => {
  it("shows a structure as JSON text and anything else as plain text", () => {
    expect(asText(5)).toBe("5");
    expect(asText("auto")).toBe("auto");
    expect(asText(null)).toBe("");
    expect(JSON.parse(asText({ a: { b: 1 } }))).toEqual({ a: { b: 1 } });
  });

  it("reads a number, a structure and a word as what the parameter is", () => {
    const num = param({});
    expect(parseInput(num, " 12 ")).toEqual({ ok: true, value: 12 });
    expect(parseInput(num, "")).toEqual({ ok: false, why: "notNumber" });
    expect(parseInput(num, "twelve")).toEqual({ ok: false, why: "notNumber" });
    const json = param({ kind: "json" });
    expect(parseInput(json, '{"a":1}')).toEqual({ ok: true, value: { a: 1 } });
    expect(parseInput(json, "{oops")).toEqual({ ok: false, why: "notJson" });
  });
});

describe("ParamsPage", () => {
  it("lists the parameters by group, each with its words, value and default", async () => {
    setup();
    const row = await screen.findByTestId("param-agent_max_turns");
    expect(within(row).getByText("Modellrunden, die eine Prüfung höchstens braucht.")).toBeTruthy();
    expect(within(row).getByText(/Standard: 30/)).toBeTruthy();
    expect(within(row).getByText(/Erlaubt: 1 bis 200/)).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Prüfungen" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Chat" })).toBeTruthy();
    expect(screen.getByTestId("param-chat_sampling").textContent).toContain("geändert");
  });

  it("saves a changed number and reloads the list", async () => {
    const api = setup();
    const row = await screen.findByTestId("param-agent_max_turns");
    const save = within(row).getByRole("button", { name: "Speichern" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true); // nothing changed yet
    fireEvent.change(within(row).getByLabelText("agent_max_turns"), { target: { value: "12" } });
    fireEvent.click(save);
    await waitFor(() => expect(api.setParam).toHaveBeenCalledWith("agent_max_turns", 12));
    await waitFor(() => expect(api.params).toHaveBeenCalledTimes(2));
    expect((await within(row).findByRole("status")).textContent).toBe("Gespeichert.");
  });

  it("does not send a value that is not a number, and says so", async () => {
    const api = setup();
    const row = await screen.findByTestId("param-agent_max_turns");
    fireEvent.change(within(row).getByLabelText("agent_max_turns"), { target: { value: "viele" } });
    fireEvent.click(within(row).getByRole("button", { name: "Speichern" }));
    expect((await within(row).findByRole("alert")).textContent).toBe("Das ist keine Zahl.");
    expect(api.setParam).not.toHaveBeenCalled();
  });

  it("shows what the hub refused", async () => {
    const refused = Object.assign(new Error("agent_max_turns must be between 1 and 200"), {
      code: "range",
    });
    setup({ setParam: vi.fn().mockRejectedValue(refused) });
    const row = await screen.findByTestId("param-agent_max_turns");
    fireEvent.change(within(row).getByLabelText("agent_max_turns"), { target: { value: "999" } });
    fireEvent.click(within(row).getByRole("button", { name: "Speichern" }));
    expect((await within(row).findByRole("alert")).textContent).toContain("must be between 1 and 200");
  });

  it("saves a switch and a choice at once", async () => {
    const api = setup();
    const sw = within(await screen.findByTestId("param-review_enabled")).getByRole("checkbox");
    fireEvent.click(sw);
    await waitFor(() => expect(api.setParam).toHaveBeenCalledWith("review_enabled", false));
    const pick = within(screen.getByTestId("param-agent_tool_mode")).getByLabelText("agent_tool_mode");
    fireEvent.change(pick, { target: { value: "json" } });
    await waitFor(() => expect(api.setParam).toHaveBeenCalledWith("agent_tool_mode", "json"));
  });

  it("offers the way back only for a parameter that was changed", async () => {
    const api = setup();
    expect(
      within(await screen.findByTestId("param-agent_max_turns")).queryByRole("button", {
        name: "Zurück zum Standard",
      }),
    ).toBeNull();
    const row = screen.getByTestId("param-chat_sampling");
    fireEvent.click(within(row).getByRole("button", { name: "Zurück zum Standard" }));
    await waitFor(() => expect(api.resetParam).toHaveBeenCalledWith("chat_sampling"));
  });

  it("loads the history when it is opened", async () => {
    const api = setup({
      paramHistory: vi.fn().mockResolvedValue([
        { id: 2, old: 30, new: 12, reset: false, by: "u", at: "2026-10-21T10:00:00Z" },
        { id: 1, old: 12, new: null, reset: true, by: "u", at: "2026-10-20T10:00:00Z" },
      ]),
    });
    const row = await screen.findByTestId("param-agent_max_turns");
    const details = row.querySelector("details") as HTMLDetailsElement;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    await waitFor(() => expect(api.paramHistory).toHaveBeenCalledWith("agent_max_turns"));
    expect(await within(row).findByText(/30 → 12/)).toBeTruthy();
    expect(within(row).getByText(/auf Standard zurückgesetzt/)).toBeTruthy();
  });

  it("shows the preset and puts one back, refusing text that is not a preset", async () => {
    const api = setup();
    await screen.findByTestId("param-agent_max_turns");
    fireEvent.click(screen.getByRole("button", { name: "Voreinstellung anzeigen" }));
    const field = (await screen.findByLabelText("Voreinstellung (JSON)")) as HTMLTextAreaElement;
    await waitFor(() => expect(field.value).toContain('"agent_max_turns": 7'));
    fireEvent.click(screen.getByRole("button", { name: "Voreinstellung einspielen" }));
    await waitFor(() =>
      expect(api.importParams).toHaveBeenCalledWith({ version: 1, values: { agent_max_turns: 7 } }),
    );
    expect(await screen.findByText("Übernommen: 1")).toBeTruthy();
    fireEvent.change(field, { target: { value: "nonsense" } });
    fireEvent.click(screen.getByRole("button", { name: "Voreinstellung einspielen" }));
    expect((await screen.findAllByRole("alert")).some((a) => a.textContent === "Das ist keine Voreinstellung.")).toBe(true);
  });
});
