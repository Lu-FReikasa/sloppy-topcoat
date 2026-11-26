import { useEffect, useLayoutEffect, useRef, useState, type ReactElement } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { Dialog } from "../components/Dialog";
import { describeError, toActionError } from "../errors";
import { chooseFit } from "./fit";
import { move, ORIENTATIONS, pageBox, pageRule, PAPERS, printColumns, type Orientation, type Paper } from "./print";
import { Body, withoutTabs } from "./SheetsPage";
import type { SheetDetail, SheetViewParams } from "./types";
import "./print.css";

/** A song offered for printing: the sheet (one version) and how the list names it. */
export interface PrintSong {
  id: string;
  title: string;
  artist: string;
}

export interface PrintOptions {
  paper: Paper;
  orientation: Orientation;
  hideTabs: boolean;
}

interface DialogProps {
  api: ApiClient;
  /** The songs in the order they are printed; all are picked at the start. */
  songs: PrintSong[];
  /** The view of every sheet (instrument, transposition); empty: each sheet's own. */
  view?: SheetViewParams;
  /** The stage mode's choice, the start value here. */
  hideTabs: boolean;
  onClose: () => void;
}

/** The class on <body> while sheets are printed: the print style then shows only them. */
export const PRINTING_CLASS = "sheets-printing";

/** Print or save as PDF: pick and order the songs, choose paper and tab lines, then the browser's
 *  print dialog opens with one song per page. Saved as a PDF, the songs open without the hub. */
export function PrintDialog({ api, songs, view = {}, hideTabs, onClose }: DialogProps): ReactElement {
  const { t, i18n } = useTranslation();
  const [order, setOrder] = useState<PrintSong[]>(songs);
  const [picked, setPicked] = useState<Set<string>>(() => new Set(songs.map((s) => s.id)));
  const [options, setOptions] = useState<PrintOptions>({ paper: "a4", orientation: "portrait", hideTabs });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sheets, setSheets] = useState<SheetDetail[] | null>(null);
  const chosen = order.filter((s) => picked.has(s.id));

  const toggle = (id: string) =>
    setPicked((old) => {
      const next = new Set(old);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const start = async (): Promise<void> => {
    setBusy(true);
    setError("");
    try {
      setSheets(await Promise.all(chosen.map((s) => api.sheet(s.id, view))));
    } catch (e: unknown) {
      setBusy(false);
      setError(describeError(toActionError(e), t, i18n));
    }
  };

  const printed = () => {
    setSheets(null);
    setBusy(false);
    onClose();
  };

  return (
    <Dialog title={t("sheets.print.title")} onClose={busy ? () => undefined : onClose}>
      <div className="sheet-print-dialog" data-testid="sheet-print-dialog">
        <p className="muted small">{t("sheets.print.hint")}</p>
        {order.length > 1 && (
          <p className="small">
            {t("sheets.print.picked", { count: chosen.length, total: order.length })}{" "}
            <button type="button" className="link-button" onClick={() => setPicked(new Set(order.map((s) => s.id)))}>
              {t("sheets.print.all")}
            </button>{" "}
            <button type="button" className="link-button" onClick={() => setPicked(new Set())}>
              {t("sheets.print.none")}
            </button>
          </p>
        )}
        <ol className="sheet-print-songs" aria-label={t("sheets.print.songs")}>
          {order.map((s, i) => (
            <li key={s.id}>
              <label className="check">
                <input type="checkbox" checked={picked.has(s.id)} onChange={() => toggle(s.id)} />
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
            <input type="checkbox" checked={options.hideTabs} onChange={(e) => setOptions({ ...options, hideTabs: e.target.checked })} />{" "}
            {t("sheets.stage.hideTabs")}
          </label>
        </div>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {busy && (
          <p className="muted small" role="status">
            {t("sheets.print.preparing")}
          </p>
        )}
        <div className="dialog-actions">
          <button type="button" className="link-button" onClick={onClose} disabled={busy}>
            {t("sheets.cancel")}
          </button>
          <button type="button" onClick={() => void start()} disabled={busy || chosen.length === 0} data-testid="sheet-print-start">
            {t("sheets.print.start")}
          </button>
        </div>
      </div>
      {sheets && <PrintPages sheets={sheets} options={options} onDone={printed} />}
    </Dialog>
  );
}

/** The pages to print, outside the desktop (a portal on <body>): laid out off screen at the
 *  paper's size, fitted song by song, then printed. They go away once the print dialog closes. */
export function PrintPages({ sheets, options, onDone }: { sheets: SheetDetail[]; options: PrintOptions; onDone: () => void }): ReactElement {
  const { t } = useTranslation();
  const root = useRef<HTMLDivElement>(null);
  // the print runs once; a new callback from a parent's render does not start it again
  const done = useRef(onDone);
  useLayoutEffect(() => {
    done.current = onDone;
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
    // most browsers hold here until the dialog closes; afterprint covers the ones that do not
    const frame = window.requestAnimationFrame(() => {
      try {
        window.print();
      } finally {
        finish();
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
      {sheets.map((s) => {
        const v = s.view;
        const sections = options.hideTabs ? withoutTabs(v.sections) : v.sections;
        return (
          <section key={s.id} className="sheet-print-page" style={{ width: `${box.width}px`, height: `${box.height}px` }} data-testid="sheet-print-page">
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
                <Body sections={sections} />
              </div>
            </div>
          </section>
        );
      })}
    </div>,
    document.body,
  );
}
