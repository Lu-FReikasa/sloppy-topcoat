import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ApiError, UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { ConfigEditor } from "./ConfigEditor";
import {
  ALL_LAYERS,
  CONTEXT_STEPS,
  applyForm,
  formFromConfig,
  formProblems,
  restartNeeded,
  type BackendEntry,
  type Backend,
  type MemoryMode,
  type NodeForm,
} from "./nodeConfigModel";

interface Props {
  api: ApiClient;
  nodeId: string;
  config: Record<string, unknown> | null;
  onSaved: () => void;
}

/**
 * The node configuration as a form: selections, sliders and an explanation for each value. The
 * JSON editor stays one click away for everything the form does not cover (expert view).
 */
export function NodeConfigForm({ api, nodeId, config, onSaved }: Props) {
  const { t, i18n } = useTranslation();
  const [form, setForm] = useState<NodeForm | null>(null);
  const [expert, setExpert] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  function open() {
    setForm(formFromConfig(config));
    setError(null);
    setSaved(null);
    setExpert(false);
  }

  function update(patch: Partial<NodeForm>) {
    setForm((current) => (current ? { ...current, ...patch } : current));
  }

  async function save() {
    if (!form) return;
    const problems = formProblems(form);
    if (problems.length > 0) {
      setError(problems.map((p) => problemText(p)).join(" "));
      return;
    }
    const next = applyForm(config ?? {}, form);
    setBusy(true);
    setError(null);
    try {
      await api.putNodeConfig(nodeId, next);
      setSaved(restartNeeded(config, next) ? t("nodeConfig.savedRestart") : t("config.saved"));
      setForm(null);
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

  function problemText(problem: string): string {
    if (problem === "release") return t("nodeConfig.problem.release");
    if (problem === "emptyModel") return t("nodeConfig.problem.emptyModel");
    if (problem === "emptyRule") return t("nodeConfig.problem.emptyRule");
    return t("nodeConfig.problem.duplicate", { model: problem.slice("duplicate:".length) });
  }

  if (form === null) {
    return (
      <>
        <button type="button" onClick={open} disabled={config === null}>
          {t("nodeConfig.edit")}
        </button>
        {saved && (
          <p className="notice" role="status">
            {saved}
          </p>
        )}
      </>
    );
  }

  return (
    <div className="node-config-form">
      <div className="dialog-actions">
        <button type="button" aria-pressed={!expert} onClick={() => setExpert(false)}>
          {t("nodeConfig.form")}
        </button>
        <button type="button" aria-pressed={expert} onClick={() => setExpert(true)}>
          {t("nodeConfig.expert")}
        </button>
      </div>

      {expert ? (
        <ConfigEditor api={api} nodeId={nodeId} config={config} onSaved={onSaved} />
      ) : (
        <>
          <Section title={t("nodeConfig.section.runtime")}>
            <Field label={t("nodeConfig.backend.label")} info={t("nodeConfig.backend.info")}>
              <select
                aria-label={t("nodeConfig.backend.label")}
                value={form.backend}
                onChange={(e) => update({ backend: e.target.value as Backend })}
              >
                <option value="auto">{t("nodeConfig.backend.auto")}</option>
                <option value="cuda">{t("nodeConfig.backend.cuda")}</option>
                <option value="vulkan">{t("nodeConfig.backend.vulkan")}</option>
                <option value="cpu">{t("nodeConfig.backend.cpu")}</option>
              </select>
            </Field>
            <Field label={t("nodeConfig.release.label")} info={t("nodeConfig.release.info")}>
              <input
                aria-label={t("nodeConfig.release.label")}
                type="text"
                value={form.llamacppRelease}
                placeholder="b11223"
                onChange={(e) => update({ llamacppRelease: e.target.value })}
              />
            </Field>
            <Field label={t("nodeConfig.threads.label")} info={t("nodeConfig.threads.info")}>
              <AutoSlider
                label={t("nodeConfig.threads.label")}
                value={form.threads}
                min={1}
                max={64}
                autoLabel={t("nodeConfig.auto")}
                onChange={(threads) => update({ threads })}
              />
            </Field>
            <Field label={t("nodeConfig.jinja.label")} info={t("nodeConfig.jinja.info")}>
              <label>
                <input
                  type="checkbox"
                  checked={form.jinja}
                  onChange={(e) => update({ jinja: e.target.checked })}
                />{" "}
                {t("nodeConfig.jinja.on")}
              </label>
            </Field>
          </Section>

          <Section title={t("nodeConfig.section.context")}>
            <Field
              label={t("nodeConfig.defaultContext.label")}
              info={t("nodeConfig.defaultContext.info")}
            >
              <StepSlider
                label={t("nodeConfig.defaultContext.label")}
                value={form.defaultContext}
                autoLabel={t("nodeConfig.auto")}
                onChange={(defaultContext) => update({ defaultContext })}
              />
            </Field>
            <Field label={t("nodeConfig.maxContext.label")} info={t("nodeConfig.maxContext.info")}>
              <StepSlider
                label={t("nodeConfig.maxContext.label")}
                value={form.maxContext}
                autoLabel={t("nodeConfig.auto")}
                onChange={(maxContext) => update({ maxContext })}
              />
            </Field>
            <Field label={t("nodeConfig.contexts.label")} info={t("nodeConfig.contexts.info")}>
              <ListEditor
                addLabel={t("nodeConfig.add")}
                removeLabel={t("nodeConfig.remove")}
                items={form.contexts}
                onChange={(contexts) => update({ contexts })}
                empty={() => ({ model: "", length: 8192 })}
                render={(item, set) => (
                  <>
                    <input
                      aria-label={t("nodeConfig.contexts.model")}
                      type="text"
                      value={item.model}
                      placeholder={t("nodeConfig.contexts.model")}
                      onChange={(e) => set({ ...item, model: e.target.value })}
                    />
                    <StepSlider
                      label={t("nodeConfig.contexts.length")}
                      value={item.length}
                      autoLabel={t("nodeConfig.auto")}
                      onChange={(length) => set({ ...item, length: length ?? 8192 })}
                    />
                  </>
                )}
              />
            </Field>
          </Section>

          <Section title={t("nodeConfig.section.parallel")}>
            <Field label={t("nodeConfig.parallel.label")} info={t("nodeConfig.parallel.info")}>
              <input
                aria-label={t("nodeConfig.parallel.label")}
                type="range"
                min={1}
                max={16}
                value={form.parallel}
                onChange={(e) => update({ parallel: Number(e.target.value) })}
              />{" "}
              <output>{form.parallel}</output>
            </Field>
          </Section>

          <Section title={t("nodeConfig.section.memory")}>
            <Field label={t("nodeConfig.memory.label")} info={t("nodeConfig.memory.info")}>
              <select
                aria-label={t("nodeConfig.memory.label")}
                value={form.memoryMode}
                onChange={(e) => update({ memoryMode: e.target.value as MemoryMode })}
              >
                <option value="refuse">{t("nodeConfig.memory.refuse")}</option>
                <option value="warn">{t("nodeConfig.memory.warn")}</option>
                <option value="off">{t("nodeConfig.memory.off")}</option>
              </select>
            </Field>
            <Field label={t("nodeConfig.reserve.label")} info={t("nodeConfig.reserve.info")}>
              <AutoSlider
                label={t("nodeConfig.reserve.label")}
                value={form.memoryReserveMb > 0 ? form.memoryReserveMb : null}
                min={512}
                max={65536}
                step={512}
                autoLabel={t("nodeConfig.auto")}
                onChange={(mb) => update({ memoryReserveMb: mb ?? 0 })}
              />
            </Field>
          </Section>

          <Section title={t("nodeConfig.section.perModel")}>
            <p className="muted small">{t("nodeConfig.backends.info")}</p>
            <ListEditor
              addLabel={t("nodeConfig.backends.add")}
              removeLabel={t("nodeConfig.remove")}
              items={form.backends}
              onChange={(backends) => update({ backends })}
              empty={() => emptyBackend()}
              render={(item, set) => <BackendRow item={item} set={set} />}
            />
          </Section>

          <Section title={t("nodeConfig.section.watcher")}>
            <Field label={t("nodeConfig.grace.label")} info={t("nodeConfig.grace.info")}>
              <input
                aria-label={t("nodeConfig.grace.label")}
                type="number"
                min={0}
                value={form.graceSeconds}
                onChange={(e) => update({ graceSeconds: Number(e.target.value) })}
              />
            </Field>
            <Field label={t("nodeConfig.cooldown.label")} info={t("nodeConfig.cooldown.info")}>
              <input
                aria-label={t("nodeConfig.cooldown.label")}
                type="number"
                min={0}
                value={form.cooldownSeconds}
                onChange={(e) => update({ cooldownSeconds: Number(e.target.value) })}
              />
            </Field>
            <Field label={t("nodeConfig.rules.label")} info={t("nodeConfig.rules.info")}>
              <ListEditor
                addLabel={t("nodeConfig.rules.add")}
                removeLabel={t("nodeConfig.remove")}
                items={form.rules}
                onChange={(rules) => update({ rules })}
                empty={() => ({ kind: "process" as const, value: "" })}
                render={(item, set) => (
                  <>
                    <select
                      aria-label={t("nodeConfig.rules.kind")}
                      value={item.kind}
                      onChange={(e) =>
                        set({ ...item, kind: e.target.value as "process" | "path" })
                      }
                    >
                      <option value="process">{t("nodeConfig.rules.process")}</option>
                      <option value="path">{t("nodeConfig.rules.path")}</option>
                    </select>
                    <input
                      aria-label={t("nodeConfig.rules.value")}
                      type="text"
                      value={item.value}
                      placeholder={
                        item.kind === "process" ? "spiel.exe" : "D:\\SteamLibrary\\steamapps\\common\\"
                      }
                      onChange={(e) => set({ ...item, value: e.target.value })}
                    />
                  </>
                )}
              />
            </Field>
          </Section>

          <p className="muted small">{t("nodeConfig.restartHint")}</p>
        </>
      )}

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!expert && (
        <div className="dialog-actions">
          <button type="button" onClick={() => setForm(null)} disabled={busy}>
            {t("dialog.cancel")}
          </button>
          <button type="button" className="primary" onClick={() => void save()} disabled={busy}>
            {t("config.save")}
          </button>
        </div>
      )}
    </div>
  );
}

function emptyBackend(): BackendEntry {
  return {
    model: "",
    backend: "",
    layersMode: "fit",
    layers: 0,
    moeLayers: null,
    noHost: false,
  };
}

function BackendRow({ item, set }: { item: BackendEntry; set: (next: BackendEntry) => void }) {
  const { t } = useTranslation();
  return (
    <>
      <input
        aria-label={t("nodeConfig.backends.model")}
        type="text"
        value={item.model}
        placeholder={t("nodeConfig.backends.model")}
        onChange={(e) => set({ ...item, model: e.target.value })}
      />
      <select
        aria-label={t("nodeConfig.backends.backend")}
        value={item.backend}
        onChange={(e) => set({ ...item, backend: e.target.value as BackendEntry["backend"] })}
      >
        <option value="">{t("nodeConfig.backends.nodeDefault")}</option>
        <option value="cuda">{t("nodeConfig.backend.cuda")}</option>
        <option value="vulkan">{t("nodeConfig.backend.vulkan")}</option>
        <option value="cpu">{t("nodeConfig.backend.cpu")}</option>
      </select>
      <select
        aria-label={t("nodeConfig.backends.layers")}
        value={item.layersMode}
        onChange={(e) => set({ ...item, layersMode: e.target.value as BackendEntry["layersMode"] })}
      >
        <option value="fit">{t("nodeConfig.backends.fit")}</option>
        <option value="all">{t("nodeConfig.backends.all")}</option>
        <option value="fixed">{t("nodeConfig.backends.fixed")}</option>
      </select>
      {item.layersMode === "fixed" && (
        <AutoSlider
          label={t("nodeConfig.backends.layerCount")}
          value={item.layers}
          min={0}
          max={ALL_LAYERS}
          autoLabel={t("nodeConfig.auto")}
          onChange={(layers) => set({ ...item, layers: layers ?? 0 })}
        />
      )}
      <label>
        <input
          type="checkbox"
          checked={item.moeLayers !== null}
          onChange={(e) => set({ ...item, moeLayers: e.target.checked ? 0 : null })}
        />{" "}
        {t("nodeConfig.backends.moe")}
      </label>
      {item.moeLayers !== null && (
        <AutoSlider
          label={t("nodeConfig.backends.moeCount")}
          value={item.moeLayers}
          min={0}
          max={200}
          autoLabel={t("nodeConfig.auto")}
          onChange={(moeLayers) => set({ ...item, moeLayers: moeLayers ?? 0 })}
        />
      )}
      <label>
        <input
          type="checkbox"
          checked={item.noHost}
          onChange={(e) => set({ ...item, noHost: e.target.checked })}
        />{" "}
        {t("nodeConfig.backends.noHost")}
      </label>
    </>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <fieldset className="node-config-section">
      <legend>{title}</legend>
      {children}
    </fieldset>
  );
}

function Field({ label, info, children }: { label: string; info: string; children: ReactNode }) {
  return (
    <div className="node-config-field">
      <div className="node-config-label">{label}</div>
      {children}
      <p className="muted small">{info}</p>
    </div>
  );
}

/** A slider with an "automatic" box: null means the node chooses. */
function AutoSlider({
  label,
  value,
  min,
  max,
  step = 1,
  autoLabel,
  onChange,
}: {
  label: string;
  value: number | null;
  min: number;
  max: number;
  step?: number;
  autoLabel: string;
  onChange: (value: number | null) => void;
}) {
  return (
    <span>
      <label>
        <input
          type="checkbox"
          checked={value === null}
          onChange={(e) => onChange(e.target.checked ? null : min)}
        />{" "}
        {autoLabel}
      </label>
      {value !== null && (
        <>
          <input
            aria-label={label}
            type="range"
            min={min}
            max={max}
            step={step}
            value={value}
            onChange={(e) => onChange(Number(e.target.value))}
          />{" "}
          <output>{value}</output>
        </>
      )}
    </span>
  );
}

/** A slider over the context steps; null means the node's default. */
function StepSlider({
  label,
  value,
  autoLabel,
  onChange,
}: {
  label: string;
  value: number | null;
  autoLabel: string;
  onChange: (value: number | null) => void;
}) {
  const index = value === null ? 0 : nearestStep(value);
  return (
    <span>
      <label>
        <input
          type="checkbox"
          checked={value === null}
          onChange={(e) => onChange(e.target.checked ? null : CONTEXT_STEPS[4] ?? 8192)}
        />{" "}
        {autoLabel}
      </label>
      {value !== null && (
        <>
          <input
            aria-label={label}
            type="range"
            min={0}
            max={CONTEXT_STEPS.length - 1}
            step={1}
            value={index}
            onChange={(e) => onChange(CONTEXT_STEPS[Number(e.target.value)] ?? index)}
          />{" "}
          <output>{(CONTEXT_STEPS[index] ?? 0).toLocaleString()}</output>
        </>
      )}
    </span>
  );
}

function nearestStep(value: number): number {
  let best = 0;
  for (let i = 1; i < CONTEXT_STEPS.length; i++) {
    if (Math.abs((CONTEXT_STEPS[i] ?? 0) - value) < Math.abs((CONTEXT_STEPS[best] ?? 0) - value)) {
      best = i;
    }
  }
  return best;
}

/** A list the person adds to and removes from. */
function ListEditor<T>({
  items,
  onChange,
  empty,
  render,
  addLabel,
  removeLabel,
}: {
  items: T[];
  onChange: (items: T[]) => void;
  empty: () => T;
  render: (item: T, set: (next: T) => void) => ReactNode;
  addLabel: string;
  removeLabel: string;
}) {
  return (
    <div className="node-config-list">
      {items.map((item, index) => (
        <div className="node-config-row" key={index}>
          {render(item, (next) => onChange(items.map((o, i) => (i === index ? next : o))))}
          <button type="button" onClick={() => onChange(items.filter((_, i) => i !== index))}>
            {removeLabel}
          </button>
        </div>
      ))}
      <button type="button" onClick={() => onChange([...items, empty()])}>
        {addLabel}
      </button>
    </div>
  );
}
