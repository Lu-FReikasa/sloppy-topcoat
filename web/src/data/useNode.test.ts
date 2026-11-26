import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { makeDetail, makeNode } from "../test/fixtures";
import type { NodeDetail, NodeEvent, NodeSummary } from "../types";
import { createHubEvents, type HubEvents } from "./hubEvents";
import { MAX_EVENTS, useNode } from "./useNode";

interface Deferred<T> {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

/** A fake client: every `getNode` waits until the test answers it. */
function fakeClient() {
  const calls: Deferred<NodeDetail>[] = [];
  const getNode = vi.fn((): Promise<NodeDetail> => {
    const answer = deferred<NodeDetail>();
    calls.push(answer);
    return answer.promise;
  });
  return { api: { getNode }, calls };
}

async function answer(calls: Deferred<NodeDetail>[], i: number, value: NodeDetail): Promise<void> {
  await act(async () => {
    const call = calls[i];
    if (!call) throw new Error(`no call #${i}`);
    call.resolve(value);
    await Promise.resolve();
  });
}

function event(id: number, kind = "game_detected", data: Record<string, unknown> = {}): NodeEvent {
  return { id, kind, data, ts: "2026-01-01T12:00:00Z", received_at: "" };
}

function publishEvent(bus: HubEvents, nodeId: string, ev: NodeEvent): void {
  act(() => bus.publish({ event: "node_event", data: JSON.stringify({ node_id: nodeId, event: ev }) }));
}

function publishSummary(bus: HubEvents, node: NodeSummary): void {
  act(() => bus.publish({ event: "node_updated", data: JSON.stringify(node) }));
}

async function setup(detail: NodeDetail = makeDetail()) {
  const { api, calls } = fakeClient();
  const bus = createHubEvents();
  const onLoad = vi.fn();
  const hook = renderHook(({ id }) => useNode(api, id, { events: bus, onLoad }), {
    initialProps: { id: "n1" },
  });
  await answer(calls, 0, detail);
  return { api, calls, bus, onLoad, hook };
}

const ids = (d: NodeDetail | undefined) => d?.events.map((e) => e.id);

describe("useNode", () => {
  it("loads the node's detail and hands each loaded detail to onLoad", async () => {
    const { api, hook, onLoad } = await setup();
    expect(api.getNode).toHaveBeenCalledWith("n1");
    expect(hook.result.current.data?.name).toBe("node-nvidia");
    expect(onLoad).toHaveBeenCalledTimes(1);
    expect(onLoad).toHaveBeenCalledWith(hook.result.current.data);
  });

  it("merges node_updated of this node and keeps the events, config and benchmarks", async () => {
    const config = { roles: ["gpu"] };
    const { bus, hook, onLoad } = await setup(makeDetail({ config }));
    publishSummary(bus, makeNode({ id: "other", state: "offline" }));
    expect(hook.result.current.data?.state).toBe("available");
    publishSummary(bus, makeNode({ state: "busy_gaming" }));
    const data = hook.result.current.data;
    expect(data?.state).toBe("busy_gaming");
    expect(ids(data)).toEqual([1]);
    expect(data?.config).toEqual(config);
    // a live update is no load: the node list has the summary from the stream already
    expect(onLoad).toHaveBeenCalledTimes(1);
  });

  it("puts a node_event at the top once, even when the stream sends it twice", async () => {
    const { bus, hook } = await setup();
    publishEvent(bus, "n1", event(2));
    publishEvent(bus, "n1", event(2));
    publishEvent(bus, "other", event(3));
    expect(ids(hook.result.current.data)).toEqual([2, 1]);
  });

  it("keeps at most MAX_EVENTS events", async () => {
    const { bus, hook } = await setup(makeDetail({ events: [] }));
    act(() => {
      for (let i = 0; i < MAX_EVENTS + 5; i++) {
        bus.publish({
          event: "node_event",
          data: JSON.stringify({ node_id: "n1", event: event(i) }),
        });
      }
    });
    const events = hook.result.current.data?.events ?? [];
    expect(events).toHaveLength(MAX_EVENTS);
    expect(events[0]?.id).toBe(MAX_EVENTS + 4);
  });

  it("applies an event that came during a reload once, whether the answer holds it or not", async () => {
    const { bus, calls, hook } = await setup();
    // the answer already holds the event: it is not shown twice
    act(() => void hook.result.current.reload());
    publishEvent(bus, "n1", event(2));
    await answer(calls, 1, makeDetail({ events: [event(2), event(1)] }));
    expect(ids(hook.result.current.data)).toEqual([2, 1]);
    // the answer was read before the event: the event is put on top of it
    act(() => void hook.result.current.reload());
    publishEvent(bus, "n1", event(3));
    await answer(calls, 2, makeDetail({ events: [event(2), event(1)] }));
    expect(ids(hook.result.current.data)).toEqual([3, 2, 1]);
  });

  it("reloads when a benchmark ends or the config changed, not for other events", async () => {
    const { api, bus, calls, hook } = await setup();
    publishEvent(bus, "n1", event(2, "benchmark_started", { benchmark_id: "b" }));
    expect(api.getNode).toHaveBeenCalledTimes(1);

    publishEvent(bus, "n1", event(3, "benchmark_finished", { benchmark_id: "b", ok: true }));
    // shown at once, the benchmark rows follow with the reload
    expect(ids(hook.result.current.data)).toEqual([3, 2, 1]);
    await waitFor(() => expect(api.getNode).toHaveBeenCalledTimes(2));
    const benchmarks = [
      { benchmark_id: "b", model: "qwen-7b", backend: "cuda", pp512: 1, tg128: 2, ts: "t" },
    ] as unknown as NodeDetail["benchmarks"];
    await answer(calls, 1, makeDetail({ events: [event(3), event(2), event(1)], benchmarks }));
    expect(hook.result.current.data?.benchmarks).toEqual(benchmarks);

    // the same event again changes nothing and loads nothing
    publishEvent(bus, "n1", event(3, "benchmark_finished", { benchmark_id: "b", ok: true }));
    publishEvent(bus, "n1", event(4, "config_changed", { source: "manual_unload" }));
    await waitFor(() => expect(api.getNode).toHaveBeenCalledTimes(3));
  });

  it("reloads after the stream reconnects", async () => {
    const { api, bus } = await setup();
    act(() => bus.connected());
    expect(api.getNode).toHaveBeenCalledTimes(2);
  });

  it("shows nothing of the old node for a new id and does not reload for its marks", async () => {
    const { api, calls, hook } = await setup(
      makeDetail({ events: [event(5, "config_changed")] }),
    );
    hook.rerender({ id: "n2" });
    expect(hook.result.current.data).toBeUndefined();
    await answer(calls, 1, makeDetail({ id: "n2", events: [event(9, "config_changed")] }));
    expect(hook.result.current.data?.id).toBe("n2");
    // another node's first detail only sets its marks
    expect(api.getNode).toHaveBeenCalledTimes(2);
  });
});
