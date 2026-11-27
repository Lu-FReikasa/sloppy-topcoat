import { useTranslation } from "react-i18next";
import { AppBadge } from "./AppBadge";
import { DoneBox, DueText, LabelChips, PriorityMark } from "./parts";
import type { ProjectNode } from "./types";

export interface Row {
  node: ProjectNode;
  depth: number;
  hasChildren: boolean;
  childCount: number;
}

function icon(n: ProjectNode): string {
  if (n.kind === "topic") return "▣";
  if (n.kind === "task") return "☐";
  return "✎";
}

function label(n: ProjectNode, t: (key: string) => string): string {
  if (n.kind === "task" && n.status)
    return t(`project.taskStatuses.${n.status}`);
  if (n.kind === "report" && n.report_kind)
    return t(`project.reportKinds.${n.report_kind}`);
  return t("project.kindTopic");
}

/** The tree (or, with a filter, the flat list of what matched): number, title, status, due time,
 *  priority and labels, and a box to mark a task done. */
export function ListView({
  rows,
  collapsed,
  selectedId,
  now,
  zone,
  onToggle,
  onOpen,
  onComplete,
}: {
  rows: Row[];
  collapsed: Set<string>;
  selectedId: string | null;
  now: Date;
  zone: string;
  onToggle: (id: string) => void;
  onOpen: (id: string) => void;
  onComplete: (node: ProjectNode) => void;
}) {
  const { t } = useTranslation();
  return (
    <ul className="project-list" aria-label={t("project.tree")}>
      {rows.map(({ node: n, depth, hasChildren, childCount }) => (
        <li key={n.id} style={{ paddingLeft: `${depth * 1.25}rem` }}>
          <div
            className={`project-row${n.id === selectedId ? " is-selected" : ""}${n.current === false ? " is-replaced" : ""}`}
          >
            {hasChildren ? (
              <button
                type="button"
                className="project-toggle"
                aria-label={
                  collapsed.has(n.id)
                    ? t("project.expand")
                    : t("project.collapse")
                }
                onClick={() => onToggle(n.id)}
              >
                {collapsed.has(n.id) ? `▸ ${childCount}` : "▾"}
              </button>
            ) : (
              <span className="project-toggle" aria-hidden="true" />
            )}
            <DoneBox node={n} onComplete={onComplete} />
            <button
              type="button"
              className="project-open"
              data-node-id={n.id}
              onClick={() => onOpen(n.id)}
            >
              <span className="project-number">{n.number}</span>
              <span className="project-icon" aria-hidden="true">
                {icon(n)}
              </span>
              <span className="project-name">{n.title}</span>
              <AppBadge application={n.application} mode={n.mode} />
              <span className="project-muted">{label(n, t)}</span>
              {n.current === false && (
                <span className="project-chip">{t("project.replaced")}</span>
              )}
            </button>
            <PriorityMark priority={n.priority} />
            <DueText node={n} now={now} zone={zone} />
            <LabelChips labels={n.labels} />
          </div>
        </li>
      ))}
    </ul>
  );
}
