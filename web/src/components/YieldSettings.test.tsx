import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { mockFetch } from "../test/mockFetch";
import { PowerSettings, YieldSettings } from "./YieldSettings";

const CONFIG = { roles: ["gpu"], runner: { type: "llamaserver" } };

function setup(ui: (api: ApiClient, onSaved: () => void) => React.ReactElement) {
  const mock = mockFetch({ "PUT /api/nodes/n1/config": { config: CONFIG, config_hash: "h" } });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  const onSaved = vi.fn();
  render(ui(api, onSaved));
  return { mock, onSaved };
}

describe("YieldSettings", () => {
  it("summarizes: on for a laptop, off for a node with a dedicated GPU", () => {
    setup((api, onSaved) => (
      <YieldSettings api={api} nodeId="n1" config={CONFIG} onSaved={onSaved} dedicatedGpu={false} />
    ));
    expect(screen.getByText(/^An: Der Node friert seine Modelle ein/)).toBeTruthy();
    expect(screen.getByText(/niedrigsten Prozesspriorität/)).toBeTruthy();
  });

  it("shows off with a dedicated GPU", () => {
    setup((api, onSaved) => (
      <YieldSettings api={api} nodeId="n1" config={CONFIG} onSaved={onSaved} dedicatedGpu />
    ));
    expect(screen.getByText(/^Aus:/)).toBeTruthy();
  });

  it("saves the section together with the rest of the config", async () => {
    const { mock, onSaved } = setup((api, cb) => (
      <YieldSettings api={api} nodeId="n1" config={CONFIG} onSaved={cb} dedicatedGpu />
    ));
    fireEvent.click(screen.getByRole("button", { name: "Einstellungen ändern" }));
    fireEvent.click(
      screen.getByRole("checkbox", {
        name: "Modelle einfrieren, solange der Rechner benutzt wird",
      }),
    );
    fireEvent.change(
      screen.getByRole("spinbutton", {
        name: "Als benutzt gilt bis so lange nach der letzten Eingabe (s)",
      }),
      { target: { value: "120" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    const body = mock.calls[0]?.body as Record<string, unknown>;
    expect(body.runner).toEqual(CONFIG.runner);
    expect(body.yield).toMatchObject({ enabled: true, idle_seconds: 120, low_priority: true });
  });

  it("refuses a value out of range", async () => {
    const { mock } = setup((api, cb) => (
      <YieldSettings api={api} nodeId="n1" config={CONFIG} onSaved={cb} dedicatedGpu={false} />
    ));
    fireEvent.click(screen.getByRole("button", { name: "Einstellungen ändern" }));
    fireEvent.change(
      screen.getByRole("spinbutton", {
        name: "CPU-Last anderer Programme, die als Nutzung zählt (%, 0 = aus)",
      }),
      {
        target: { value: "150" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Ungültiger Wert");
    expect(mock.calls).toHaveLength(0);
  });
});

describe("PowerSettings", () => {
  it("saves the profile and the sleep time in seconds", async () => {
    const { mock, onSaved } = setup((api, cb) => (
      <PowerSettings api={api} nodeId="n1" config={CONFIG} onSaved={cb} />
    ));
    expect(screen.getByText(/^Ausgewogen:/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Einstellungen ändern" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Profil" }), {
      target: { value: "saver" },
    });
    fireEvent.change(
      screen.getByRole("spinbutton", { name: "Ruhezustand nach Leerlauf (min, 0 = nie)" }),
      {
        target: { value: "30" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect((mock.calls[0]?.body as Record<string, unknown>).power).toEqual({
      profile: "saver",
      sleep_after_idle_seconds: 1800,
    });
  });
});
