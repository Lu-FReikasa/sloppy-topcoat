import { isComputeNode } from "./ModelCatalogue";
import type { NodeMetricPoint, NodeSummary } from "../types";

/** What a machine shows as a whole, taken from the nodes that run on it: the utilisation of the
 *  CPU and of the GPU with its history, and RAM and VRAM. A machine can carry
 *  several nodes (a compute node, a tool node, a dev node); each reading comes from the node that
 *  reports it, the compute node first, since it sees the GPU. */
export interface Meters {
  cpu: (number | null)[];
  gpu: (number | null)[];
  ram: { usedMb: number | null; totalMb: number } | null;
  vram: { name: string; usedMb: number | null; totalMb: number }[];
}

const RANK = (n: NodeSummary) =>
  isComputeNode(n) ? 0 : n.roles?.includes("dev") ? 1 : 2;

/** The readings of a group of nodes (offline nodes have none). */
export function metersOf(
  nodes: NodeSummary[],
  metrics: Record<string, NodeMetricPoint[]>,
): Meters {
  const live = nodes
    .filter((n) => n.state !== "offline")
    .sort((a, b) => RANK(a) - RANK(b));
  const series = (pick: (p: NodeMetricPoint) => number | null | undefined) =>
    live
      .map((n) => (metrics[n.id] ?? []).map((p) => pick(p) ?? null))
      .find((s) => s.some((v) => v !== null)) ?? [];
  const withRam = live.find((n) => n.ram_total_mb);
  return {
    cpu: series((p) => p.cpu_util),
    gpu: series((p) => p.gpu_util),
    ram: withRam
      ? { usedMb: withRam.ram_used_mb, totalMb: withRam.ram_total_mb as number }
      : null,
    vram: live.flatMap((n) =>
      n.gpus.map((g) => ({
        name: g.name,
        usedMb: g.vram_used_mb,
        totalMb: g.vram_total_mb,
      })),
    ),
  };
}
