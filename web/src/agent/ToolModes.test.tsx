import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { ToolModes } from "./ToolModes";

const SETTINGS = {
  tool_modes: { "qwen-7b": "json" },
  default_tool_mode: "auto",
  modes: ["auto", "native", "json"],
};

describe("ToolModes", () => {
  it("shows the mode per model, changes it and saves the whole map", async () => {
    const mock = mockFetch({
      "GET /api/agent/settings": SETTINGS,
      "PUT /api/agent/settings": (init?: RequestInit) =>
        jsonResponse({
          ...SETTINGS,
          tool_modes: JSON.parse(String(init?.body)).tool_modes,
        }),
    });
    render(
      <ToolModes api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} />,
    );
    const select = (await screen.findByLabelText(
      "qwen-7b",
    )) as HTMLSelectElement;
    expect(select.value).toBe("json");
    fireEvent.change(select, { target: { value: "native" } });
    fireEvent.change(screen.getByPlaceholderText("Modellname"), {
      target: { value: "gpt-oss-20b" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Hinzufügen" }));
    expect(screen.getByLabelText("gpt-oss-20b")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(screen.getByText("Gespeichert.")).toBeTruthy());
    const put = mock.calls.find((c) => c.method === "PUT");
    expect(put?.body).toEqual({
      tool_modes: { "qwen-7b": "native", "gpt-oss-20b": "native" },
    });
  });

  it("removes an entry", async () => {
    const admin = mockFetch({
      "GET /api/agent/settings": SETTINGS,
      "PUT /api/agent/settings": { ...SETTINGS, tool_modes: {} },
    });
    render(
      <ToolModes api={new ApiClient({ token: "t", fetchImpl: admin.fetch })} />,
    );
    await screen.findAllByLabelText("qwen-7b");
    fireEvent.click(screen.getAllByRole("button", { name: "Entfernen" })[0]!);
    fireEvent.click(screen.getAllByRole("button", { name: "Speichern" })[0]!);
    await waitFor(() =>
      expect(admin.calls.some((c) => c.method === "PUT")).toBe(true),
    );
    expect(admin.calls.find((c) => c.method === "PUT")?.body).toEqual({
      tool_modes: {},
    });
  });
});
