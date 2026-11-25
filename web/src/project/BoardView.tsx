import { useRef, useState, type PointerEvent } from "react";
import { useTranslation } from "react-i18next";
import { DueText, LabelChips, PriorityMark } from "./parts";
import type { ProjectNode, ProjectTaskStatus } from "./types";

/** The columns of the board, in the order of the workflow. Dropped tasks are archived: no column. */
export const COLUMNS: readonly ProjectTaskStatus[] = [
  "todo",
  "in_progress",
  "in_review",
  "blocked",
  "done",
];

const DRAG_DISTANCE = 6; // px before a press counts as a drag and not as a click

/** The column under a point of the screen, or null. Reads the page, so it needs a real layout. */
export function columnAt(x: number, y: number): ProjectTaskStatus | null {
  const hit = document.elementFromPoint?.(x, y)?.closest("[data-column]");
  const id = hit?.getAttribute("data-column");
  return COLUMNS.find((c) => c === id) ?? null;
}

/** The board: one column per status, a card per task. A card moves by drag (pointer events, so a
 *  finger works too) and by the menu on the card, which is the way for the keyboard. Both ask the
 *  same thing: `onMove(task, status)`. A column that holds more than its limit says so; it never
 *  refuses a move. */
export function BoardView({
  tasks,
  limits,
  selectedId,
  now,
  zone,
  onMove,
  onOpen,
  onLimit,
}: {
  tasks: ProjectNode[];
  limits: Partial<Record<ProjectTaskStatus, number>>;
  selectedId: string | null;
  now: Date;
  zone: string;
  onMove: (task: ProjectNode, status: ProjectTaskStatus) => void;
  onOpen: (id: string) => void;
  onLimit: (status: ProjectTaskStatus, limit: number | null) => void;
}) {
  const { t } = useTranslation();
  const press = useRef<{
    id: string;
    x: number;
    y: number;
    dragging: boolean;
  } | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);
  const [over, setOver] = useState<ProjectTaskStatus | null>(null);

  const down = (task: ProjectNode, e: PointerEvent<HTMLElement>) => {
    if (
      e.button !== 0 ||
      (e.target as HTMLElement).closest("select,button,input")
    )
      return;
    press.current = {
      id: task.id,
      x: e.clientX,
      y: e.clientY,
      dragging: false,
    };
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };
  const move = (e: PointerEvent<HTMLElement>) => {
    const p = press.current;
    if (!p) return;
    if (
      !p.dragging &&
      Math.hypot(e.clientX - p.x, e.clientY - p.y) > DRAG_DISTANCE
    ) {
      p.dragging = true;
      setDragId(p.id);
    }
    if (p.dragging) setOver(columnAt(e.clientX, e.clientY));
  };
  const up = (task: ProjectNode, e: PointerEvent<HTMLElement>) => {
    const p = press.current;
    press.current = null;
    setDragId(null);
    setOver(null);
    if (!p) return;
    if (!p.dragging) return onOpen(task.id);
    const target = columnAt(e.clientX, e.clientY);
    if (target && target !== task.status) onMove(task, target);
  };

  return (
    <div className="project-board-wrap">
      <p className="project-muted">{t("project.boardHint")}</p>
      <div className="project-board">
        {COLUMNS.map((status) => {
          const cards = tasks.filter((n) => n.status === status);
          const limit = limits[status];
          const full = limit !== undefined && cards.length > limit;
          return (
            <section
              key={status}
              data-column={status}
              className={`project-column${over === status ? " is-over" : ""}${full ? " is-full" : ""}`}
              aria-label={t(`project.taskStatuses.${status}`)}
            >
              <header>
                <h3>
                  {t(`project.taskStatuses.${status}`)}{" "}
                  <span className="project-muted">({cards.length})</span>
                </h3>
                <label className="project-wip">
                  <span className="project-muted">{t("project.wipLimit")}</span>
                  <input
                    type="number"
                    min={1}
                    max={99}
                    aria-label={`${t(`project.taskStatuses.${status}`)}: ${t("project.wipLimit")}`}
                    value={limit ?? ""}
                    onChange={(e) =>
                      onLimit(
                        status,
                        e.target.value ? Number(e.target.value) : null,
                      )
                    }
                  />
                </label>
                {full && (
                  <span role="status" className="project-error">
                    {t("project.wipOver")}
                  </span>
                )}
              </header>
              {cards.length === 0 && (
                <p className="project-muted">{t("project.boardEmpty")}</p>
              )}
              <ul>
                {cards.map((task) => (
                  <li
                    key={task.id}
                    className={`project-card${task.id === selectedId ? " is-selected" : ""}${dragId === task.id ? " is-dragging" : ""}`}
                    onPointerDown={(e) => down(task, e)}
                    onPointerMove={move}
                    onPointerUp={(e) => up(task, e)}
                    onPointerCancel={() => {
                      press.current = null;
                      setDragId(null);
                      setOver(null);
                    }}
                  >
                    <button
                      type="button"
                      className="project-open"
                      onClick={() => onOpen(task.id)}
                    >
                      <span className="project-number">{task.number}</span>{" "}
                      {task.title}
                    </button>
                    <div className="project-cardmeta">
                      <PriorityMark priority={task.priority} />
                      <DueText node={task} now={now} zone={zone} />
                      <LabelChips labels={task.labels} />
                    </div>
                    <select
                      aria-label={t("project.moveToColumn", { ref: task.ref })}
                      value={task.status ?? ""}
                      onChange={(e) =>
                        onMove(task, e.target.value as ProjectTaskStatus)
                      }
                    >
                      {COLUMNS.map((c) => (
                        <option key={c} value={c}>
                          {t(`project.taskStatuses.${c}`)}
                        </option>
                      ))}
                    </select>
                  </li>
                ))}
              </ul>
            </section>
          );
        })}
      </div>
    </div>
  );
}
