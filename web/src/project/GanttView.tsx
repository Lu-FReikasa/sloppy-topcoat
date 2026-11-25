import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
} from "react";
import { useTranslation } from "react-i18next";
import { dayIn } from "./quickAdd";
import {
  addDays,
  daysBetween,
  dayKey,
  ganttLayout,
  shiftDates,
} from "./schedule";
import type { ProjectNode } from "./types";

/** Days from the start of the chart to today. */
const today_ = (
  start: Parameters<typeof daysBetween>[0],
  zone: string,
): number => daysBetween(start, dayIn(new Date(), zone));

const ROW = 30;
const HEAD = 34;
const DRAG_DISTANCE = 4;
export const ZOOMS = [12, 24, 48] as const;

type Patch = ReturnType<typeof shiftDates>;

/** The chart of a project: a row per entry, bars from start to end, diamonds for milestones, summary
 *  bars over a topic, arrows from what a task waits for. Plain SVG, no library. A bar moves by drag
 *  (by whole days) or with the arrow keys when it has the focus (a day; with Shift a week). The
 *  chart scrolls inside its own container. */
export function GanttView({
  nodes,
  zone,
  selectedId,
  onOpen,
  onMove,
}: {
  nodes: ProjectNode[];
  zone: string;
  selectedId: string | null;
  onOpen: (id: string) => void;
  onMove: (task: ProjectNode, patch: Patch) => void;
}) {
  const { t, i18n } = useTranslation();
  const [dayWidth, setDayWidth] = useState<number>(24);
  const layout = useMemo(
    () => ganttLayout(nodes, zone, dayWidth),
    [nodes, zone, dayWidth],
  );
  const rows = nodes.filter((n) => n.kind !== "report");
  const byId = new Map(rows.map((n) => [n.id, n]));
  const barOf = new Map(layout.bars.map((b) => [b.id, b]));
  const width = layout.days * dayWidth;
  const height = HEAD + rows.length * ROW;
  const today = daysBetween(layout.start, dayIn(new Date(), zone));
  const press = useRef<{ id: string; x: number; dragging: boolean } | null>(
    null,
  );
  const scroller = useRef<HTMLDivElement>(null);
  // the chart opens at its first bar (or at today), not at the far left of a long time scale
  const firstX = useMemo(() => {
    const xs = layout.bars
      .filter((b) => b.kind !== "undated" && !b.offscreen)
      .map((b) => b.x);
    return xs.length > 0
      ? Math.min(...xs)
      : Math.max(0, today_(layout.start, zone) * dayWidth);
  }, [layout, dayWidth, zone]);
  useEffect(() => {
    if (scroller.current)
      scroller.current.scrollLeft = Math.max(0, firstX - 3 * dayWidth);
  }, [firstX, dayWidth]);
  const [drag, setDrag] = useState<{ id: string; dx: number } | null>(null);

  const dateLabel = (n: ProjectNode) => {
    const b = barOf.get(n.id);
    if (!b?.from || !b.to) return t("project.ganttNoDate");
    const f = new Intl.DateTimeFormat(i18n.language, {
      day: "numeric",
      month: "short",
      timeZone: "UTC",
    });
    const d = (x: typeof b.from) =>
      f.format(new Date(Date.UTC(x!.y, x!.m - 1, x!.d)));
    return b.from && b.to && dayKey(b.from) !== dayKey(b.to)
      ? `${d(b.from)} – ${d(b.to)}`
      : d(b.from);
  };
  const movable = (n: ProjectNode) =>
    n.kind === "task" && barOf.get(n.id)?.kind !== "undated";

  const down = (n: ProjectNode, e: PointerEvent<SVGGElement>) => {
    if (e.button !== 0 || !movable(n)) return;
    press.current = { id: n.id, x: e.clientX, dragging: false };
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };
  const move = (e: PointerEvent<SVGGElement>) => {
    const p = press.current;
    if (!p) return;
    const dx = e.clientX - p.x;
    if (!p.dragging && Math.abs(dx) > DRAG_DISTANCE) p.dragging = true;
    if (p.dragging)
      setDrag({ id: p.id, dx: Math.round(dx / dayWidth) * dayWidth });
  };
  const up = (n: ProjectNode, e: PointerEvent<SVGGElement>) => {
    const p = press.current;
    press.current = null;
    setDrag(null);
    if (!p) return;
    if (!p.dragging) return onOpen(n.id);
    const days = Math.round((e.clientX - p.x) / dayWidth);
    if (days !== 0) onMove(n, shiftDates(n, days, zone));
  };
  const key = (n: ProjectNode, e: KeyboardEvent<SVGGElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      return onOpen(n.id);
    }
    if (!movable(n) || (e.key !== "ArrowLeft" && e.key !== "ArrowRight"))
      return;
    e.preventDefault();
    const days = (e.key === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 7 : 1);
    onMove(n, shiftDates(n, days, zone));
  };

  // the month names over the day scale
  const months: { x: number; label: string }[] = [];
  for (let i = 0; i < layout.days; i++) {
    const d = addDays(layout.start, i);
    if (i === 0 || d.d === 1)
      months.push({
        x: i * dayWidth,
        label: new Intl.DateTimeFormat(i18n.language, {
          month: "short",
          year: "numeric",
          timeZone: "UTC",
        }).format(new Date(Date.UTC(d.y, d.m - 1, 1))),
      });
  }
  const outside = Object.keys(layout.outside).length;

  return (
    <section className="project-gantt" aria-label={t("project.viewGantt")}>
      <div className="project-bar">
        <label>
          {t("project.ganttZoom")}
          <select
            value={dayWidth}
            onChange={(e) => setDayWidth(Number(e.target.value))}
          >
            {ZOOMS.map((z) => (
              <option key={z} value={z}>
                {t(`project.ganttZooms.${z}`)}
              </option>
            ))}
          </select>
        </label>
        {outside > 0 && (
          <span className="project-muted">
            {t("project.ganttOutside", { count: outside })}
          </span>
        )}
      </div>
      <p className="project-muted">{t("project.ganttHint")}</p>
      <div className="project-ganttwrap">
        <ul className="project-ganttlabels" style={{ paddingTop: HEAD }}>
          {rows.map((n) => (
            <li
              key={n.id}
              style={{ height: ROW }}
              className={n.id === selectedId ? "is-selected" : undefined}
            >
              <button
                type="button"
                className="project-open"
                onClick={() => onOpen(n.id)}
              >
                <span className="project-number">{n.number}</span>
                <span className="project-name">{n.title}</span>
              </button>
            </li>
          ))}
        </ul>
        <div className="project-ganttscroll" ref={scroller}>
          <svg
            width={width}
            height={height}
            role="group"
            aria-label={t("project.viewGantt")}
          >
            <defs>
              <marker
                id="gantt-arrow"
                markerWidth="6"
                markerHeight="6"
                refX="5"
                refY="3"
                orient="auto"
              >
                <path d="M0,0 L6,3 L0,6 z" fill="currentColor" />
              </marker>
            </defs>
            {months.map((m) => (
              <g key={m.x}>
                <line
                  x1={m.x}
                  x2={m.x}
                  y1={0}
                  y2={height}
                  className="gantt-grid"
                />
                <text x={m.x + 4} y={14} className="gantt-month">
                  {m.label}
                </text>
              </g>
            ))}
            {dayWidth >= 24 &&
              Array.from({ length: layout.days }, (_, i) => (
                <text
                  key={i}
                  x={i * dayWidth + dayWidth / 2}
                  y={HEAD - 6}
                  className="gantt-day"
                  textAnchor="middle"
                >
                  {addDays(layout.start, i).d}
                </text>
              ))}
            {today >= 0 && today < layout.days && (
              <line
                x1={today * dayWidth + dayWidth / 2}
                x2={today * dayWidth + dayWidth / 2}
                y1={HEAD}
                y2={height}
                className="gantt-today"
              />
            )}
            {layout.arrows.map((a) => {
              const from = barOf.get(a.from)!;
              const to = barOf.get(a.to)!;
              const x1 = from.x + from.width;
              const y1 = HEAD + from.row * ROW + ROW / 2;
              const x2 = to.x;
              const y2 = HEAD + to.row * ROW + ROW / 2;
              const mid = Math.max(x1 + 6, Math.min(x2 - 6, (x1 + x2) / 2));
              return (
                <path
                  key={`${a.from}>${a.to}`}
                  d={`M${x1},${y1} H${mid} V${y2} H${x2}`}
                  className="gantt-arrow"
                  fill="none"
                  markerEnd="url(#gantt-arrow)"
                  data-from={a.from}
                  data-to={a.to}
                />
              );
            })}
            {layout.bars
              .filter((b) => b.kind !== "undated")
              .map((b) => {
                const n = byId.get(b.id)!;
                const y = HEAD + b.row * ROW;
                const dx = drag?.id === b.id ? drag.dx : 0;
                if (b.offscreen) {
                  // wholly outside the chart: a mark at the edge of its row, still reachable by key
                  const label = `${byId.get(b.id)!.ref} ${byId.get(b.id)!.title}, ${dateLabel(byId.get(b.id)!)}`;
                  return (
                    <g
                      key={b.id}
                      className="gantt-edge"
                      transform={`translate(${b.offscreen === "before" ? 2 : width - 16},${y})`}
                      tabIndex={0}
                      role="button"
                      aria-label={label}
                      data-id={b.id}
                      onClick={() => onOpen(b.id)}
                      onKeyDown={(e) => key(byId.get(b.id)!, e)}
                    >
                      <text y={ROW / 2 + 4}>
                        {b.offscreen === "before" ? "◀" : "▶"}
                      </text>
                      <title>{label}</title>
                    </g>
                  );
                }
                const label = `${n.ref} ${n.title}, ${dateLabel(n)}`;
                return (
                  <g
                    key={b.id}
                    className={`gantt-bar gantt-${b.kind}${n.id === selectedId ? " is-selected" : ""}${n.status === "done" ? " is-done" : ""}`}
                    transform={`translate(${b.x + dx},${y})`}
                    tabIndex={0}
                    role="button"
                    aria-label={label}
                    aria-keyshortcuts={
                      movable(n)
                        ? "ArrowLeft ArrowRight Shift+ArrowLeft Shift+ArrowRight"
                        : undefined
                    }
                    data-id={b.id}
                    onPointerDown={(e) => down(n, e)}
                    onPointerMove={move}
                    onPointerUp={(e) => up(n, e)}
                    onPointerCancel={() => {
                      press.current = null;
                      setDrag(null);
                    }}
                    onKeyDown={(e) => key(n, e)}
                  >
                    {b.kind === "milestone" ? (
                      <polygon
                        points={`${b.width / 2},4 ${b.width / 2 + 9},${ROW / 2} ${b.width / 2},${ROW - 4} ${b.width / 2 - 9},${ROW / 2}`}
                      />
                    ) : (
                      <rect
                        x={0}
                        y={b.kind === "summary" ? 9 : 5}
                        width={b.width}
                        height={b.kind === "summary" ? 8 : ROW - 10}
                        rx={3}
                      />
                    )}
                    <title>{label}</title>
                  </g>
                );
              })}
          </svg>
        </div>
      </div>
    </section>
  );
}
