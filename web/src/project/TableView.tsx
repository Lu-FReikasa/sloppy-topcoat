import { useTranslation } from "react-i18next";
import { LabelChips, PriorityMark, DueText, DoneBox } from "./parts";
import type { ProjectNode } from "./types";

/** Column -> the hub's sort key, and the direction the first click gives. */
export const COLUMNS = [
  { id: "number", key: "number", first: "asc" },
  { id: "title", key: "title", first: "asc" },
  { id: "status", key: "status", first: "asc" },
  { id: "due", key: "dueDate", first: "asc" },
  { id: "priority", key: "priority", first: "desc" },
  { id: "progress", key: "percent", first: "desc" },
] as const;

/** The sort rule after a click on a column header: a second click turns the direction, a click on
 *  another column sorts by that one first and keeps the old rule behind it. Pure. */
export function nextSort(
  current: string,
  key: string,
  first: "asc" | "desc",
): string {
  const parts = current
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean);
  const [head, ...rest] = parts;
  const [name, dir] = (head ?? "").split(":");
  if (name === key) {
    const flipped = (dir ?? "asc") === "asc" ? "desc" : "asc";
    return [`${key}:${flipped}`, ...rest].join(",");
  }
  const kept = parts.filter((p) => p.split(":")[0] !== key).slice(0, 3);
  return [`${key}:${first}`, ...kept].join(",");
}

/** The direction mark for a column in the current rule: "asc", "desc" or "". */
export function sortMark(sort: string, key: string): "asc" | "desc" | "" {
  const [name, dir] = (sort.split(",")[0] ?? "").trim().split(":");
  if (name !== key) return "";
  return dir === "desc" ? "desc" : "asc";
}

export function TableView({
  nodes,
  sort,
  selectedId,
  now,
  zone,
  onSort,
  onOpen,
  onComplete,
}: {
  nodes: ProjectNode[];
  sort: string;
  selectedId: string | null;
  now: Date;
  zone: string;
  onSort: (sort: string) => void;
  onOpen: (id: string) => void;
  onComplete: (node: ProjectNode) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="project-tablewrap">
      <table className="project-table">
        <thead>
          <tr>
            <th aria-label={t("project.complete")} />
            {COLUMNS.map((c) => {
              const mark = sortMark(sort, c.key);
              return (
                <th
                  key={c.id}
                  aria-sort={
                    mark === "asc"
                      ? "ascending"
                      : mark === "desc"
                        ? "descending"
                        : "none"
                  }
                >
                  <button
                    type="button"
                    onClick={() => onSort(nextSort(sort, c.key, c.first))}
                  >
                    {t(`project.col.${c.id}`)}
                    {mark && (
                      <span aria-hidden="true">
                        {mark === "asc" ? " ▲" : " ▼"}
                      </span>
                    )}
                  </button>
                </th>
              );
            })}
            <th>{t("project.col.labels")}</th>
          </tr>
        </thead>
        <tbody>
          {nodes.map((n) => (
            <tr
              key={n.id}
              className={n.id === selectedId ? "is-selected" : undefined}
            >
              <td>
                <DoneBox node={n} onComplete={onComplete} />
              </td>
              <td className="project-number">{n.number}</td>
              <td>
                <button
                  type="button"
                  className="project-open"
                  onClick={() => onOpen(n.id)}
                >
                  {n.title}
                </button>
              </td>
              <td>{n.status ? t(`project.taskStatuses.${n.status}`) : ""}</td>
              <td>
                <DueText node={n} now={now} zone={zone} />
              </td>
              <td>
                <PriorityMark priority={n.priority} />
              </td>
              <td>
                {n.kind === "task"
                  ? `${n.progress ?? n.percent_done ?? 0} %`
                  : ""}
              </td>
              <td>
                <LabelChips labels={n.labels} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
