import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type { ModelSwitches } from "../types";

const DAYS = [1, 7, 30];

/** When the machines loaded or changed a model, and which work caused the loads (plan P2a).
 *  Only figures, from the record of requests; nothing is changed by them. */
export function ModelSwitchesPanel({ api }: { api: ApiClient }) {
  const { t, i18n } = useTranslation();
  const [days, setDays] = useState(7);
  const [view, setView] = useState<ModelSwitches | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"alt" | "neu">("alt");

  const changeMode = (next: "alt" | "neu") => {
    api.setChoiceMode(next).then(
      () => {
        setMode(next);
        setError(null);
      },
      (err: unknown) => setError(describeError(toActionError(err), t, i18n)),
    );
  };

  useEffect(() => {
    let live = true;
    api.getModelSwitches(days).then(
      (v) => {
        if (live) {
          setView(v);
          setMode(v.choice_mode);
          setError(null);
        }
      },
      (err: unknown) => live && setError(describeError(toActionError(err), t, i18n)),
    );
    return () => {
      live = false;
    };
  }, [api, days, t, i18n]);

  const number = (value: number, digits = 1) =>
    value.toLocaleString(i18n.language, { maximumFractionDigits: digits });

  const reviewShare = view?.review_load_share;
  const share = reviewShare == null ? "–" : `${Math.round(reviewShare * 100)} %`;

  return (
    <section className="card" data-testid="model-switches">
      <h2>{t("modelSwitches.title")}</h2>
      <p className="muted small">{t("modelSwitches.hint")}</p>
      <label>
        {t("measurements.period")}{" "}
        <select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          aria-label={t("measurements.period")}
        >
          {DAYS.map((d) => (
            <option key={d} value={d}>
              {t("measurements.days", { count: d })}
            </option>
          ))}
        </select>
      </label>
      <label>
        {t("modelSwitches.choice")}{" "}
        <select
          value={mode}
          onChange={(e) => changeMode(e.target.value as "alt" | "neu")}
          aria-label={t("modelSwitches.choice")}
        >
          <option value="alt">{t("modelSwitches.choiceAlt")}</option>
          <option value="neu">{t("modelSwitches.choiceNeu")}</option>
        </select>
      </label>
      {error && (
        <p className="bad" role="alert">
          {error}
        </p>
      )}
      {view && view.totals.requests === 0 && (
        <p className="muted small">{t("modelSwitches.none")}</p>
      )}
      {view && view.totals.requests > 0 && (
        <>
          <p className="small" data-testid="model-switches-totals">
            {t("modelSwitches.totals", {
              loads: number(view.totals.loads, 0),
              switches: number(view.totals.switches, 0),
              review: number(view.totals.review_loads, 0),
              share,
            })}
          </p>
          {view.totals.loads === 0 && <p className="muted small">{t("modelSwitches.noLoads")}</p>}
          {view.cells.length > 0 && (
            <table className="plain" data-testid="model-switches-table">
              <thead>
                <tr>
                  <th>{t("modelSwitches.date")}</th>
                  <th>{t("modelSwitches.node")}</th>
                  <th>{t("modelSwitches.requests")}</th>
                  <th>{t("modelSwitches.loads")}</th>
                  <th>{t("modelSwitches.switches")}</th>
                  <th>{t("modelSwitches.reviewLoads")}</th>
                </tr>
              </thead>
              <tbody>
                {view.cells.map((c) => (
                  <tr key={`${c.date}|${c.node}`}>
                    <td>{c.date}</td>
                    <td>{c.node}</td>
                    <td>{c.requests}</td>
                    <td>{c.loads}</td>
                    <td>{c.switches}</td>
                    <td>{c.review_loads}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </section>
  );
}
