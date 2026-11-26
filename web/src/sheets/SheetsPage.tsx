import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { sheetsHref } from "../route";
import type {
  Instrument,
  SheetComparison,
  SheetDetail,
  SheetLine,
  SheetResearchJob,
  SheetSection,
  SheetSummary,
} from "./types";
import { chooseFit, isTabLine, maxColumns, neighbour, SORT_MODES, visibleGroups, type Fit, type SortMode } from "./fit";
import { artistsOf, type SheetGroup } from "./group";
import { PrintDialog } from "./PrintSheets";
import "./sheets.css";

export { artistsOf, groupSheets, type SheetGroup } from "./group";

interface Props {
  api: ApiClient;
  /** The id of the open sheet. */
  id?: string;
}

const POLL_MS = 3000;
const SORT_KEY = "topcoat.sheets.sort";
const TABS_KEY = "topcoat.sheets.hideTabs";
const STAGE_KEY = "topcoat.sheets.stage";

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
 *  except "capo shapes on/off", which the sheet keeps. */
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
  const [sort, setSortState] = useState<SortMode>(() => {
    const r = remembered(SORT_KEY) as SortMode | null;
    return r && SORT_MODES.includes(r) ? r : "title";
  });
  const setSort = (m: SortMode) => {
    setSortState(m);
    remember(SORT_KEY, m);
  };
  // the stage mode survives paging: a new address may mount this page afresh
  const [stage, setStageState] = useState(() => {
    try {
      return window.sessionStorage.getItem(STAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const setStage = useCallback((on: boolean) => {
    setStageState(on);
    try {
      if (on) window.sessionStorage.setItem(STAGE_KEY, "1");
      else window.sessionStorage.removeItem(STAGE_KEY);
    } catch {
      /* the stage mode then ends with this page */
    }
  }, []);
  const [printing, setPrinting] = useState(false);
  const exitStage = useCallback(() => {
    setStage(false);
    if (document.fullscreenElement) document.exitFullscreen?.().catch(() => undefined);
  }, [setStage]);
  const groups = useMemo(() => visibleGroups(list ?? [], query, artist, sort), [list, query, artist, sort]);

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

  if (stage && id) return <Stage api={api} id={id} groups={groups} onExit={exitStage} />;
  return (
    <section className="sheets-shell" data-testid="sheets">
      <div className="sheets">
        <nav className="sheets-nav" aria-label={t("sheets.list")}>
          <h1 className="sheets-title">{t("sheets.title")}</h1>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <ResearchForm api={api} onStarted={reload} onError={fail} />
          {/* a finished research shows as its sheet in the list; only running and failed ones here */}
          {jobs.some((j) => j.status !== "done") && <Jobs jobs={jobs.filter((j) => j.status !== "done")} />}
          <PasteForm api={api} onError={fail} />
          {list !== null && list.length === 0 && <p className="muted small">{t("sheets.empty")}</p>}
          {list && list.length > 0 && <SheetList
              list={list}
              groups={groups}
              openId={id}
              filter={{ query, setQuery, artist, setArtist, sort, setSort }}
            />}
          {groups.length > 0 && (
            <button type="button" className="link-button small" onClick={() => setPrinting(true)} data-testid="sheets-print-list">
              {t("sheets.print.list")}
            </button>
          )}
          {printing && (
            <PrintDialog
              api={api}
              songs={groups.flatMap((g) => {
                // the newest version of each song, as the stage mode pages through them
                const s = g.versions[g.versions.length - 1];
                return s ? [{ id: s.id, title: g.title, artist: g.artist }] : [];
              })}
              hideTabs={remembered(TABS_KEY) !== "0"}
              onClose={() => setPrinting(false)}
            />
          )}
        </nav>
        {id ? (
          <OpenSheet key={id} api={api} id={id} onChanged={reload} onStage={() => setStage(true)} />
        ) : (
          <p className="sheets-pick muted">{t("sheets.pick")}</p>
        )}
      </div>
    </section>
  );
}

interface ListFilter {
  query: string;
  setQuery: (q: string) => void;
  artist: string;
  setArtist: (a: string) => void;
  sort: SortMode;
  setSort: (m: SortMode) => void;
}

function SheetList({ list, groups, openId, filter }: { list: SheetSummary[]; groups: SheetGroup[]; openId?: string; filter: ListFilter }) {
  const { t } = useTranslation();
  const { query, setQuery, artist, setArtist, sort, setSort } = filter;
  const artists = useMemo(() => artistsOf(list), [list]);
  const facts = (s: SheetSummary) =>
    [s.key, s.capo ? t("sheets.capoN", { n: s.capo }) : "", t(`sheets.instrument.${s.instrument}`)].filter(Boolean).join(" · ");
  return (
    <>
      <div className="sheets-filter">
        <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("sheets.filter.search")} aria-label={t("sheets.filter.search")} />
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
        <select value={sort} onChange={(e) => setSort(e.target.value as SortMode)} aria-label={t("sheets.sort.label")}>
          {SORT_MODES.map((m) => (
            <option key={m} value={m}>
              {t(`sheets.sort.${m}`)}
            </option>
          ))}
        </select>
      </div>
      {groups.length === 0 && <p className="muted small">{t("sheets.filter.none")}</p>}
      <ul className="sheets-list" data-testid="sheets-list">
        {groups.map((g) => {
          const only = g.versions.length === 1 ? g.versions[0] : undefined;
          return only ? (
            <li key={g.key}>
              <a href={sheetsHref(only.id)} aria-current={only.id === openId ? "page" : undefined}>
                <span className="sheets-list-title">{g.title}</span>
                <span className="muted small">{[g.artist, facts(only)].filter(Boolean).join(" · ")}</span>
              </a>
            </li>
          ) : (
            <li key={g.key} className="sheets-group">
              <div className="sheets-group-head">
                <span className="sheets-list-title">{g.title}</span>
                {g.artist && <span className="muted small">{g.artist}</span>}
              </div>
              <ul className="sheets-versions">
                {g.versions.map((s, i) => (
                  <li key={s.id}>
                    <a href={sheetsHref(s.id)} aria-current={s.id === openId ? "page" : undefined}>
                      <span className="sheets-version">{t("sheets.version", { n: i + 1 })}</span>
                      <span className="muted small">{[facts(s), s.version_note].filter(Boolean).join(" · ")}</span>
                    </a>
                  </li>
                ))}
              </ul>
            </li>
          );
        })}
      </ul>
    </>
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

function PasteForm({ api, onError }: { api: ApiClient; onError: (e: unknown) => void }) {
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
          hideTabs={remembered(TABS_KEY) !== "0"}
          onClose={() => setPrinting(false)}
        />
      )}
      {error && <p className="error">{error}</p>}
      {tab === "sheet" && <Body sections={v.sections} />}
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

export function Body({ sections }: { sections: SheetSection[] }) {
  const { t } = useTranslation();
  return (
    <div className="sheet-body">
      {sections.map((s) => (
        <section key={s.index} id={`sheet-part-${s.index}`} className={`sheet-part ${kindClass(s.kind)}`}>
          <h3 className="sheet-part-label">
            {s.label || t(`sheets.kind.${KINDS.includes(s.kind) ? s.kind : "other"}`)}
            {s.repeat_of !== null && <span className="muted small"> · {t("sheets.repeat")}</span>}
          </h3>
          {s.lines.map((ln, i) => (
            <Line key={i} line={ln} />
          ))}
        </section>
      ))}
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

/** The text of a line as it reads, chords included (to spot tab lines). */
function lineText(line: SheetLine): string {
  return line.comment ?? (line.segments ?? []).map((g) => (g.chord ?? "") + g.text).join("");
}

/** The sections without tab parts and tab lines; a part left empty goes too. */
export function withoutTabs(sections: SheetSection[]): SheetSection[] {
  return sections
    .filter((s) => s.kind !== "tab")
    .map((s) => ({ ...s, lines: s.lines.filter((ln) => !isTabLine(lineText(ln))) }))
    .filter((s) => s.lines.length > 0 || s.repeat_of !== null);
}

/** The stage mode: the sheet alone, full screen, fitted on one screen in columns. ←/→ and
 *  PageUp/PageDown (a foot pedal sends these) page through the songs of the list as it is
 *  filtered and sorted; Esc leaves. */
function Stage({ api, id: startId, groups, onExit }: { api: ApiClient; id: string; groups: SheetGroup[]; onExit: () => void }) {
  const { t, i18n } = useTranslation();
  // paging stays inside the stage: a new address would be taken over by the desktop shell
  const [id, setId] = useState(startId);
  const [sheet, setSheet] = useState<SheetDetail | null>(null);
  const [error, setError] = useState("");
  const [hideTabs, setHideTabsState] = useState(() => remembered(TABS_KEY) !== "0");
  const [fit, setFit] = useState<Fit | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const setHideTabs = (on: boolean) => {
    setHideTabsState(on);
    remember(TABS_KEY, on ? "1" : "0");
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

  const prev = neighbour(groups, id, -1);
  const next = neighbour(groups, id, 1);
  const go = (to: string | null) => {
    if (to) setId(to);
  };
  const leave = useCallback(() => {
    onExit();
    if (id !== startId) window.location.hash = sheetsHref(id);
  }, [onExit, id, startId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") leave();
      else if (e.key === "ArrowRight" || e.key === "PageDown") go(next);
      else if (e.key === "ArrowLeft" || e.key === "PageUp") go(prev);
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

  const sections = useMemo(
    () => (sheet ? (hideTabs ? withoutTabs(sheet.view.sections) : sheet.view.sections) : []),
    [sheet, hideTabs],
  );

  // fit: columns and the largest font at which nothing spills into an extra column
  useLayoutEffect(() => {
    const b = box.current;
    const c = body.current;
    if (!b || !c || sections.length === 0) return;
    const measure = () => {
      const height = b.clientHeight;
      c.style.height = `${height}px`;
      const f = chooseFit(maxColumns(b.clientWidth), (columns, font) => {
        c.style.columnCount = String(columns);
        c.style.fontSize = `${font}px`;
        return c.scrollWidth <= c.clientWidth + 1;
      });
      c.style.columnCount = String(f.columns);
      c.style.fontSize = `${f.font}px`;
      c.style.height = f.fits ? `${height}px` : "auto";
      setFit((old) => (old && old.columns === f.columns && old.font === f.font && old.fits === f.fits ? old : f));
    };
    measure();
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    ro?.observe(b);
    return () => ro?.disconnect();
  }, [sections]);

  const v = sheet?.view;
  return (
    <div className="sheet-stage" data-testid="sheet-stage">
      <header className="sheet-stage-head">
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
        <div className="sheet-stage-tools">
          <button type="button" onClick={() => go(prev)} disabled={!prev} aria-label={t("sheets.stage.prev")} title={t("sheets.stage.prev")}>
            ←
          </button>
          <button type="button" onClick={() => go(next)} disabled={!next} aria-label={t("sheets.stage.next")} title={t("sheets.stage.next")}>
            →
          </button>
          <button type="button" aria-pressed={hideTabs} onClick={() => setHideTabs(!hideTabs)}>
            {t("sheets.stage.hideTabs")}
          </button>
          <button type="button" onClick={leave}>
            {t("sheets.stage.exit")}
          </button>
        </div>
      </header>
      <div ref={box} className={`sheet-stage-box${fit && !fit.fits ? " is-scroll" : ""}`}>
        <div ref={body} className="sheet-stage-body">
          {v && <Body sections={sections} />}
        </div>
      </div>
    </div>
  );
}
