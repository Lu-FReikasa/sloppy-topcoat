import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { makeNode } from "../test/fixtures";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type { ModelTransfer, NodeEvent } from "../types";
import { TransferDialog, sourceProblem, targetProblem } from "./TransferDialog";
import { TransferList } from "./TransferList";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const source = makeNode({ id: "n1", name: "node-nvidia" });
const model = source.models[0]!; // qwen-7b, 4.5 GB
const GIB = 1024 * 1024;

const nodes = [
  source,
  makeNode({ id: "n2", name: "node-amd", disk_free_mb: 100 * 1024 }),
  makeNode({ id: "n3", name: "node-small", disk_free_mb: 1024 }),
  makeNode({ id: "n4", name: "node-off", state: "offline", connected: false }),
  makeNode({ id: "n5", name: "node-denied", allowed_actions: ["models.load"] }),
];

const row = (o: Partial<ModelTransfer> = {}): ModelTransfer => ({
  id: "t1",
  user_id: "u1",
  source_node_id: "n1",
  target_node_id: "n2",
  model: "qwen-7b",
  target_name: null,
  size: 2_000_000_000,
  sha256: null,
  move: false,
  status: "transferring",
  error_code: null,
  error: null,
  created_at: "",
  updated_at: "",
  ...o,
});

function setupDialog(move: boolean, create: unknown = row()) {
  const mock = mockFetch({ "GET /api/nodes": nodes, "POST /api/model-transfers": create });
  const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
  const onClose = vi.fn();
  render(<TransferDialog api={api} node={source} model={model} move={move} onClose={onClose} />);
  return { mock, onClose };
}

describe("targetProblem", () => {
  it("checks state, allowed actions and free space (size + 1 GiB)", () => {
    expect(targetProblem(nodes[3]!, 1)).toBe("offline");
    expect(targetProblem(nodes[4]!, 1)).toBe("noAction");
    expect(targetProblem(makeNode({ disk_free_mb: 1500 }), 1 * GIB * 1024 * 1.2 / 1024)).toBeNull();
    expect(targetProblem(nodes[2]!, 4_500_000_000)).toBe("noSpace");
    expect(targetProblem(makeNode(), 4_500_000_000)).toBeNull(); // free space unknown
  });
});

describe("TransferDialog", () => {
  it("lists targets with free space and disables unusable ones", async () => {
    setupDialog(false);
    const ok = (await screen.findByLabelText(/node-amd/)) as HTMLInputElement;
    expect(ok.disabled).toBe(false);
    expect(screen.queryByLabelText(/node-nvidia/)).toBeNull(); // same node
    expect((screen.getByLabelText(/node-small/) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText(/node-off/) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText(/node-denied/) as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText("nicht genug Platz")).toBeTruthy();
  });

  it("copies to the chosen node", async () => {
    const { mock, onClose } = setupDialog(false);
    fireEvent.click(await screen.findByLabelText(/node-amd/));
    fireEvent.change(screen.getByLabelText(/Name auf dem Ziel/), { target: { value: " neu " } });
    fireEvent.click(screen.getByRole("button", { name: "Kopieren" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(mock.calls.at(-1)?.body).toEqual({
      source_node_id: "n1",
      target_node_id: "n2",
      model: "qwen-7b",
      target_name: "neu",
    });
  });

  it("requires explicit confirmation for a move", async () => {
    const { mock, onClose } = setupDialog(true);
    fireEvent.click(await screen.findByLabelText(/node-amd/));
    const submit = screen.getByRole("button", { name: "Verschieben" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText(/wird auf node-nvidia gelöscht/));
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(mock.calls.at(-1)?.body).toMatchObject({ move: true, target_node_id: "n2" });
  });

  it("shows hub error codes and stays open", async () => {
    const { onClose } = setupDialog(false, () =>
      jsonResponse({ detail: { code: "insufficient_disk", message: "x" } }, 409),
    );
    fireEvent.click(await screen.findByLabelText(/node-amd/));
    fireEvent.click(screen.getByRole("button", { name: "Kopieren" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Auf dem Ziel-Node ist nicht genug Platz frei.",
    );
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("TransferDialog mode from (fixed target)", () => {
  const holder = (o: Parameters<typeof makeNode>[0]) =>
    makeNode({ models: [{ name: "qwen-7b", size_bytes: 4_500_000_000, loaded: false, vram_used_mb: null }], ...o });
  const target = makeNode({ id: "t0", name: "node-target", disk_free_mb: 100 * 1024, models: [] });

  function setupFrom(list: ReturnType<typeof makeNode>[], fixed = target, create: unknown = row()) {
    const mock = mockFetch({ "GET /api/nodes": [fixed, ...list], "POST /api/model-transfers": create });
    const onClose = vi.fn();
    render(
      <TransferDialog
        api={new ApiClient({ token: "t", fetchImpl: mock.fetch })}
        node={fixed}
        mode="from"
        model={model}
        move
        onClose={onClose}
      />,
    );
    return { mock, onClose };
  }

  it("checks sendability", () => {
    expect(sourceProblem(makeNode())).toBeNull();
    expect(sourceProblem(makeNode({ connected: false, state: "offline" }))).toBe("offline");
    expect(sourceProblem(makeNode({ allowed_actions: ["models.load"] }))).toBe("noAction");
  });

  it("lists only nodes that store the model and preselects a single one", async () => {
    const { mock, onClose } = setupFrom([
      holder({ id: "s1", name: "node-amd" }),
      makeNode({ id: "x", name: "node-empty", models: [] }),
    ]);
    const radio = (await screen.findByLabelText(/node-amd/)) as HTMLInputElement;
    expect(radio.checked).toBe(true);
    expect(screen.queryByLabelText(/node-empty/)).toBeNull();
    expect(screen.getByText(/Auf Node node-target/)).toBeTruthy();
    // a move is not offered in this mode: no confirmation, copy button
    expect(screen.queryByLabelText(/wird auf .* gelöscht/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Kopieren" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(mock.calls.at(-1)?.body).toEqual({
      source_node_id: "s1",
      target_node_id: "t0",
      model: "qwen-7b",
    });
  });

  it("makes the user pick when several nodes store it and disables unusable ones", async () => {
    setupFrom([
      holder({ id: "s1", name: "node-amd" }),
      holder({ id: "s2", name: "node-off", connected: false, state: "offline" }),
      holder({ id: "s3", name: "node-denied", allowed_actions: ["models.load"] }),
    ]);
    const amd = (await screen.findByLabelText(/node-amd/)) as HTMLInputElement;
    expect(amd.checked).toBe(false);
    expect((screen.getByLabelText(/node-off/) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText(/node-denied/) as HTMLInputElement).disabled).toBe(true);
    const submit = screen.getByRole("button", { name: "Kopieren" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.click(amd);
    expect(submit.disabled).toBe(false);
  });

  it("blocks when the fixed target is full or offline", async () => {
    setupFrom([holder({ id: "s1", name: "node-amd" })], { ...target, disk_free_mb: 1024 });
    await screen.findByLabelText(/node-amd/);
    expect(screen.getByRole("alert").textContent).toContain("nicht genug Platz");
    expect((screen.getByRole("button", { name: "Kopieren" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("TransferList", () => {
  const ev = (id: number, data: Record<string, unknown>): NodeEvent => ({
    id,
    kind: "transfer_progress",
    data,
    ts: "2026-01-01T12:00:00Z",
    received_at: "",
  });

  function setupList(rows: ModelTransfer[], extra: Record<string, unknown> = {}, events: NodeEvent[] = []) {
    const mock = mockFetch({
      "GET /api/model-transfers?node_id=n1": () => jsonResponse({ transfers: rows }),
      "GET /api/nodes": nodes,
      ...extra,
    });
    const api = new ApiClient({ token: "t", fetchImpl: mock.fetch });
    render(<TransferList api={api} nodeId="n1" events={events} />);
    return mock;
  }

  it("shows progress from events and cancels", async () => {
    const canceled = row({ status: "canceled" });
    const mock = setupList(
      [row()],
      { "POST /api/model-transfers/t1/cancel": canceled },
      [ev(2, { transfer_id: "t1", role: "send", status: "running", transferred_bytes: 500_000_000, total_bytes: 2_000_000_000 })],
    );
    expect(await screen.findByText("qwen-7b nach node-amd")).toBeTruthy();
    expect(screen.getByText("500 MB / 2,0 GB (25 %)")).toBeTruthy();
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("25");
    fireEvent.click(screen.getByRole("button", { name: "Abbrechen" }));
    expect(await screen.findByText("abgebrochen")).toBeTruthy();
    expect(mock.calls.some((c) => c.url === "/api/model-transfers/t1/cancel")).toBe(true);
    expect(screen.queryByRole("button", { name: "Abbrechen" })).toBeNull();
  });

  it("explains no_route and shows incoming transfers", async () => {
    setupList([
      row({ status: "failed", error_code: "no_route", source_node_id: "n2", target_node_id: "n1" }),
    ]);
    expect(await screen.findByText("qwen-7b von node-amd")).toBeTruthy();
    expect(screen.getByText(/Ports 47000-47099/)).toBeTruthy();
    expect(screen.getByText("fehlgeschlagen")).toBeTruthy();
  });

  it("polls every 3 s while a transfer is active", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const mock = setupList([row()]);
    await screen.findByText("qwen-7b nach node-amd");
    const count = () => mock.calls.filter((c) => c.url.startsWith("/api/model-transfers")).length;
    const before = count();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3100);
    });
    expect(count()).toBeGreaterThan(before);
  });

  it("lists all transfers of one model without a node", async () => {
    const mock = mockFetch({
      "GET /api/model-transfers": () =>
        jsonResponse({ transfers: [row(), row({ id: "t2", model: "other" })] }),
      "GET /api/nodes": nodes,
    });
    render(<TransferList api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} model="qwen-7b" events={[]} />);
    expect(await screen.findByText("qwen-7b von node-nvidia nach node-amd")).toBeTruthy();
    expect(screen.queryByText(/other/)).toBeNull();
  });

  it("renders nothing without transfers", async () => {
    const { container } = render(
      <TransferList
        api={new ApiClient({ token: "t", fetchImpl: mockFetch({ "GET /api/model-transfers?node_id=n1": { transfers: [] } }).fetch })}
        nodeId="n1"
        events={[]}
      />,
    );
    await Promise.resolve();
    expect(container.textContent).toBe("");
  });
});

describe("TransferDialog busy", () => {
  it("explains busy as a transfer problem", async () => {
    const mock = mockFetch({
      "GET /api/nodes": nodes,
      "POST /api/model-transfers": () => jsonResponse({ detail: { code: "busy", message: "x" } }, 409),
    });
    render(
      <TransferDialog
        api={new ApiClient({ token: "t", fetchImpl: mock.fetch })}
        node={source}
        model={model}
        move={false}
        onClose={vi.fn()}
      />,
    );
    fireEvent.click(await screen.findByLabelText(/node-amd/));
    fireEvent.click(screen.getByRole("button", { name: "Kopieren" }));
    expect((await screen.findByRole("alert")).textContent).toContain("beschäftigt");
  });
});
