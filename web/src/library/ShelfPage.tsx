import { useCallback, useEffect, useMemo, useState, type MouseEvent } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { APPLICATION_ORDER } from "../chat/applications";
import { dayGroup, type DayGroup } from "../chat/chatListModel";
import { docView } from "../desktop/docs";
import { programHue } from "../desktop/programs";
import { describeError, toActionError } from "../errors";
import { formatDateTime } from "../format";
import { requestOpen } from "../layout/intent";
import { libraryHref } from "../route";
import type { Artifact, Report } from "../types";
import { REPORT_PREFIX } from "./ReportsPage";
import { appCounts, matches, shelfItems, type ShelfItem } from "./shelfModel";

const GROUPS: DayGroup[] = ["today", "yesterday", "week", "older"];
/** How many entries the list shows before "more". */
const PAGE = 40;
/** Windows that are no application of their own (Start): their Ablage shows every application. */
const ALL_OF = new Set(["chat", "inbox", "settings"]);

interface Props {
  api: ApiClient;
  tick?: number;
  /** The application the window belongs to: its Ablage holds what that application made. */
  application?: string;
}

function hrefOf(item: ShelfItem): string {
  return libraryHref(item.kind === "report" ? `${REPORT_PREFIX}${item.id}` : item.id);
}

/** The Ablage (2026-11-13): the documents an application made — its research reports and the
 *  reports of its tasks — newest first and grouped by day. The conversations are in the archive
 *  (a view of their own). A document opens in a window of its own (desktop/docs.ts), where it can
 *  be taken into the wiki. Start shows the documents of every application, with a filter. */
export function ShelfPage({ api, tick = 0, application }: Props) {
  const { t, i18n } = useTranslation();
  const own = application && !ALL_OF.has(application) ? application : null;
  const [app, setApp] = useState("all");
  const [auto, setAuto] = useState(false);
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(PAGE);
  const [reports, setReports] = useState<Report[] | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[] | null>(null);
  const [error, setError] = useState("");

  const fail = useCallback(
    (e: unknown) => {
      if (!(e instanceof UnauthorizedError)) setError(describeError(toActionError(e), t, i18n));
    },
    [t, i18n],
  );

  useEffect(() => {
    api.reports().then((r) => setReports(r.reports), fail);
    api.artifacts().then((r) => setArtifacts(r.artifacts), fail);
  }, [api, tick, fail]);

  const loaded = reports !== null && artifacts !== null;
  const all = useMemo(
    () => shelfItems(reports ?? [], artifacts ?? [], []).filter((i) => !own || i.application === own),
    [reports, artifacts, own],
  );
  const filter = { app: own ? "all" : app, kind: "documents" as const, auto, query };
  const shown = all.filter((i) => matches(i, filter));
  const hidden = all.filter((i) => i.auto && matches(i, { ...filter, auto: true })).length;
  const apps = own ? [] : appCounts(all.filter((i) => auto || !i.auto), APPLICATION_ORDER);
  const page = shown.slice(0, limit);
  const now = new Date();
  const groups = GROUPS.map((group) => ({
    group,
    items: page.filter((i) => dayGroup(i.updated, now) === group),
  })).filter((g) => g.items.length > 0);

  function open(e: MouseEvent, item: ShelfItem) {
    if (item.kind === "chat" || e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    const view = docView(item.kind, item.id, item.title);
    requestOpen({ module: view.module, props: view.props });
  }

  return (
    <section className="shelf">
      <h1>{t("shelf.heading")}</h1>
      <p className="muted">
        {own
          ? t("shelf.introOwn", { app: t(`workspace.${own}`, { defaultValue: own }) })
          : t("shelf.intro")}
      </p>
      <div className="shelf-bar">
        <input
          type="search"
          aria-label={t("shelf.search")}
          placeholder={t("shelf.search")}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setLimit(PAGE);
          }}
        />
      </div>
      {apps.length > 1 && (
        <div className="archive-apps" role="group" aria-label={t("archive.apps")}>
          {[{ application: "all", count: 0 }, ...apps].map((a) => (
            <button
              key={a.application}
              type="button"
              aria-pressed={app === a.application}
              onClick={() => {
                setApp(a.application);
                setLimit(PAGE);
              }}
              style={{ ["--pg-hue" as string]: String(programHue(a.application)) }}
            >
              {a.application === "all"
                ? t("archive.all")
                : t(`workspace.${a.application}`, { defaultValue: a.application })}
              {a.application !== "all" && <span className="muted small"> {a.count}</span>}
            </button>
          ))}
        </div>
      )}
      {(hidden > 0 || auto) && (
        <label className="shelf-auto small">
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
          <span>{t("shelf.auto", { count: hidden })}</span>
        </label>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!loaded ? (
        <p className="muted">{t("app.loading")}</p>
      ) : shown.length === 0 ? (
        <p className="muted">{query ? t("shelf.noMatch") : t("shelf.empty")}</p>
      ) : (
        groups.map(({ group, items }) => (
          <div key={group}>
            <h2>{t(`chat.group.${group}`)}</h2>
            <ul className="shelf-list">
              {items.map((item) => (
                <li key={item.key} className={item.unread ? "shelf-unread" : undefined}>
                  {!own && (
                    <span
                      className="archive-app"
                      title={
                        item.application
                          ? t(`workspace.${item.application}`, { defaultValue: item.application })
                          : undefined
                      }
                      style={{ ["--pg-hue" as string]: String(programHue(item.application ?? "")) }}
                      aria-hidden="true"
                    />
                  )}
                  <span className="shelf-kind" aria-hidden="true" title={t(`shelf.kindOf.${item.kind}`)}>
                    {item.kind === "report" ? "📑" : "📄"}
                  </span>
                  <a className="shelf-title" href={hrefOf(item)} title={item.title} onClick={(e) => open(e, item)}>
                    {item.title || t("chat.untitled")}
                  </a>
                  <span className="shelf-meta muted small">
                    {item.unread && <span className="shelf-new">{t("shelf.new")}</span>}
                    {item.status && (
                      <>
                        {t(`reports.status.${item.status}`)}
                        {item.version && item.version > 1 ? ` · v${item.version}` : ""} ·{" "}
                      </>
                    )}
                    {formatDateTime(item.updated, i18n.language)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ))
      )}
      {shown.length > limit && (
        <button type="button" onClick={() => setLimit((n) => n + PAGE)}>
          {t("shelf.more", { count: shown.length - limit })}
        </button>
      )}
    </section>
  );
}
