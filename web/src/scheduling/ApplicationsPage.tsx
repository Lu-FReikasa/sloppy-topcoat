import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type { Measurements, QualityCounters } from "../types";

/** The applications that have a tile, in the order of the menu; the title key of each. */
const APPLICATIONS: [string, string][] = [
  ["chat", "header.chat"],
  ["research", "workspace.research"],
  ["dev", "workspace.dev"],
  ["admin", "workspace.admin"],
  ["project", "workspace.project"],
];

export interface Tile {
  application: string;
  titleKey: string;
  requests: number;
  stopped: number;
  cutOff: number;
  waitedSeconds: number | null;
  nodes: number;
  topModel: string | null;
  greenShare: number | null;
}

/** One tile per application from the figures of the last days (no new endpoint): requests,
 *  failures, waiting, the machines and the most used model, and the share of green second looks. */
export function tilesOf(measure: Measurements, quality: QualityCounters): Tile[] {
  const known = new Set(APPLICATIONS.map(([a]) => a));
  const extra = [...new Set(measure.rows.map((r) => r.application))].filter(
    (a) => a && !known.has(a),
  );
  const all: [string, string][] = [
    ...APPLICATIONS,
    ...extra.map((a): [string, string] => [a, ""]),
  ];
  return all.map(([application, titleKey]) => {
    const rows = measure.rows.filter((r) => r.application === application);
    const requests = rows.reduce((s, r) => s + r.requests, 0);
    const waitedWeighted = rows.reduce((s, r) => s + r.waited_seconds * r.requests, 0);
    const perModel = new Map<string, number>();
    for (const r of rows) perModel.set(r.model, (perModel.get(r.model) ?? 0) + r.requests);
    let topModel: string | null = null;
    let most = 0;
    for (const [model, n] of perModel) {
      if (n > most) {
        most = n;
        topModel = model;
      }
    }
    const looks = quality.rows.filter((r) => r.application === application);
    const looked = looks.reduce((s, r) => s + r.looks, 0);
    const ok = looks.reduce((s, r) => s + r.ok, 0);
    return {
      application,
      titleKey,
      requests,
      stopped: rows.reduce((s, r) => s + r.failed + r.cancelled, 0),
      cutOff: rows.reduce((s, r) => s + r.cut_off, 0),
      waitedSeconds: requests ? waitedWeighted / requests : null,
      nodes: new Set(rows.map((r) => r.node)).size,
      topModel,
      greenShare: looked ? ok / looked : null,
    };
  });
}

/** The rows of the measurements that belong to one application (the detail tab shows them). */
export function rowsOf(measure: Measurements, application: string) {
  return measure.rows.filter((r) => r.application === application);
}

type Tab = "overview" | "measurements";

/** Detail page of one application: tabs for the figures and the measurements (plan P1). The
 *  other tabs of the plan (flow, models as exceptions, tools and rights, memory) follow later. */
function ApplicationDetail({
  tile,
  measure,
  onBack,
}: {
  tile: Tile;
  measure: Measurements;
  onBack: () => void;
}) {
  const { t, i18n } = useTranslation();
  const [tab, setTab] = useState<Tab>("overview");
  const number = (value: number, digits = 1) =>
    value.toLocaleString(i18n.language, { maximumFractionDigits: digits });
  const rows = rowsOf(measure, tile.application);

  return (
    <section data-testid="application-detail">
      <button type="button" onClick={onBack}>
        {t("applications.back")}
      </button>
      <h3>{tile.titleKey ? t(tile.titleKey) : tile.application}</h3>
      <div role="tablist" aria-label={t("applications.tabs")}>
        {(["overview", "measurements"] as Tab[]).map((key) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            onClick={() => setTab(key)}
          >
            {t(`applications.tab.${key}`)}
          </button>
        ))}
      </div>
      {tab === "overview" && (
        <dl className="small">
          <dt>{t("applications.requests")}</dt>
          <dd>{tile.requests}</dd>
          <dt>{t("applications.stopped")}</dt>
          <dd>{tile.stopped}</dd>
          <dt>{t("applications.cutOff")}</dt>
          <dd>{tile.cutOff}</dd>
          <dt>{t("applications.waited")}</dt>
          <dd>{tile.waitedSeconds === null ? "–" : number(tile.waitedSeconds)}</dd>
          <dt>{t("applications.nodes")}</dt>
          <dd>{tile.nodes}</dd>
          <dt>{t("applications.topModel")}</dt>
          <dd>{tile.topModel ?? "–"}</dd>
          <dt>{t("applications.green")}</dt>
          <dd>
            {tile.greenShare === null ? "–" : `${Math.round(tile.greenShare * 100)} %`}
          </dd>
        </dl>
      )}
      {tab === "measurements" &&
        (rows.length === 0 ? (
          <p className="muted small">{t("applications.noMeasurements")}</p>
        ) : (
          <table className="plain" data-testid="application-measurements">
            <thead>
              <tr>
                <th>{t("measurements.model")}</th>
                <th>{t("applications.nodes")}</th>
                <th>{t("measurements.requests")}</th>
                <th>{t("measurements.failed")}</th>
                <th>{t("measurements.cutOff")}</th>
                <th>{t("measurements.generated")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.model}|${r.node}`}>
                  <td>{r.model}</td>
                  <td>{r.node}</td>
                  <td>{r.requests}</td>
                  <td>{r.failed + r.cancelled}</td>
                  <td>{r.cut_off}</td>
                  <td>{r.generated_tps === null ? "–" : number(r.generated_tps)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
    </section>
  );
}

/** Administration: one tile per application, from the last seven days (plan P1). A tile opens
 *  the detail page with its tabs. The figures are the same as on the scheduling page. */
export function ApplicationsPage({ api }: { api: ApiClient }) {
  const { t, i18n } = useTranslation();
  const [tiles, setTiles] = useState<Tile[] | null>(null);
  const [measure, setMeasure] = useState<Measurements | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    Promise.all([api.getMeasurements(7), api.getQuality(7)]).then(
      ([m, quality]) => {
        if (live) {
          setMeasure(m);
          setTiles(tilesOf(m, quality));
          setError(null);
        }
      },
      (err: unknown) => live && setError(describeError(toActionError(err), t, i18n)),
    );
    return () => {
      live = false;
    };
  }, [api, t, i18n]);

  const number = (value: number, digits = 1) =>
    value.toLocaleString(i18n.language, { maximumFractionDigits: digits });

  const current = tiles?.find((x) => x.application === open);
  if (current && measure) {
    return (
      <ApplicationDetail tile={current} measure={measure} onBack={() => setOpen(null)} />
    );
  }

  return (
    <section data-testid="applications">
      <h2>{t("applications.title")}</h2>
      <p className="muted small">{t("applications.hint")}</p>
      {error && (
        <p className="bad" role="alert">
          {error}
        </p>
      )}
      {tiles && (
        <div
          style={{
            display: "grid",
            gap: "12px",
            gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
          }}
        >
          {tiles.map((tile) => (
            <article key={tile.application} className="card" data-testid="application-tile">
              <h3>
                <button type="button" onClick={() => setOpen(tile.application)}>
                  {tile.titleKey ? t(tile.titleKey) : tile.application}
                </button>
              </h3>
              <p className="small">
                {t("applications.requests")}: <strong>{tile.requests}</strong>
                {" · "}
                {t("applications.stopped")}: {tile.stopped}
                {" · "}
                {t("applications.cutOff")}: {tile.cutOff}
              </p>
              <p className="small">
                {t("applications.waited")}:{" "}
                {tile.waitedSeconds === null ? "–" : number(tile.waitedSeconds)}
                {" · "}
                {t("applications.nodes")}: {tile.nodes}
              </p>
              <p className="small">
                {t("applications.topModel")}: {tile.topModel ?? "–"}
              </p>
              <p className="small">
                {t("applications.green")}:{" "}
                {tile.greenShare === null ? "–" : `${Math.round(tile.greenShare * 100)} %`}
              </p>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
