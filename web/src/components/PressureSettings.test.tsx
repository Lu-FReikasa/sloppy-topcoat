import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { PRESSURE_DEFAULTS } from "../pressure";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import { PressureSettings } from "./PressureSettings";

const CONFIG = {
  roles: ["gpu"],
  runner: { type: "llamaserver", model_dirs: [], backend: "auto" },
  residents: [{ model: "qwen-7b", priority: 50, auto_reload: true }],
  pressure: { ...PRESSURE_DEFAULTS },
};

function setup(config: Record<string, unknown> | null, putRoute: unknown = {}) {
  const mock = mockFetch({ "PUT /api/nodes/n1/config": putRoute });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  const onSaved = vi.fn();
  render(<PressureSettings api={api} nodeId="n1" config={config} onSaved={onSaved} />);
  return { mock, onSaved };
}

function field(name: string): HTMLInputElement {
  return screen.getByRole("spinbutton", { name }) as HTMLInputElement;
}

describe("PressureSettings", () => {
  it("summarizes the effective settings", () => {
    setup(CONFIG);
    expect(screen.getByText(/weniger als 1\.024 MB frei sind/)).toBeTruthy();
    expect(screen.getByText(/60 s lang frei ist/)).toBeTruthy();
  });

  it("shows off for configs without a pressure object", () => {
    setup({ roles: ["gpu"] });
    expect(screen.getByText(/^Aus:/)).toBeTruthy();
  });

  it("edits the settings with German labels and saves the whole config", async () => {
    const { mock, onSaved } = setup(CONFIG, { config: CONFIG, config_hash: "h" });
    fireEvent.click(screen.getByRole("button", { name: "Einstellungen ändern" }));
    const enabled = screen.getByRole("checkbox", {
      name: "VRAM automatisch freigeben, wenn andere Programme ihn brauchen",
    }) as HTMLInputElement;
    expect(enabled.checked).toBe(true);
    const free = field("Freigeben, wenn weniger VRAM frei ist als (MB)");
    expect(free.value).toBe("1024");
    fireEvent.change(free, { target: { value: "2048" } });
    fireEvent.change(field("Wieder laden nur bis zu dieser GPU-Last anderer Programme (%)"), {
      target: { value: "12.5" },
    });
    fireEvent.click(
      screen.getByRole("checkbox", { name: "Auch wieder laden, während eine Spiele-Regel greift" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    const body = mock.calls[0]?.body as Record<string, unknown>;
    expect(body.residents).toEqual(CONFIG.residents); // the rest of the config is kept
    expect(body.pressure).toEqual({
      ...PRESSURE_DEFAULTS,
      unload_below_free_mb: 2048,
      reload_max_foreign_gpu_util: 12.5,
      reload_while_rule_matches: true,
    });
    expect(screen.getByRole("status").textContent).toMatch(/gespeichert/);
  });

  it("rejects values outside the schema range before saving", () => {
    const { mock } = setup(CONFIG);
    fireEvent.click(screen.getByRole("button", { name: "Einstellungen ändern" }));
    fireEvent.change(field("Prüfintervall (s)"), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    expect(screen.getByRole("alert").textContent).toBe(
      "Ungültiger Wert bei „Prüfintervall (s)“ (erlaubt: 1–60).",
    );
    fireEvent.change(field("Prüfintervall (s)"), { target: { value: "2" } });
    fireEvent.change(field("RAM: freigeben, wenn weniger frei ist als (MB)"), {
      target: { value: "1.5" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    expect(screen.getByRole("alert").textContent).toMatch(/RAM: freigeben/);
    expect(mock.calls).toHaveLength(0);
  });

  it("shows the hub's validation error", async () => {
    setup(
      CONFIG,
      jsonResponse({ detail: { code: "invalid_config", message: "pressure: bad" } }, 400),
    );
    fireEvent.click(screen.getByRole("button", { name: "Einstellungen ändern" }));
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Vom Hub abgelehnt: pressure: bad");
  });
});
