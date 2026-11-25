import type { BlockRunStep } from "../types";

/** A block as the program holds it (the fields of its kind, see hub `blocks/program.py`). */
export type Block = Record<string, unknown> & { b: string };

export type ChainState = "waiting" | "active" | "done" | "failed";

/** What the live view knows about one block, from the run's events. */
export interface Live {
  state: ChainState;
  model?: string;
  node?: string;
  /** The last thing the block did (an agent's plan, a call, a wait), as a translation key. */
  activity?: { key: string; values: Record<string, string> };
  started?: number;
  ended?: number;
  /** A loop's end: why, and after how many rounds. */
  loop?: { reason: string; rounds: number };
}

export interface ChainGroup {
  label: string;
  /** The path of the list; the block at index i has the path `${base}[${i}]`. */
  base: string;
  blocks: Block[];
}

/** The paths the engine gives a block's children (hub `blocks/engine.py`). The top level is `stack`. */
export function childGroups(path: string, block: Block): ChainGroup[] {
  const out: ChainGroup[] = [];
  const list = (key: string, label: string, base: string) => {
    const value = block[key];
    if (Array.isArray(value) && value.length > 0) {
      out.push({ label, base, blocks: value as Block[] });
    }
  };
  list("stack", "do", `${path}.stack`);
  list("then", "then", `${path}.then`);
  list("else", "else", `${path}.else`);
  if (Array.isArray(block.lanes)) {
    (block.lanes as Block[][]).forEach((lane, i) => {
      out.push({ label: `lane ${i + 1}`, base: `${path}.lanes[${i}]`, blocks: lane });
    });
  }
  return out;
}

/** Reads the events of a run into the state of every block, keyed by the engine's path. The
 *  latest event of a block wins, so a block of a loop shows its latest round. */
export function liveByPath(steps: BlockRunStep[], runFailed: boolean): Map<string, Live> {
  const map = new Map<string, Live>();
  const at = (path: string): Live => {
    let live = map.get(path);
    if (!live) {
      live = { state: "waiting" };
      map.set(path, live);
    }
    return live;
  };
  for (const s of steps) {
    if (!s.path) continue;
    const live = at(s.path);
    switch (s.event) {
      case "step":
        if (s.state === "start") {
          Object.assign(live, { state: "active", started: s.at, ended: undefined, activity: undefined });
        } else if (s.state === "done") {
          Object.assign(live, { state: "done", ended: s.at });
        } else if (s.state === "end") {
          // the block stopped with an error (failed) or by a stop block (done)
          Object.assign(live, { state: runFailed ? "failed" : "done", ended: s.at });
        }
        break;
      case "pick":
        if (s.model) live.model = s.model;
        if (s.node) live.node = s.node;
        break;
      case "call":
        if (s.model) live.model = s.model;
        if (s.node) live.node = s.node;
        break;
      case "activity": {
        const activity = describeActivity(s);
        if (activity) live.activity = activity;
        break;
      }
      case "loop_end":
        live.loop = { reason: String(s.reason ?? ""), rounds: Number(s.rounds ?? 0) };
        break;
    }
  }
  return map;
}

/** One agent event as a line: a plan with its tool, node and reason; a waiting heartbeat. */
export function describeActivity(
  s: BlockRunStep,
): { key: string; values: Record<string, string> } | null {
  const data = s.data ?? {};
  const text = (name: string) => (typeof data[name] === "string" ? (data[name] as string) : "");
  switch (s.what) {
    case "tools_start":
      return { key: "started", values: {} };
    case "tools_wait":
      return { key: "waiting", values: {} };
    case "tool_step": {
      const kind = text("kind");
      if (kind === "plan" || kind === "call") {
        return {
          key: kind,
          values: {
            capability: text("capability"),
            node: text("node"),
            reason: text("text"),
          },
        };
      }
      if (kind === "note" || kind === "step_check") {
        return { key: "note", values: { text: text("text") || text("note") } };
      }
      return null;
    }
    default:
      return null;
  }
}

/** Blocks at the top level that are done, of all top-level blocks (a loop counts as one). */
export function topLevelProgress(stack: Block[], live: Map<string, Live>): { done: number; total: number } {
  const done = stack.filter((_, i) => live.get(`stack[${i}]`)?.state === "done").length;
  return { done, total: stack.length };
}
