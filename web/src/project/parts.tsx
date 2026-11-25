import { useTranslation } from "react-i18next";
import type { ProjectLabel, ProjectNode } from "./types";
import { formatDue, isOverdue } from "./time";

/** The chips of a node's labels. */
export function LabelChips({ labels }: { labels?: ProjectLabel[] }) {
  if (!labels || labels.length === 0) return null;
  return (
    <span className="project-chips">
      {labels.map((l) => (
        <span
          key={l.id}
          className="project-chip"
          style={l.color ? { borderColor: l.color, color: l.color } : undefined}
        >
          {l.title}
        </span>
      ))}
    </span>
  );
}

/** "!3" for a priority from 1 to 5; nothing for none. */
export function PriorityMark({ priority }: { priority?: number }) {
  const { t } = useTranslation();
  if (!priority) return null;
  return (
    <span
      className={`project-priority p${priority}`}
      title={t(`project.priorityLevels.${priority}`)}
    >
      !{priority}
    </span>
  );
}

/** The due time of an open task, red when it has passed. */
export function DueText({
  node,
  now,
  zone,
}: {
  node: ProjectNode;
  now: Date;
  zone: string;
}) {
  const { i18n } = useTranslation();
  if (!node.due_at) return null;
  const open = node.status !== "done" && node.status !== "dropped";
  return (
    <time
      className={`project-due${open && isOverdue(node.due_at, now) ? " is-overdue" : ""}`}
      dateTime={node.due_at}
    >
      {formatDue(node.due_at, i18n.language, zone)}
    </time>
  );
}

/** The checkbox that marks a task done. A task that is done shows ticked and cannot be ticked
 *  again; a repeating one comes back open from the hub. */
export function DoneBox({
  node,
  onComplete,
}: {
  node: ProjectNode;
  onComplete: (node: ProjectNode) => void;
}) {
  const { t } = useTranslation();
  if (node.kind !== "task")
    return <span className="project-donespace" aria-hidden="true" />;
  const done = node.status === "done";
  // the label is the tap area: a finger needs more than the box itself
  return (
    <label className="project-donelabel">
      <input
        type="checkbox"
        className="project-donebox"
        aria-label={`${t("project.complete")}: ${node.ref}`}
        checked={done}
        disabled={done || node.status === "dropped"}
        onChange={() => onComplete(node)}
      />
    </label>
  );
}
