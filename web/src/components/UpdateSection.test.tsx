import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { mockFetch } from "../test/mockFetch";
import { makeNode } from "../test/fixtures";
import type { NodeUpdate } from "../types";
import { UpdateSection } from "./UpdateSection";

const READY: NodeUpdate = {
  channel: "stable",
  capable: true,
  updater: "ok",
  key_ids: ["0123456789abcdef"],
  available: "0.3.0",
  newer: true,
  action_allowed: true,
};
const CONFIG = { roles: ["gpu"], allowed_actions: ["models.list"] };

function setup(update: Partial<NodeUpdate> | null, routes: Record<string, unknown> = {}) {
  const mock = mockFetch(routes);
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  const onChanged = vi.fn();
  const node = makeNode(update === null ? {} : { update: { ...READY, ...update } });
  render(<UpdateSection api={api} node={node} config={CONFIG} onChanged={onChanged} />);
  return { mock, onChanged };
}

afterEach(() => vi.restoreAllMocks());

describe("UpdateSection", () => {
  it("shows nothing for a hub without the update channel", () => {
    setup(null);
    expect(screen.queryByText("Update")).toBeNull();
  });

  it("shows the versions and starts the update after a confirmation", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { mock, onChanged } = setup({}, { "POST /api/nodes/n1/actions": { ok: true } });
    expect(screen.getByTestId("update-facts").textContent).toContain("0.1.0");
    expect(screen.getByTestId("update-facts").textContent).toContain("0.3.0");
    fireEvent.click(screen.getByRole("button", { name: "Auf 0.3.0 aktualisieren" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(mock.calls[0]?.body).toEqual({ name: "node.update", args: {} });
    expect(screen.getByRole("status").textContent).toMatch(/holt das Update/);
  });

  it("names the updater's version and warns when it is older than the node", () => {
    setup({ updater_version: "0.0.9", updater_behind: true });
    expect(screen.getByTestId("update-facts").textContent).toContain("0.0.9");
    const warn = screen.getByTestId("update-updater-behind");
    expect(warn.textContent).toContain("0.0.9");
    expect(warn.textContent).toContain("topcoat-node install");
  });

  it("does not warn about an updater that is current or unknown", () => {
    setup({ updater_version: "0.1.0", updater_behind: false });
    expect(screen.queryByTestId("update-updater-behind")).toBeNull();
  });

  it("does nothing when the confirmation is declined", () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const { mock } = setup({});
    fireEvent.click(screen.getByRole("button", { name: "Auf 0.3.0 aktualisieren" }));
    expect(mock.calls).toHaveLength(0);
  });

  it("shows the node's refusal", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    setup(
      {},
      {
        "POST /api/nodes/n1/actions": {
          ok: false,
          error: { code: "not_implemented", message: "the updater service is missing" },
        },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Auf 0.3.0 aktualisieren" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/updater service/));
  });

  it("explains what is missing instead of offering a button", () => {
    setup({ updater: "missing" });
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByTestId("update-state").textContent).toMatch(/Updater-Dienst läuft/);
  });

  it("explains a build without key", () => {
    setup({ capable: false });
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByTestId("update-state").textContent).toMatch(/keinen Update-Schlüssel/);
  });

  it("allows the action by adding it to the config", async () => {
    const { mock, onChanged } = setup(
      { action_allowed: false },
      { "PUT /api/nodes/n1/config": { config: CONFIG, config_hash: "h" } },
    );
    expect(screen.queryByRole("button", { name: /aktualisieren/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Updates auf diesem Node erlauben" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(mock.calls[0]?.body).toEqual({
      roles: ["gpu"],
      allowed_actions: ["models.list", "node.update"],
    });
  });

  it("says when the node is current", () => {
    setup({ newer: false });
    expect(screen.getByTestId("update-state").textContent).toMatch(/neuesten Version/);
  });
});
