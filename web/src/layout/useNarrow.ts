import { useSyncExternalStore } from "react";

/** Below this width every shell shows one window at a time (the stack form). */
export const NARROW_PX = 760;

function subscribe(cb: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => undefined;
  const mq = window.matchMedia(`(max-width: ${NARROW_PX - 1}px)`);
  mq.addEventListener("change", cb);
  return () => mq.removeEventListener("change", cb);
}

function snapshot(): boolean {
  return typeof window !== "undefined" && !!window.matchMedia && window.matchMedia(`(max-width: ${NARROW_PX - 1}px)`).matches;
}

export function useNarrow(): boolean {
  return useSyncExternalStore(subscribe, snapshot, () => false);
}
