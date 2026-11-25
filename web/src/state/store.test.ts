import { describe, expect, it } from "vitest";
import { makeDetail, makeNode } from "../test/fixtures";
import { actionFromStreamEvent, initialState, MAX_EVENTS, reducer, type AppState } from "./store";

describe("reducer", () => {
  it("loads nodes sorted by name", () => {
    const s = reducer(initialState, {
      type: "nodes_loaded",
      nodes: [makeNode({ id: "b", name: "zeta" }), makeNode({ id: "a", name: "alpha" })],
    });
    expect(s.nodesLoaded).toBe(true);
    expect(s.nodes.map((n) => n.id)).toEqual(["a", "b"]);
  });

  it("node_updated replaces an existing node", () => {
    const s0 = reducer(initialState, { type: "nodes_loaded", nodes: [makeNode()] });
    const s1 = reducer(s0, { type: "node_updated", node: makeNode({ state: "busy_gaming" }) });
    expect(s1.nodes).toHaveLength(1);
    expect(s1.nodes[0]?.state).toBe("busy_gaming");
  });

  it("node_updated inserts an unknown node", () => {
    const s0 = reducer(initialState, { type: "nodes_loaded", nodes: [makeNode()] });
    const s1 = reducer(s0, { type: "node_updated", node: makeNode({ id: "n2", name: "node-amd" }) });
    expect(s1.nodes.map((n) => n.id)).toEqual(["n2", "n1"]);
  });

  it("node_updated merges into the open detail and keeps events", () => {
    const s0: AppState = { ...initialState, detail: makeDetail() };
    const s1 = reducer(s0, { type: "node_updated", node: makeNode({ state: "offline", connected: false }) });
    expect(s1.detail?.state).toBe("offline");
    expect(s1.detail?.events).toHaveLength(1);
    const s2 = reducer(s0, { type: "node_updated", node: makeNode({ id: "other" }) });
    expect(s2.detail).toBe(s0.detail);
  });

  it("node_event prepends to the open detail only, without duplicates", () => {
    const s0: AppState = { ...initialState, detail: makeDetail() };
    const ev = { id: 2, kind: "game_detected", data: {}, ts: "t", received_at: "t" };
    const s1 = reducer(s0, { type: "node_event", nodeId: "n1", event: ev });
    expect(s1.detail?.events.map((e) => e.id)).toEqual([2, 1]);
    expect(reducer(s1, { type: "node_event", nodeId: "n1", event: ev })).toBe(s1);
    expect(reducer(s0, { type: "node_event", nodeId: "other", event: ev })).toBe(s0);
    expect(reducer(initialState, { type: "node_event", nodeId: "n1", event: ev })).toBe(initialState);
  });

  it("config_updated replaces the config of the open detail only", () => {
    const s0: AppState = { ...initialState, detail: makeDetail() };
    const config = { residents: [{ model: "qwen-7b", priority: 50, auto_reload: true }] };
    const s1 = reducer(s0, { type: "config_updated", nodeId: "n1", config, configHash: "h2" });
    expect(s1.detail?.config).toEqual(config);
    expect(s1.detail?.config_hash).toBe("h2");
    expect(s1.detail?.events).toBe(s0.detail?.events);
    expect(reducer(s0, { type: "config_updated", nodeId: "other", config, configHash: "h" })).toBe(s0);
  });

  it("caps the event list", () => {
    let s: AppState = { ...initialState, detail: makeDetail({ events: [] }) };
    for (let i = 0; i < MAX_EVENTS + 5; i++) {
      s = reducer(s, { type: "node_event", nodeId: "n1", event: { id: i, kind: "error", data: {}, ts: "", received_at: "" } });
    }
    expect(s.detail?.events).toHaveLength(MAX_EVENTS);
    expect(s.detail?.events[0]?.id).toBe(MAX_EVENTS + 4);
  });
});

describe("actionFromStreamEvent", () => {
  it("maps node_updated", () => {
    const node = makeNode();
    expect(actionFromStreamEvent({ event: "node_updated", data: JSON.stringify(node) })).toEqual({
      type: "node_updated",
      node,
    });
  });

  it("maps node_event", () => {
    const event = { id: 5, kind: "paired", data: {}, ts: "t", received_at: "t" };
    expect(
      actionFromStreamEvent({ event: "node_event", data: JSON.stringify({ node_id: "n1", event }) }),
    ).toEqual({ type: "node_event", nodeId: "n1", event });
  });

  it("ignores malformed or unknown events", () => {
    expect(actionFromStreamEvent({ event: "node_updated", data: "{nope" })).toBeNull();
    expect(actionFromStreamEvent({ event: "node_updated", data: "{}" })).toBeNull();
    expect(actionFromStreamEvent({ event: "other", data: "{}" })).toBeNull();
  });
});
