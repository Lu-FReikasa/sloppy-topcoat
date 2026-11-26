import { useEffect, useLayoutEffect, useRef, useState, type ReactElement } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { Dialog } from "../components/Dialog";
import { describeError, toActionError } from "../errors";
import { chooseFit } from "./fit";
import { move, ORIENTATIONS, pageBox, pageRule, PAPERS, printColumns, type Orientation, type Paper } from "./print";
import { asWritten, collapseRepeats } from "./collapse";
import { Body } from "./SheetsPage";
import type { SheetDetail, SheetViewParams } from "./types";
import "./print.css";

/** A song offered for printing: the sheet (one version) and how the list names it. */
export interface PrintSong {
  /** Tells the songs apart when a sheet is in the list twice (a setlist): its position there. */
  key?: string;
  id: string;
  title: string;
  artist: string;
}

export interface PrintOptions {
  paper: Paper;
  orientation: Orientation;
  /** Repeated parts as one line ("Chorus ×2"), as the stage mode can show them. */
  collapse: boolean;
}

interface DialogProps {
  api: ApiClient;
  /** The songs in the order they are printed; all are picked at the start. */
  songs: PrintSong[];
  /** The view of every sheet (instrument, transposition); empty: each sheet's own. */
  view?: SheetViewParams;
  /** The stage mode's choice, the start value here. */
  collapse: boolean;
  onClose: () => void;
}

const keyOf = (s: PrintSong): string => s.key ?? s.id;

/** The class on <body> while sheets are printed: the print style then shows only them. */
export const PRINTING_CLASS = "sheets-printing";

/** Print or save as PDF: pick and order the songs, choose the paper, then the browser's
 *  print dialog opens with one song per page. Saved as a PDF, the songs open without the hub. */
export function PrintDialog({ api, songs, view = {}, collapse, onClose }: DialogProps): ReactElement {
  const { t, i18n } = useTranslation();
  const [order, setOrder] = useState<PrintSong[]>(songs);
  const [picked, setPicked] = useState<Set<string>>(() => new Set(songs.map(keyOf)));
  const [options, setOptions] = useState<PrintOptions>({ paper: "a4", orientation: "portrait", collapse });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sheets, setSheets] = useState<SheetDetail[] | null>(null);
  // print() returned before the print was done (phones and tablets): the pages stay until afterprint
  // or until the person closes this dialog
  const [waiting, setWaiting] = useState(false);
  const [run, setRun] = useState(0);
  const chosen = order.filter((s) => picked.has(keyOf(s)));

  const toggle = (id: string) =>
    setPicked((old) => {
      const next = new Set(old);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const start = async (): Promise<void> => {
    setBusy(true);
    setWaiting(false);
    setError("");
    try {
      const loaded = await Promise.all(chosen.map((s) => api.sheet(s.id, view)));
      setRun((n) => n + 1);
      setSheets(loaded);
    } catch (e: unknown) {
      setBusy(false);
      setError(describeError(toActionError(e), t, i18n));
    }
  };

  const printed = () => {
    setSheets(null);
    setBusy(false);
    setWaiting(false);
    onClose();
  };

  const returned = () => {
    setBusy(false);
    setWaiting(true);
  };

  return (
    <Dialog title={t("sheets.print.title")} onClose={busy ? () => undefined : onClose}>
      <div className="sheet-print-dialog" data-testid="sheet-print-dialog">
        <p className="muted small">{t("sheets.print.hint")}</p>
        {order.length > 1 && (
          <p className="small">
            {t("sheets.print.picked", { count: chosen.length, total: order.length })}{" "}
            <button type="button" className="link-button" onClick={() => setPicked(new Set(order.map(keyOf)))}>
              {t("sheets.print.all")}
            </button>{" "}
            <button type="button" className="link-button" onClick={() => setPicked(new Set())}>
              {t("sheets.print.none")}
            </button>
          </p>
        )}
        <ol className="sheet-print-songs" aria-label={t("sheets.print.songs")}>
          {order.map((s, i) => (
            <li key={keyOf(s)}>
              <label className="check">
                <input type="checkbox" checked={picked.has(keyOf(s))} onChange={() => toggle(keyOf(s))} />
                <span>
                  {s.title}
                  {s.artist && <span className="muted small"> · {s.artist}</span>}
                </span>
              </label>
              {order.length > 1 && (
                <span className="sheet-print-move">
                  <button type="button" disabled={i === 0} onClick={() => setOrder((o) => move(o, i, -1))} aria-label={t("sheets.print.up", { title: s.title })}>
                    ↑
                  </button>
                  <button type="button" disabled={i === order.length - 1} onClick={() => setOrder((o) => move(o, i, 1))} aria-label={t("sheets.print.down", { title: s.title })}>
                    ↓
                  </button>
                </span>
              )}
            </li>
          ))}
        </ol>
        <div className="sheets-row">
          <select value={options.paper} onChange={(e) => setOptions({ ...options, paper: e.target.value as Paper })} aria-label={t("sheets.print.paper")}>
            {PAPERS.map((p) => (
              <option key={p} value={p}>
                {t(`sheets.print.paperName.${p}`)}
              </option>
            ))}
          </select>
          <select value={options.orientation} onChange={(e) => setOptions({ ...options, orientation: e.target.value as Orientation })} aria-label={t("sheets.print.orientation")}>
            {ORIENTATIONS.map((o) => (
              <option key={o} value={o}>
                {t(`sheets.print.orientationName.${o}`)}
              </option>
            ))}
          </select>
          <label className="check">
            <input type="checkbox" checked={options.collapse} onChange={(e) => setOptions({ ...options, collapse: e.target.checked })} />{" "}
            {t("sheets.collapse")}
          </label>
        </div>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {(busy || waiting) && (
          <p className="muted small" role="status">
            {t(busy ? "sheets.print.preparing" : "sheets.print.waiting")}
          </p>
        )}
        <div className="dialog-actions">
          <button type="button" className="link-button" onClick={onClose} disabled={busy}>
            {t(waiting ? "sheets.print.close" : "sheets.cancel")}
          </button>
          <button type="button" onClick={() => void start()} disabled={busy || chosen.length === 0} data-testid="sheet-print-start">
            {t("sheets.print.start")}
          </button>
        </div>
      </div>
      {sheets && <PrintPages key={run} sheets={sheets} options={options} onDone={printed} onReturned={returned} />}
    </Dialog>
  );
}

/** The pages to print, outside the desktop (a portal on <body>): laid out off screen at the
 *  paper's size, fitted song by song, then printed. They go away once the print dialog closes
 *  (afterprint). Where print() returns before that (phones and tablets lay the pages out after the
 *  call), they stay until afterprint or until the dialog that holds them closes. */
export function PrintPages({
  sheets,
  options,
  onDone,
  onReturned,
}: {
  sheets: SheetDetail[];
  options: PrintOptions;
  onDone: () => void;
  /** print() returned without afterprint: the pages are still wanted. */
  onReturned?: () => void;
}): ReactElement {
  const { t } = useTranslation();
  const root = useRef<HTMLDivElement>(null);
  // the print runs once; a new callback from a parent's render does not start it again
  const done = useRef(onDone);
  const back = useRef(onReturned);
  useLayoutEffect(() => {
    done.current = onDone;
    back.current = onReturned;
  });
  const box = pageBox(options.paper, options.orientation);

  useEffect(() => {
    document.body.classList.add(PRINTING_CLASS);
    return () => document.body.classList.remove(PRINTING_CLASS);
  }, []);

  // each page: the columns and the largest font at which the song stays on it
  useLayoutEffect(() => {
    const pages = Array.from(root.current?.querySelectorAll<HTMLElement>(".sheet-print-page") ?? []);
    pages.forEach((page) => {
      const b = page.querySelector<HTMLElement>(".sheet-print-box");
      const c = page.querySelector<HTMLElement>(".sheet-print-body");
      if (!b || !c) return;
      const height = b.clientHeight;
      c.style.height = `${height}px`;
      const f = chooseFit(printColumns(b.clientWidth), (columns, font) => {
        c.style.columnCount = String(columns);
        c.style.fontSize = `${font}px`;
        return c.scrollWidth <= c.clientWidth + 1;
      });
      c.style.columnCount = String(f.columns);
      c.style.fontSize = `${f.font}px`;
      // too long for one page even in the smallest font: it runs on over the next pages
      c.style.height = f.fits ? `${height}px` : "auto";
      page.classList.toggle("is-long", !f.fits);
    });
  }, [sheets, options]);

  // after the fit above (layout effects run first): print once, then the pages go
  useEffect(() => {
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      done.current();
    };
    window.addEventListener("afterprint", finish);
    // desktop browsers hold here until the dialog closes and fire afterprint before returning;
    // others return at once and lay the pages out later, so the pages must outlive the call
    const frame = window.requestAnimationFrame(() => {
      try {
        window.print();
      } finally {
        if (!finished) {
          if (back.current) back.current();
          else finish();
        }
      }
    });
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("afterprint", finish);
    };
  }, []);

  return createPortal(
    <div ref={root} className="sheet-print" data-testid="sheet-print">
      <style>{pageRule(options.paper, options.orientation)}</style>
      {sheets.map((s, i) => {
        const v = s.view;
        const sections = v.sections;
        return (
          <section key={i} className="sheet-print-page" style={{ width: `${box.width}px`, height: `${box.height}px` }} data-testid="sheet-print-page">
            <header className="sheet-print-head">
              <strong>{v.title}</strong>
              {v.artist && <span>{v.artist}</span>}
              {v.shown_key && (
                <span>
                  {t("sheets.key")}: <strong>{v.shown_key}</strong>
                </span>
              )}
              {v.capo > 0 && v.capo_view && <span>{t("sheets.capoN", { n: v.capo })}</span>}
            </header>
            <div className="sheet-print-box">
              <div className="sheet-print-body">
                <Body items={options.collapse ? collapseRepeats(sections) : asWritten(sections)} />
              </div>
            </div>
          </section>
        );
      })}
    </div>,
    document.body,
  );
}
