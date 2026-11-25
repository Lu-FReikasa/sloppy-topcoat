import { useState } from "react";
import { useTranslation } from "react-i18next";
import { ApiError, UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatInt } from "../format";
import { effectivePressure, type Pressure, type RamPressure } from "../pressure";

type NumKey = {
  [K in keyof Pressure]: Pressure[K] extends number ? K : never;
}[keyof Pressure];
type RamNumKey = {
  [K in keyof RamPressure]: RamPressure[K] extends number ? K : never;
}[keyof RamPressure];

interface NumField<K> {
  key: K;
  min: number;
  max: number;
  integer: boolean;
}

// Ranges from node_config.schema.json; the hub validates again.
const MB = 1048576;
const FIELDS: NumField<NumKey>[] = [
  { key: "unload_below_free_mb", min: 0, max: MB, integer: true },
  { key: "margin_mb", min: 0, max: MB, integer: true },
  { key: "foreign_shared_trigger_mb", min: 0, max: MB, integer: true },
  { key: "sustain_seconds", min: 0, max: 600, integer: true },
  { key: "reload_after_seconds", min: 0, max: 86400, integer: true },
  { key: "reload_headroom_mb", min: 0, max: MB, integer: true },
  { key: "reload_max_foreign_gpu_util", min: 0, max: 100, integer: false },
  { key: "min_off_seconds", min: 0, max: 86400, integer: true },
  { key: "poll_seconds", min: 1, max: 60, integer: true },
];
const RAM_FIELDS: NumField<RamNumKey>[] = [
  { key: "unload_below_free_mb", min: 0, max: MB, integer: true },
  { key: "margin_mb", min: 0, max: MB, integer: true },
  { key: "reload_headroom_mb", min: 0, max: MB, integer: true },
];

interface Form {
  enabled: boolean;
  reloadWhileRule: boolean;
  ramEnabled: boolean;
  values: Record<string, string>;
  ram: Record<string, string>;
}

function toForm(p: Pressure): Form {
  return {
    enabled: p.enabled,
    reloadWhileRule: p.reload_while_rule_matches,
    ramEnabled: p.ram.enabled,
    values: Object.fromEntries(FIELDS.map((f) => [f.key, String(p[f.key])])),
    ram: Object.fromEntries(RAM_FIELDS.map((f) => [f.key, String(p.ram[f.key])])),
  };
}

function parse(text: string, f: NumField<string>): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  if (!Number.isFinite(n) || n < f.min || n > f.max) return null;
  if (f.integer && !Number.isInteger(n)) return null;
  return n;
}

interface Props {
  api: ApiClient;
  nodeId: string;
  config: Record<string, unknown> | null;
  onSaved: () => void;
  /** The node has a GPU with its own memory (RAM handling is then off unless the config says so). */
  dedicatedGpu?: boolean;
}

/** Form for `config.pressure` with German labels; saves the whole config (PUT). */
export function PressureSettings({ api, nodeId, config, onSaved, dedicatedGpu = true }: Props) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const current = effectivePressure(config, dedicatedGpu);
  const [form, setForm] = useState<Form | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  function open() {
    setForm(toForm(current));
    setError(null);
    setSaved(false);
  }

  async function save() {
    if (!form || !config) return;
    const values: Record<string, number> = {};
    for (const f of FIELDS) {
      const n = parse(form.values[f.key] ?? "", f);
      if (n === null) {
        setError(t("pressure.invalid", { field: t(`pressure.${f.key}`), min: f.min, max: f.max }));
        return;
      }
      values[f.key] = n;
    }
    const ram: Record<string, number> = {};
    for (const f of RAM_FIELDS) {
      const n = parse(form.ram[f.key] ?? "", f);
      if (n === null) {
        const field = t(`pressure.ram_${f.key}`);
        setError(t("pressure.invalid", { field, min: f.min, max: f.max }));
        return;
      }
      ram[f.key] = n;
    }
    const pressure = {
      enabled: form.enabled,
      ...values,
      reload_while_rule_matches: form.reloadWhileRule,
      ram: { enabled: form.ramEnabled, ...ram },
    };
    setBusy(true);
    setError(null);
    try {
      await api.putNodeConfig(nodeId, { ...config, pressure });
      setForm(null);
      setSaved(true);
      onSaved();
    } catch (err) {
      if (err instanceof UnauthorizedError) return;
      if (err instanceof ApiError && err.code === "invalid_config") {
        setError(t("config.rejected", { message: err.message }));
      } else {
        setError(describeError(toActionError(err), t, i18n));
      }
    } finally {
      setBusy(false);
    }
  }

  if (!form) {
    return (
      <div className="pressure-summary">
        <p>
          {current.enabled
            ? t("pressure.summaryOn", {
                free: formatInt(current.unload_below_free_mb, locale),
                after: formatInt(current.reload_after_seconds, locale),
              })
            : t("pressure.summaryOff")}
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
  const setRam = (key: string, v: string) => setForm({ ...form, ram: { ...form.ram, [key]: v } });

  return (
    <div className="pressure-form">
      <label className="check">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
        />
        {t("pressure.enabled")}
      </label>
      <div className="field-grid">
        {FIELDS.map((f) => (
          <label key={f.key}>
            <span>{t(`pressure.${f.key}`)}</span>
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
      <label className="check">
        <input
          type="checkbox"
          checked={form.reloadWhileRule}
          onChange={(e) => setForm({ ...form, reloadWhileRule: e.target.checked })}
        />
        {t("pressure.reload_while_rule_matches")}
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={form.ramEnabled}
          onChange={(e) => setForm({ ...form, ramEnabled: e.target.checked })}
        />
        {t("pressure.ramEnabled")}
      </label>
      <div className="field-grid">
        {RAM_FIELDS.map((f) => (
          <label key={f.key}>
            <span>{t(`pressure.ram_${f.key}`)}</span>
            <input
              type="number"
              min={f.min}
              max={f.max}
              step={1}
              value={form.ram[f.key] ?? ""}
              disabled={!form.ramEnabled}
              onChange={(e) => setRam(f.key, e.target.value)}
            />
          </label>
        ))}
      </div>
      <p className="muted small">{t("pressure.hint")}</p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="dialog-actions">
        <button type="button" onClick={() => setForm(null)} disabled={busy}>
          {t("dialog.cancel")}
        </button>
        <button type="button" className="primary" onClick={() => void save()} disabled={busy}>
          {t("pressure.save")}
        </button>
      </div>
    </div>
  );
}
