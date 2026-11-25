import { useEffect, useState, type CSSProperties } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import "./clipper.css";
import { PIXELS } from "./clipperPixels";

/** What Clipper is doing, from the chat's own state (no model is asked). */
export type Mood = "idle" | "thinking" | "working" | "done" | "error" | "waiting";

export const LINES_PER_MOOD = 4;

/** What he holds per application (the desktop plan, 6.3): a magnifier, a keyboard, a clipboard, a
 *  screwdriver, books, a speech bubble. The stylesheet moves each its own way while he works. */
const PROPS: Record<string, string> = {
  research: "🔍",
  dev: "⌨️",
  project: "📋",
  admin: "🔧",
  wiki: "📚",
  chat: "💬",
};

/** His ticket number for a task: two digits from its id, the same each time. */
export function ticketNumber(id: string): number {
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) % 9973;
  return 10 + (h % 90);
}

export interface MoodInput {
  /** An answer is being made. */
  running: boolean;
  /** The hub is checking something for it (tool steps came in). */
  checking: boolean;
  /** The status of the last answer shown. */
  last?: string;
  /** A card waits for the person's click (a proposal, a theme). */
  waiting?: boolean;
}

/** The mood for the chat's state: work beats everything, then a failure, then a click that waits. */
export function moodOf({ running, checking, last, waiting }: MoodInput): Mood {
  if (running) return checking ? "working" : "thinking";
  if (last === "error" || last === "interrupted") return "error";
  if (waiting) return "waiting";
  if (last === "complete") return "done";
  return "idle";
}

/** Which line of a mood is said: the same seed always gives the same line (testable, and the line
 *  does not change while one looks at it). */
export function lineOf(mood: Mood, seed: number): string {
  return `clipper.${mood}.${Math.abs(seed) % LINES_PER_MOOD}`;
}

/** Is Clipper the person's persona? (the shipped template with the key "clipper") `enabled` false
 *  asks nothing (a page that would not show him anyway). */
export function useClipperActive(api: ApiClient, enabled = true): boolean {
  const [active, setActive] = useState(false);
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    api.personas().then(
      (lib) => {
        if (!live) return;
        const all = [...lib.templates, ...lib.own];
        setActive(all.some((p) => p.id === lib.default_id && p.key === "clipper"));
      },
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [api, enabled]);
  return active;
}

/** Clipper: a bent paper clip with a cigarette, a little fed up, helpful anyway. Drawn as one SVG
 *  whose parts (lids, pupils, cigarette, ember, smoke) the stylesheet moves per mood; with less
 *  motion asked for by the system it stands still. */
export function Clipper({
  mood,
  seed,
  app,
  burn,
  queue,
}: {
  mood: Mood;
  seed: number;
  /** The application he works for: its prop and motion (the desktop plan, 6.3). */
  app?: string;
  /** How far the work is (0..1): the cigarette burns down with it (6.3a). */
  burn?: number | null;
  /** Waiting in the queue: his ticket and how many are ahead (6.3a). */
  queue?: { number: number; ahead: number } | null;
}) {
  const { t } = useTranslation();
  const line = queue ? t(`clipper.queue.${Math.abs(seed) % 3}`, { n: queue.number }) : t(lineOf(mood, seed));
  const style = typeof burn === "number" ? ({ ["--burn" as string]: String(Math.min(1, Math.max(0, burn))) } as CSSProperties) : undefined;
  return (
    <figure
      className={`clipper clip-${mood}${app ? ` clip-app-${app}` : ""}${queue ? " clip-queued" : ""}${typeof burn === "number" ? " clip-burning" : ""}`}
      data-mood={mood}
      aria-label={t("clipper.label")}
      style={style}
    >
      <p className="clipper-say" role="status">
        {line}
      </p>
      {queue && (
        <span className="clipper-ticket" data-testid="clipper-ticket">
          <b>{t("clipper.ticket", { n: queue.number })}</b>
          <small>{t("clipper.ahead", { count: queue.ahead })}</small>
          <span className="clipper-line" aria-hidden="true">
            {Array.from({ length: Math.min(queue.ahead, 5) }, (_, i) => (
              <i key={i} />
            ))}
          </span>
        </span>
      )}
      {app && (
        <span className="clipper-prop" aria-hidden="true">
          {PROPS[app] ?? PROPS.chat}
        </span>
      )}
      <svg
        viewBox="0 0 120 160"
        width="120"
        height="160"
        shapeRendering="crispEdges"
        aria-hidden="true"
      >
        <g className="clip-body">
          <path className="clip-outline" d={PIXELS.outline} />
          <path className="clip-wire" d={PIXELS.wire} />
          <path className="clip-brow" d={PIXELS.brows} />
          <path className="clip-eyeline" d={PIXELS.eyeline} />
          <path className="clip-eye" d={PIXELS.eyewhite} />
          <path className="clip-pupil" d={PIXELS.pupils} />
          <path className="clip-lid" d={PIXELS.lids} />
          <path className="clip-shut" d={PIXELS.shut} />
          <path className="clip-mouth" d={PIXELS.mouth} />
          <g className="clip-cig">
            <path className="clip-filter" d={PIXELS.filter} />
            <path className="clip-paper" d={PIXELS.paper} />
            <path className="clip-ember" d={PIXELS.ember} />
            <path className="clip-smoke" d={PIXELS.smoke} />
          </g>
        </g>
      </svg>
    </figure>
  );
}
