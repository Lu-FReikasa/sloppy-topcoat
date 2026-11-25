import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { toActionError } from "../errors";
import { formatDateTime } from "../format";
import type { CareChange, CareSettings, MemoryCareOverview, MemoryRun } from "../types";

interface Props {
  api: ApiClient;
  care: MemoryCareOverview;
  locale: string;
  /** Run an action on the memory, then reload and announce the change. */
  act: <T>(fn: () => Promise<T>, done?: string) => Promise<void>;
  onError: (err: unknown) => void;
  onMessage: (text: string) => void;
  reload: () => void;
}

const REFUSED = ["no_node_available", "already_running", "memory_disabled", "digest_disabled"];

/** The care of the memory (E3-05): start a run, read what the runs did — each change with the
 *  reason and its undo — and see the numbers the care works with. */
export function CareSection({ api, care, locale, act, onError, onMessage, reload }: Props) {
  const { t } = useTranslation();
  const [undone, setUndone] = useState<Set<string>>(new Set());

  async function runNow() {
    try {
      await api.careMemory();
      reload();
    } catch (err) {
      const code = toActionError(err).code;
      if (REFUSED.includes(code)) onMessage(t(`memory.care.refused.${code}`));
      else onError(err);
    }
  }

  async function digestNow() {
    try {
      const { started } = await api.digestMemory();
      onMessage(t("memory.care.digestStarted", { count: started }));
    } catch (err) {
      const code = toActionError(err).code;
      if (REFUSED.includes(code)) onMessage(t(`memory.care.refused.${code}`));
      else onError(err);
    }
  }

  function undo(key: string, change: CareChange) {
    const fn =
      change.undo === "restore"
        ? () => api.restoreMemory(change.item_id)
        : () => api.revertMemory(change.item_id, change.from_version ?? 1);
    void act(async () => {
      await fn();
      setUndone((s) => new Set(s).add(key));
    });
  }

  return (
    <section className="care" data-testid="care-section">
      <h2>{t("memory.care.heading")}</h2>
      <p className="muted">{t("memory.care.intro")}</p>
      <div className="memory-actions">
        <button type="button" onClick={() => void runNow()} disabled={care.running}>
          {care.running ? t("memory.care.running") : t("memory.care.runNow")}
        </button>
        <button type="button" onClick={() => void digestNow()}>
          {t("memory.care.digestNow")}
        </button>
      </div>
      <p className="muted small">{t("memory.care.digestHint")}</p>
      {care.runs.length === 0 ? (
        <p className="muted small" data-testid="care-empty">
          {t("memory.care.noRuns")}
        </p>
      ) : (
        <ul className="care-runs">
          {care.runs.map((run) => (
            <CareRun key={run.id} run={run} locale={locale} undone={undone} onUndo={undo} />
          ))}
        </ul>
      )}
      <CareNumbers settings={care.settings} />
    </section>
  );
}

function changesOf(run: MemoryRun): CareChange[] {
  const raw = (run.stats as { changes?: unknown }).changes;
  return Array.isArray(raw) ? (raw as CareChange[]) : [];
}

function CareRun({
  run,
  locale,
  undone,
  onUndo,
}: {
  run: MemoryRun;
  locale: string;
  undone: Set<string>;
  onUndo: (key: string, change: CareChange) => void;
}) {
  const { t } = useTranslation();
  const s = run.stats as Record<string, number | undefined>;
  const changes = changesOf(run);
  return (
    <li className="care-run" data-testid="care-run">
      <details>
        <summary>
          {formatDateTime(run.finished_at ?? run.started_at, locale) ?? "–"} ·{" "}
          {t(`memory.care.trigger.${run.trigger}`, {
            defaultValue: run.trigger,
          })}{" "}
          · {t(`memory.runStatus.${run.status}`)}
          <span className="muted small" data-testid="care-summary">
            {" – "}
            {t("memory.care.summary", {
              checked: s.checked ?? 0,
              unchanged: s.unchanged ?? 0,
              changed: s.changed ?? 0,
              asked: s.asked ?? 0,
              expired: s.expired ?? 0,
              archived: s.archived ?? 0,
              unverifiable: s.unverifiable ?? 0,
            })}
          </span>
        </summary>
        {changes.length === 0 ? (
          <p className="muted small">{t("memory.care.nothingChanged")}</p>
        ) : (
          <ul className="care-changes">
            {changes.map((c, i) => {
              const key = `${run.id}-${i}`;
              return (
                <li key={key} data-testid="care-change">
                  <p>
                    <strong>{t(`memory.care.action.${c.action}`)}</strong>: “{c.new_text ?? c.text}”
                    {c.new_text && (
                      <span className="muted small">
                        {" "}
                        ({t("memory.care.was", { text: c.text })})
                      </span>
                    )}
                  </p>
                  <p className="muted small">{c.reason}</p>
                  {c.undo &&
                    (undone.has(key) ? (
                      <span className="muted small" role="status">
                        {t("memory.care.undone")}
                      </span>
                    ) : (
                      <button type="button" onClick={() => onUndo(key, c)}>
                        {t(
                          c.undo === "restore"
                            ? "memory.care.undoRestore"
                            : "memory.care.undoRevert",
                        )}
                      </button>
                    ))}
                </li>
              );
            })}
          </ul>
        )}
      </details>
    </li>
  );
}

const pct = (v: number) => Math.round(v * 100);

function CareNumbers({ settings }: { settings: CareSettings }) {
  const { t } = useTranslation();
  return (
    <details className="care-settings" data-testid="care-settings">
      <summary>{t("memory.care.settings")}</summary>
      <ul>
        <li>
          {settings.hour >= 0
            ? t("memory.care.when", {
                hour: String(settings.hour).padStart(2, "0"),
                minutes: Math.max(Math.round(settings.interval_seconds / 60), 1),
              })
            : t("memory.care.whenNoHour")}
        </li>
        <li>
          {t("memory.care.halflives", {
            person: settings.halflife_days.person,
            preference: settings.halflife_days.preference,
            operational: settings.halflife_days.operational,
            volatile: settings.halflife_days.volatile,
          })}
        </li>
        <li>
          {t("memory.care.thresholds", {
            check: pct(settings.check_threshold),
            useful: pct(settings.archive.usefulness),
            reliable: pct(settings.archive.reliability),
            days: settings.archive.idle_days,
          })}
        </li>
      </ul>
      <p className="muted small">{t("memory.care.settingsHint")}</p>
    </details>
  );
}
