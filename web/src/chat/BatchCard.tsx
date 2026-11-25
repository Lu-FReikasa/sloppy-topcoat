import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { AppBadge } from "../project/AppBadge";
import type { ProjectBatchState, ProjectChange } from "../project/types";

/** A batch the planner prepared, as its own list in the chat (project_tools.py, BATCH_TOOL).
 *  Before the click it lists the tasks in order with the start button under them. After the
 *  click it shows the state of each task while the batch runs, read from the hub every few
 *  seconds until the batch has ended. */

const POLL_MS = 5000;

export function BatchCard({
  change,
  api,
  busy,
  onDecide,
}: {
  change: ProjectChange;
  api: ApiClient;
  busy: boolean;
  onDecide: (apply: boolean) => void;
}) {
  const { t } = useTranslation();
  const batchId = batchIdOf(change);
  const [state, setState] = useState<ProjectBatchState | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!batchId) return;
    let stopped = false;
    let timer: number | undefined;
    const load = () => {
      api
        .projectBatchState(batchId)
        .then((next) => {
          if (stopped) return;
          setState(next);
          setFailed(false);
          if (next.status === "active") timer = window.setTimeout(load, POLL_MS);
        })
        .catch(() => {
          if (stopped) return;
          setFailed(true);
          timer = window.setTimeout(load, POLL_MS);
        });
    };
    load();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [api, batchId]);

  const tasks = change.preview.fields.tasks;
  const names = Array.isArray(tasks) ? (tasks as string[]) : [];
  const apps = (change.preview.fields.task_apps ?? []) as {
    application: string | null;
    mode: string;
  }[];

  return (
    <div className="batch-card" data-testid="batch-card" data-status={change.status}>
      <div>
        <strong>{t("changes.cap.projectBatch")}</strong>
        {change.preview.target && <span> · {change.preview.target}</span>}
        <span className="badge" data-testid="change-status">
          {t(`changes.status.${change.status}`)}
        </span>
      </div>

      {change.status === "pending" && (
        <>
          <ol className="batch-tasks">
            {names.map((name, i) => (
              <li key={name}>
                {name}
                <AppBadge
                  application={apps[i]?.application}
                  mode={apps[i]?.mode}
                />
              </li>
            ))}
          </ol>
          <p className="muted small">
            {t("changes.batch.runs", {
              runs: 0,
              max: change.preview.fields.max_runs,
            })}
          </p>
          <div className="row">
            <button
              type="button"
              disabled={busy}
              onClick={() => onDecide(true)}
            >
              {t("changes.batch.start")}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onDecide(false)}
            >
              {t("changes.refuse")}
            </button>
          </div>
        </>
      )}

      {change.status !== "pending" && batchId && !state && (
        <p className="muted small">
          {failed ? t("changes.batch.loadFailed") : t("changes.batch.loading")}
        </p>
      )}

      {state && (
        <>
          <p className="small" data-testid="batch-status">
            {t(`changes.batch.status.${state.status}`)} ·{" "}
            {t("changes.batch.runs", { runs: state.runs, max: state.max_runs })}
          </p>
          {state.stop_reason && (
            <p className="notice-line small">
              {t("changes.batch.stopReason", { reason: state.stop_reason })}
            </p>
          )}
          <ol className="batch-tasks" data-testid="batch-tasks">
            {state.tasks.map((task) => (
              <li key={task.node_id} data-status={task.status}>
                <span>
                  {task.number} {task.title}
                </span>{" "}
                <AppBadge application={task.application} mode={task.mode} />{" "}
                {task.class && task.class !== "public" && (
                  <span className="badge" data-testid="batch-task-class">
                    {t(`changes.batch.class.${task.class}`)}
                  </span>
                )}{" "}
                <span className="badge" data-testid="batch-task-status">
                  {t(`changes.batch.node.${task.status}`, {
                    defaultValue: task.status,
                  })}
                </span>
                {task.waits_for.length > 0 && (
                  <p className="muted small">
                    {t("changes.batch.waitsFor", {
                      tasks: task.waits_for.map((w) => w.number).join(", "),
                    })}
                  </p>
                )}
                {task.reason && (
                  <p className="notice-line small">
                    {t("changes.batch.reason", { reason: task.reason })}
                  </p>
                )}
              </li>
            ))}
          </ol>
        </>
      )}

      {change.status === "failed" && change.result?.message != null && (
        <p className="notice-line">{String(change.result.message)}</p>
      )}
    </div>
  );
}

/** The batch the click started, when it started one (the apply returns it in its result). */
function batchIdOf(change: ProjectChange): string | null {
  const batch = change.result?.batch;
  if (change.status !== "applied" || batch === null || typeof batch !== "object") return null;
  const id = (batch as { id?: unknown }).id;
  return typeof id === "string" ? id : null;
}
