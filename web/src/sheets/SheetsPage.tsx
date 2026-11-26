import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { Dialog } from "../components/Dialog";
import { describeError, toActionError } from "../errors";
import { sheetsHref } from "../route";
import type {
  Instrument,
  Setlist,
  SheetComparison,
  SheetDetail,
  SheetLine,
  SheetResearchJob,
  SheetSection,
  SheetSummary,
} from "./types";
import {
  balanceColumns,
  chooseFit,
  packColumns,
  maxColumns,
  neighbour,
  positionOf,
  SORT_MODES,
  visibleGroups,
  type Fit,
  type SortMode,
} from "./fit";
import { artistsOf, type SheetGroup } from "./group";
import { PrintDialog, type PrintSong } from "./PrintSheets";
import { asWritten, collapseRepeats, type BodyItem } from "./collapse";
import { setlistGroups } from "./setlist";
import { Setlists } from "./Setlists";
import "./sheets.css";

export { artistsOf, groupSheets, type SheetGroup } from "./group";

interface Props {
  api: ApiClient;
  /** The id of the open sheet. */
  id?: string;
}

const POLL_MS = 3000;
const SORT_KEY = "topcoat.sheets.sort";
const COLLAPSE_KEY = "topcoat.sheets.stageCollapse";
const STAGE_KEY = "topcoat.sheets.stage";
const SIDE_KEY = "topcoat.sheets.side";

/** A remembered choice of this browser; storage may be missing or blocked. */
function remembered(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function remember(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* not remembered; the choice still holds for this page */
  }
}

/** A choice of this tab that outlives a new address (both shells mount the page afresh). */
function session(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function keepForSession(key: string, on: boolean) {
  try {
    if (on) window.sessionStorage.setItem(key, "1");
    else window.sessionStorage.removeItem(key);
  } catch {
    /* the choice then ends with this page */
  }
}

const KINDS = ["intro", "verse", "pre_chorus", "chorus", "bridge", "solo", "riff", "interlude", "outro", "tab", "other"];

/** The colour class of a part of the song (the same colour in the flow and in the sheet). */
export function kindClass(kind: string): string {
  return `sheet-kind-${KINDS.includes(kind) ? kind : "other"}`;
}

/** A short name of a part for the flow: "Verse 2" → "V2", "Chorus" → "C". */
export function flowLabel(kind: string, label: string): string {
  const n = /(\d+)\s*$/.exec(label)?.[1] ?? "";
  const short: Record<string, string> = {
    intro: "In", verse: "V", pre_chorus: "Pre", chorus: "C", bridge: "B",
    solo: "Solo", riff: "Riff", interlude: "Int", outro: "Out", tab: "Tab",
  };
  return (short[kind] ?? (label.slice(0, 3) || "?")) + n;
}

/** Sheets: the person's chord sheets, a new research (the profile "sheets") and the open sheet as
 *  its instrument shows it. The view (instrument, capo, transposition) changes nothing stored
 *  except "capo shapes on/off", which the sheet keeps.
 *
 *  Two views (#148): with no sheet open, the library fills the page (search first, then sort and
 *  artist, then the songs as a table, the setlists beside it). An open sheet takes the whole width;
 *  only a song opened from a setlist keeps that setlist beside it as a narrow side list. */
export function SheetsPage({ api, id }: Props) {
  const { t, i18n } = useTranslation();
  const [list, setList] = useState<SheetSummary[] | null>(null);
  const [jobs, setJobs] = useState<SheetResearchJob[]>([]);
  const [error, setError] = useState("");
  const [loads, setLoads] = useState(0);
  const reload = useCallback(() => setLoads((n) => n + 1), []);
  const fail = useCallback((e: unknown) => setError(describeError(toActionError(e), t, i18n)), [t, i18n]);
  const [query, setQuery] = useState("");
  const [artist, setArtist] = useState("");
  const [adding, setAdding] = useState(false);
  const [sort, setSortState] = useState<SortMode>(() => {
    const r = remembered(SORT_KEY) as SortMode | null;
    return r && SORT_MODES.includes(r) ? r : "title";
  });
  const setSort = (m: SortMode) => {
    setSortState(m);
    remember(SORT_KEY, m);
  };
  // the stage mode survives paging: a new address may mount this page afresh
  const [stage, setStageState] = useState(() => session(STAGE_KEY) === "1");
  const setStage = useCallback((on: boolean) => {
    setStageState(on);
    keepForSession(STAGE_KEY, on);
  }, []);
  // the setlist beside the open sheet: on when the song was opened from a setlist (or the person
  // asked for it), off when it was opened from the library. Kept per tab, like the stage mode.
  const [side, setSideState] = useState(() => session(SIDE_KEY) === "1");
  const setSide = useCallback((on: boolean) => {
    setSideState(on);
    keepForSession(SIDE_KEY, on);
  }, []);
  // the songs offered for printing: the shown list or a setlist (null: no print dialog)
  const [printSongs, setPrintSongs] = useState<PrintSong[] | null>(null);
  // a setlist played on the stage: its songs page instead of the list, from the song at `at`.
  // The address stays as it is (paging happens inside the stage), so the page is not remounted.
  const [playing, setPlaying] = useState<{ setlist: Setlist; at: number } | null>(null);
  const exitStage = useCallback(() => {
    setStage(false);
    setPlaying(null);
    if (document.fullscreenElement) document.exitFullscreen?.().catch(() => undefined);
  }, [setStage]);
  const groups = useMemo(() => visibleGroups(list ?? [], query, artist, sort), [list, query, artist, sort]);
  const playGroups = useMemo(() => (playing ? setlistGroups(playing.setlist.items, list ?? []) : null), [playing, list]);
  const play = (setlist: Setlist, from: number) => {
    const songs = setlistGroups(setlist.items, list ?? []);
    const at = Math.max(0, songs.findIndex((g) => Number(g.key) >= from));
    if (!songs[at]) return;
    setPlaying({ setlist, at });
    setStage(true);
  };

  useEffect(() => {
    api.sheets().then((r) => setList(r.sheets), fail);
    api.sheetResearchJobs().then((r) => setJobs(r.jobs), () => setJobs([]));
  }, [api, fail, loads]);

  // while a research runs, its progress is read every few seconds; a finished one refreshes the list
  const running = jobs.some((j) => j.status === "running");
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => {
      api.sheetResearchJobs().then((r) => {
        setJobs(r.jobs);
        if (!r.jobs.some((j) => j.status === "running")) reload();
      }, () => undefined);
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [api, running, reload]);

  // keys of the normal view: Esc goes back to the library; "/" or Ctrl+F finds the search
  const shell = useRef<HTMLElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const inStage = stage && (!!id || !!playing);
  useEffect(() => {
    if (inStage) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || !inFront(shell.current)) return;
      const find = (e.key === "/" && !e.ctrlKey && !e.metaKey && !e.altKey) || ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "f");
      if (id && e.key === "Escape" && !typing(e.target) && !shellPopupOpen(shell.current)) {
        e.preventDefault();
        window.location.hash = sheetsHref();
      } else if (!id && find && (e.key !== "/" || !typing(e.target)) && search.current) {
        e.preventDefault();
        search.current.focus();
        search.current.select();
      }
    };
    // capture: before the desktop shell's own Esc (minimise the window), which then leaves it
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [id, inStage]);

  const first = playing && playGroups?.[playing.at]?.versions[0];
  if (stage && playing && playGroups && first)
    return (
      <Stage
        key={`setlist:${playing.setlist.id}:${playing.at}`}
        api={api}
        id={first.id}
        from={id}
        groups={playGroups}
        at={playing.at}
        setlistName={playing.setlist.name}
        onExit={exitStage}
      />
    );
  if (stage && id) return <Stage api={api} id={id} groups={groups} onExit={exitStage} />;
  const setlists = list && list.length > 0 && (
    <Setlists
      api={api}
      list={list}
      openId={id}
      onOpen={() => setSide(true)}
      onPlay={play}
      onPrint={(s) => setPrintSongs(setlistSongs(s, list))}
      onError={fail}
    />
  );
  return (
    <section ref={shell} className="sheets-shell" data-testid="sheets">
      {id ? (
        <div className={`sheets-open${side ? " has-side" : ""}`}>
          <div className="sheets-bar">
            <a className="sheets-back" href={sheetsHref()} data-testid="sheets-back" aria-keyshortcuts="Escape">
              ← {t("sheets.back")}
            </a>
            {list && list.length > 0 && (
              <button type="button" className="sheets-side-toggle" aria-pressed={side} onClick={() => setSide(!side)}>
                {t("sheets.setlists.side")}
              </button>
            )}
          </div>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <div className="sheets-open-body">
            {side && setlists && (
              <aside className="sheets-side" aria-label={t("sheets.setlists.title")}>
                {setlists}
              </aside>
            )}
            <OpenSheet key={id} api={api} id={id} onChanged={reload} onStage={() => setStage(true)} />
          </div>
        </div>
      ) : (
        <div className="sheets-library" aria-label={t("sheets.list")} role="region">
          <header className="sheets-head">
            <h1 className="sheets-title">{t("sheets.title")}</h1>
            <button
              type="button"
              onClick={() => {
                setError("");
                setAdding(true);
              }}
              data-testid="sheets-new"
            >
              {t("sheets.new.open")}
            </button>
          </header>
          {error && !adding && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          {/* a finished research shows as its sheet in the list; only running and failed ones here */}
          {jobs.some((j) => j.status !== "done") && <Jobs jobs={jobs.filter((j) => j.status !== "done")} />}
          {list !== null && list.length === 0 && <p className="muted">{t("sheets.empty")}</p>}
          {list && list.length > 0 && (
            <div className="sheets-library-body">
              {/* the print link stays with the table: on a phone the setlists stand below it */}
              <div className="sheets-library-main">
                <SheetTable
                  list={list}
                  groups={groups}
                  search={search}
                  onOpen={() => setSide(false)}
                  filter={{ query, setQuery, artist, setArtist, sort, setSort }}
                />
                {groups.length > 0 && (
                  <button type="button" className="link-button small" onClick={() => setPrintSongs(songsOf(groups))} data-testid="sheets-print-list">
                    {t("sheets.print.list")}
                  </button>
                )}
              </div>
              <aside className="sheets-library-side">{setlists}</aside>
            </div>
          )}
        </div>
      )}
      {printSongs && (
        <PrintDialog
          api={api}
          songs={printSongs}
          collapse={remembered(COLLAPSE_KEY) !== "0"}
          onClose={() => setPrintSongs(null)}
        />
      )}
      {adding && (
        <Dialog title={t("sheets.new.title")} onClose={() => setAdding(false)}>
          <div className="sheets-new">
            {/* a refused research or paste shows here: the library's own line is behind the dialog */}
            {error && (
              <p className="error" role="alert">
                {error}
              </p>
            )}
            <ResearchForm
              api={api}
              onStarted={() => {
                setAdding(false);
                reload();
              }}
              onError={fail}
            />
            <PasteForm api={api} onSaved={() => setAdding(false)} onError={fail} />
            <div className="dialog-actions">
              <button type="button" className="link-button" onClick={() => setAdding(false)}>
                {t("sheets.close")}
              </button>
            </div>
          </div>
        </Dialog>
      )}
    </section>
  );
}

/** The songs to print, in the given order: the newest version of each (a setlist's songs hold
 *  only the sheet it plays). A song's key tells apart a sheet that is in a setlist twice. */
function songsOf(groups: SheetGroup[]): PrintSong[] {
  return groups.flatMap((g) => {
    const s = g.versions[g.versions.length - 1];
    return s ? [{ key: g.key, id: s.id, title: g.title, artist: g.artist }] : [];
  });
}

/** A setlist's songs to print, in its order; sheets deleted meanwhile are left out. */
export function setlistSongs(setlist: Setlist, list: SheetSummary[]): PrintSong[] {
  return songsOf(setlistGroups(setlist.items, list));
}

/** Whether the keys belong to this page: in the desktop shell only the window in front takes
 *  them, and never while a dialog is open. */
function inFront(el: HTMLElement | null): boolean {
  if (!el || document.querySelector('[aria-modal="true"]')) return false;
  const win = el.closest(".dt-program");
  return !win || win.classList.contains("dt-focused");
}

/** Whether the shell around the page has a menu or drawer open (the web shell's drawer or user
 *  menu, the taskbar menu): Esc then closes that, it does not leave the sheet. Expanded controls
 *  inside this page or in other windows do not count. */
function shellPopupOpen(el: HTMLElement | null): boolean {
  return [...document.querySelectorAll('[aria-expanded="true"]')].some((x) => !el?.contains(x) && !x.closest(".dt-program"));
}

function typing(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

interface ListFilter {
  query: string;
  setQuery: (q: string) => void;
  artist: string;
  setArtist: (a: string) => void;
  sort: SortMode;
  setSort: (m: SortMode) => void;
}

/** The sheet a song opens with: its newest version, as the stage and the setlists play it. */
const newestOf = (g: SheetGroup) => g.versions[g.versions.length - 1];

function SheetTable({
  list,
  groups,
  search,
  onOpen,
  filter,
}: {
  list: SheetSummary[];
  groups: SheetGroup[];
  search: React.RefObject<HTMLInputElement | null>;
  onOpen: () => void;
  filter: ListFilter;
}) {
  const { t } = useTranslation();
  const { query, setQuery, artist, setArtist, sort, setSort } = filter;
  const artists = useMemo(() => artistsOf(list), [list]);
  const keyOf = (s: SheetSummary) => [s.key, s.capo ? t("sheets.capoN", { n: s.capo }) : ""].filter(Boolean).join(" · ");
  const openFirst = (e: React.KeyboardEvent) => {
    if (e.key !== "Enter") return;
    const s = groups[0] && newestOf(groups[0]);
    if (!s) return;
    e.preventDefault();
    onOpen();
    window.location.hash = sheetsHref(s.id);
  };
  return (
    <div className="sheets-library-main">
      <div className="sheets-filter">
        <input
          ref={search}
          type="search"
          className="sheets-search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={openFirst}
          placeholder={t("sheets.filter.search")}
          aria-label={t("sheets.filter.search")}
          aria-keyshortcuts="/ Control+F"
          title={t("sheets.filter.keys")}
          data-testid="sheets-search"
        />
        <select value={sort} onChange={(e) => setSort(e.target.value as SortMode)} aria-label={t("sheets.sort.label")}>
          {SORT_MODES.map((m) => (
            <option key={m} value={m}>
              {t(`sheets.sort.${m}`)}
            </option>
          ))}
        </select>
        {artists.length > 1 && (
          <select value={artist} onChange={(e) => setArtist(e.target.value)} aria-label={t("sheets.filter.artist")}>
            <option value="">{t("sheets.filter.allArtists")}</option>
            {artists.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        )}
      </div>
      {groups.length === 0 ? (
        <p className="muted small">{t("sheets.filter.none")}</p>
      ) : (
        <table className="sheets-table" data-testid="sheets-list">
          <thead>
            <tr>
              <th scope="col">{t("sheets.paste.songTitle")}</th>
              <th scope="col" className="sheets-col-artist">
                {t("sheets.research.artist")}
              </th>
              <th scope="col">{t("sheets.key")}</th>
              <th scope="col">{t("sheets.versions")}</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => {
              const s = newestOf(g)!;
              return (
                <tr key={g.key}>
                  <td className="sheets-col-title">
                    <a href={sheetsHref(s.id)} onClick={onOpen}>
                      {g.title}
                    </a>
                    {g.artist && <span className="sheets-row-artist muted small">{g.artist}</span>}
                  </td>
                  <td className="sheets-col-artist">{g.artist}</td>
                  <td className="sheets-col-key">{keyOf(s)}</td>
                  <td className="sheets-col-versions">
                    {g.versions.length > 1 && (
                      <span className="sheets-versions">
                        {g.versions.map((v, i) => (
                          <a
                            key={v.id}
                            href={sheetsHref(v.id)}
                            onClick={onOpen}
                            title={[keyOf(v), t(`sheets.instrument.${v.instrument}`), v.version_note].filter(Boolean).join(" · ")}
                          >
                            {t("sheets.version", { n: i + 1 })}
                          </a>
                        ))}
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ResearchForm({
  api,
  onStarted,
  onError,
}: {
  api: ApiClient;
  onStarted: () => void;
  onError: (e: unknown) => void;
}) {
  const { t } = useTranslation();
  const [song, setSong] = useState("");
  const [artist, setArtist] = useState("");
  const [instrument, setInstrument] = useState<Instrument>("guitar");
  const [wish, setWish] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!song.trim()) return;
    setBusy(true);
    api
      .sheetResearchStart({ song: song.trim(), artist: artist.trim(), instrument, wish: wish.trim() })
      .then(() => {
        setSong("");
        setWish("");
        onStarted();
      }, onError)
      .finally(() => setBusy(false));
  };
  return (
    <form className="sheets-form" onSubmit={submit} aria-label={t("sheets.research.title")}>
      <h2>{t("sheets.research.title")}</h2>
      <input value={song} onChange={(e) => setSong(e.target.value)} placeholder={t("sheets.research.song")} aria-label={t("sheets.research.song")} />
      <input value={artist} onChange={(e) => setArtist(e.target.value)} placeholder={t("sheets.research.artist")} aria-label={t("sheets.research.artist")} />
      <div className="sheets-row">
        <select value={instrument} onChange={(e) => setInstrument(e.target.value as Instrument)} aria-label={t("sheets.instrumentLabel")}>
          <option value="guitar">{t("sheets.instrument.guitar")}</option>
          <option value="piano">{t("sheets.instrument.piano")}</option>
        </select>
        <input value={wish} onChange={(e) => setWish(e.target.value)} placeholder={t("sheets.research.wish")} aria-label={t("sheets.research.wish")} />
      </div>
      <button type="submit" disabled={busy || !song.trim()}>
        {t("sheets.research.start")}
      </button>
    </form>
  );
}

function Jobs({ jobs }: { jobs: SheetResearchJob[] }) {
  const { t } = useTranslation();
  return (
    <ul className="sheets-jobs" data-testid="sheets-jobs">
      {jobs.slice(0, 5).map((j) => (
        <li key={j.id} className={`sheets-job sheets-job-${j.status}`}>
          <div>
            <strong>{j.song}</strong> {j.artist && <span className="muted">– {j.artist}</span>}{" "}
            <span className="sheets-badge">{t(`sheets.research.status.${j.status}`)}</span>
          </div>
          {j.status === "running" && j.steps.length > 0 && <div className="muted small">{j.steps[j.steps.length - 1]}</div>}
          {j.status === "failed" && <div className="error small">{j.error}</div>}
          {j.status === "done" && (
            <div className="small">
              {j.sheet_id ? <a href={sheetsHref(j.sheet_id)}>{t("sheets.research.openSheet")}</a> : <span className="muted">{t("sheets.research.noSheet")}</span>}
              {j.report_id && (
                <>
                  {" · "}
                  <a href={`#/library/report:${j.report_id}`}>{t("sheets.research.openReport")}</a>
                </>
              )}
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function PasteForm({ api, onSaved, onError }: { api: ApiClient; onSaved: () => void; onError: (e: unknown) => void }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  const [format, setFormat] = useState<"plain" | "chordpro">("plain");
  const [instrument, setInstrument] = useState<Instrument>("guitar");
  if (!open)
    return (
      <button type="button" className="link-button small" onClick={() => setOpen(true)}>
        {t("sheets.paste.open")}
      </button>
    );
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    api.sheetCreate({ text, format, instrument, title: title.trim() }).then((s) => {
      setOpen(false);
      setText("");
      onSaved();
      window.location.hash = sheetsHref(s.id);
    }, onError);
  };
  return (
    <form className="sheets-form" onSubmit={submit} aria-label={t("sheets.paste.title")}>
      <h2>{t("sheets.paste.title")}</h2>
      <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t("sheets.paste.songTitle")} aria-label={t("sheets.paste.songTitle")} />
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={8} placeholder={t("sheets.paste.text")} aria-label={t("sheets.paste.text")} />
      <div className="sheets-row">
        <select value={format} onChange={(e) => setFormat(e.target.value as "plain" | "chordpro")} aria-label={t("sheets.paste.format")}>
          <option value="plain">{t("sheets.paste.plain")}</option>
          <option value="chordpro">ChordPro</option>
        </select>
        <select value={instrument} onChange={(e) => setInstrument(e.target.value as Instrument)} aria-label={t("sheets.instrumentLabel")}>
          <option value="guitar">{t("sheets.instrument.guitar")}</option>
          <option value="piano">{t("sheets.instrument.piano")}</option>
        </select>
      </div>
      <div className="sheets-row">
        <button type="submit" disabled={!text.trim()}>
          {t("sheets.paste.save")}
        </button>
        <button type="button" className="link-button" onClick={() => setOpen(false)}>
          {t("sheets.cancel")}
        </button>
      </div>
    </form>
  );
}

function OpenSheet({ api, id, onChanged, onStage }: { api: ApiClient; id: string; onChanged: () => void; onStage: () => void }) {
  const { t, i18n } = useTranslation();
  const [sheet, setSheet] = useState<SheetDetail | null>(null);
  const [instrument, setInstrument] = useState<Instrument | null>(null);
  const [transpose, setTranspose] = useState(0);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<"sheet" | "sources" | "chordpro">("sheet");
  const [printing, setPrinting] = useState(false);
  const [collapse, setCollapse] = useState(false);
  const fail = useCallback((e: unknown) => setError(describeError(toActionError(e), t, i18n)), [t, i18n]);

  useEffect(() => {
    api.sheet(id, { instrument: instrument ?? undefined, transpose }).then(setSheet, fail);
  }, [api, id, instrument, transpose, fail]);

  if (!sheet) return error ? <p className="error">{error}</p> : <p className="muted">{t("sheets.loading")}</p>;
  const v = sheet.view;
  const shownInstrument = instrument ?? sheet.instrument;
  const toggleCapo = () =>
    api.sheetUpdate(id, { capo_view: !sheet.capo_view }).then(() => {
      // the stored choice changed; the view is read again with the same instrument and transposition
      api.sheet(id, { instrument: instrument ?? undefined, transpose }).then(setSheet, fail);
      onChanged();
    }, fail);
  const remove = () => {
    if (!window.confirm(t("sheets.deleteConfirm", { title: sheet.title }))) return;
    api.sheetDelete(id).then(() => {
      window.location.hash = sheetsHref();
      onChanged();
    }, fail);
  };
  return (
    <article className="sheet" data-testid="sheet">
      <header className="sheet-head">
        <h2 className="sheet-name">{v.title}</h2>
        {v.artist && <div className="sheet-artist">{v.artist}</div>}
        <div className="sheet-facts" data-testid="sheet-facts">
          <span>
            {t("sheets.key")}: <strong>{v.shown_key ?? "?"}</strong>
            {v.key_estimated && <span className="muted"> ({t("sheets.estimated")})</span>}
          </span>
          {v.capo > 0 && (
            <span>
              {t("sheets.capoN", { n: v.capo })}
              {!v.capo_view && <span className="muted"> · {t("sheets.soundingIn", { key: v.key })}</span>}
            </span>
          )}
          {v.capo_view && v.key && <span className="muted">{t("sheets.soundingIn", { key: v.key })}</span>}
          {sheet.version_note && <span className="muted">{sheet.version_note}</span>}
        </div>
        <div className="sheet-controls">
          <button type="button" onClick={onStage} data-testid="sheet-stage-open">
            {t("sheets.stage.open")}
          </button>
          <div className="sheet-switch" role="group" aria-label={t("sheets.instrumentLabel")}>
            {(["guitar", "piano"] as const).map((ins) => (
              <button key={ins} type="button" aria-pressed={shownInstrument === ins} onClick={() => setInstrument(ins)}>
                {t(`sheets.instrument.${ins}`)}
              </button>
            ))}
          </div>
          {shownInstrument === "guitar" && sheet.capo > 0 && (
            <button type="button" onClick={toggleCapo} aria-pressed={sheet.capo_view}>
              {sheet.capo_view ? t("sheets.showSounding") : t("sheets.showCapo")}
            </button>
          )}
          <div className="sheet-switch" role="group" aria-label={t("sheets.transpose")}>
            <button type="button" onClick={() => setTranspose((n) => Math.max(-11, n - 1))} aria-label={t("sheets.down")}>
              −
            </button>
            <span className="sheet-transpose">{transpose > 0 ? `+${transpose}` : transpose}</span>
            <button type="button" onClick={() => setTranspose((n) => Math.min(11, n + 1))} aria-label={t("sheets.up")}>
              +
            </button>
          </div>
          <button type="button" aria-pressed={collapse} onClick={() => setCollapse(!collapse)}>
            {t("sheets.collapse")}
          </button>
          <button type="button" onClick={() => setPrinting(true)} data-testid="sheet-print-open">
            {t("sheets.print.open")}
          </button>
          {sheet.report_id && (
            <a className="small" href={`#/library/report:${sheet.report_id}`}>
              {t("sheets.research.openReport")}
            </a>
          )}
          <button type="button" className="link-button small" onClick={remove}>
            {t("sheets.delete")}
          </button>
        </div>
        <Flow sheet={sheet} />
        <div className="sheet-tabs" role="tablist">
          {(["sheet", "sources", "chordpro"] as const).map((k) => (
            <button key={k} type="button" role="tab" aria-selected={tab === k} onClick={() => setTab(k)}>
              {t(`sheets.tab.${k}`, { n: sheet.sources })}
            </button>
          ))}
        </div>
      </header>
      {printing && (
        <PrintDialog
          api={api}
          songs={[{ id, title: v.title, artist: v.artist }]}
          view={{ instrument: instrument ?? undefined, transpose }}
          collapse={collapse}
          onClose={() => setPrinting(false)}
        />
      )}
      {error && <p className="error">{error}</p>}
      {tab === "sheet" && <Body items={collapse ? collapseRepeats(v.sections) : asWritten(v.sections)} />}
      {tab === "sources" && <Sources api={api} sheet={sheet} />}
      {tab === "chordpro" && <pre className="sheet-chordpro">{v.chordpro}</pre>}
    </article>
  );
}

function Flow({ sheet }: { sheet: SheetDetail }) {
  const { t } = useTranslation();
  return (
    <ol className="sheet-flow" aria-label={t("sheets.flow")} data-testid="sheet-flow">
      {sheet.view.flow.map((f) => (
        <li key={f.index} className={`sheet-flow-step ${kindClass(f.kind)}`} title={f.label}>
          <a href={`#sheet-part-${f.index}`} onClick={(e) => {
            e.preventDefault();
            document.getElementById(`sheet-part-${f.repeat_of ?? f.index}`)?.scrollIntoView({ block: "start", behavior: "smooth" });
          }}>
            {flowLabel(f.kind, f.label)}
          </a>
        </li>
      ))}
    </ol>
  );
}

/** `anchors` off for the stage's hidden measuring copy, so ids stay unique on the page. */
export function Body({ items, anchors: withAnchors = true }: { items: BodyItem[]; anchors?: boolean }) {
  const { t } = useTranslation();
  const name = (s: SheetSection) => s.label || t(`sheets.kind.${KINDS.includes(s.kind) ? s.kind : "other"}`);
  return (
    <div className="sheet-body">
      {items.map(({ section: s, again, times, indices }) => {
        // the flow links to every part; parts merged into this block keep their anchor here
        const id = (i: number) => (withAnchors ? `sheet-part-${i}` : undefined);
        const anchors = withAnchors && indices.slice(1).map((i) => <span key={i} id={id(i)} />);
        const count = times > 1 && <span className="sheet-times"> ×{times}</span>;
        if (again)
          return (
            <section key={s.index} id={id(s.index)} className={`sheet-part sheet-again ${kindClass(s.kind)}`} data-testid="sheet-again">
              {anchors}
              <h3 className="sheet-part-label">
                {name(s)}
                {count}
                <span className="muted small"> · {t("sheets.asAbove")}</span>
              </h3>
            </section>
          );
        return (
          <section key={s.index} id={id(s.index)} className={`sheet-part ${kindClass(s.kind)}`}>
            {anchors}
            <h3 className="sheet-part-label">
              {name(s)}
              {count}
              {s.repeat_of !== null && <span className="muted small"> · {t("sheets.repeat")}</span>}
            </h3>
            {s.lines.map((ln, i) => (
              <Line key={i} line={ln} />
            ))}
          </section>
        );
      })}
    </div>
  );
}

/** A line as chords over lyrics: each piece is a column with its chord on top. */
function Line({ line }: { line: SheetLine }) {
  if (line.comment !== undefined) return <div className="sheet-comment">{line.comment}</div>;
  const segs = line.segments ?? [];
  // a line of chords only; "x4" after it is a repeat mark, shown as such
  const repeat = /(?:^|\s)x(\d+)(?=\s|$)/i.exec(segs.map((g) => g.text).join(" "))?.[1];
  const lyricless = segs.every((g) => !g.text.replace(/(?:^|\s)x\d+(?=\s|$)/i, "").trim());
  return (
    <div className={`sheet-line${lyricless ? " sheet-line-chords" : ""}`}>
      {segs.map((g, i) => (
        <span key={i} className="sheet-seg">
          <span className="sheet-chord">{g.chord ?? " "}</span>
          <span className="sheet-text">{g.text.length ? g.text : " "}</span>
        </span>
      ))}
      {lyricless && repeat && <span className="sheet-repeat">×{repeat}</span>}
    </div>
  );
}

function Sources({ api, sheet }: { api: ApiClient; sheet: SheetDetail }) {
  const { t, i18n } = useTranslation();
  const [open, setOpen] = useState<number | null>(null);
  // the comparison with the source it belongs to: a slow answer for another source is not shown
  const [cmp, setCmp] = useState<{ source: number; data: SheetComparison } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (open === null) return;
    api.sheetCompare(sheet.id, open).then(
      (data) => setCmp({ source: open, data }),
      (e) => setError(describeError(toActionError(e), t, i18n)),
    );
  }, [api, sheet.id, open, t, i18n]);
  if (sheet.source_list.length === 0) return <p className="muted">{t("sheets.noSources")}</p>;
  return (
    <div className="sheet-sources">
      <ul>
        {sheet.source_list.map((s, i) => (
          <li key={i} className={open === i ? "is-open" : undefined}>
            <button type="button" className="link-button" onClick={() => setOpen(open === i ? null : i)}>
              {t("sheets.compare")}
            </button>{" "}
            <span>[{i}]</span>{" "}
            {s.url ? (
              <a href={s.url} target="_blank" rel="noreferrer noopener">
                {s.title || s.url}
              </a>
            ) : (
              s.title
            )}
            {s.version && <span className="muted small"> · {s.version}</span>}
          </li>
        ))}
      </ul>
      {error && <p className="error">{error}</p>}
      {open !== null && cmp?.source === open && <Comparison cmp={cmp.data} />}
    </div>
  );
}

function Comparison({ cmp }: { cmp: SheetComparison }) {
  const { t } = useTranslation();
  const groups = useMemo(() => {
    const out = new Map<string, SheetComparison["differences"]>();
    for (const d of cmp.differences) out.set(d.kind, [...(out.get(d.kind) ?? []), d]);
    return [...out.entries()];
  }, [cmp]);
  return (
    <div className="sheet-compare" data-testid="sheet-compare">
      <p className="small">
        {t("sheets.similarity", {
          chords: Math.round(cmp.chords_similarity * 100),
          lyrics: Math.round(cmp.lyrics_similarity * 100),
        })}
        {cmp.shift !== 0 && <> · {t("sheets.shifted", { n: cmp.shift > 0 ? `+${cmp.shift}` : cmp.shift })}</>}
      </p>
      {groups.length === 0 && <p>{t("sheets.noDifference")}</p>}
      {groups.map(([kind, diffs]) => (
        <section key={kind} className={`sheet-diff sheet-diff-${kind}`}>
          <h4>
            {t(`sheets.diff.${kind}`, { defaultValue: kind })} ({diffs.length})
          </h4>
          <table>
            <tbody>
              {diffs.map((d, i) => (
                <tr key={i}>
                  <td className="muted small">{d.where}</td>
                  <td>
                    <code>{d.a || "–"}</code>
                  </td>
                  <td>→</td>
                  <td>
                    <code>{d.b || "–"}</code>
                  </td>
                  <td className="muted small">{d.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}
    </div>
  );
}

/** The stage mode: the sheet alone, full screen, fitted on one screen in columns. ←/→ and
 *  PageUp/PageDown (a foot pedal sends these) page through the songs of the list as it is
 *  filtered and sorted, or through a setlist (`setlistName`; `at` is where `id` is in it, since a
 *  setlist may hold one song twice); Esc leaves. `from` is the address open before the stage
 *  (default `id`): leaving opens the song paged to when it differs. */
function Stage({
  api,
  id: startId,
  from,
  groups,
  at: startAt,
  setlistName,
  onExit,
}: {
  api: ApiClient;
  id: string;
  from?: string;
  groups: SheetGroup[];
  at?: number;
  setlistName?: string;
  onExit: () => void;
}) {
  const { t, i18n } = useTranslation();
  // paging stays inside the stage: a new address would be taken over by the desktop shell
  const [id, setId] = useState(startId);
  const [at, setAt] = useState(startAt);
  const opened = from ?? startId;
  const [sheet, setSheet] = useState<SheetDetail | null>(null);
  const [error, setError] = useState("");
  // default on: a repeat written out once saves lines when a song has to fit one screen
  const [collapse, setCollapseState] = useState(() => remembered(COLLAPSE_KEY) !== "0");
  const [fit, setFit] = useState<Fit | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const setCollapse = (on: boolean) => {
    setCollapseState(on);
    remember(COLLAPSE_KEY, on ? "1" : "0");
  };

  useEffect(() => {
    api.sheet(id, {}).then(
      (s) => {
        setError("");
        setSheet(s);
      },
      (e) => setError(describeError(toActionError(e), t, i18n)),
    );
  }, [api, id, t, i18n]);

  const here = positionOf(groups, id, at);
  const prev = neighbour(groups, id, -1, at);
  const next = neighbour(groups, id, 1, at);
  const go = (to: string | null, step: number) => {
    if (!to) return;
    setId(to);
    setAt(here + step);
  };
  const leave = useCallback(() => {
    onExit();
    if (id !== opened) window.location.hash = sheetsHref(id);
  }, [onExit, id, opened]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") leave();
      else if (e.key === "ArrowRight" || e.key === "PageDown") go(next, 1);
      else if (e.key === "ArrowLeft" || e.key === "PageUp") go(prev, -1);
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // full screen where the browser allows it. Leaving full screen does not end the stage (some
  // browsers drop full screen on their own); Esc and the button do.
  useEffect(() => {
    if (!document.fullscreenElement) document.documentElement.requestFullscreen?.().catch(() => undefined);
  }, []);

  const items = useMemo(() => {
    const sections = sheet ? sheet.view.sections : [];
    return collapse ? collapseRepeats(sections) : asWritten(sections);
  }, [sheet, collapse]);

  // fit: columns and the largest font at which every part has its place. The parts are measured in
  // a hidden copy one column wide and placed into the columns here (packColumns): browsers differ in
  // how CSS columns overflow (some wrap them below), so the layout does not rely on that.
  const [cols, setCols] = useState<number[][]>([]);
  useLayoutEffect(() => {
    const b = box.current;
    const m = body.current;
    if (!b || !m || items.length === 0) return;
    const heights = (columns: number, font: number): number[] => {
      const gap = 2 * font;
      m.style.width = `${(b.clientWidth - gap * (columns - 1)) / columns}px`;
      m.style.fontSize = `${font}px`;
      const parts = [...(m.firstElementChild?.children ?? [])] as HTMLElement[];
      return parts.map((p) => p.getBoundingClientRect().height + (parseFloat(getComputedStyle(p).marginBottom) || 0));
    };
    const measure = () => {
      const height = b.clientHeight;
      const f = chooseFit(maxColumns(b.clientWidth), (columns, font) => packColumns(heights(columns, font), columns, height) !== null);
      const hs = heights(f.columns, f.font);
      setCols((f.fits && packColumns(hs, f.columns, height)) || balanceColumns(hs, f.columns));
      setFit((old) => (old && old.columns === f.columns && old.font === f.font && old.fits === f.fits ? old : f));
    };
    measure();
    // turning the screen comes in steps (full screen, rotation, scrollbar): measure once the layout
    // has settled, in the next frame and again a moment later; resize events cover what the
    // observer of the box misses on some devices
    let frame = 0;
    let later = 0;
    const again = () => {
      cancelAnimationFrame(frame);
      window.clearTimeout(later);
      frame = requestAnimationFrame(measure);
      later = window.setTimeout(measure, 400);
    };
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(again);
    ro?.observe(b);
    window.addEventListener("resize", again);
    window.addEventListener("orientationchange", again);
    screen.orientation?.addEventListener?.("change", again);
    return () => {
      ro?.disconnect();
      window.removeEventListener("resize", again);
      window.removeEventListener("orientationchange", again);
      screen.orientation?.removeEventListener?.("change", again);
      cancelAnimationFrame(frame);
      window.clearTimeout(later);
    };
  }, [items]);

  const v = sheet?.view;
  return (
    <>
      <div className="sheet-stage" data-testid="sheet-stage">
      <header className="sheet-stage-head">
        {/* one line: a long title or artist is cut short, the buttons keep their place */}
        <div className="sheet-stage-info">
        {setlistName && here >= 0 && (
          <span className="sheet-stage-set" data-testid="sheet-stage-set">
            {t("sheets.setlists.position", { name: setlistName, n: here + 1, total: groups.length })}
          </span>
        )}
        {v ? (
          <>
            <strong>{v.title}</strong>
            {v.artist && <span className="muted">{v.artist}</span>}
            {v.shown_key && (
              <span>
                {t("sheets.key")}: <strong>{v.shown_key}</strong>
              </span>
            )}
            {v.capo > 0 && v.capo_view && <span>{t("sheets.capoN", { n: v.capo })}</span>}
            {fit && !fit.fits && <span className="sheet-stage-hint">{t("sheets.stage.scroll")}</span>}
          </>
        ) : (
          <span className="muted">{error || t("sheets.loading")}</span>
        )}
        </div>
        <div className="sheet-stage-tools">
          <button type="button" className="sheet-stage-arrow" onClick={() => go(prev, -1)} disabled={!prev} aria-label={t("sheets.stage.prev")} title={t("sheets.stage.prev")}>
            ←
          </button>
          <button type="button" className="sheet-stage-arrow" onClick={() => go(next, 1)} disabled={!next} aria-label={t("sheets.stage.next")} title={t("sheets.stage.next")}>
            →
          </button>
          <button type="button" aria-pressed={collapse} onClick={() => setCollapse(!collapse)}>
            {t("sheets.collapse")}
          </button>
          <button type="button" onClick={leave}>
            {t("sheets.stage.exit")}
          </button>
        </div>
      </header>
      <div ref={box} className={`sheet-stage-box${fit && !fit.fits ? " is-scroll" : ""}`}>
        {v && fit && (
          <div
            className="sheet-stage-cols"
            style={{ gridTemplateColumns: `repeat(${fit.columns}, minmax(0, 1fr))`, fontSize: `${fit.font}px`, columnGap: `${2 * fit.font}px` }}
          >
            {cols.map((idx, k) => (
              <div key={k} className="sheet-stage-body sheet-stage-col">
                <Body items={idx.map((i) => items[i]!).filter(Boolean)} />
              </div>
            ))}
          </div>
        )}
      </div>
      </div>
      {/* the measuring copy: out of sight, one column wide, at the font being tried */}
      <div ref={body} className="sheet-stage-body sheet-stage-measure" aria-hidden="true">
        {v && <Body items={items} anchors={false} />}
      </div>
    </>
  );
}
