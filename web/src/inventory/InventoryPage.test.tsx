import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { mockFetch } from "../test/mockFetch";
import type { InventoryNode } from "../types";
import { InventoryPage } from "./InventoryPage";

const OK = { state: "ok", error: null, read_at: "2026-10-03T10:00:00Z", age_seconds: 20, truncated: false, current: true } as const;

function node(over: Partial<InventoryNode> = {}): InventoryNode {
  return {
    node: { id: "n1", name: "vm-docker1" },
    online: true,
    status: "ok",
    error: null,
    attempted_at: "2026-10-03T10:00:00Z",
    collected_at: "2026-10-03T10:00:00Z",
    age_seconds: 20,
    stale: false,
    current: true,
    sections: { host: OK, containers: OK, services: OK },
    host: { os: "Ubuntu 24.04" },
    containers: [
      { kind: "container", name: "plex", state: "running", detail: { image: "plexinc/pms", compose_project: "media" }, gone: false },
      { kind: "container", name: "old", state: "running", gone: true, gone_at: "2026-10-03T09:00:00Z" },
    ],
    services: [{ kind: "service", name: "docker.service", state: "active", detail: { description: "Docker Engine" }, gone: false }],
    compose: [{ kind: "compose", name: "media", state: "running", services: 1, running: 1, containers: ["plex"], gone: false }],
    ...over,
  };
}

function show(nodes: InventoryNode[], extra: Record<string, unknown> = {}) {
  const mock = mockFetch({
    "GET /api/inventory": { nodes, stale_after_seconds: 900 },
    ...extra,
  });
  render(<InventoryPage api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} />);
  return mock;
}

describe("InventoryPage", () => {
  it("lists containers, compose projects and services and says the look is current", async () => {
    show([node()]);
    const card = await screen.findByTestId("inventory-node");
    expect(card.textContent).toContain("vm-docker1");
    expect(within(card).getByTestId("inventory-verdict").textContent).toBe("Aktuell");
    expect(within(card).getByTestId("inventory-container").textContent).toContain("plex");
    expect(within(card).getByTestId("inventory-container").textContent).toContain("plexinc/pms");
    expect(within(card).getByTestId("inventory-compose").textContent).toContain("1 von 1 laufen");
    expect(within(card).getByTestId("inventory-service").textContent).toContain("docker.service");
    expect(card.textContent).toContain("vor 20 s");
  });

  it("keeps what disappeared out of the list until asked", async () => {
    show([node()]);
    const containers = await screen.findByTestId("inventory-container");
    expect(containers.textContent).not.toContain("old");
    fireEvent.click(within(containers).getByRole("button", { name: /1 verschwundene/ }));
    expect(screen.getByTestId("inventory-container").textContent).toContain("old");
  });

  it("never shows an offline node or an old look as current", async () => {
    show([
      node({ online: false, current: false }),
      node({ node: { id: "n2", name: "old-one" }, stale: true, current: false, age_seconds: 7200 }),
    ]);
    const cards = await screen.findAllByTestId("inventory-node");
    expect(within(cards[0]!).getByTestId("inventory-verdict").textContent).toContain("Offline");
    expect(cards[0]!.textContent).toContain("letzte bekannte Stand");
    expect(within(cards[0]!).getByRole("button", { name: "Jetzt lesen" }).hasAttribute("disabled")).toBe(true);
    expect(within(cards[1]!).getByTestId("inventory-verdict").textContent).toBe("Veraltet");
    expect(cards[1]!.textContent).toContain("vor 2 Std.");
  });

  it("says which source could not be read and keeps showing the earlier list", async () => {
    show([
      node({
        status: "partial",
        current: false,
        sections: { host: OK, containers: { ...OK, state: "failed", error: "Docker API answered 500", current: false }, services: OK },
      }),
    ]);
    const note = await screen.findByTestId("inventory-failed-section");
    expect(note.textContent).toContain("Die Container konnten beim letzten Blick nicht gelesen werden");
    expect(note.textContent).toContain("Docker API answered 500");
    expect(screen.getByTestId("inventory-container").textContent).toContain("plex");
  });

  it("explains a node that does not offer the inventory", async () => {
    show([node({ status: "unavailable", error: "inventory.snapshot is not switched on for this node", collected_at: null, age_seconds: null, current: false, containers: [], services: [], compose: [] })]);
    expect((await screen.findByTestId("inventory-unavailable")).textContent).toContain("not switched on");
    expect(screen.getByTestId("inventory-verdict").textContent).toBe("Nicht angeboten");
  });

  it("reads a node on demand", async () => {
    const mock = show([node()], {
      "POST /api/inventory/nodes/n1/refresh": node({
        containers: [{ kind: "container", name: "fresh", state: "running", gone: false }],
      }),
    });
    fireEvent.click(await screen.findByRole("button", { name: "Jetzt lesen" }));
    await waitFor(() => expect(screen.getByTestId("inventory-container").textContent).toContain("fresh"));
    expect(mock.calls.some((c: { method: string; url: string }) => c.method === "POST" && c.url === "/api/inventory/nodes/n1/refresh")).toBe(true);
  });

  it("tells a user without access that nothing is shared yet", async () => {
    show([]);
    expect((await screen.findByText(/Ein Admin muss dir das Inventar/)).textContent).toBeTruthy();
  });

  describe("buttons next to a container or a stack", () => {
    const can = { read: ["compose.logs", "docker.logs"], change: ["compose.restart", "docker.restart"] };
    const pending = {
      id: "p1",
      task_id: null,
      origin: "manual",
      node: "vm-docker1",
      capability: "docker.restart",
      input: { container: "plex" },
      reason: "reason text",
      effect: "Container plex is stopped and started again.",
      who_notices: "Everyone who uses plex.",
      way_back: "Nothing to undo.",
      evidence: [],
      hash: "h".repeat(64),
      status: "pending",
      expires_at: "2026-10-03T11:00:00Z",
      decided_at: null,
      result: null,
      created_at: "2026-10-03T10:30:00Z",
    };

    it("shows no button the user may not use, and none for a node that is offline", async () => {
      show([node(), node({ node: { id: "n2", name: "off" }, online: false, current: false, can })]);
      const cards = await screen.findAllByTestId("inventory-node");
      expect(within(cards[0]!).queryByRole("button", { name: "Logs" })).toBeNull();
      expect(within(cards[0]!).queryByRole("button", { name: /Neu starten/ })).toBeNull();
      for (const b of within(cards[1]!).getAllByRole("button", { name: "Logs" })) expect(b.hasAttribute("disabled")).toBe(true);
    });

    it("reads the logs of a container through docker.logs", async () => {
      const mock = show([node({ can })], {
        "POST /api/nodes/n1/capabilities/docker.logs": {
          ok: true,
          output: { log: "2026-10-03T10:00:00Z hello from plex", lines: 1, truncated: false, masked: 0 },
        },
      });
      const containers = await screen.findByTestId("inventory-container");
      fireEvent.click(within(containers).getByRole("button", { name: "Logs" }));
      expect((await screen.findByTestId("log-text")).textContent).toContain("hello from plex");
      const call = mock.calls.find((c: { url: string }) => c.url.endsWith("/docker.logs"));
      expect(call?.body).toEqual({ input: { container: "plex", lines: 100 } });
    });

    it("reads the logs of a whole stack through compose.logs with the filter", async () => {
      const mock = show([node({ can })], {
        "POST /api/nodes/n1/capabilities/compose.logs": {
          ok: true,
          output: { log: "x [plex] ERROR boom", lines: 1, truncated: true, masked: 2 },
        },
      });
      const stacks = await screen.findByTestId("inventory-compose");
      fireEvent.click(within(stacks).getByRole("button", { name: "Logs" }));
      const text = await screen.findByTestId("log-text");
      expect(text.textContent).toContain("[plex] ERROR boom");
      expect(screen.getByTestId("log-meta").textContent).toContain("Gekürzt");
      fireEvent.change(screen.getByPlaceholderText("z. B. error"), { target: { value: "error" } });
      fireEvent.click(screen.getByRole("button", { name: "Neu lesen" }));
      await waitFor(() => expect(mock.calls.filter((c: { url: string }) => c.url.endsWith("/compose.logs")).length).toBe(2));
      const last = mock.calls.filter((c: { url: string }) => c.url.endsWith("/compose.logs")).pop();
      expect(last?.body).toEqual({ input: { project: "media", lines: 100, contains: "error" } });
    });

    it("says why the logs could not be read", async () => {
      show([node({ can })], {
        "POST /api/nodes/n1/capabilities/docker.logs": { ok: false, error: { code: "x", message: "no such container" } },
      });
      fireEvent.click(within(await screen.findByTestId("inventory-container")).getByRole("button", { name: "Logs" }));
      expect((await screen.findByRole("alert")).textContent).toContain("no such container");
    });

    it("asks for a restart only after it showed exactly what it does, and runs it on the second click", async () => {
      const mock = show([node({ can })], {
        "POST /api/agent/proposals/manual": pending,
        "POST /api/agent/proposals/p1/execute": { ...pending, status: "executed", result: { ok: true, output: { restarted: true } } },
        "GET /api/agent/proposals/p1": { ...pending, status: "executed", result: { ok: true, output: { restarted: true } } },
        "POST /api/inventory/nodes/n1/refresh": node({ can }),
      });
      const containers = await screen.findByTestId("inventory-container");
      fireEvent.click(within(containers).getByRole("button", { name: /Neu starten/ }));
      // the card says what it does; nothing has been executed yet
      expect(await screen.findByText("Container plex is stopped and started again.")).toBeTruthy();
      expect(mock.calls.some((c: { url: string }) => c.url.includes("/execute"))).toBe(false);
      expect(mock.calls.find((c: { url: string }) => c.url.endsWith("/manual"))?.body).toEqual({
        node_id: "n1",
        capability: "docker.restart",
        input: { container: "plex" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Ausführen" }));
      expect(await screen.findByText(/Erledigt/)).toBeTruthy();
      expect(mock.calls.filter((c: { url: string }) => c.url.endsWith("/execute")).length).toBe(1);
      // an inventory of a proposal that came from no task has no follow-up check
      expect(screen.queryByRole("button", { name: /Prüfen, ob es geholfen hat/ })).toBeNull();
    });

    it("offers the restart of a stack and a stopped container gets no restart button", async () => {
      show([
        node({
          can,
          containers: [{ kind: "container", name: "stopped-one", state: "exited", gone: false }],
        }),
      ]);
      const stacks = await screen.findByTestId("inventory-compose");
      expect(within(stacks).getByRole("button", { name: /Stack neu starten/ })).toBeTruthy();
      expect(within(screen.getByTestId("inventory-container")).queryByRole("button", { name: /Neu starten/ })).toBeNull();
    });
  });
});
