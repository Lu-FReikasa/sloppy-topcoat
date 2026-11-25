import type { SseEvent } from "../api/sse";
import type { NodeDetail, NodeEvent, NodeSummary } from "../types";

/** Maximum number of events kept in the open detail view. */
export const MAX_EVENTS = 200;

export interface AppState {
  nodes: NodeSummary[];
  nodesLoaded: boolean;
  /** Detail of the node currently open in the detail view, if any. */
  detail: NodeDetail | null;
}

export type Action =
  | { type: "nodes_loaded"; nodes: NodeSummary[] }
  | { type: "node_updated"; node: NodeSummary }
  | { type: "node_event"; nodeId: string; event: NodeEvent }
  | { type: "detail_loaded"; detail: NodeDetail }
  | {
      type: "config_updated";
      nodeId: string;
      config: Record<string, unknown>;
      configHash: string;
    };

export const initialState: AppState = { nodes: [], nodesLoaded: false, detail: null };

function sortNodes(nodes: NodeSummary[]): NodeSummary[] {
  return [...nodes].sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
}

export function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "nodes_loaded": {
      let detail = state.detail;
      if (detail) {
        const fresh = action.nodes.find((n) => n.id === detail?.id);
        if (fresh) detail = { ...detail, ...fresh };
      }
      return { ...state, nodes: sortNodes(action.nodes), nodesLoaded: true, detail };
    }
    case "node_updated": {
      const node = action.node;
      const others = state.nodes.filter((n) => n.id !== node.id);
      const detail =
        state.detail && state.detail.id === node.id ? { ...state.detail, ...node } : state.detail;
      return { ...state, nodes: sortNodes([...others, node]), detail };
    }
    case "node_event": {
      const detail = state.detail;
      if (!detail || detail.id !== action.nodeId) return state;
      if (detail.events.some((e) => e.id === action.event.id)) return state;
      return {
        ...state,
        detail: { ...detail, events: [action.event, ...detail.events].slice(0, MAX_EVENTS) },
      };
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
      return { ...state, detail: d, nodes };
    }
    case "config_updated": {
      // A config the hub confirmed (e.g. a resident toggle); node_updated carries no config.
      const detail = state.detail;
      if (!detail || detail.id !== action.nodeId) return state;
      return {
        ...state,
        detail: { ...detail, config: action.config, config_hash: action.configHash },
      };
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
  if (ev.event === "node_event") {
    const p = payload as { node_id?: unknown; event?: unknown };
    if (typeof p.node_id !== "string" || !p.event || typeof p.event !== "object") return null;
    return { type: "node_event", nodeId: p.node_id, event: p.event as NodeEvent };
  }
  return null;
}
