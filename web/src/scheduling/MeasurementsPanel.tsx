import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type { Measurements } from "../types";

const DAYS = [1, 7, 30];

/** What the models did in everyday use (SCH-06): speeds, failures and waiting per application,
 *  model and node, from the hub's record of requests. Only figures. */
export function MeasurementsPanel({ api }: { api: ApiClient }) {
  const { t, i18n } = useTranslation();
  const [days, setDays] = useState(7);
  const [view, setView] = useState<Measurements | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.getMeasurements(days).then(
      (v) => {
        if (live) {
          setView(v);
          setError(null);
        }
      },
      (err: unknown) => live && setError(describeError(toActionError(err), t, i18n)),
    );
    return () => {
      live = false;
    };
  }, [api, days, t, i18n]);

  const number = (value: number | null, digits = 1) =>
    value === null ? "–" : value.toLocaleString(i18n.language, { maximumFractionDigits: digits });

  return (
    <section className="card" data-testid="measurements">
      <h2>{t("measurements.title")}</h2>
      <p className="muted small">{t("measurements.hint")}</p>
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
      {error && (
        <p className="bad" role="alert">
          {error}
        </p>
      )}
      {view && view.rows.length === 0 && <p className="muted small">{t("measurements.none")}</p>}
      {view && view.rows.length > 0 && (
        <table className="plain" data-testid="measurements-table">
          <thead>
            <tr>
              <th>{t("measurements.application")}</th>
              <th>{t("measurements.model")}</th>
              <th>{t("measurements.requests")}</th>
              <th>{t("measurements.failed")}</th>
              <th>{t("measurements.cutOff")}</th>
              <th>{t("measurements.waited")}</th>
              <th>{t("measurements.generated")}</th>
              <th>{t("measurements.prompt")}</th>
              <th>{t("measurements.cached")}</th>
            </tr>
          </thead>
          <tbody>
            {view.rows.map((r) => (
              <tr key={`${r.application}|${r.model}|${r.node}`}>
                <td>{r.application || "–"}</td>
                <td>
                  {r.model} <span className="muted">@{r.node}</span>
                </td>
                <td>{r.requests}</td>
                <td>{r.failed + r.cancelled}</td>
                <td>{r.cut_off}</td>
                <td>{number(r.waited_seconds)}</td>
                <td>{number(r.generated_tps)}</td>
                <td>{number(r.prompt_tps)}</td>
                <td>{r.cached_share === null ? "–" : `${Math.round(r.cached_share * 100)} %`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
