// "Open this module with these props" across a change of address. A link of a page (`#/chat/<id>`,
// `#/admin/nodes/<id>`) does not open a page: the app records what the address stands for here,
// moves to the workspace and the workspace opens or brings forward the window that owns it. A window
// can ask for the same thing without a change of address (`requestOpen`).
import type { PropValue } from "./types";

export interface OpenIntent {
  module: string;
  props: Record<string, PropValue>;
  /** Open the module in this program (its key) instead of the one that fits: a chat of the
   *  development program, while the chat program is in front. */
  program?: string;
}

let pending: OpenIntent | null = null;

/** Fired when a window asks the workspace to open a module (`requestOpen`). */
export const INTENT_EVENT = "topcoat:intent";

export function setIntent(intent: OpenIntent | null): void {
  pending = intent;
}

/** The recorded intent, once. */
export function takeIntent(): OpenIntent | null {
  const intent = pending;
  pending = null;
  return intent;
}

/** Ask the workspace to open a module from inside a window (an inbox item that points at a task). */
export function requestOpen(intent: OpenIntent): void {
  pending = intent;
  window.dispatchEvent(new Event(INTENT_EVENT));
}
