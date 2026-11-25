import { useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { useTranslation } from "react-i18next";
import { PriorityMark } from "./parts";
import { dayIn } from "./quickAdd";
import {
  addDays,
  addMonths,
  dayKey,
  dayOf,
  mainDay,
  monthGrid,
  moveTo,
  parseDayKey,
  sameDay,
  weekOf,
  type Day,
} from "./schedule";
import type { ProjectNode } from "./types";

const DRAG_DISTANCE = 6;

/** The day cell under a point of the screen, or null. Reads the page, so it needs a real layout. */
export function dayAt(x: number, y: number): Day | null {
  const hit = document.elementFromPoint?.(x, y)?.closest("[data-day]");
  return parseDayKey(hit?.getAttribute("data-day") ?? "");
}

type Patch = ReturnType<typeof moveTo>;

/** A month or a week with the tasks on their due day (or, with no due date, on their end or start day).
 *  A task moves by drag (pointer events) or, with the keyboard, by Alt and the arrow keys: left and
 *  right by a day, up and down by a week. All its dates move together. */
export function CalendarView({
  tasks,
  zone,
  selectedId,
  onOpen,
  onMove,
}: {
  tasks: ProjectNode[];
  zone: string;
  selectedId: string | null;
  onOpen: (id: string) => void;
  onMove: (task: ProjectNode, patch: Patch) => void;
}) {
  const { t, i18n } = useTranslation();
  const today = dayIn(new Date(), zone);
  const [mode, setMode] = useState<"month" | "week">("month");
  const [cursor, setCursor] = useState<Day>(today);
  const press = useRef<{
    id: string;
    x: number;
    y: number;
    dragging: boolean;
  } | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);

  const rows: Day[][] =
    mode === "month" ? monthGrid(cursor.y, cursor.m) : [weekOf(cursor)];
  const byDay = new Map<string, ProjectNode[]>();
  const starts = new Map<string, ProjectNode[]>();
  const undated: ProjectNode[] = [];
  for (const task of tasks) {
    const main = mainDay(task, zone);
    if (!main) {
      undated.push(task);
      continue;
    }
    byDay.set(dayKey(main), [...(byDay.get(dayKey(main)) ?? []), task]);
    const start = dayOf(task.start_at, zone);
    if (start && !sameDay(start, main))
      starts.set(dayKey(start), [...(starts.get(dayKey(start)) ?? []), task]);
  }

  const step = (n: number) =>
    setCursor((c) => (mode === "month" ? addMonths(c, n) : addDays(c, 7 * n)));
  const title =
    mode === "month"
      ? new Intl.DateTimeFormat(i18n.language, {
          month: "long",
          year: "numeric",
          timeZone: "UTC",
        }).format(new Date(Date.UTC(cursor.y, cursor.m - 1, 1)))
      : `${dayKey(rows[0]![0]!)} – ${dayKey(rows[0]![6]!)}`;
  const weekday = (d: Day) =>
    new Intl.DateTimeFormat(i18n.language, {
      weekday: "short",
      timeZone: "UTC",
    }).format(new Date(Date.UTC(d.y, d.m - 1, d.d)));

  const down = (task: ProjectNode, e: PointerEvent<HTMLElement>) => {
    if (e.button !== 0) return;
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
    if (p.dragging) {
      const d = dayAt(e.clientX, e.clientY);
      setOver(d ? dayKey(d) : null);
    }
  };
  const up = (task: ProjectNode, e: PointerEvent<HTMLElement>) => {
    const p = press.current;
    press.current = null;
    setDragId(null);
    setOver(null);
    if (!p) return;
    if (!p.dragging) return onOpen(task.id);
    const target = dayAt(e.clientX, e.clientY);
    if (target) {
      const patch = moveTo(task, target, zone);
      const main = mainDay(task, zone);
      if (!main || !sameDay(main, target)) onMove(task, patch);
    }
  };
  const key = (task: ProjectNode, e: KeyboardEvent<HTMLElement>) => {
    if (!e.altKey) return;
    const by = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[
      e.key
    ];
    if (by === undefined) return;
    e.preventDefault();
    const main = mainDay(task, zone) ?? today;
    onMove(task, moveTo(task, addDays(main, by), zone));
  };

  const chip = (task: ProjectNode, startOnly = false) => (
    <li
      key={`${task.id}${startOnly ? "-start" : ""}`}
      className={`project-chiptask${task.id === selectedId ? " is-selected" : ""}${dragId === task.id ? " is-dragging" : ""}${startOnly ? " is-start" : ""}${task.status === "done" ? " is-done" : ""}`}
      onPointerDown={startOnly ? undefined : (e) => down(task, e)}
      onPointerMove={startOnly ? undefined : move}
      onPointerUp={startOnly ? undefined : (e) => up(task, e)}
      onPointerCancel={() => {
        press.current = null;
        setDragId(null);
        setOver(null);
      }}
    >
      <button
        type="button"
        className="project-open"
        aria-keyshortcuts="Alt+ArrowLeft Alt+ArrowRight Alt+ArrowUp Alt+ArrowDown"
        onClick={() => onOpen(task.id)}
        onKeyDown={(e) => key(task, e)}
      >
        {startOnly ? "▸ " : ""}
        {task.title}
      </button>
      {!startOnly && <PriorityMark priority={task.priority} />}
    </li>
  );

  return (
    <section
      className="project-calendar"
      aria-label={t("project.viewCalendar")}
    >
      <div className="project-bar">
        <button
          type="button"
          onClick={() => step(-1)}
          aria-label={t("project.calPrev")}
        >
          ◀
        </button>
        <strong aria-live="polite">{title}</strong>
        <button
          type="button"
          onClick={() => step(1)}
          aria-label={t("project.calNext")}
        >
          ▶
        </button>
        <button type="button" onClick={() => setCursor(today)}>
          {t("project.calToday")}
        </button>
        <button
          type="button"
          aria-pressed={mode === "month"}
          onClick={() => setMode("month")}
        >
          {t("project.calMonth")}
        </button>
        <button
          type="button"
          aria-pressed={mode === "week"}
          onClick={() => setMode("week")}
        >
          {t("project.calWeek")}
        </button>
      </div>
      <p className="project-muted">{t("project.calHint")}</p>
      <div className="project-calgrid" role="grid">
        <div className="project-calhead" role="row">
          {rows[0]!.map((d) => (
            <span key={dayKey(d)} role="columnheader">
              {weekday(d)}
            </span>
          ))}
        </div>
        {rows.map((week) => (
          <div key={dayKey(week[0]!)} className="project-calweek" role="row">
            {week.map((d) => {
              const k = dayKey(d);
              const cell = byDay.get(k) ?? [];
              const begins = starts.get(k) ?? [];
              return (
                <div
                  key={k}
                  role="gridcell"
                  data-day={k}
                  aria-label={k}
                  className={`project-calcell${sameDay(d, today) ? " is-today" : ""}${mode === "month" && d.m !== cursor.m ? " is-outside" : ""}${over === k ? " is-over" : ""}`}
                >
                  <span className="project-caldate">{d.d}</span>
                  <ul>
                    {cell.map((task) => chip(task))}
                    {begins.map((task) => chip(task, true))}
                  </ul>
                </div>
              );
            })}
          </div>
        ))}
      </div>
      {undated.length > 0 && (
        <section aria-label={t("project.calUndated")}>
          <h3>{t("project.calUndated")}</h3>
          <ul className="project-calundated">
            {undated.map((task) => chip(task))}
          </ul>
        </section>
      )}
    </section>
  );
}
