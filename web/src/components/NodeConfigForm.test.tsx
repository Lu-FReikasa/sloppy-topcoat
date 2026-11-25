import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { NodeConfigForm } from "./NodeConfigForm";

const CONFIG = {
  roles: ["gpu"],
  runner: { type: "llamaserver", backend: "auto", model_dirs: [] },
  watcher: { grace_seconds: 5, cooldown_seconds: 60, rules: [] },
};

function setup(putRoute: unknown) {
  const mock = mockFetch({ "PUT /api/nodes/n1/config": putRoute });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  const onSaved = vi.fn();
  render(<NodeConfigForm api={api} nodeId="n1" config={CONFIG} onSaved={onSaved} />);
  fireEvent.click(screen.getByRole("button", { name: "Einstellungen ändern" }));
  return { mock, onSaved };
}

describe("NodeConfigForm", () => {
  it("saves a changed backend and keeps the keys the form does not show", async () => {
    const { mock, onSaved } = setup({ config: CONFIG, config_hash: "a".repeat(64) });
    fireEvent.change(screen.getByLabelText("Backend"), { target: { value: "vulkan" } });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    const body = mock.calls[0]?.body as { runner: Record<string, unknown>; roles: string[] };
    expect(body.runner.backend).toBe("vulkan");
    expect(body.runner.type).toBe("llamaserver");
    expect(body.roles).toEqual(["gpu"]);
  });

  it("shows the hub's rejection and keeps the form open", async () => {
    const { onSaved } = setup(() =>
      jsonResponse(
        { detail: { code: "invalid_config", message: "node_config: runner/backend: bad" } },
        400,
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Vom Hub abgelehnt: node_config: runner/backend: bad",
    );
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Backend")).toBeTruthy();
  });

  it("refuses a malformed llama.cpp release before it reaches the hub", () => {
    const { mock } = setup({});
    fireEvent.change(screen.getByLabelText("llama.cpp-Version"), { target: { value: "11223" } });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    expect(screen.getByRole("alert").textContent).toMatch(/b11223/);
    expect(mock.calls).toHaveLength(0);
  });

  it("switches to the JSON expert view and back", () => {
    setup({});
    fireEvent.click(screen.getByRole("button", { name: "Expertenansicht (JSON)" }));
    fireEvent.click(screen.getByRole("button", { name: "Konfiguration bearbeiten" }));
    expect(screen.getByRole("textbox", { name: "Konfiguration (JSON)" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Einstellungen" }));
    expect(screen.getByLabelText("Backend")).toBeTruthy();
  });

  it("can be cancelled", () => {
    setup({});
    fireEvent.click(screen.getByRole("button", { name: "Abbrechen" }));
    expect(screen.queryByLabelText("Backend")).toBeNull();
  });
});
