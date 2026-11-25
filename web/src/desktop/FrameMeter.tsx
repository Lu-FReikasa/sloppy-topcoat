import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { frameStats, gpuRenderer, isSoftware, refreshOf, rendersSoFar, type FrameStats } from "./frames";

/** The last two seconds or so of frame gaps. */
const KEEP = 300;

type Shown = FrameStats & { hz: number; rps: number };

/** The frame meter (P0 of the desktop plan): frames per second, the slowest frames, renders of the
 *  desktop per second and the renderer (GPU or software). Ctrl+Shift+F shows and hides it;
 *  `?fps=1` in the address starts with it shown. It measures only while it is shown. */
export function FrameMeter() {
  const { t } = useTranslation();
  const [shown, setShown] = useState(() => new URLSearchParams(window.location.search).get("fps") === "1");
  const [stats, setStats] = useState<Shown | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && !e.altKey && (e.key === "F" || e.key === "f")) {
        e.preventDefault();
        setShown((s) => !s);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (!shown) return;
    const gaps: number[] = [];
    let last = performance.now();
    let frame = 0;
    let rendersAt = rendersSoFar();
    let since = last;
    const tick = (now: number) => {
      gaps.push(now - last);
      if (gaps.length > KEEP) gaps.shift();
      last = now;
      if (now - since >= 500) {
        const hz = refreshOf(gaps);
        const r = rendersSoFar();
        setStats({ ...frameStats(gaps, 1000 / hz), hz, rps: Math.round(((r - rendersAt) * 1000) / (now - since)) });
        rendersAt = r;
        since = now;
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [shown]);

  // asked once it is shown (a WebGL context, given back at once)
  const gpu = useMemo(() => (shown ? gpuRenderer() : ""), [shown]);
  if (!shown) return null;
  return (
    <aside className="dt-meter" data-testid="frame-meter" aria-label={t("desktop.meter.label")}>
      <b>{stats ? `${stats.fps} fps` : "…"}</b>
      {stats && (
        <>
          <span>{t("desktop.meter.refresh", { hz: stats.hz })}</span>
          <span>{t("desktop.meter.p99", { ms: stats.p99Ms })}</span>
          <span>{t("desktop.meter.worst", { ms: stats.worstMs })}</span>
          <span className={stats.slow > 0 ? "warn" : undefined}>{t("desktop.meter.slow", { n: stats.slow })}</span>
          <span>{t("desktop.meter.renders", { n: stats.rps })}</span>
        </>
      )}
      <span className={!gpu || isSoftware(gpu) ? "bad" : undefined} title={gpu}>
        {gpu ? t("desktop.meter.gpu", { name: gpu.replace(/^ANGLE \((.*)\)$/, "$1") }) : t("desktop.meter.noGpu")}
      </span>
      <span>
        {window.innerWidth}×{window.innerHeight} @ {window.devicePixelRatio}
      </span>
    </aside>
  );
}
