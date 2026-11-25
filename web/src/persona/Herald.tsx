import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { clientCommand } from "../desktop/client";
import { programHue } from "../desktop/programs";
import { Clipper } from "./Clipper";
import { noticeOf, TASK_EVENT, type Notice, type TaskSignal } from "./heraldNews";
import "./clipper.css";

/** How long a message stays, and how long Clipper takes to leave. */
export const SHOW_MS = 8000;
const LEAVE_MS = 450;

/** The herald's page (`?herald=1`): Clipper pops up at the screen's corner when a task the person
 *  started ends, says so in a bubble and goes again; a click opens the task in the overlay. The
 *  desktop client shows and hides the window (`client=herald-show/hide`) and never gives it the
 *  focus. Several ends in a row come one after the other. */
export function Herald() {
  const { t } = useTranslation();
  const [queue, setQueue] = useState<Notice[]>([]);
  const [leaving, setLeaving] = useState(false);
  const shownOnce = useRef(false);
  const current = queue[0];

  useEffect(() => {
    document.documentElement.classList.add("tc-herald");
    // a page that starts has nothing to say: a herald left open by the page before it (reloaded
    // while it showed) goes (2026-11-13: an empty window at the corner caught every click)
    clientCommand("herald-hide");
    const onTask = (e: Event) => {
      const n = noticeOf((e as CustomEvent<TaskSignal>).detail);
      if (n) setQueue((q) => (q.some((x) => x.id === n.id) ? q : [...q, n]));
    };
    window.addEventListener(TASK_EVENT, onTask);
    return () => {
      window.removeEventListener(TASK_EVENT, onTask);
      document.documentElement.classList.remove("tc-herald");
    };
  }, []);

  // the window comes for each message (the client hides it by itself after a while), and goes
  // when the last message has left
  useEffect(() => {
    if (current) {
      shownOnce.current = true;
      clientCommand("herald-show");
    }
    if (!current && shownOnce.current) {
      shownOnce.current = false;
      clientCommand("herald-hide");
    }
  }, [current]);

  useEffect(() => {
    if (!current) return;
    const leave = window.setTimeout(() => setLeaving(true), SHOW_MS);
    const next = window.setTimeout(() => {
      setLeaving(false);
      setQueue((q) => q.slice(1));
    }, SHOW_MS + LEAVE_MS);
    return () => {
      window.clearTimeout(leave);
      window.clearTimeout(next);
    };
  }, [current]);

  if (!current) return null;
  const line = current.failed
    ? t("herald.failed", { title: current.title || t("herald.someTask") })
    : t("herald.done", { title: current.title || t("herald.someTask") });
  return (
    <button
      type="button"
      className={`herald${leaving ? " is-leaving" : ""}`}
      style={{ ["--pg-hue" as string]: String(programHue(current.application ?? "chat")) }}
      onClick={() => clientCommand("open-task", { id: current.id })}
      aria-label={line}
      data-testid="herald"
    >
      <span className={`herald-bubble${current.failed ? " is-failed" : ""}`}>
        {line}
        <span className="herald-hint muted">{t("herald.open")}</span>
      </span>
      <span className="herald-figure" aria-hidden="true">
        <Clipper mood={current.failed ? "error" : "done"} seed={current.id.length} />
      </span>
    </button>
  );
}
