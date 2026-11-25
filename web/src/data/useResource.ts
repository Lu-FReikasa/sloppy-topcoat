/**
 * The web GUI's data layer (P4, issue #8): data access that lives outside page components, so a
 * theme can later arrange views without taking the loading code with it.
 *
 * The pattern
 * - A page does not call `api.*` in a `useEffect` and keeps no `useState` for what it loaded.
 *   It calls a hook from `web/src/data/` (`useNode(api, id)`, ...) and renders what the hook
 *   gives it: `data`, `loading`, `error`.
 * - Such a hook is a thin wrapper around `useResource`: a key that names the resource, the
 *   `api` call that loads it, and the stream events that change it (`live`).
 * - Actions that change a resource live in their own hook next to it (`useNodeActions`): call
 *   the api, then `mutate` with the hub's answer or `reload`.
 * - No JSX in `data/`; the hooks are tested with a fake client and a fake bus
 *   (`createHubEvents()`), see `useResource.test.ts`.
 *
 * What `useResource` guarantees
 * - Loads on mount and whenever `key` changes; data of the old key is never shown for the new.
 * - An answer that arrives late is dropped: after the key changed, after unmount, or after a
 *   newer local change (`mutate`, a live update) that it would otherwise overwrite.
 * - `reload()` while a load is on its way does not pile up requests: one more load follows the
 *   running one, and every caller's promise resolves after that.
 * - A failed reload keeps the data it had and sets `error`; the next good load clears it.
 *   A 401 is no error here: the client already signs the person out.
 * - Live: listed stream events are handed to `apply`, which returns new data, `RELOAD`, or
 *   `undefined` (not about this resource). Without `apply` every listed event reloads. After the
 *   stream reconnects a live resource reloads (events sent meanwhile are lost).
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { UnauthorizedError } from "../api/client";
import type { SseEvent } from "../api/sse";
import { toActionError } from "../errors";
import type { ActionError } from "../types";
import { hubEvents, type HubEvents } from "./hubEvents";

/** Returned by `apply` when an event means "this changed, fetch it again". */
export const RELOAD: unique symbol = Symbol("reload");

export interface LiveOptions<T> {
  /** Names of the stream events that may change this resource (`node_updated`, ...). */
  readonly events: readonly string[];
  /** What a listed event does: new data, `RELOAD`, or `undefined` to ignore it (another node's
   *  update). `current` is undefined before the first load. Without it, every listed event reloads. */
  readonly apply?: (current: T | undefined, ev: SseEvent) => T | typeof RELOAD | undefined;
}

export interface ResourceOptions<T> {
  readonly live?: LiveOptions<T>;
  /** The stream bus; the app's by default, a fake one in tests. */
  readonly events?: HubEvents;
}

export interface Resource<T> {
  /** The last good data for this key; undefined until the first load. */
  readonly data: T | undefined;
  /** A load is on its way (the first one or a reload). */
  readonly loading: boolean;
  /** Why the last load failed; null after a good one. */
  readonly error: ActionError | null;
  /** Load again; resolves when the data is fresh (or the load failed). */
  readonly reload: () => Promise<void>;
  /** Put data in place at once (an action's answer); a load already on its way is dropped. */
  readonly mutate: (next: T | ((current: T | undefined) => T)) => void;
}

interface Snapshot<T> {
  readonly key: string;
  readonly data: T | undefined;
  readonly loading: boolean;
  readonly error: ActionError | null;
}

function fresh<T>(key: string): Snapshot<T> {
  return { key, data: undefined, loading: true, error: null };
}

export function useResource<T>(
  key: string,
  load: () => Promise<T>,
  options: ResourceOptions<T> = {},
): Resource<T> {
  const bus = options.events ?? hubEvents;
  const isLive = options.live !== undefined;

  const [snapshot, setSnapshot] = useState<Snapshot<T>>(() => fresh<T>(key));
  // the source of truth for callbacks (apply, mutate) that must see the very latest data
  const current = useRef<Snapshot<T>>(snapshot);
  const commit = useCallback((next: Snapshot<T>): void => {
    current.current = next;
    setSnapshot(next);
  }, []);

  // the caller's functions change every render; the latest is used, nothing re-subscribes
  const loadRef = useRef(load);
  const liveRef = useRef(options.live);
  const keyRef = useRef(key);
  useLayoutEffect(() => {
    loadRef.current = load;
    liveRef.current = options.live;
    keyRef.current = key;
  });

  // `epoch` moves on a key change and on unmount: every answer started before is dropped.
  // `changed` moves on a local change: an answer started before it is older than what is shown.
  const epoch = useRef(0);
  const changed = useRef(0);
  const running = useRef<Promise<void> | null>(null);
  const runs = useRef(0);
  const queued = useRef<Promise<void> | null>(null);

  const snapshotOf = useCallback((forKey: string): Snapshot<T> => {
    return current.current.key === forKey ? current.current : fresh<T>(forKey);
  }, []);

  const runOnce = useCallback(async (): Promise<void> => {
    const startEpoch = epoch.current;
    const startChanged = changed.current;
    const forKey = keyRef.current;
    commit({ ...snapshotOf(forKey), loading: true });
    try {
      const data = await loadRef.current();
      if (epoch.current !== startEpoch) return;
      const now = snapshotOf(forKey);
      if (changed.current !== startChanged) commit({ ...now, loading: false });
      else commit({ key: forKey, data, loading: false, error: null });
    } catch (err: unknown) {
      if (epoch.current !== startEpoch) return;
      const now = snapshotOf(forKey);
      if (err instanceof UnauthorizedError) commit({ ...now, loading: false });
      else commit({ ...now, loading: false, error: toActionError(err) });
    }
  }, [commit, snapshotOf]);

  // starts a load now; `running` holds it until it ends (unless a newer one took its place)
  const startRun = useCallback((): Promise<void> => {
    runs.current += 1;
    const token = runs.current;
    const run = (async (): Promise<void> => {
      try {
        await runOnce();
      } finally {
        if (runs.current === token) running.current = null;
      }
    })();
    running.current = run;
    return run;
  }, [runOnce]);

  const reload = useCallback((): Promise<void> => {
    const inFlight = running.current;
    if (inFlight === null) return startRun();
    if (queued.current === null) {
      const queuedEpoch = epoch.current;
      queued.current = (async (): Promise<void> => {
        await inFlight;
        if (epoch.current !== queuedEpoch) return;
        queued.current = null;
        // a load someone started after this request is fresh enough: wait for it instead
        await (running.current ?? startRun());
      })();
    }
    return queued.current;
  }, [startRun]);

  const mutate = useCallback(
    (next: T | ((current: T | undefined) => T)): void => {
      const now = snapshotOf(keyRef.current);
      const data =
        typeof next === "function" ? (next as (current: T | undefined) => T)(now.data) : next;
      changed.current += 1;
      commit({ ...now, data, error: null });
    },
    [commit, snapshotOf],
  );

  // load on mount and for every new key
  useEffect(() => {
    epoch.current += 1;
    running.current = null;
    queued.current = null;
    void reload();
    return () => {
      epoch.current += 1;
      running.current = null;
      queued.current = null;
    };
  }, [key, reload]);

  // live updates from the hub's stream
  useEffect(() => {
    if (!isLive) return;
    const offEvent = bus.subscribe((ev) => {
      const live = liveRef.current;
      if (!live || !live.events.includes(ev.event)) return;
      if (!live.apply) {
        void reload();
        return;
      }
      const now = snapshotOf(keyRef.current);
      const result = live.apply(now.data, ev);
      if (result === undefined) return;
      if (result === RELOAD) {
        void reload();
        return;
      }
      changed.current += 1;
      commit({ ...now, data: result });
    });
    const offConnect = bus.onConnect(() => {
      void reload();
    });
    return () => {
      offEvent();
      offConnect();
    };
  }, [isLive, bus, reload, commit, snapshotOf]);

  // before the effect for a new key has run, show nothing of the old key
  const shown = snapshot.key === key ? snapshot : fresh<T>(key);
  return useMemo(
    () => ({ data: shown.data, loading: shown.loading, error: shown.error, reload, mutate }),
    [shown.data, shown.loading, shown.error, reload, mutate],
  );
}
