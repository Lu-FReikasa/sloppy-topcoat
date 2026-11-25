import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, StrictMode, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, UnauthorizedError } from "../api/client";
import { createHubEvents, type HubEvents } from "./hubEvents";
import { RELOAD, useResource, type LiveOptions } from "./useResource";

interface Node {
  readonly id: string;
  readonly name: string;
  readonly version: number;
}

interface Deferred<T> {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
  readonly reject: (err: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve: (value: T) => void = () => undefined;
  let reject: (err: unknown) => void = () => undefined;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** A fake api client: every `getNode` call waits until the test answers it. */
function fakeClient() {
  const calls: { readonly id: string; readonly answer: Deferred<Node> }[] = [];
  const getNode = vi.fn((id: string): Promise<Node> => {
    const answer = deferred<Node>();
    calls.push({ id, answer });
    return answer.promise;
  });
  return { api: { getNode }, calls };
}

function node(id: string, version = 1, name = `name-${id}`): Node {
  return { id, name, version };
}

/** Answers the call with index `i` and lets React settle. */
async function answer(
  calls: ReturnType<typeof fakeClient>["calls"],
  i: number,
  value: Node | Error,
): Promise<void> {
  await act(async () => {
    const call = calls[i];
    if (!call) throw new Error(`no call #${i}`);
    if (value instanceof Error) call.answer.reject(value);
    else call.answer.resolve(value);
    await Promise.resolve();
  });
}

function useNodeResource(
  api: ReturnType<typeof fakeClient>["api"],
  id: string,
  events: HubEvents,
  live?: LiveOptions<Node>,
) {
  return useResource(`node:${id}`, () => api.getNode(id), { events, live });
}

const nodeUpdated: LiveOptions<Node> = {
  events: ["node_updated", "node_benchmark"],
  apply: (current, ev) => {
    if (ev.event === "node_benchmark") return RELOAD;
    const next = JSON.parse(ev.data) as Node;
    return current && next.id === current.id ? next : undefined;
  },
};

describe("useResource", () => {
  it("loads on mount and reports loading, then the data", async () => {
    const { api, calls } = fakeClient();
    const { result } = renderHook(() => useNodeResource(api, "n1", createHubEvents()));
    expect(result.current).toMatchObject({ data: undefined, loading: true, error: null });

    await answer(calls, 0, node("n1"));
    expect(result.current).toMatchObject({ data: node("n1"), loading: false, error: null });
    expect(api.getNode).toHaveBeenCalledTimes(1);
  });

  it("shows a failed load as an error and keeps the data on a failed reload", async () => {
    const { api, calls } = fakeClient();
    const { result } = renderHook(() => useNodeResource(api, "n1", createHubEvents()));
    await answer(calls, 0, new ApiError(404, "not_found", "no such node"));
    expect(result.current).toMatchObject({
      data: undefined,
      loading: false,
      error: { code: "not_found", message: "no such node" },
    });

    act(() => void result.current.reload());
    await answer(calls, 1, node("n1"));
    expect(result.current).toMatchObject({ data: node("n1"), error: null });

    act(() => void result.current.reload());
    await answer(calls, 2, new ApiError(0, "network", "hub unreachable"));
    expect(result.current).toMatchObject({
      data: node("n1"),
      loading: false,
      error: { code: "network" },
    });
  });

  it("does not show a 401 as an error (the client signs the person out)", async () => {
    const { api, calls } = fakeClient();
    const { result } = renderHook(() => useNodeResource(api, "n1", createHubEvents()));
    await answer(calls, 0, new UnauthorizedError());
    expect(result.current).toMatchObject({ data: undefined, loading: false, error: null });
  });

  it("never shows the old key's data for a new key, not even when its answer comes late", async () => {
    const { api, calls } = fakeClient();
    const events = createHubEvents();
    const { result, rerender } = renderHook(({ id }) => useNodeResource(api, id, events), {
      initialProps: { id: "n1" },
    });
    await answer(calls, 0, node("n1"));

    rerender({ id: "n2" });
    expect(result.current).toMatchObject({ data: undefined, loading: true });

    rerender({ id: "n3" });
    // n2's answer arrives after the page moved on to n3
    await answer(calls, 1, node("n2"));
    expect(result.current.data).toBeUndefined();
    await answer(calls, 2, node("n3"));
    expect(result.current.data).toEqual(node("n3"));
    expect(calls.map((c) => c.id)).toEqual(["n1", "n2", "n3"]);
  });

  it("drops a late answer of an earlier visit to the same key", async () => {
    const { api, calls } = fakeClient();
    const events = createHubEvents();
    const { result, rerender } = renderHook(({ id }) => useNodeResource(api, id, events), {
      initialProps: { id: "n1" },
    });
    rerender({ id: "n2" });
    rerender({ id: "n1" });
    await answer(calls, 2, node("n1", 2));
    // the first visit's load was slow and answers with what n1 was back then
    await answer(calls, 0, node("n1", 1));
    expect(result.current.data).toEqual(node("n1", 2));
  });

  it("drops an answer that arrives after unmount", async () => {
    const { api, calls } = fakeClient();
    const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { unmount } = renderHook(() => useNodeResource(api, "n1", createHubEvents()));
    unmount();
    await answer(calls, 0, node("n1"));
    expect(errors).not.toHaveBeenCalled();
    errors.mockRestore();
  });

  it("coalesces reloads while a load is on its way into one more load", async () => {
    const { api, calls } = fakeClient();
    const { result } = renderHook(() => useNodeResource(api, "n1", createHubEvents()));
    await answer(calls, 0, node("n1"));

    let done = 0;
    const reloadAndCount = async (): Promise<void> => {
      await result.current.reload();
      done += 1;
    };
    act(() => {
      void reloadAndCount();
      void reloadAndCount();
      void reloadAndCount();
    });
    expect(calls).toHaveLength(2);

    // the running reload ends with older data: the callers that asked meanwhile still wait
    await answer(calls, 1, node("n1", 2));
    await waitFor(() => expect(calls).toHaveLength(3));
    expect(done).toBe(1);

    await answer(calls, 2, node("n1", 3));
    await waitFor(() => expect(done).toBe(3));
    expect(result.current.data).toEqual(node("n1", 3));
    expect(calls).toHaveLength(3);
  });

  it("mutate shows an action's answer at once and wins over an older load on its way", async () => {
    const { api, calls } = fakeClient();
    const { result } = renderHook(() => useNodeResource(api, "n1", createHubEvents()));
    await answer(calls, 0, node("n1"));

    act(() => void result.current.reload());
    act(() => result.current.mutate((cur) => ({ ...(cur ?? node("n1")), name: "renamed", version: 2 })));
    expect(result.current.data).toEqual(node("n1", 2, "renamed"));

    // the reload started before the rename and answers with the old name
    await answer(calls, 1, node("n1"));
    expect(result.current).toMatchObject({ data: node("n1", 2, "renamed"), loading: false });

    // a reload asked for after the change counts again
    act(() => void result.current.reload());
    await answer(calls, 2, node("n1", 3, "renamed"));
    expect(result.current.data).toEqual(node("n1", 3, "renamed"));
  });

  describe("live", () => {
    it("applies a listed event and ignores what is not about this resource", async () => {
      const { api, calls } = fakeClient();
      const events = createHubEvents();
      const { result } = renderHook(() => useNodeResource(api, "n1", events, nodeUpdated));
      await answer(calls, 0, node("n1"));

      act(() => events.publish({ event: "node_updated", data: JSON.stringify(node("n1", 2)) }));
      expect(result.current.data).toEqual(node("n1", 2));

      act(() => events.publish({ event: "node_updated", data: JSON.stringify(node("n2", 9)) }));
      act(() => events.publish({ event: "inbox", data: JSON.stringify(node("n1", 9)) }));
      expect(result.current.data).toEqual(node("n1", 2));
      expect(calls).toHaveLength(1);
    });

    it("reloads when apply says RELOAD, and on every listed event without apply", async () => {
      const { api, calls } = fakeClient();
      const events = createHubEvents();
      const { result, rerender } = renderHook(
        ({ live }) => useNodeResource(api, "n1", events, live),
        { initialProps: { live: nodeUpdated } },
      );
      await answer(calls, 0, node("n1"));

      act(() => events.publish({ event: "node_benchmark", data: "{}" }));
      expect(calls).toHaveLength(2);
      await answer(calls, 1, node("n1", 2));
      expect(result.current.data).toEqual(node("n1", 2));

      rerender({ live: { events: ["node_config"] } });
      act(() => events.publish({ event: "node_config", data: "{}" }));
      expect(calls).toHaveLength(3);
    });

    it("an update from the stream wins over an older load on its way", async () => {
      const { api, calls } = fakeClient();
      const events = createHubEvents();
      const { result } = renderHook(() => useNodeResource(api, "n1", events, nodeUpdated));
      await answer(calls, 0, node("n1"));

      act(() => void result.current.reload());
      act(() => events.publish({ event: "node_updated", data: JSON.stringify(node("n1", 5)) }));
      await answer(calls, 1, node("n1", 4));
      expect(result.current.data).toEqual(node("n1", 5));
    });

    it("a load still on its way keeps what it brings; the stream's updates go on top of it", async () => {
      // a node's detail: the stream adds events, a reload brings the rest (config, benchmarks)
      interface Detail {
        readonly id: string;
        readonly config: string;
        readonly events: readonly string[];
      }
      const loads: Deferred<Detail>[] = [];
      const load = (): Promise<Detail> => {
        const d = deferred<Detail>();
        loads.push(d);
        return d.promise;
      };
      const events = createHubEvents();
      const live: LiveOptions<Detail> = {
        events: ["node_event"],
        apply: (cur, ev) =>
          cur && !cur.events.includes(ev.data) ? { ...cur, events: [ev.data, ...cur.events] } : undefined,
      };
      const { result } = renderHook(() => useResource("node:n1", load, { events, live }));
      await act(async () => loads[0]?.resolve({ id: "n1", config: "old", events: [] }));

      act(() => void result.current.reload());
      act(() => events.publish({ event: "node_event", data: "e1" }));
      expect(result.current.data?.events).toEqual(["e1"]);

      // the reload was answered before e1 reached the hub's database: new config, no e1 yet
      await act(async () => loads[1]?.resolve({ id: "n1", config: "new", events: [] }));
      expect(result.current.data).toEqual({ id: "n1", config: "new", events: ["e1"] });

      // and once more with an answer that already holds the event: it is not added twice
      act(() => void result.current.reload());
      act(() => events.publish({ event: "node_event", data: "e2" }));
      await act(async () => loads[2]?.resolve({ id: "n1", config: "newer", events: ["e2", "e1"] }));
      expect(result.current.data).toEqual({ id: "n1", config: "newer", events: ["e2", "e1"] });
    });

    it("listens once under StrictMode's double effects and shows the second load's answer", async () => {
      const { api, calls } = fakeClient();
      const events = createHubEvents();
      const wrapper = ({ children }: { children: ReactNode }) => createElement(StrictMode, null, children);
      const { result } = renderHook(
        () => useNodeResource(api, "n1", events, { events: ["node_benchmark"] }),
        { wrapper },
      );
      const first = calls.length;
      await answer(calls, first - 1, node("n1", 2));
      if (first > 1) await answer(calls, 0, node("n1", 1));
      expect(result.current).toMatchObject({ data: node("n1", 2), loading: false });

      act(() => events.publish({ event: "node_benchmark", data: "{}" }));
      expect(calls).toHaveLength(first + 1);
    });

    it("reloads after the stream reconnects; a resource that is not live does not", async () => {
      const { api, calls } = fakeClient();
      const other = fakeClient();
      const events = createHubEvents();
      renderHook(() => useNodeResource(api, "n1", events, nodeUpdated));
      renderHook(() => useNodeResource(other.api, "n2", events));
      await answer(calls, 0, node("n1"));
      await answer(other.calls, 0, node("n2"));

      act(() => events.connected());
      expect(calls).toHaveLength(2);
      expect(other.calls).toHaveLength(1);
    });

    it("stops listening when the page closes", async () => {
      const { api, calls } = fakeClient();
      const events = createHubEvents();
      const { unmount } = renderHook(() => useNodeResource(api, "n1", events, nodeUpdated));
      await answer(calls, 0, node("n1"));
      unmount();

      act(() => {
        events.publish({ event: "node_benchmark", data: "{}" });
        events.connected();
      });
      expect(calls).toHaveLength(1);
    });
  });
});

describe("createHubEvents", () => {
  it("tells every listener, even when one of them throws", () => {
    const events = createHubEvents();
    const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const seen: string[] = [];
    events.subscribe(() => {
      throw new Error("broken page");
    });
    const off = events.subscribe((ev) => seen.push(ev.event));
    events.publish({ event: "a", data: "" });
    off();
    events.publish({ event: "b", data: "" });
    expect(seen).toEqual(["a"]);
    // the broken listener is still subscribed and failed on both events
    expect(errors).toHaveBeenCalledTimes(2);
    errors.mockRestore();
  });
});
