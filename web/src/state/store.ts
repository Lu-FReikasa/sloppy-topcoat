import type { SseEvent } from "../api/sse";
import type { NodeDetail, NodeSummary } from "../types";

/** The node list. The open node's detail lives in its page (web/src/data/useNode.ts). */
export interface AppState {
  nodes: NodeSummary[];
  nodesLoaded: boolean;
}

export type Action =
  | { type: "nodes_loaded"; nodes: NodeSummary[] }
  | { type: "node_updated"; node: NodeSummary }
  /** The detail page loaded a node: its summary in the list follows. */
  | { type: "detail_loaded"; detail: NodeDetail };

export const initialState: AppState = { nodes: [], nodesLoaded: false };

function sortNodes(nodes: NodeSummary[]): NodeSummary[] {
  return [...nodes].sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
}

export function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "nodes_loaded":
      return { ...state, nodes: sortNodes(action.nodes), nodesLoaded: true };
    case "node_updated": {
      const node = action.node;
      const others = state.nodes.filter((n) => n.id !== node.id);
      return { ...state, nodes: sortNodes([...others, node]) };
    }
    case "detail_loaded": {
      const d = action.detail;
      // Keep the summary list consistent with the freshly fetched detail.
      const {
        events: _events,
        config: _config,
        config_hash: _hash,
        benchmarks: _benchmarks,
        ...summary
      } = d;
      void _events;
      void _config;
      void _hash;
      void _benchmarks;
      const others = state.nodes.filter((n) => n.id !== d.id);
      const nodes = state.nodesLoaded ? sortNodes([...others, summary]) : state.nodes;
      return { ...state, nodes };
    }
  }
}

function parseJson(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return undefined;
  }
}

/** Translate a raw stream event into a store action (or null to ignore it). */
export function actionFromStreamEvent(ev: SseEvent): Action | null {
  const payload = parseJson(ev.data);
  if (!payload || typeof payload !== "object") return null;
  if (ev.event === "node_updated") {
    const node = payload as NodeSummary;
    if (typeof node.id !== "string") return null;
    return { type: "node_updated", node };
  }
  return null;
}
