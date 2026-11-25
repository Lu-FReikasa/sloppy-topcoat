import { useTranslation } from "react-i18next";
import { formatDecimal, mbToGb, percent } from "../format";
import { nodeHref } from "../route";
import type {
  ActivityView,
  Machine,
  MachinesView,
  NodeMetricPoint,
  NodeState,
  NodeSummary,
  NodeWork,
} from "../types";
import { duration } from "./ActivityPanel";
import { metersOf, type Meters } from "./MachineMeters";
import { isComputeNode } from "./ModelCatalogue";
import { Sparkline } from "./Sparkline";
import { StateBadge } from "./StateBadge";

export type NodeFilter = "all" | "compute" | "tools";

/** One line of the table: a machine with the nodes that run on it. */
export interface MachineRow {
  key: string;
  name: string;
  /** A virtual machine is indented below its host. */
  vmOf: string | null;
  nodes: NodeSummary[];
}

function passes(n: NodeSummary, filter: NodeFilter): boolean {
  return filter === "all" ? true : filter === "compute" ? isComputeNode(n) : !isComputeNode(n);
}

/**
 * The rows of the table, hosts first with their virtual machines right below. Without a machine
 * view (or for a node it does not know) every node is a machine of its own, so nothing is lost.
 * A machine without a node that passes the filter is left out, unless a VM below it stays.
 */
export function rowsOf(
  view: MachinesView | null,
  nodes: NodeSummary[],
  filter: NodeFilter,
): MachineRow[] {
  const byName = new Map(nodes.map((n) => [n.name, n]));
  const placed = new Set<string>();
  const nodesOf = (m: Machine) =>
    m.nodes
      .map((name) => byName.get(name))
      .filter((n): n is NodeSummary => n !== undefined)
      .filter((n) => {
        placed.add(n.name);
        return passes(n, filter);
      });
  const rows: MachineRow[] = [];
  for (const m of view?.machines.filter((x) => x.kind === "physical") ?? []) {
    const guests = (view?.machines ?? [])
      .filter((g) => g.kind === "vm" && g.host === m.key)
      .map((g) => ({ g, nodes: nodesOf(g) }))
      .filter((x) => x.nodes.length > 0);
    const own = nodesOf(m);
    if (own.length === 0 && guests.length === 0) continue;
    rows.push({ key: m.key, name: m.name, vmOf: null, nodes: own });
    for (const { g, nodes: gn } of guests) {
      rows.push({ key: g.key, name: g.name, vmOf: m.name, nodes: gn });
    }
  }
  for (const n of nodes) {
    if (placed.has(n.name) || !passes(n, filter)) continue;
    rows.push({ key: `node:${n.id}`, name: n.name, vmOf: null, nodes: [n] });
  }
  return rows;
}

function machineState(nodes: NodeSummary[]): NodeState | null {
  if (nodes.length === 0) return null;
  return nodes.find((n) => n.state !== "offline")?.state ?? "offline";
}

function kindOf(n: NodeSummary, t: (k: string) => string): string {
  return isComputeNode(n)
    ? t("overview.kindCompute")
    : n.roles?.includes("dev")
      ? t("overview.kindDev")
      : t("overview.kindTools");
}

function lastKnown(values: (number | null)[]): number | null {
  return [...values].reverse().find((v) => v !== null && v !== undefined) ?? null;
}

/** CPU or GPU: a sparkline with its value written out. */
function LoadCell({ label, values }: { label: string; values: (number | null)[] }) {
  const { t } = useTranslation();
  if (!values.some((v) => v !== null)) return <span className="muted">–</span>;
  const now = lastKnown(values);
  const text = now === null ? "?" : String(Math.round(now));
  return (
    <span className="mt-load">
      <Sparkline
        values={values}
        max={100}
        width={64}
        height={22}
        label={t("overview.loadNow", { what: label, value: text })}
      />
      <span className="mt-pct">{now === null ? "–" : `${text} %`}</span>
    </span>
  );
}

/** RAM or VRAM: a small bar and a percentage; the exact figures are the tooltip, in GB. */
function MemCell({
  label,
  usedMb,
  totalMb,
  detail,
}: {
  label: string;
  usedMb: number | null;
  totalMb: number;
  /** Several GPUs: one line each in the tooltip. */
  detail?: string;
}) {
  const { t, i18n } = useTranslation();
  const gb = (mb: number) => formatDecimal(mbToGb(mb), i18n.language);
  const pct = percent(usedMb, totalMb);
  const exact =
    detail ??
    (usedMb === null
      ? t("machineTable.memUnknown", { total: gb(totalMb) })
      : t("machineTable.mem", { used: gb(usedMb), total: gb(totalMb) }));
  const level = pct === null ? "unknown" : pct >= 90 ? "high" : pct >= 70 ? "mid" : "low";
  return (
    <span className="mt-mem" title={`${label}: ${exact}`}>
      <span
        className={`usage-track usage-${level} mt-track`}
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct ?? undefined}
        aria-valuetext={pct === null ? exact : `${pct} % (${exact})`}
      >
        <span className="usage-fill" style={{ width: `${Math.min(100, pct ?? 0)}%` }} />
      </span>
      <span className="mt-pct">{pct === null ? "–" : `${pct} %`}</span>
    </span>
  );
}

function VramCell({ meters }: { meters: Meters }) {
  const { t, i18n } = useTranslation();
  if (meters.vram.length === 0) return <span className="muted">–</span>;
  const gb = (mb: number) => formatDecimal(mbToGb(mb), i18n.language);
  const total = meters.vram.reduce((s, g) => s + g.totalMb, 0);
  const known = meters.vram.every((g) => g.usedMb !== null);
  const used = known ? meters.vram.reduce((s, g) => s + (g.usedMb as number), 0) : null;
  const detail = meters.vram
    .map((g) =>
      g.usedMb === null
        ? `${g.name}: ${t("machineTable.memUnknown", { total: gb(g.totalMb) })}`
        : `${g.name}: ${t("machineTable.mem", { used: gb(g.usedMb), total: gb(g.totalMb) })}`,
    )
    .join("; ");
  return <MemCell label={t("node.vram")} usedMb={used} totalMb={total} detail={detail} />;
}

/** What runs now: the pieces of work, otherwise the loaded models, otherwise the tool groups. */
function Doing({
  nodes,
  work,
  hasActivity,
}: {
  nodes: NodeSummary[];
  work: NodeWork[];
  hasActivity: boolean;
}) {
  const { t } = useTranslation();
  const live = nodes.filter((n) => n.state !== "offline");
  if (nodes.length > 0 && live.length === 0) {
    return <span className="muted">{t("overview.offlineNoData")}</span>;
  }
  const models = Array.from(new Set(live.flatMap((n) => n.loaded.map((m) => m.name))));
  const groups = Array.from(
    new Set(
      live
        .filter((n) => !isComputeNode(n))
        .flatMap((n) => (n.capabilities ?? []).map((c) => c.split(".")[0] as string)),
    ),
  ).sort();
  return (
    <ul className="mt-doing">
      {work.map((w, i) => (
        <li key={`${w.kind}-${w.model}-${i}`} data-testid="machine-work">
          <strong>{t(`activity.kind.${w.kind}`, { defaultValue: w.kind })}</strong>{" "}
          <span className="muted">
            {w.model} · {duration(w.seconds)}
          </span>
        </li>
      ))}
      {work.length === 0 && hasActivity && live.some(isComputeNode) && (
        <li className="muted">{t("machineTable.free")}</li>
      )}
      {work.length === 0 && models.length > 0 && <li>{models.join(", ")}</li>}
      {work.length === 0 && groups.length > 0 && (
        <li>{t("machineTable.tools", { groups: groups.join(", ") })}</li>
      )}
      {work.length === 0 && models.length === 0 && groups.length === 0 && !hasActivity && (
        <li className="muted">–</li>
      )}
    </ul>
  );
}

function MachineRowView({
  row,
  metrics,
  activity,
}: {
  row: MachineRow;
  metrics: Record<string, NodeMetricPoint[]>;
  activity: ActivityView | null;
}) {
  const { t, i18n } = useTranslation();
  const { nodes } = row;
  const meters = metersOf(nodes, metrics);
  const state = machineState(nodes);
  const names = new Set(nodes.map((n) => n.name));
  const work = (activity?.nodes ?? [])
    .filter((a) => names.has(a.name))
    .flatMap((a) => a.work);
  const queued = (activity?.nodes ?? [])
    .filter((a) => names.has(a.name))
    .reduce((s, a) => s + (a.queued ? a.queued.chat + a.queued.answer + a.queued.idle : 0), 0);
  const first = nodes[0];
  const single = nodes.length === 1 ? first : undefined;
  const ramText =
    meters.ram === null
      ? null
      : meters.ram.usedMb === null
        ? t("machineTable.memUnknown", {
            total: formatDecimal(mbToGb(meters.ram.totalMb), i18n.language),
          })
        : t("machineTable.mem", {
            used: formatDecimal(mbToGb(meters.ram.usedMb), i18n.language),
            total: formatDecimal(mbToGb(meters.ram.totalMb), i18n.language),
          });
  // the node's name is shown only where it adds something: as a tooltip on a single node that is
  // named differently from its machine, and as a chip per node where there are several
  const nodeTitle = single && single.name !== row.name ? single.name : undefined;
  const open = () => {
    if (first) window.location.hash = nodeHref(first.id);
  };
  return (
    <tr
      className={`mt-row${row.vmOf ? " mt-vm" : ""}${state === "offline" ? " state-offline" : ""}`}
      data-testid={`machine-${row.key}`}
      onClick={(e) => {
        if (!(e.target as HTMLElement).closest("a, button")) open();
      }}
    >
      <th scope="row" className="mt-name">
        <span className="mt-title">
          {first ? (
            <a
              href={nodeHref(first.id)}
              title={nodeTitle}
              aria-label={t("overview.openDetail", { name: row.name })}
            >
              {row.name}
            </a>
          ) : (
            row.name
          )}
          {single && <span className="kind-badge">{kindOf(single, t)}</span>}
          {row.vmOf && <span className="kind-badge">{t("machines.vmOn", { host: row.vmOf })}</span>}
        </span>
        {nodes.length > 1 && (
          <ul className="mt-chips" aria-label={t("machineTable.nodesOf", { name: row.name })}>
            {nodes.map((n) => (
              <li key={n.id} data-testid={`node-chip-${n.id}`}>
                <a
                  href={nodeHref(n.id)}
                  aria-label={t("overview.openDetail", { name: n.name })}
                >
                  {n.name}
                </a>{" "}
                <span className="kind-badge">{kindOf(n, t)}</span> <StateBadge state={n.state} />
              </li>
            ))}
          </ul>
        )}
      </th>
      <td data-label={t("machineTable.state")}>{state && <StateBadge state={state} />}</td>
      <td data-label={t("overview.cpuLoad")}>
        <LoadCell label={t("overview.cpuLoad")} values={meters.cpu} />
      </td>
      <td data-label={t("overview.gpuLoad")}>
        <LoadCell label={t("overview.gpuLoad")} values={meters.gpu} />
      </td>
      <td data-label={t("node.ram")}>
        {meters.ram ? (
          <MemCell
            label={t("node.ram")}
            usedMb={meters.ram.usedMb}
            totalMb={meters.ram.totalMb}
            detail={ramText ?? undefined}
          />
        ) : (
          <span className="muted">–</span>
        )}
      </td>
      <td data-label={t("node.vram")}>
        <VramCell meters={meters} />
      </td>
      <td data-label={t("machineTable.doing")}>
        <Doing nodes={nodes} work={work} hasActivity={activity !== null} />
        {queued > 0 && <span className="muted small"> {t("machineTable.queuedCount", { count: queued })}</span>}
      </td>
    </tr>
  );
}

/** One row per machine, virtual machines indented below their host; each reading appears once. */
export function MachineTable({
  rows,
  metrics,
  activity,
}: {
  rows: MachineRow[];
  metrics: Record<string, NodeMetricPoint[]>;
  activity: ActivityView | null;
}) {
  const { t } = useTranslation();
  if (rows.length === 0) return null;
  return (
    <table className="machine-table" data-testid="machine-table">
      <caption className="sr-only">{t("machineTable.caption")}</caption>
      <thead>
        <tr>
          <th scope="col">{t("machineTable.machine")}</th>
          <th scope="col">{t("machineTable.state")}</th>
          <th scope="col">{t("node.cpu")}</th>
          <th scope="col">{t("machineTable.gpu")}</th>
          <th scope="col">{t("node.ram")}</th>
          <th scope="col">{t("node.vram")}</th>
          <th scope="col">{t("machineTable.doing")}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <MachineRowView key={row.key} row={row} metrics={metrics} activity={activity} />
        ))}
      </tbody>
    </table>
  );
}

/** Work that waits, below the table. The hub tells only how many requests wait on each node
 *  today; the single jobs come from GET /api/activity (SCH-03 step 3).
 *  The counts, and since SCH-03 step 3 the single waiting requests. */
export function WaitingWork({ activity }: { activity: ActivityView | null }) {
  const { t } = useTranslation();
  const waiting = (activity?.nodes ?? []).filter(
    (n) => n.queued && n.queued.chat + n.queued.answer + n.queued.idle > 0,
  );
  const pool = activity?.pool_waiting ?? [];
  if (waiting.length === 0 && pool.length === 0 && !(activity && activity.digest.waiting > 0))
    return null;
  return (
    <section className="mt-waiting" data-testid="waiting-work">
      <h2>{t("machineTable.waiting")}</h2>
      {activity && activity.digest.waiting > 0 && (
        <p role="status" data-testid="activity-digest">
          {t("activity.digest", { running: activity.digest.running, waiting: activity.digest.waiting })}
        </p>
      )}
      {pool.length > 0 && (
        <div data-testid="activity-pool-waiting">
          <p>
            <strong>{t("activity.poolWaiting", { count: pool.length })}</strong>
          </p>
          <ul>
            {pool.map((w, i) => (
              <li key={`${w.kind}-${i}`}>
                {t(`activity.kind.${w.kind}`, { defaultValue: w.kind })}{" "}
                <span className="muted">
                  {w.model ? `${w.model} · ` : ""}
                  {t("activity.poolAhead", { count: w.ahead })} · {duration(w.seconds)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <ul>
        {waiting.map((n) => (
          <li key={n.name} data-testid={`activity-queued-${n.name}`}>
            <strong>{n.name}</strong> <span className="muted">{t("activity.queued", n.queued)}</span>
            {n.waiting && n.waiting.length > 0 && (
              <ul data-testid={`activity-waiting-${n.name}`}>
                {n.waiting.map((w, i) => (
                  <li key={`${w.kind}-${i}`}>
                    {t(`activity.kind.${w.kind}`, { defaultValue: w.kind })}{" "}
                    <span className="muted">
                      {w.model ? `${w.model} · ` : ""}
                      {duration(w.seconds)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
