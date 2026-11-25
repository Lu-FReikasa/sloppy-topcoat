import type { TFunction } from "i18next";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ActivityItem } from "../types";

/** What the chat shows of the work behind an answer (E7-01): the chain of thoughts, calls, checks
 *  and retries, as the hub records it. It grows while the answer is made and stays with the
 *  answer afterwards. A call that failed is red at once, and a chain with a failure stays open. */

export interface ActivitySummary {
  steps: number;
  failed: number;
  current: ActivityItem | undefined;
  ms: number;
}

export function summarize(items: ActivityItem[]): ActivitySummary {
  const current = [...items].reverse().find((i) => i.status === "running");
  return {
    steps: items.filter((i) => i.kind !== "note" && i.kind !== "plan" && i.kind !== "step_check").length,
    failed: items.filter((i) => i.status === "failed").length,
    current,
    ms: items.reduce((sum, i) => sum + (i.ms ?? 0), 0),
  };
}

function seconds(ms: number): string {
  return ms < 950 ? `${Math.max(ms, 0)} ms` : `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
}

/** The first line of a longer text, for a collapsed summary. */
function preview(text: string): string {
  const line = text.trim().split("\n")[0] ?? "";
  return line.length > 90 ? `${line.slice(0, 87)}…` : line;
}

/** A check that reads only the web and the notes is a look-up, not a server check. */
function isLookup(item: ActivityItem): boolean {
  const order = item.kind === "check" ? asOrder(item.data.order) : undefined;
  return !!order && order.tools.length > 0 && order.tools.every((g) => g === "web" || g === "notes");
}

function label(t: TFunction, item: ActivityItem): string {
  if (isLookup(item)) return t("chain.kind.lookup");
  return t(`chain.kind.${item.kind}`, { defaultValue: item.kind });
}

/** The words for what one item is doing right now, for the closed summary line. */
function nowText(t: TFunction, item: ActivityItem): string {
  return item.title ? `${label(t, item)} · ${item.title}` : label(t, item);
}

/** The fixed order of a server check (pruefauftrag T4.1), as the hub records it in the check item. */
export interface OrderCard {
  target: string;
  tools: string[];
  goal: string;
  result: string;
  window: string;
  corrections: string[];
  mode: string;
  max_calls?: number | null;
  /** What earlier checks of this application usually needed (the hub's check_steps.py). */
  typical_calls?: number | null;
}

/** The order card: what the check reads, on which machine, for which period, in which form. The
 *  badges are chips; a read-only order has an outline look. Corrections come from the hub as text. */
export function OrderCardView({ order }: { order: OrderCard }) {
  const { t } = useTranslation();
  return (
    <div className="order-card" data-testid="order-card">
      <p className="order-goal">{order.goal || t("order.noGoal")}</p>
      <div className="order-badges small">
        <span className="order-chip" data-testid="order-target">
          {t("order.target", { target: order.target })}
        </span>
        {order.tools.map((key) => (
          <span key={key} className="order-chip">
            {t(`order.group.${key}`, { defaultValue: key })}
          </span>
        ))}
        <span className="order-chip is-outline" data-testid="order-mode">
          {t(`order.mode.${order.mode}`, { defaultValue: order.mode })}
        </span>
        <span className="order-chip">{t("order.window", { window: order.window })}</span>
        <span className="order-chip">
          {t(`order.result.${order.result}`, { defaultValue: order.result })}
        </span>
      </div>
      {order.corrections.length > 0 && (
        <ul className="order-corrections muted small">
          {order.corrections.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** The least steps an order takes: per group one call (the web two: a search and a page), and the
 *  answer at the end. */
export function minSteps(order: OrderCard | undefined): number {
  const groups = order?.tools ?? [];
  const calls = groups.length === 0 ? 1 : groups.reduce((n, g) => n + (g === "web" ? 2 : 1), 0);
  return calls + 1;
}

export interface Progress {
  step: number;
  /** "at least x": the order's least steps, grown with the work (when nothing better is known). */
  of: number;
  /** What earlier checks of this application usually needed, and the most the run may make. */
  typical?: number | null;
  max?: number | null;
}

/** The step a check is at, and what to measure it against. The agent plans no step count: it
 *  decides after every result (2026-11-13). Known are the limit of the run and what earlier checks
 *  usually took; the bar runs towards the limit with a mark at the usual count, so it is never full
 *  before the end. Without them: "step n of at least x", x growing with the work. */
export function progressOf(
  done: number,
  least: number,
  live: boolean,
  running: boolean,
  order?: Pick<OrderCard, "max_calls" | "typical_calls">,
): Progress {
  if (!live) return { step: done, of: done };
  const step = Math.max(1, running ? done : done + 1);
  const max = order?.max_calls && order.max_calls > 0 ? order.max_calls + 1 : null; // + the answer
  const typical = order?.typical_calls && order.typical_calls > 0 ? Math.max(least, order.typical_calls + 1) : null;
  // after the step underway at least the answer comes, until the answer is the step
  const usual = max && typical ? Math.min(typical, max) : typical;
  return { step, of: Math.max(least, step), typical: usual ?? undefined, max: max ?? undefined };
}

/** The calls a check made after its start item, up to the next check (T4.2): the step count. */
export function stepsAfter(items: ActivityItem[], index: number): number {
  let n = 0;
  for (const i of items.slice(index + 1)) {
    if (i.kind === "check") break;
    if (i.kind === "tool") n += 1;
  }
  return n;
}

export function asOrder(value: unknown): OrderCard | undefined {
  if (!value || typeof value !== "object") return undefined;
  const o = value as Partial<OrderCard>;
  return typeof o.target === "string" && Array.isArray(o.tools) ? (o as OrderCard) : undefined;
}

function Row({ item }: { item: ActivityItem }) {
  const { t } = useTranslation();
  const long = item.kind === "thinking" || item.kind === "lead_in";
  const failed = item.status === "failed";
  return (
    <li
      className={`activity-item activity-${item.status} activity-kind-${item.kind}`}
      data-testid={`activity-${item.kind}`}
    >
      <span className="activity-dot" aria-hidden="true" />
      <span className="activity-main">
        <span className="activity-label">{label(t, item)}</span>
        {item.title && <code className="activity-title">{item.title}</code>}
        {typeof item.data.event === "string" && (
          <span className="activity-state muted small">
            {t(`chain.event.${item.data.event}`, { defaultValue: item.data.event })}
          </span>
        )}
        {item.status === "running" && (
          <span className="activity-state muted small">{t("chain.running")}</span>
        )}
        {failed && (
          <span className="activity-state activity-failed small" role="alert">
            {t("chain.failed")}
            {item.detail ? `: ${t(`chain.reason.${item.detail}`, { defaultValue: item.detail })}` : ""}
          </span>
        )}
        {item.status === "stopped" && (
          <span className="activity-state muted small">{t("chain.stopped")}</span>
        )}
        {item.ms !== null && item.ms > 0 && (
          <span className="activity-time muted small">{seconds(item.ms)}</span>
        )}
      </span>
      {item.detail && long && (
        <details className="activity-text" open={item.status === "running"}>
          <summary className="muted small">{preview(item.detail)}</summary>
          <pre>{item.detail}</pre>
        </details>
      )}
      {item.detail && !long && !failed && (
        <span className="activity-note muted small">{item.detail}</span>
      )}
    </li>
  );
}

/** How many of the last items the compact view shows. */
const RECENT = 3;

/** The progress of a check: "Step 4 of at least 6" and a bar. */
export function StepBar({
  step,
  of,
  live,
  typical,
  max,
}: {
  step: number;
  of: number;
  live: boolean;
  typical?: number | null;
  max?: number | null;
}) {
  const { t } = useTranslation();
  const text = !live
    ? t("chain.progressDone", { count: step })
    : max
      ? typical
        ? t("chain.progressTypical", { n: step, typical, max })
        : t("chain.progressMax", { n: step, max })
      : t("chain.progress", { n: step, of });
  // with a limit the bar measures against it (never full before the end), else against "at least"
  const scale = max ?? of;
  const pct = (n: number) => `${Math.round((Math.min(n, scale) / Math.max(scale, 1)) * 100)}%`;
  return (
    <span className="activity-progress-wrap">
      {live && (
        <span
          className="activity-progress"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={scale}
          aria-valuenow={step}
          aria-label={text}
        >
          <span style={{ width: pct(step) }} />
          {max && typical ? (
            <i className="activity-progress-mark" style={{ left: pct(typical) }} data-testid="progress-typical" />
          ) : null}
        </span>
      )}
      <span className="muted small" data-testid="activity-progress">
        {text}
      </span>
    </span>
  );
}

/** The work behind an answer, compact (the maintainer, 2026-11-12): one line with what runs and
 *  how far it is, the order once, the last few steps; the whole chain on request. */
export function ActivityTimeline({ items, live }: { items: ActivityItem[]; live: boolean }) {
  const { t } = useTranslation();
  const [all, setAll] = useState(false);
  if (items.length === 0) return null;
  const sum = summarize(items);
  let checkAt = -1;
  items.forEach((i, idx) => {
    if (i.kind === "check") checkAt = idx;
  });
  const check = checkAt >= 0 ? items[checkAt] : undefined;
  const order = check ? asOrder(check.data.order) : undefined;
  const progress = order
    ? progressOf(stepsAfter(items, checkAt), minSteps(order), live, sum.current?.kind === "tool", order)
    : null;
  const line = live
    ? sum.current
      ? t("chain.now", { what: nowText(t, sum.current) })
      : t("chain.working")
    : sum.failed > 0
      ? t("chain.summaryFailed", { count: sum.steps, failed: sum.failed })
      : t("chain.summary", { count: sum.steps });
  // the steps worth a line: no plan lines and no notes in the compact view
  const steps = items.filter((i) => i.kind !== "plan" && i.kind !== "note" && i.kind !== "check");
  const shown = all ? items : steps.slice(-RECENT);
  const hidden = items.length - shown.length;
  return (
    <details
      className={`activity${sum.failed > 0 ? " has-failed" : ""}${live ? " is-live" : ""}`}
      open={live}
      data-testid="activity"
    >
      <summary>
        <span>{line}</span>
        {progress && (
          <StepBar step={progress.step} of={progress.of} live={live} typical={progress.typical} max={progress.max} />
        )}
        {sum.failed > 0 && live && (
          <span className="activity-failed small" role="status">
            {t("chain.failedSoFar", { failed: sum.failed })}
          </span>
        )}
        {!live && sum.ms > 0 && <span className="muted small"> · {seconds(sum.ms)}</span>}
      </summary>
      {order && <OrderCardView order={order} />}
      <ol className="activity-list">
        {shown.map((i) => (
          <Row key={i.id} item={i} />
        ))}
      </ol>
      {(hidden > 0 || all) && (
        <button type="button" className="linklike small activity-more" onClick={() => setAll((a) => !a)}>
          {all ? t("chain.showLess") : t("chain.showAll", { count: items.length })}
        </button>
      )}
    </details>
  );
}
