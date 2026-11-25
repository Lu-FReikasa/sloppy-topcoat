// Which shell a browser draws (the desktop plan, decision 6 and phase 4): the classic navigation
// of the web GUI, or the window desktop. The desktop client (overlay, solo windows) always draws the
// desktop; a plain browser draws the navigation unless the person chose windows. The choice is a
// matter of the device, so it stays in this browser.
import { useSyncExternalStore } from "react";
import { clientConfig } from "../desktop/client";
import { overlayOf, soloOf } from "../desktop/state";

export type ShellChoice = "nav" | "windows";

const KEY = "topcoat.shell";
export const SHELL_EVENT = "topcoat:shell";

export function shellChoice(): ShellChoice {
  try {
    return window.localStorage.getItem(KEY) === "windows" ? "windows" : "nav";
  } catch {
    return "nav";
  }
}

export function setShellChoice(choice: ShellChoice): void {
  try {
    window.localStorage.setItem(KEY, choice);
  } catch {
    // a browser without storage keeps the navigation
  }
  window.dispatchEvent(new Event(SHELL_EVENT));
}

/** This page is a window or the overlay of the desktop client. */
export function inClient(search = window.location.search): boolean {
  return clientConfig() !== null || soloOf(search) !== null || overlayOf(search);
}

/** The navigation is drawn: a plain browser whose person did not choose windows. */
export function wantsNav(search = window.location.search): boolean {
  return !inClient(search) && shellChoice() === "nav";
}

function subscribe(cb: () => void): () => void {
  window.addEventListener(SHELL_EVENT, cb);
  window.addEventListener("storage", cb);
  return () => {
    window.removeEventListener(SHELL_EVENT, cb);
    window.removeEventListener("storage", cb);
  };
}

/** `wantsNav()`, and drawn again when the person switches. */
export function useWantsNav(): boolean {
  return useSyncExternalStore(subscribe, () => wantsNav());
}
