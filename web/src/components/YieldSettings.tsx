import { useState } from "react";
import { useTranslation } from "react-i18next";
import { ApiError, UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import {
  effectivePower,
  effectiveYield,
  POWER_PROFILES,
  type PowerProfile,
  type Yield,
} from "../yield";

type NumKey = {
  [K in keyof Yield]: Yield[K] extends number ? K : never;
}[keyof Yield];

interface NumField {
  key: NumKey;
  min: number;
  max: number;
  integer: boolean;
}

// Ranges from node_config.schema.json; the hub validates again.
const FIELDS: NumField[] = [
  { key: "idle_seconds", min: 1, max: 86400, integer: true },
  { key: "cpu_percent", min: 0, max: 100, integer: false },
  { key: "sustain_seconds", min: 0, max: 3600, integer: true },
  { key: "resume_seconds", min: 0, max: 3600, integer: true },
  { key: "unload_after_pause_seconds", min: 0, max: 604800, integer: true },
  { key: "poll_seconds", min: 1, max: 300, integer: true },
];

function parse(text: string, min: number, max: number, integer: boolean): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  if (!Number.isFinite(n) || n < min || n > max) return null;
  if (integer && !Number.isInteger(n)) return null;
  return n;
}

interface Props {
  api: ApiClient;
  nodeId: string;
  config: Record<string, unknown> | null;
  onSaved: () => void;
  /** The node has a GPU with its own memory (freezing is then off unless the config says so). */
  dedicatedGpu?: boolean;
}

/** Saves one config section (PUT of the whole config) and reports errors like the other forms. */
function useSectionSave(
  api: ApiClient,
  nodeId: string,
  config: Record<string, unknown> | null,
  key: string,
  onSaved: () => void,
) {
  const { t, i18n } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  async function save(section: Record<string, unknown>): Promise<boolean> {
    if (!config) return false;
    setBusy(true);
    setError(null);
    try {
      await api.putNodeConfig(nodeId, { ...config, [key]: section });
      setSaved(true);
      onSaved();
      return true;
    } catch (err) {
      if (err instanceof UnauthorizedError) return false;
      if (err instanceof ApiError && err.code === "invalid_config") {
        setError(t("config.rejected", { message: err.message }));
      } else {
        setError(describeError(toActionError(err), t, i18n));
      }
      return false;
    } finally {
      setBusy(false);
    }
  }
  return { busy, error, setError, saved, setSaved, save };
}

/** Form for `config.yield`: how the node steps back for the person using the machine. */
export function YieldSettings({ api, nodeId, config, onSaved, dedicatedGpu = true }: Props) {
  const { t } = useTranslation();
  const current = effectiveYield(config, dedicatedGpu);
  const [form, setForm] = useState<{
    enabled: boolean;
    lowPriority: boolean;
    battery: boolean;
    values: Record<string, string>;
  } | null>(null);
  const { busy, error, setError, saved, setSaved, save } = useSectionSave(
    api,
    nodeId,
    config,
    "yield",
    onSaved,
  );

  function open() {
    setForm({
      enabled: current.enabled,
      lowPriority: current.low_priority,
      battery: current.pause_on_battery,
      values: Object.fromEntries(FIELDS.map((f) => [f.key, String(current[f.key])])),
    });
    setError(null);
    setSaved(false);
  }

  async function submit() {
    if (!form) return;
    const values: Record<string, number> = {};
    for (const f of FIELDS) {
      const n = parse(form.values[f.key] ?? "", f.min, f.max, f.integer);
      if (n === null) {
        setError(t("yield.invalid", { field: t(`yield.${f.key}`), min: f.min, max: f.max }));
        return;
      }
      values[f.key] = n;
    }
    const ok = await save({
      enabled: form.enabled,
      low_priority: form.lowPriority,
      pause_on_battery: form.battery,
      ...values,
    });
    if (ok) setForm(null);
  }

  if (!form) {
    return (
      <div className="pressure-summary">
        <p>
          {current.enabled
            ? t("yield.summaryOn", {
                idle: current.idle_seconds,
                cpu: current.cpu_percent,
                unload: Math.round(current.unload_after_pause_seconds / 60),
              })
            : t("yield.summaryOff")}
          {current.low_priority ? ` ${t("yield.priorityOn")}` : ""}
        </p>
        <button type="button" onClick={open} disabled={config === null}>
          {t("pressure.edit")}
        </button>
        {saved && (
          <p className="notice" role="status">
            {t("pressure.saved")}
          </p>
        )}
      </div>
    );
  }

  const setValue = (key: string, v: string) =>
    setForm({ ...form, values: { ...form.values, [key]: v } });

  return (
    <div className="pressure-form">
      <label className="check">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
        />
        {t("yield.enabled")}
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={form.lowPriority}
          onChange={(e) => setForm({ ...form, lowPriority: e.target.checked })}
        />
        {t("yield.low_priority")}
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={form.battery}
          disabled={!form.enabled}
          onChange={(e) => setForm({ ...form, battery: e.target.checked })}
        />
        {t("yield.pause_on_battery")}
      </label>
      <div className="field-grid">
        {FIELDS.map((f) => (
          <label key={f.key}>
            <span>{t(`yield.${f.key}`)}</span>
            <input
              type="number"
              min={f.min}
              max={f.max}
              step={f.integer ? 1 : "any"}
              value={form.values[f.key] ?? ""}
              disabled={!form.enabled}
              onChange={(e) => setValue(f.key, e.target.value)}
            />
          </label>
        ))}
      </div>
      <p className="muted small">{t("yield.hint")}</p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="dialog-actions">
        <button type="button" onClick={() => setForm(null)} disabled={busy}>
          {t("dialog.cancel")}
        </button>
        <button type="button" className="primary" onClick={() => void submit()} disabled={busy}>
          {t("pressure.save")}
        </button>
      </div>
    </div>
  );
}

/** Form for `config.power`: the energy profile and the optional sleep after idle time. */
export function PowerSettings({ api, nodeId, config, onSaved }: Omit<Props, "dedicatedGpu">) {
  const { t } = useTranslation();
  const current = effectivePower(config);
  const [form, setForm] = useState<{ profile: PowerProfile; sleep: string } | null>(null);
  const { busy, error, setError, saved, setSaved, save } = useSectionSave(
    api,
    nodeId,
    config,
    "power",
    onSaved,
  );

  function open() {
    setForm({
      profile: current.profile,
      sleep: String(Math.round(current.sleep_after_idle_seconds / 60)),
    });
    setError(null);
    setSaved(false);
  }

  async function submit() {
    if (!form) return;
    const minutes = parse(form.sleep, 0, 43200, true);
    if (minutes === null) {
      setError(t("yield.invalid", { field: t("power.sleepMinutes"), min: 0, max: 43200 }));
      return;
    }
    const ok = await save({ profile: form.profile, sleep_after_idle_seconds: minutes * 60 });
    if (ok) setForm(null);
  }

  if (!form) {
    return (
      <div className="pressure-summary">
        <p>
          {t(`power.summary.${current.profile}`)}
          {current.sleep_after_idle_seconds > 0
            ? ` ${t("power.sleepOn", { minutes: Math.round(current.sleep_after_idle_seconds / 60) })}`
            : ""}
        </p>
        <button type="button" onClick={open} disabled={config === null}>
          {t("pressure.edit")}
        </button>
        {saved && (
          <p className="notice" role="status">
            {t("pressure.saved")}
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="pressure-form">
      <div className="field-grid">
        <label>
          <span>{t("power.profile")}</span>
          <select
            value={form.profile}
            onChange={(e) => setForm({ ...form, profile: e.target.value as PowerProfile })}
          >
            {POWER_PROFILES.map((p) => (
              <option key={p} value={p}>
                {t(`power.profiles.${p}`)}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>{t("power.sleepMinutes")}</span>
          <input
            type="number"
            min={0}
            max={43200}
            step={1}
            value={form.sleep}
            onChange={(e) => setForm({ ...form, sleep: e.target.value })}
          />
        </label>
      </div>
      <p className="muted small">{t("power.hint")}</p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="dialog-actions">
        <button type="button" onClick={() => setForm(null)} disabled={busy}>
          {t("dialog.cancel")}
        </button>
        <button type="button" className="primary" onClick={() => void submit()} disabled={busy}>
          {t("pressure.save")}
        </button>
      </div>
    </div>
  );
}
