import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatBytes, percent } from "../format";
import type { ModelTransfer, NodeEvent } from "../types";

const POLL_MS = 3000;
const SHOWN = 8;
const EVENT_KINDS = new Set(["transfer_ready", "transfer_progress"]);

export function isActiveTransfer(tr: ModelTransfer): boolean {
  return tr.status === "pending" || tr.status === "preparing" || tr.status === "ready" || tr.status === "transferring";
}

/** Newest transfer event id: a change triggers a refetch (the stream pushes node events). */
function latestTransferEventId(events: NodeEvent[]): number {
  let id = 0;
  for (const e of events) if (EVENT_KINDS.has(e.kind) && e.id > id) id = e.id;
  return id;
}

/** Bytes done so far for a transfer, from the newest progress events of either side. */
function progressOf(tr: ModelTransfer, events: NodeEvent[]): { done: number; total: number } | null {
  let best: { done: number; total: number } | null = null;
  for (const e of events) {
    if (e.kind !== "transfer_progress" || e.data.transfer_id !== tr.id) continue;
    const done = Number(e.data.transferred_bytes);
    const total = Number(e.data.total_bytes) || tr.size || 0;
    if (!Number.isFinite(done)) continue;
    if (!best || done > best.done) best = { done, total };
  }
  if (tr.status === "done" && tr.size) return { done: tr.size, total: tr.size };
  return best;
}

/**
 * Transfers of one node (as source or target; `nodeId`) or of all nodes, optionally only for
 * one `model`. Newest first, live while one is active.
 */
export function TransferList({
  api,
  nodeId,
  model,
  events,
  refreshKey,
}: {
  api: ApiClient;
  nodeId?: string;
  model?: string;
  events: NodeEvent[];
  /** Change it to refetch (e.g. after a transfer was created). */
  refreshKey?: number;
}) {
  const { t, i18n } = useTranslation();
  const [transfers, setTransfers] = useState<ModelTransfer[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [canceling, setCanceling] = useState<string | null>(null);
  // id -> name, to show the other end of a transfer.
  const [nodeNames, setNodeNames] = useState<Record<string, string>>({});
  const hasRows = transfers.length > 0;
  useEffect(() => {
    if (!hasRows) return;
    let alive = true;
    api.listNodes().then(
      (list) => {
        if (alive) setNodeNames(Object.fromEntries(list.map((n) => [n.id, n.name])));
      },
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [api, hasRows]);

  const load = useCallback(() => {
    api.listModelTransfers(nodeId).then(
      (list) => setTransfers(model === undefined ? list : list.filter((x) => x.model === model)),
      () => undefined,
    );
  }, [api, nodeId, model]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const eventId = latestTransferEventId(events);
  const lastEventId = useRef(eventId);
  useEffect(() => {
    if (eventId !== lastEventId.current) {
      lastEventId.current = eventId;
      load();
    }
  }, [eventId, load]);

  const active = transfers.some(isActiveTransfer);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [active, load]);

  async function cancel(id: string) {
    setCanceling(id);
    setError(null);
    try {
      const row = await api.cancelModelTransfer(id);
      setTransfers((list) => list.map((x) => (x.id === row.id ? row : x)));
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) {
        setError(describeError(toActionError(err), t, i18n));
      }
    }
    setCanceling(null);
  }

  if (transfers.length === 0) return null;
  return (
    <>
      <h3>{t("transfer.heading")}</h3>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <ul className="downloads transfers">
        {transfers.slice(0, SHOWN).map((tr) => (
          <TransferItem
            key={tr.id}
            transfer={tr}
            nodeId={nodeId}
            events={events}
            nodeNames={nodeNames}
            canceling={canceling === tr.id}
            onCancel={() => void cancel(tr.id)}
          />
        ))}
      </ul>
    </>
  );
}

function TransferItem({
  transfer: tr,
  nodeId,
  events,
  nodeNames,
  canceling,
  onCancel,
}: {
  transfer: ModelTransfer;
  nodeId: string | undefined;
  events: NodeEvent[];
  nodeNames: Record<string, string>;
  canceling: boolean;
  onCancel: () => void;
}) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const outgoing = tr.source_node_id === nodeId;
  const otherId = outgoing ? tr.target_node_id : tr.source_node_id;
  const other = nodeNames[otherId] ?? otherId;
  const prog = progressOf(tr, events);
  const pct = prog && prog.total > 0 ? percent(prog.done, prog.total) : null;
  const active = isActiveTransfer(tr);
  const showBar = tr.status === "transferring" || (active && prog !== null);
  const label =
    nodeId === undefined
      ? t("transfer.fromTo", {
          model: tr.model,
          source: nodeNames[tr.source_node_id] ?? tr.source_node_id,
          target: nodeNames[tr.target_node_id] ?? tr.target_node_id,
        })
      : t(outgoing ? "transfer.to" : "transfer.fromNode", { model: tr.model, node: other });

  let problem: string | null = null;
  if (tr.error_code) {
    const key = `transfer.errorCode.${tr.error_code}`;
    problem = i18n.exists(key) ? t(key) : tr.error || tr.error_code;
  } else if (tr.error) {
    problem = tr.error;
  }

  return (
    <li className={`download download-${tr.status === "done" ? "done" : tr.status === "failed" ? "failed" : "running"}`}>
      <span className="model-name">{label}</span>
      {tr.move && <span className="muted small">{t("transfer.moveTag")}</span>}
      <span className="download-status">{t(`transfer.status.${tr.status}`)}</span>
      {prog && prog.total > 0 && (
        <span className="usage-value">
          {t("download.amountOf", {
            done: formatBytes(prog.done, locale),
            total: formatBytes(prog.total, locale),
            pct,
          })}
        </span>
      )}
      {problem && (
        <span className={tr.status === "done" ? "warn-text" : "error"} role={tr.status === "done" ? undefined : "alert"}>
          {problem}
        </span>
      )}
      {active && (
        <button type="button" disabled={canceling} onClick={onCancel}>
          {t("transfer.cancel")}
        </button>
      )}
      {showBar && (
        <div
          className="usage-track"
          role="progressbar"
          aria-label={label}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={pct ?? undefined}
        >
          <div className="usage-fill" style={{ width: `${Math.min(100, pct ?? 0)}%` }} />
        </div>
      )}
    </li>
  );
}
