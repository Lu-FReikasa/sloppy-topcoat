// The hub's live stream (`/api/stream`) as seen by the data layer. The app keeps the one stream
// connection (App.tsx) and hands every event and every (re)connect to this bus; data hooks listen
// here instead of opening a connection of their own.
import type { SseEvent } from "../api/sse";

export type HubEventListener = (ev: SseEvent) => void;

export interface HubEvents {
  /** Every event from the stream, in order; returns the unsubscribe. */
  subscribe(listener: HubEventListener): () => void;
  /** Each time the stream is (re)connected: what the hub sent while it was down is lost, so
   *  live data reloads. Returns the unsubscribe. */
  onConnect(listener: () => void): () => void;
  /** Called by the stream owner (App.tsx) for each event. */
  publish(ev: SseEvent): void;
  /** Called by the stream owner (App.tsx) on each (re)connect. */
  connected(): void;
}

function notify<A extends unknown[]>(listeners: Set<(...args: A) => void>, ...args: A): void {
  // a copy: a listener may unsubscribe (a page closes) while the others are told
  [...listeners].forEach((listener) => {
    try {
      listener(...args);
    } catch (err) {
      // one broken listener must not keep the event from the others
      console.error("hub event listener failed", err);
    }
  });
}

export function createHubEvents(): HubEvents {
  const events = new Set<HubEventListener>();
  const connects = new Set<() => void>();
  return {
    subscribe(listener) {
      events.add(listener);
      return () => {
        events.delete(listener);
      };
    },
    onConnect(listener) {
      connects.add(listener);
      return () => {
        connects.delete(listener);
      };
    },
    publish(ev) {
      notify(events, ev);
    },
    connected() {
      notify(connects);
    },
  };
}

/** The app's bus, fed by the stream in App.tsx. Tests pass their own (`createHubEvents()`). */
export const hubEvents: HubEvents = createHubEvents();
