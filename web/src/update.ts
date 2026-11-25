import type { NodeSummary } from "./types";

/** What the update card says and offers, from the summary the hub sends (E2-06). */
export type UpdateState =
  | "unknown" // the hub does not know the update channel
  | "no_release" // nothing published for this node's channel and platform
  | "current" // the newest release is not newer than the installed version
  | "not_capable" // this build of the node carries no update key
  | "not_allowed" // config.allowed_actions lacks node.update
  | "updater_missing" // the privileged updater service is not installed or does not run
  | "updater_stale" // it ran once but stopped reporting
  | "available"; // an update can be started now

export function updateState(node: NodeSummary): UpdateState {
  const u = node.update;
  if (!u) return "unknown";
  if (u.available === null) return "no_release";
  if (!u.newer) return "current";
  if (!u.capable) return "not_capable";
  if (!u.action_allowed) return "not_allowed";
  if (u.updater === "stale") return "updater_stale";
  if (u.updater !== "ok") return "updater_missing";
  return "available";
}

