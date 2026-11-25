import { useCallback, useEffect, useState, type ReactNode } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatDateTime } from "../format";
import type { InventoryItem, InventoryNode } from "../types";
import { ChangeDialog, LogsDialog, type Target } from "./Actions";

interface Props {
  api: ApiClient;
}

/** How old a look is, in words a person reads at a glance. */
export function ageText(seconds: number | null, t: TFunction): string {
  if (seconds === null) return t("inventory.never");
  if (seconds < 90) return t("inventory.ageSeconds", { count: seconds });
  if (seconds < 5400) return t("inventory.ageMinutes", { count: Math.round(seconds / 60) });
  if (seconds < 172800) return t("inventory.ageHours", { count: Math.round(seconds / 3600) });
  return t("inventory.ageDays", { count: Math.round(seconds / 86400) });
}

/** The inventory (E3-10): what runs on which node — containers, compose projects, services — and
 *  how old that knowledge is. A node that is offline, a look that is old and a source that could not
 *  be read are shown as such: the page never presents a stored look as the live state. */
export function InventoryPage({ api }: Props) {
  const { t, i18n } = useTranslation();
  const [nodes, setNodes] = useState<InventoryNode[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fail = useCallback(
    (err: unknown) => {
      if (!(err instanceof UnauthorizedError)) setError(describeError(toActionError(err), t, i18n));
    },
    [t, i18n],
  );
  const load = useCallback(() => {
    api.inventory().then((r) => {
      setError(null);
      setNodes(r.nodes);
    }, fail);
  }, [api, fail]);

  useEffect(() => {
    load();
    const timer = setInterval(load, 30000);
    return () => clearInterval(timer);
  }, [load]);

  return (
    <section className="inventory">
      <h1>{t("inventory.heading")}</h1>
      <p className="muted">{t("inventory.intro")}</p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {nodes && nodes.length === 0 && <p className="muted">{t("inventory.none")}</p>}
      {nodes?.map((n) => (
        <NodeInventory
          key={n.node.id}
          n={n}
          api={api}
          onChanged={(updated) =>
            setNodes((all) => all?.map((x) => (x.node.id === updated.node.id ? updated : x)) ?? all)
          }
          fail={fail}
          locale={i18n.resolvedLanguage ?? "en"}
        />
      ))}
    </section>
  );
}

function verdict(n: InventoryNode): "current" | "stale" | "offline" | "never" | "unavailable" {
  if (n.status === "unavailable") return "unavailable";
  if (n.collected_at === null) return "never";
  if (!n.online) return "offline";
  return n.current ? "current" : "stale";
}

function NodeInventory({
  n,
  api,
  onChanged,
  fail,
  locale,
}: {
  n: InventoryNode;
  api: ApiClient;
  onChanged: (n: InventoryNode) => void;
  fail: (e: unknown) => void;
  locale: string;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [logsFor, setLogsFor] = useState<Target | null>(null);
  const [changeFor, setChangeFor] = useState<Target | null>(null);
  const v = verdict(n);
  const can = n.can ?? { read: [], change: [] };

  async function refresh() {
    setBusy(true);
    try {
      onChanged(await api.refreshInventory(n.node.id));
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  /** The buttons next to one row: logs where the user may read them, a restart where the node and
   *  the hub offer it. A restart asks first (ChangeDialog): the row only opens that card. */
  function rowActions(i: InventoryItem): ReactNode {
    if (i.gone) return null;
    const target: Target = {
      kind: i.kind === "compose" ? "stack" : i.kind === "service" ? "service" : "container",
      name: i.name,
    };
    const logs = i.kind === "container" ? "docker.logs" : i.kind === "compose" ? "compose.logs" : null;
    const restart = i.kind === "container" ? "docker.restart" : i.kind === "compose" ? "compose.restart" : "service.restart";
    const alive =
      i.kind === "container" ? i.state === "running" || i.state === "restarting" : i.kind === "compose" ? i.state === "running" || i.state === "partial" : i.state === "active";
    return (
      <span className="row-actions">
        {logs && can.read.includes(logs) && (
          <button type="button" className="small" disabled={!n.online} onClick={() => setLogsFor(target)}>
            {t("inventory.actions.logs")}
          </button>
        )}
        {can.change.includes(restart) && alive && (
          <button type="button" className="small" disabled={!n.online} onClick={() => setChangeFor(target)}>
            {t(i.kind === "compose" ? "inventory.actions.restartStack" : "inventory.actions.restart")}
          </button>
        )}
      </span>
    );
  }

  const failed = Object.entries(n.sections).filter(([, s]) => s.state === "failed");
  return (
    <article className="card" data-testid="inventory-node">
      <div className="card-head">
        <h2>{n.node.name}</h2>
        <span className={`badge badge-${v === "current" ? "available" : "degraded"}`} data-testid="inventory-verdict">
          {t(`inventory.verdict.${v}`)}
        </span>
        <span className="spacer" />
        <button
          type="button"
          disabled={busy || !n.online}
          title={n.online ? undefined : t("inventory.offlineNoRefresh")}
          onClick={() => void refresh()}
        >
          {t("inventory.refresh")}
        </button>
      </div>
      <p className="muted small">
        {n.host?.os ? `${n.host.os} · ` : ""}
        {t("inventory.lastLook", {
          age: ageText(n.age_seconds, t),
          at: formatDateTime(n.collected_at, locale) ?? "–",
        })}
      </p>
      {n.status === "unavailable" && n.error && (
        <p className="notice" data-testid="inventory-unavailable">
          {t("inventory.unavailable", { reason: n.error })}
        </p>
      )}
      {v === "offline" && <p className="notice">{t("inventory.offlineLastKnown")}</p>}
      {v === "stale" && n.online && <p className="notice">{t("inventory.staleNotice")}</p>}
      {failed.map(([name, s]) => (
        <p key={name} className="notice" data-testid="inventory-failed-section">
          {t("inventory.sectionFailed", {
            section: t(`inventory.section.${name}`),
            reason: s.error ?? "",
          })}
        </p>
      ))}
      <ItemTable title={t("inventory.compose")} items={n.compose} kind="compose" actions={rowActions} />
      <ItemTable title={t("inventory.containers")} items={n.containers} kind="container" actions={rowActions} />
      <ItemTable title={t("inventory.services")} items={n.services} kind="service" actions={rowActions} />
      {logsFor && <LogsDialog api={api} nodeId={n.node.id} target={logsFor} onClose={() => setLogsFor(null)} />}
      {changeFor && (
        <ChangeDialog
          api={api}
          nodeId={n.node.id}
          target={changeFor}
          onClose={() => setChangeFor(null)}
          onDone={() => void refresh()}
        />
      )}
    </article>
  );
}

function detailText(i: InventoryItem, t: TFunction): string {
  if (i.kind === "compose") return t("inventory.composeCount", { running: i.running ?? 0, total: i.services ?? 0 });
  const d = i.detail ?? {};
  const parts = [d.image, d.compose_project ? `${t("inventory.project")} ${String(d.compose_project)}` : "", d.description]
    .filter((x): x is string => typeof x === "string" && x !== "");
  return parts.join(" · ");
}

function ItemTable({
  title,
  items,
  kind,
  actions,
}: {
  title: string;
  items: InventoryItem[];
  kind: string;
  actions?: (i: InventoryItem) => ReactNode;
}) {
  const { t } = useTranslation();
  const [showGone, setShowGone] = useState(false);
  const live = items.filter((i) => !i.gone);
  const gone = items.filter((i) => i.gone);
  if (items.length === 0) return null;
  const rows = showGone ? [...live, ...gone] : live;
  return (
    <div data-testid={`inventory-${kind}`}>
      <h3>
        {title} <span className="muted small">({live.length})</span>
      </h3>
      <div className="table-wrap">
        <table className="models">
          <tbody>
            {rows.map((i) => (
              <tr key={`${i.kind}:${i.name}`} className={i.gone ? "muted" : undefined}>
                <td>{i.name}</td>
                <td>{i.gone ? t("inventory.gone") : i.state}</td>
                <td className="muted small">{detailText(i, t)}</td>
                {actions && <td className="actions-cell">{actions(i)}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {gone.length > 0 && (
        <button type="button" className="link" onClick={() => setShowGone((s) => !s)}>
          {showGone ? t("inventory.hideGone") : t("inventory.showGone", { count: gone.length })}
        </button>
      )}
    </div>
  );
}
