import { useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { ApiError, type ApiClient } from "../api/client";
import type { ProjectNotificationSettings } from "./types";

/** The IANA names the browser knows, for the time zone box. */
export function zoneNames(): string[] {
  const intl = Intl as unknown as {
    supportedValuesOf?: (key: string) => string[];
  };
  try {
    return intl.supportedValuesOf?.("timeZone") ?? [];
  } catch {
    return [];
  }
}

/** The person's time zone, the quiet hours of reminders and the hour of the daily list (04h, D4-4).
 *  The zone is the person's own or the hub's; their "today", their reminders and their quiet hours
 *  follow it, and so do the dates this page shows. */
export function ProjectSettings({
  api,
  zone,
  ownZone,
  hubZone,
  onZone,
  onError,
}: {
  api: ApiClient;
  /** The zone in force. */
  zone: string;
  /** The zone the person chose, or null to follow the hub. */
  ownZone: string | null;
  hubZone: string;
  onZone: (own: string | null, effective: string) => void;
  onError: (err: unknown) => void;
}) {
  const { t } = useTranslation();
  const [tz, setTz] = useState(ownZone ?? "");
  const [tzMessage, setTzMessage] = useState<string | null>(null);
  const [prefs, setPrefs] = useState<ProjectNotificationSettings | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.projectNotificationSettings().then(
      (p) => live && setPrefs(p),
      (err) => live && onError(err),
    );
    return () => {
      live = false;
    };
  }, [api, onError]);

  const saveZone = async (e: FormEvent) => {
    e.preventDefault();
    setTzMessage(null);
    try {
      const me = await api.putMyTimezone(tz.trim() || null);
      onZone(me.timezone ?? null, me.effective_timezone ?? zone);
      setTzMessage(t("project.tzSaved"));
    } catch (err) {
      if (err instanceof ApiError && err.status === 422)
        setTzMessage(t("project.tzInvalid"));
      else onError(err);
    }
  };

  const savePrefs = async (e: FormEvent) => {
    e.preventDefault();
    setSaved(null);
    setProblem(null);
    if (!prefs) return;
    if (!!prefs.quiet_start !== !!prefs.quiet_end)
      return setProblem(t("project.notifNeedsBoth"));
    try {
      setPrefs(await api.putProjectNotificationSettings(prefs));
      setSaved(t("project.notifSaved"));
    } catch (err) {
      onError(err);
    }
  };

  const names = zoneNames();
  return (
    <section
      className="project-settings"
      aria-label={t("project.settingsTitle")}
    >
      <h2>{t("project.settingsTitle")}</h2>
      <form className="project-form" onSubmit={saveZone}>
        <label>
          {t("project.tzLabel")}
          <input
            list="project-zones"
            value={tz}
            placeholder={t("project.tzHub", { zone: hubZone })}
            onChange={(e) => setTz(e.target.value)}
          />
        </label>
        <datalist id="project-zones">
          {names.map((n) => (
            <option key={n} value={n} />
          ))}
        </datalist>
        <p className="project-muted">
          {t("project.tzHelp")} ({zone})
        </p>
        <button type="submit">{t("project.save")}</button>
        {tzMessage && <p role="status">{tzMessage}</p>}
      </form>
      {prefs && (
        <form className="project-form" onSubmit={savePrefs}>
          <div className="project-bar">
            <label>
              {t("project.quietStart")}
              <input
                type="time"
                value={prefs.quiet_start ?? ""}
                onChange={(e) =>
                  setPrefs({ ...prefs, quiet_start: e.target.value || null })
                }
              />
            </label>
            <label>
              {t("project.quietEnd")}
              <input
                type="time"
                value={prefs.quiet_end ?? ""}
                onChange={(e) =>
                  setPrefs({ ...prefs, quiet_end: e.target.value || null })
                }
              />
            </label>
          </div>
          <p className="project-muted">{t("project.quietHelp")}</p>
          <label>
            {t("project.digestHour")}
            <select
              value={prefs.digest_hour ?? ""}
              onChange={(e) =>
                setPrefs({
                  ...prefs,
                  digest_hour:
                    e.target.value === "" ? null : Number(e.target.value),
                })
              }
            >
              <option value="">{t("project.digestNone")}</option>
              {Array.from({ length: 24 }, (_, h) => (
                <option key={h} value={h}>
                  {t("project.digestHourOption", { hour: h })}
                </option>
              ))}
            </select>
          </label>
          <button type="submit">{t("project.save")}</button>
          {problem && (
            <p role="alert" className="project-error">
              {problem}
            </p>
          )}
          {saved && <p role="status">{saved}</p>}
        </form>
      )}
    </section>
  );
}
