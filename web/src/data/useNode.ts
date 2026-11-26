/**
 * One node's detail (`GET /api/nodes/{id}`), kept live by the stream: the node detail page's data
 * (issue #9). See `useResource.ts` for the pattern and what it guarantees.
 *
 * Live
 * - `node_updated` (the summary: state, usage, runner, ...) is merged into the detail.
 * - `node_event` puts the event at the top of the event list.
 * - Benchmark rows and the config are not in either: a new benchmark result or end of a run, and
 *   a config change (another admin, a manual unload, a benchmark that set the threads), reload
 *   the detail once the event is shown.
 */
import { useEffect, useRef } from "react";
import type { ApiClient } from "../api/client";
import type { SseEvent } from "../api/sse";
import { latestBenchmarkEventId, latestConfigEventId } from "../nodeActivity";
import type { NodeDetail, NodeEvent, NodeSummary } from "../types";
import type { HubEvents } from "./hubEvents";
import { useResource, type LiveOptions, type Resource } from "./useResource";

/** Maximum number of events kept in the open detail. */
export const MAX_EVENTS = 200;

export interface NodeOptions {
  /** Each detail the hub sent (not the live updates): the app keeps its node list in step. */
  readonly onLoad?: (detail: NodeDetail) => void;
  /** The stream bus; the app's by default, a fake one in tests. */
  readonly events?: HubEvents;
}

function parse(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return undefined;
  }
}

/** The stream's `node_updated` payload, if it is about node `id`. */
function summaryFor(id: string, ev: SseEvent): NodeSummary | undefined {
  const p = parse(ev.data) as Partial<NodeSummary> | undefined;
  return p && typeof p === "object" && p.id === id ? (p as NodeSummary) : undefined;
}

/** The stream's `node_event` payload, if it is about node `id`. */
function eventFor(id: string, ev: SseEvent): NodeEvent | undefined {
  const p = parse(ev.data) as { node_id?: unknown; event?: unknown } | undefined;
  if (!p || typeof p !== "object" || p.node_id !== id) return undefined;
  const event = p.event as Partial<NodeEvent> | undefined;
  return event && typeof event === "object" && typeof event.id === "number"
    ? (event as NodeEvent)
    : undefined;
}

/** How the stream changes the detail of node `id`. */
export function nodeLive(id: string): LiveOptions<NodeDetail> {
  return {
    events: ["node_updated", "node_event"],
    apply: (current, ev) => {
      // before the first load there is nothing to merge into: the load brings it
      if (current === undefined) return undefined;
      if (ev.event === "node_updated") {
        const summary = summaryFor(id, ev);
        // a merge: the same summary twice gives the same detail
        return summary ? { ...current, ...summary } : undefined;
      }
      const event = eventFor(id, ev);
      if (!event) return undefined;
      // Live updates must be safe to apply twice (useResource.ts): this runs again on a load's
      // answer that may already hold the event. So events are merged by id, never appended blindly.
      if (current.events.some((e) => e.id === event.id)) return undefined;
      return { ...current, events: [event, ...current.events].slice(0, MAX_EVENTS) };
    },
  };
}

/** The ids that say "the benchmarks or the config changed": a reload is due when one moves. */
function marks(detail: NodeDetail): string {
  return `${latestBenchmarkEventId(detail.events)}/${latestConfigEventId(detail.events)}`;
}

export function useNode(
  api: Pick<ApiClient, "getNode">,
  id: string,
  options: NodeOptions = {},
): Resource<NodeDetail> {
  const onLoad = useRef(options.onLoad);
  useEffect(() => {
    onLoad.current = options.onLoad;
  });

  const resource = useResource(
    `node:${id}`,
    async () => {
      const detail = await api.getNode(id);
      onLoad.current?.(detail);
      return detail;
    },
    { live: nodeLive(id), events: options.events },
  );

  // Benchmarks and config are not in the stream: reload when a new event says they changed.
  // The first detail of a node only sets the marks.
  const { data, reload } = resource;
  const seen = useRef<{ readonly id: string; readonly marks: string } | null>(null);
  useEffect(() => {
    if (data === undefined || data.id !== id) return;
    const now = marks(data);
    const before = seen.current;
    seen.current = { id, marks: now };
    if (before !== null && before.id === id && before.marks !== now) void reload();
  }, [data, id, reload]);

  return resource;
}
