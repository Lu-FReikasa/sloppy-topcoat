import type { Handover, HandoverStatus } from "../types";

/** The groups of the hand-over list (issue #177), in the order the page shows them. */
export const HANDOVER_GROUPS = [
  "waiting",
  "running",
  "done",
  "withdrawn",
] as const;
export type HandoverGroup = (typeof HANDOVER_GROUPS)[number];

/** Which group a status belongs to. `failed` is not named in the issue: a failed hand-over has
 *  ended like an answered one, so it sits under "done" and keeps its own status line. */
const GROUP_OF: Record<HandoverStatus, HandoverGroup> = {
  proposed: "waiting",
  sent: "running",
  done: "done",
  failed: "done",
  cancelled: "withdrawn",
};

/** Hand-overs split into the four groups; each group keeps the hub's order. An unknown status
 *  (a newer hub) lands under "done" so nothing disappears from the list. */
export function groupHandovers(
  list: readonly Handover[],
): Record<HandoverGroup, Handover[]> {
  const groups: Record<HandoverGroup, Handover[]> = {
    waiting: [],
    running: [],
    done: [],
    withdrawn: [],
  };
  for (const h of list) groups[GROUP_OF[h.status] ?? "done"].push(h);
  return groups;
}
