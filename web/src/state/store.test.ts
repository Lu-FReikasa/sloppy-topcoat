import { describe, expect, it } from "vitest";
import { makeDetail, makeNode } from "../test/fixtures";
import { actionFromStreamEvent, initialState, reducer } from "./store";

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

  it("a loaded detail updates its node in the list, without the detail's own fields", () => {
    const s0 = reducer(initialState, { type: "nodes_loaded", nodes: [makeNode()] });
    const s1 = reducer(s0, { type: "detail_loaded", detail: makeDetail({ name: "renamed" }) });
    expect(s1.nodes).toHaveLength(1);
    expect(s1.nodes[0]?.name).toBe("renamed");
    expect(s1.nodes[0]).not.toHaveProperty("events");
    expect(s1.nodes[0]).not.toHaveProperty("config");
    // before the list is loaded the detail does not start it
    expect(reducer(initialState, { type: "detail_loaded", detail: makeDetail() }).nodes).toEqual([]);
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

  it("leaves node_event to the open node's page (useNode)", () => {
    const event = { id: 5, kind: "paired", data: {}, ts: "t", received_at: "t" };
    expect(
      actionFromStreamEvent({ event: "node_event", data: JSON.stringify({ node_id: "n1", event }) }),
    ).toBeNull();
  });

  it("ignores malformed or unknown events", () => {
    expect(actionFromStreamEvent({ event: "node_updated", data: "{nope" })).toBeNull();
    expect(actionFromStreamEvent({ event: "node_updated", data: "{}" })).toBeNull();
    expect(actionFromStreamEvent({ event: "other", data: "{}" })).toBeNull();
  });
});
