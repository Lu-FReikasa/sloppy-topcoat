// The interface data in the browser (docs/DESKTOP.md): load what the person is offered and their
// choices, and change a choice (the hue, the desktop). The page shows a change at once; the hub's
// answer is the clamped version and replaces it.
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ApiClient } from "../api/client";
import { hubUnreachable } from "../errors";
import type { UiBootstrap, UiPrefs, Workspace } from "./types";

export type UiStatus = "loading" | "ready" | "error";

/** How long a lazy change waits for the next one before it goes to the hub. */
export const LAZY_MS = 800;

export interface UiHandle {
  status: UiStatus;
  error: string | null;
  /** The last load failed because the hub did not answer (errors.ts, hubUnreachable). */
  unreachable: boolean;
  boot: UiBootstrap | null;
  /** The programs the person is offered, in order. */
  workspaces: Workspace[];
  prefs: UiPrefs | null;
  /** `lazy`: shown at once, sent to the hub a moment later together with the next change (the
   *  focus, a slider: many small changes, one write). */
  setPrefs: (patch: Partial<UiPrefs>, opts?: { lazy?: boolean }) => Promise<void>;
  /** The preferences as they are now, the changes of this very moment included (for a handler
   *  that runs after the last render, before the next one). */
  latestPrefs: () => UiPrefs | null;
  reload: () => Promise<void>;
}

function describe(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** `tick` is bumped when the stream says something changed (`ui_access`, `ui_prefs`). */
export function useUi(api: ApiClient, tick = 0): UiHandle {
  const [boot, setBoot] = useState<UiBootstrap | null>(null);
  const [status, setStatus] = useState<UiStatus>("loading");
  const [error, setError] = useState<string | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  // changes of the preferences on their way to the hub: while one is, the hub's echo on the stream
  // must not bring an older state back; of several answers only the last one sent counts
  const writing = useRef(0);
  const sent = useRef(0);
  // every local change counts up: a load that started before the last change is older than what
  // the page shows and is dropped (it put a view back that a click had just opened)
  const changed = useRef(0);
  // a lazy change (the focus, a slider) waits a moment and goes with the next one
  const pending = useRef<Partial<UiPrefs> | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const latest = useRef<UiPrefs | null>(null);
  useLayoutEffect(() => {
    latest.current = boot?.prefs ?? null;
  }, [boot]);
  const latestPrefs = useCallback(() => latest.current, []);

  const accept = useCallback((b: UiBootstrap) => {
    setBoot(b);
    setStatus("ready");
    setError(null);
    setUnreachable(false);
  }, []);

  const fail = useCallback((err: unknown) => {
    setError(describe(err));
    setUnreachable(hubUnreachable(err));
    setStatus((s) => (s === "ready" ? "ready" : "error"));
  }, []);

  const load = useCallback(() => api.uiBootstrap().then(accept, fail), [api, accept, fail]);

  useEffect(() => {
    if (writing.current > 0 || pending.current) return; // our own change: its answer is newer
    let alive = true;
    const startedAt = changed.current;
    api.uiBootstrap().then(
      (b) => {
        if (!alive) return;
        if (changed.current !== startedAt) {
          // a change was made while this load was on its way: keep the page's choices, take the rest
          setBoot((cur) => (cur ? { ...b, prefs: cur.prefs } : b));
          setStatus("ready");
          return;
        }
        accept(b);
      },
      (err: unknown) => alive && fail(err),
    );
    return () => {
      alive = false;
    };
  }, [api, tick, accept, fail]);

  const send = useCallback(
    async (patch: Partial<UiPrefs>) => {
      sent.current += 1;
      const mine = sent.current;
      writing.current += 1;
      try {
        const prefs = await api.putUiPrefs(patch);
        if (mine === sent.current && !pending.current) setBoot((b) => (b ? { ...b, prefs } : b));
      } catch (err) {
        setError(describe(err));
      } finally {
        writing.current -= 1;
      }
    },
    [api],
  );

  const setPrefs = useCallback<UiHandle["setPrefs"]>(
    async (patch, opts) => {
      changed.current += 1;
      if (latest.current) latest.current = { ...latest.current, ...patch };
      setBoot((b) => (b ? { ...b, prefs: { ...b.prefs, ...patch } } : b));
      clearTimeout(timer.current);
      const all = { ...pending.current, ...patch };
      if (opts?.lazy) {
        pending.current = all;
        timer.current = setTimeout(() => {
          const due = pending.current;
          pending.current = null;
          if (due) void send(due);
        }, LAZY_MS);
        return;
      }
      pending.current = null;
      await send(all);
    },
    [send],
  );

  // a lazy change still waiting when the page goes is sent at once
  useEffect(
    () => () => {
      clearTimeout(timer.current);
      const due = pending.current;
      pending.current = null;
      if (due) void api.putUiPrefs(due).catch(() => undefined);
    },
    [api],
  );

  return {
    status,
    error,
    unreachable,
    boot,
    workspaces: boot?.workspaces ?? [],
    prefs: boot?.prefs ?? null,
    setPrefs,
    latestPrefs,
    reload: load,
  };
}
