import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { ConfigEditor } from "./ConfigEditor";

const CONFIG = {
  roles: ["gpu"],
  runner: { type: "llamaserver", model_dirs: [], backend: "auto" },
  watcher: { grace_seconds: 5, cooldown_seconds: 60, rules: [] },
};

function setup(putRoute: unknown) {
  const mock = mockFetch({ "PUT /api/nodes/n1/config": putRoute });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  const onSaved = vi.fn();
  render(<ConfigEditor api={api} nodeId="n1" config={CONFIG} onSaved={onSaved} />);
  fireEvent.click(screen.getByRole("button", { name: "Konfiguration bearbeiten" }));
  const textarea = screen.getByRole("textbox", { name: "Konfiguration (JSON)" }) as HTMLTextAreaElement;
  return { mock, onSaved, textarea };
}

describe("ConfigEditor", () => {
  it("shows pretty JSON with hints and saves via PUT", async () => {
    const edited = { ...CONFIG, runner: { ...CONFIG.runner, backend: "vulkan" } };
    const { mock, onSaved, textarea } = setup({ config: edited, config_hash: "a".repeat(64) });
    expect(textarea.value).toBe(JSON.stringify(CONFIG, null, 2));
    expect(screen.getByText(/„auto“, „cuda“, „vulkan“ oder „cpu“/)).toBeTruthy();
    expect(screen.getByText('{"process": "spiel.exe"}')).toBeTruthy();
    expect(
      screen.getByText('{"path_prefix": "D:\\\\SteamLibrary\\\\steamapps\\\\common\\\\"}'),
    ).toBeTruthy();

    fireEvent.change(textarea, { target: { value: JSON.stringify(edited) } });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(mock.calls[0]?.body).toEqual(edited);
    expect(screen.getByRole("status").textContent).toMatch(/gespeichert/);
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("rejects invalid JSON locally", () => {
    const { mock, textarea } = setup({});
    fireEvent.change(textarea, { target: { value: "{kaputt" } });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    expect(screen.getByRole("alert").textContent).toMatch(/^Ungültiges JSON/);
    expect(mock.calls).toHaveLength(0);
  });

  it("shows the hub's validation error", async () => {
    const { onSaved } = setup(() =>
      jsonResponse(
        {
          detail: {
            code: "invalid_config",
            message: "node_config: runner/backend: 'rocm' is not one of ['auto', 'cuda', 'vulkan', 'cpu']",
          },
        },
        400,
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Vom Hub abgelehnt: node_config: runner/backend: 'rocm' is not one of ['auto', 'cuda', 'vulkan', 'cpu']",
    );
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox")).toBeTruthy();
  });

  it("can be cancelled", () => {
    setup({});
    fireEvent.click(screen.getByRole("button", { name: "Abbrechen" }));
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});
