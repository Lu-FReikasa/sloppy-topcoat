import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type { QualityCounters } from "../types";

/** How the second look judged the answers of each model in each application (QLT-01). Counts only.
 *  A floor per application marks the models below it; nothing is switched because of it. */
export function QualityPanel({ api }: { api: ApiClient }) {
  const { t, i18n } = useTranslation();
  const [view, setView] = useState<QualityCounters | null>(null);
  const [error, setError] = useState<string | null>(null);
  // application -> text of the floor field (percent), only for what the admin touched
  const [draft, setDraft] = useState<Record<string, string>>({});

  const load = useCallback(() => {
    api.getQuality(30).then(
      (v) => {
        setView(v);
        setError(null);
      },
      (err: unknown) => setError(describeError(toActionError(err), t, i18n)),
    );
  }, [api, t, i18n]);
  useEffect(load, [load]);

  async function saveFloor(application: string) {
    const text = (draft[application] ?? "").trim();
    const percent = Number(text.replace(",", "."));
    const floor = text === "" ? null : percent / 100;
    if (floor !== null && (!Number.isFinite(floor) || floor < 0 || floor > 1)) {
      setError(t("secondLook.floorInvalid"));
      return;
    }
    try {
      await api.putQualityFloor(application, floor);
      setDraft({ ...draft, [application]: "" });
      load();
    } catch (err) {
      setError(describeError(toActionError(err), t, i18n));
    }
  }

  if (!view) {
    return error ? (
      <p className="bad" role="alert">
        {error}
      </p>
    ) : null;
  }
  const applications = [...new Set(view.rows.map((r) => r.application))];
  return (
    <section className="card" data-testid="quality-counters">
      <h2>{t("secondLook.title")}</h2>
      <p className="muted small">{t("secondLook.hint", { days: view.days })}</p>
      {error && (
        <p className="bad" role="alert">
          {error}
        </p>
      )}
      {view.rows.length === 0 ? (
        <p className="muted small">{t("secondLook.none")}</p>
      ) : (
        <table className="plain">
          <thead>
            <tr>
              <th>{t("measurements.application")}</th>
              <th>{t("measurements.model")}</th>
              <th>{t("secondLook.looks")}</th>
              <th>{t("secondLook.green")}</th>
              <th>{t("secondLook.orange")}</th>
              <th>{t("secondLook.red")}</th>
              <th>{t("secondLook.share")}</th>
            </tr>
          </thead>
          <tbody>
            {view.rows.map((r) => (
              <tr key={`${r.application}|${r.model}`} className={r.below_floor ? "bad" : undefined}>
                <td>{r.application}</td>
                <td>{r.model}</td>
                <td>{r.looks}</td>
                <td>{r.ok}</td>
                <td>{r.hint}</td>
                <td>{r.error}</td>
                <td>
                  {r.ok_share === null ? "–" : `${Math.round(r.ok_share * 100)} %`}
                  {r.below_floor && <strong> {t("secondLook.belowFloor")}</strong>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {applications.map((app) => (
        <p key={app}>
          <label>
            {t("secondLook.floorOf", { application: app })}{" "}
            <input
              inputMode="decimal"
              size={4}
              placeholder={
                view.floors[app] === undefined ? "–" : String(Math.round(view.floors[app] * 100))
              }
              value={draft[app] ?? ""}
              onChange={(e) => setDraft({ ...draft, [app]: e.target.value })}
            />{" "}
            %
          </label>{" "}
          <button type="button" onClick={() => void saveFloor(app)}>
            {t("secondLook.saveFloor")}
          </button>
        </p>
      ))}
      <p className="muted small">{t("secondLook.floorHint", { count: view.min_looks })}</p>
    </section>
  );
}
