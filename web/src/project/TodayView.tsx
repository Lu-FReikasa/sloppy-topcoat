import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { DoneBox, LabelChips, PriorityMark, DueText } from "./parts";
import type { ProjectFound, ProjectNode } from "./types";

/** The filter of the built-in view "Today" (hub: project_view_defs.BUILTIN). */
export const TODAY_FILTER = "done = false && dueDate < now/d+1d";
export const TODAY_SORT = "dueDate,priority:desc";

/** What is due, across all projects: the late tasks first, then today's. */
export function TodayView({
  api,
  now,
  zone,
  tick,
  onOpen,
  onComplete,
  onError,
}: {
  api: ApiClient;
  now: Date;
  zone: string;
  /** Changes when something was written elsewhere on the page: the list is read again. */
  tick: number;
  onOpen: (node: ProjectFound) => void;
  onComplete: (node: ProjectNode) => Promise<void>;
  onError: (err: unknown) => void;
}) {
  const { t } = useTranslation();
  const [nodes, setNodes] = useState<ProjectFound[] | null>(null);

  const load = useCallback(async () => {
    try {
      setNodes(
        (
          await api.searchProjectNodes({
            filter: TODAY_FILTER,
            sort: TODAY_SORT,
            limit: 200,
          })
        ).nodes,
      );
    } catch (err) {
      onError(err);
    }
  }, [api, onError]);

  useEffect(() => {
    // after this render, not inside it
    void Promise.resolve().then(load);
  }, [load, tick]);

  if (nodes === null)
    return <p className="project-muted">{t("project.loading")}</p>;
  const late = nodes.filter(
    (n) => n.due_at && new Date(n.due_at).getTime() < now.getTime(),
  );
  const today = nodes.filter((n) => !late.includes(n));
  const group = (title: string, rows: ProjectFound[]) =>
    rows.length === 0 ? null : (
      <section aria-label={title}>
        <h3>{title}</h3>
        <ul className="project-list">
          {rows.map((n) => (
            <li key={n.id} className="project-row">
              <DoneBox
                node={n}
                onComplete={async (x) => {
                  await onComplete(x);
                  await load();
                }}
              />
              <button
                type="button"
                className="project-open"
                onClick={() => onOpen(n)}
              >
                <span className="project-name">{n.title}</span>
                <span className="project-muted">{n.ref}</span>
              </button>
              <PriorityMark priority={n.priority} />
              <DueText node={n} now={now} zone={zone} />
              <LabelChips labels={n.labels} />
            </li>
          ))}
        </ul>
      </section>
    );
  return (
    <div className="project-today" aria-label={t("project.todayTitle")}>
      <h2>{t("project.todayTitle")}</h2>
      {nodes.length === 0 && (
        <p className="project-muted">{t("project.todayEmpty")}</p>
      )}
      {group(t("project.overdue"), late)}
      {group(t("project.dueToday"), today)}
    </div>
  );
}
