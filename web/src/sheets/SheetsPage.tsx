import { useCallback, useEffect, useMemo, useState } from "react";
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
import "./sheets.css";

interface Props {
  api: ApiClient;
  /** The id of the open sheet. */
  id?: string;
}

const POLL_MS = 3000;
const KINDS = ["intro", "verse", "pre_chorus", "chorus", "bridge", "solo", "riff", "interlude", "outro", "tab", "other"];

/** A name as it compares: "The Beatles" and "the beatles" are one artist. */
function plain(text: string): string {
  return text.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

export interface SheetGroup {
  key: string;
  title: string;
  artist: string;
  /** The versions of the song, oldest first: v1, v2 … */
  versions: SheetSummary[];
}

/** The sheets grouped by song (title and artist), the songs by title. */
export function groupSheets(list: SheetSummary[]): SheetGroup[] {
  const groups = new Map<string, SheetGroup>();
  for (const s of list) {
    const key = `${plain(s.title)}|${plain(s.artist)}`;
    const g = groups.get(key) ?? { key, title: s.title, artist: s.artist, versions: [] };
    g.versions.push(s);
    groups.set(key, g);
  }
  const when = (s: SheetSummary) => s.created_at ?? s.updated_at ?? "";
  for (const g of groups.values()) g.versions.sort((a, b) => when(a).localeCompare(when(b)));
  return [...groups.values()].sort((a, b) => a.title.localeCompare(b.title));
}

/** The artists of the list, one spelling each, by name. */
export function artistsOf(list: SheetSummary[]): string[] {
  const seen = new Map<string, string>();
  for (const s of list) if (s.artist && !seen.has(plain(s.artist))) seen.set(plain(s.artist), s.artist);
  return [...seen.values()].sort((a, b) => a.localeCompare(b));
}

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
          {list && list.length > 0 && <SheetList list={list} openId={id} />}
        </nav>
        {id ? (
          <OpenSheet key={id} api={api} id={id} onChanged={reload} />
        ) : (
          <p className="sheets-pick muted">{t("sheets.pick")}</p>
        )}
      </div>
    </section>
  );
}

function SheetList({ list, openId }: { list: SheetSummary[]; openId?: string }) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [artist, setArtist] = useState("");
  const artists = useMemo(() => artistsOf(list), [list]);
  const groups = useMemo(() => {
    const q = plain(query);
    return groupSheets(list).filter(
      (g) =>
        (!artist || plain(g.artist) === plain(artist)) &&
        (!q || plain(`${g.title} ${g.artist}`).includes(q)),
    );
  }, [list, query, artist]);
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

function OpenSheet({ api, id, onChanged }: { api: ApiClient; id: string; onChanged: () => void }) {
  const { t, i18n } = useTranslation();
  const [sheet, setSheet] = useState<SheetDetail | null>(null);
  const [instrument, setInstrument] = useState<Instrument | null>(null);
  const [transpose, setTranspose] = useState(0);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<"sheet" | "sources" | "chordpro">("sheet");
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

function Body({ sections }: { sections: SheetSection[] }) {
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
