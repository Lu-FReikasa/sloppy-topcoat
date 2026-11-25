import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { ActivityPanel, useActivity } from "./ActivityPanel";
import { AddNodeDialog } from "./AddNodeDialog";
import { MachineTable, WaitingWork, rowsOf, type NodeFilter } from "./MachineTable";
import { MachinesEditor } from "./MachinesEditor";
import { isComputeNode } from "./ModelCatalogue";
import type { MachinesView, NodeMetricPoint, NodeSummary } from "../types";

interface Props {
  nodes: NodeSummary[];
  loaded: boolean;
  error: string | null;
  /** With an API client the page also shows what the nodes do now (the activity panel) and the load graphs. */
  api?: ApiClient;
  /** The person may add a node (an admin): the button that opens the pairing dialog. */
  canAdd?: boolean;
}

const METRICS_POLL_MS = 10_000;

export function Overview({ nodes, loaded, error, api, canAdd = false }: Props) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState<NodeFilter>("all");
  const [metrics, setMetrics] = useState<Record<string, NodeMetricPoint[]>>({});
  const [adding, setAdding] = useState(false);
  const [machines, setMachines] = useState<MachinesView | null>(null);
  const [arranging, setArranging] = useState(false);
  const { view: activity, failed } = useActivity(api);

  useEffect(() => {
    if (!api) return;
    let alive = true;
    api.machines().then(
      (m) => alive && setMachines(m),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [api, nodes.length]);

  useEffect(() => {
    if (!api) return;
    let alive = true;
    const pull = () => {
      api
        .nodeMetrics()
        .then((m) => alive && setMetrics(m.nodes))
        .catch(() => undefined);
    };
    pull();
    const id = setInterval(pull, METRICS_POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [api]);

  const compute = nodes.filter(isComputeNode);
  const tools = nodes.filter((n) => !isComputeNode(n));
  const rows = rowsOf(machines, nodes, filter);

  return (
    <section>
      <div className="overview-head">
        <h1>{t("overview.heading")}</h1>
        {canAdd && api && machines && (
          <button type="button" onClick={() => setArranging(true)}>
            {t("machines.arrange")}
          </button>
        )}
        {canAdd && api && (
          <button
            type="button"
            className="primary"
            onClick={() => setAdding(true)}
          >
            {t("header.addNode")}
          </button>
        )}
      </div>
      {error && (
        <p className="error" role="alert">
          {t("overview.loadError", { message: error })}
        </p>
      )}
      {api && failed && !activity && (
        <p className="error" role="alert">
          {t("activity.loadError")}
        </p>
      )}
      {adding && api && (
        <AddNodeDialog api={api} onClose={() => setAdding(false)} />
      )}
      {arranging && api && machines && (
        <MachinesEditor
          api={api}
          view={machines}
          onClose={() => setArranging(false)}
          onSaved={setMachines}
        />
      )}
      {!loaded && !error && <p className="muted">{t("app.loading")}</p>}
      {loaded && nodes.length === 0 && (
        <p className="empty">{t("overview.empty")}</p>
      )}
      {nodes.length > 0 && (
        <div
          className="node-filter"
          role="group"
          aria-label={t("overview.filter")}
        >
          {(["all", "compute", "tools"] as const).map((f) => (
            <button
              key={f}
              type="button"
              aria-pressed={filter === f}
              className={filter === f ? "active" : ""}
              onClick={() => setFilter(f)}
            >
              {t(`overview.filter_${f}`)}
              {" ("}
              {f === "all"
                ? nodes.length
                : f === "compute"
                  ? compute.length
                  : tools.length}
              {")"}
            </button>
          ))}
        </div>
      )}
      <MachineTable rows={rows} metrics={metrics} activity={activity} />
      <WaitingWork activity={activity} />
      {activity && <ActivityPanel view={activity} />}
    </section>
  );
}
