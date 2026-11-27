import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { useNode } from "../data/useNode";
import { useNodeActions, type ModelAction, type NodeActions } from "../data/useNodeActions";
import type { HubEvents } from "../data/hubEvents";
import { describeError } from "../errors";
import { bytesToGb, formatDateTime, formatDecimal, formatInt } from "../format";
import { eventDetailKey, loadEventText } from "../nodeActivity";
import {
  DEFAULT_RESIDENT_PRIORITY,
  effectivePressure,
  PRESSURE_DEFAULTS,
  residentsOf,
} from "../pressure";
import { modelHref, modelsHref, overviewHref } from "../route";
import type { CpuInfo, ModelInfo, NodeDetail, NodeEvent, Resident, ResidentUpdate } from "../types";
import { BenchmarkSection } from "./BenchmarkSection";
import { NodeConfigForm } from "./NodeConfigForm";
import { DownloadDialog, DownloadList } from "./Downloads";
import { diskLow, useDiskText } from "./NodeFacts";
import { NodeHeader } from "./NodeHeader";
import { PressureSettings } from "./PressureSettings";
import { PowerSettings, YieldSettings } from "./YieldSettings";
import { HostReadSection } from "./HostReadSection";
import { ToolsSection } from "./ToolsSection";
import { RenameNode } from "./RenameNode";
import { UpdateSection } from "./UpdateSection";
import { ForeignUsage, VramBar } from "./UsageBar";
import { askConfirm } from "./askConfirm";
import { TransferList } from "./TransferList";

const DEFAULT_SHARED_TRIGGER_MB = PRESSURE_DEFAULTS.foreign_shared_trigger_mb;

/** Thread count from the reported runner, else from the desired config. */
function configuredThreads(node: NodeDetail): number | undefined {
  if (node.runner?.threads !== undefined) return node.runner.threads;
  const runner = node.config?.runner;
  if (runner && typeof runner === "object" && "threads" in runner) {
    const v = (runner as { threads: unknown }).threads;
    if (typeof v === "number") return v;
  }
  return undefined;
}

interface Props {
  api: ApiClient;
  nodeId: string;
  /** Each detail the hub sent: the app keeps its node list in step (a rename, ...). */
  onLoaded?: (detail: NodeDetail) => void;
  /** The stream bus; the app's by default, a fake one in tests. */
  events?: HubEvents;
}

/** The node detail page: renders what `useNode` and `useNodeActions` give it (web/src/data). */
export function NodeDetailView({ api, nodeId, onLoaded, events }: Props) {
  const { t, i18n } = useTranslation();
  const resource = useNode(api, nodeId, { onLoad: onLoaded, events });
  const actions = useNodeActions(api, nodeId, resource);
  const loadError = resource.error;
  const node = resource.data && resource.data.id === nodeId ? resource.data : null;

  return (
    <section>
      <p className="link-row">
        <a href={overviewHref}>← {t("detail.back")}</a>
      </p>
      {!node && loadError && (
        <p className="error" role="alert">
          {loadError.code === "not_found"
            ? t("detail.notFound")
            : t("detail.loadError", { message: describeError(loadError, t, i18n) })}
        </p>
      )}
      {!node && !loadError && <p className="muted">{t("app.loading")}</p>}
      {node && (
        <DetailBody
          api={api}
          node={node}
          actions={actions}
          reload={resource.reload}
        />
      )}
    </section>
  );
}

function DetailBody({
  api,
  node,
  actions,
  reload,
}: {
  api: ApiClient;
  node: NodeDetail;
  actions: NodeActions;
  /** Load the detail again: a section saved something the stream does not carry. */
  reload: () => Promise<void>;
}) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const disk = useDiskText(node);
  const [downloadOpen, setDownloadOpen] = useState(false);
  const { pending } = actions;
  const offline = !node.connected || node.state === "offline";

  const residents = new Map(residentsOf(node.config).map((r) => [r.model, r]));
  const trigger = effectivePressure(node.config).foreign_shared_trigger_mb;
  const sharedTrigger = trigger > 0 ? trigger : DEFAULT_SHARED_TRIGGER_MB;
  const known = new Set(node.models.map((m) => m.name));
  const missingResidents = [...residents.values()].filter((r) => !known.has(r.model));

  async function runModelAction(action: ModelAction, model: string) {
    if (action === "models.delete" && !(await askConfirm(t("detail.confirmDelete", { model })))) {
      return;
    }
    await actions.runModelAction(action, model);
  }

  const roles = node.config?.roles;
  // a tool node (E2-04) or a dev node (E6-01) has no models: its page is about capabilities instead
  const isTools =
    Array.isArray(roles) &&
    (roles.includes("tools") || roles.includes("dev")) &&
    !roles.includes("gpu");
  const lastSeen = formatDateTime(node.last_seen, locale) ?? t("node.never");
  // what matters first (issues #22, #175) is in the header; the inventory is folded

  return (
    <>
      <NodeHeader node={node} actions={actions} isTools={isTools} />
      <details className="facts-more">
        <summary>{t("detail.factsMore")}</summary>
        <dl className="facts">
          <dt>{t("node.os")}</dt>
          <dd>
            {node.os} / {node.arch}
          </dd>
          <dt>{t("node.cpu")}</dt>
          <dd>
            <CpuText cpu={node.cpu} />
          </dd>
          {disk && !diskLow(node) && <DiskFact disk={disk} low={false} />}
          {(node.network?.length ?? 0) > 0 && (
            <>
              <dt>{t("node.network")}</dt>
              <dd data-testid="node-network">
                {node.network?.map((i) => (
                  <span className="fact-item" key={`${i.name} ${i.mac}`}>
                    {i.name} {i.mac}
                  </span>
                ))}
              </dd>
            </>
          )}
          <dt>{t("node.connected")}</dt>
          <dd>{node.connected ? t("node.yes") : t("node.no")}</dd>
          <dt>{t("node.lastSeen")}</dt>
          <dd>{lastSeen}</dd>
          <dt>{t("detail.runner")}</dt>
          <dd>
            {node.runner ? (
              <>
                {t(`runnerType.${node.runner.type}`, { defaultValue: node.runner.type })}
                {" – "}
                {node.runner.available ? t("detail.runnerAvailable") : t("detail.runnerUnavailable")}
                {node.runner.detail ? ` (${node.runner.detail})` : ""}
              </>
            ) : (
              t("detail.noRunner")
            )}
          </dd>
          {!isTools && (
            <>
              <dt>{t("detail.backend")}</dt>
              <dd>
                {node.runner?.backend
                  ? t(`backend.${node.runner.backend}`, { defaultValue: node.runner.backend })
                  : t("node.unknown")}
              </dd>
              <dt>{t("detail.llamacpp")}</dt>
              <dd>{node.runner?.llamacpp_release ?? t("node.unknown")}</dd>
              <dt>{t("detail.threads")}</dt>
              <dd>{configuredThreads(node) ?? t("detail.threadsAuto")}</dd>
            </>
          )}
        </dl>
      </details>
      <NodeKindSwitch node={node} compute={actions.compute} />

      {isTools ? (
        <ToolsSection api={api} node={node} />
      ) : (
        <>
          <h2>{t("node.gpus")}</h2>
          {offline ? (
            <p className="muted">{t("overview.offlineNoData")}</p>
          ) : node.gpus.length === 0 ? (
            <p className="muted">{t("overview.noGpu")}</p>
          ) : (
            <div className="gpu-list">
              {node.gpus.map((g) => (
                <div key={g.index}>
                  <VramBar
                    label={`${t("node.vram")} ${g.name}`}
                    usedMb={g.vram_used_mb}
                    totalMb={g.vram_total_mb}
                  />
                  <ForeignUsage gpu={g} sharedTriggerMb={sharedTrigger} />
                  {g.integrated && g.shared_total_mb !== undefined && (
                    <VramBar
                      label={`${t("node.sharedMemory")} ${g.name}`}
                      usedMb={g.shared_used_mb ?? null}
                      totalMb={g.shared_total_mb}
                    />
                  )}
                </div>
              ))}
            </div>
          )}

          <div className="section-head">
            <h2>{t("detail.models")}</h2>
            <button type="button" disabled={offline} onClick={() => setDownloadOpen(true)}>
              {t("download.open")}
            </button>
          </div>
          {offline && <p className="muted">{t("detail.offlineHint")}</p>}
          {node.models.length === 0 ? (
            <p className="muted">{t("detail.noModels")}</p>
          ) : (
            <div className="table-wrap">
              <table className="models">
                <thead>
                  <tr>
                    <th>{t("detail.colName")}</th>
                    <th>{t("detail.colSize")}</th>
                    <th>{t("detail.colFormat")}</th>
                    <th>{t("detail.colLoaded")}</th>
                    <th>{t("detail.colVram")}</th>
                    <th>{t("detail.colRam")}</th>
                    <th>{t("detail.colBackend")}</th>
                    <th>{t("detail.colPort")}</th>
                    <th title={t("detail.residentHint")}>{t("detail.colResident")}</th>
                    <th>{t("detail.colAction")}</th>
                  </tr>
                </thead>
                <tbody>
                  {node.models.map((m) => (
                    <ModelRow
                      key={m.name}
                      model={m}
                      resident={residents.get(m.name)}
                      disabled={offline || pending !== null}
                      residentDisabled={pending !== null}
                      onAction={(a) => void runModelAction(a, m.name)}
                      onResident={(entry) => void actions.changeResident(m.name, entry)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {node.models.length > 0 && <p className="muted small">{t("detail.residentHint")}</p>}
          <p className="muted small">
            {t("detail.modelsElsewhere")} <a href={modelsHref}>{t("detail.modelsElsewhereLink")}</a>
          </p>
          {missingResidents.length > 0 && (
            <div className="missing-residents">
              <span className="muted">{t("detail.residentsMissing")}</span>{" "}
              {missingResidents.map((r) => (
                <span key={r.model} className="missing-resident">
                  <code>{r.model}</code>{" "}
                  <button
                    type="button"
                    disabled={pending !== null}
                    aria-label={t("detail.residentRemove", { model: r.model })}
                    onClick={() => void actions.changeResident(r.model, null)}
                  >
                    {t("detail.residentRemoveShort")}
                  </button>
                </span>
              ))}
            </div>
          )}
          <DownloadList events={node.events} />
          <TransferList api={api} nodeId={node.id} events={node.events} />
          {downloadOpen && (
            <DownloadDialog api={api} nodeId={node.id} onClose={() => setDownloadOpen(false)} />
          )}

          {node.loaded.length > 0 && (
            <Fold title={t("detail.loadedModels")}>
              <ul className="loaded-list">
                {node.loaded.map((m) => (
                  <li key={m.name}>
                    {m.name}
                    {m.vram_used_mb !== undefined &&
                      ` – ${t("detail.vramMb", { value: formatInt(m.vram_used_mb, locale) })}`}
                    {m.ram_used_mb !== undefined &&
                      ` – ${t("detail.ramMb", { value: formatInt(m.ram_used_mb, locale) })}`}
                    {m.context_length !== undefined &&
                      ` – ${
                        m.n_parallel !== undefined && m.n_parallel > 1
                          ? t("detail.contextPerSlot", {
                              value: formatInt(m.context_length, locale),
                              slots: m.n_parallel,
                            })
                          : t("detail.contextLength", {
                              value: formatInt(m.context_length, locale),
                            })
                      }`}
                    {m.backend !== undefined &&
                      ` – ${t(`backend.${m.backend}`, { defaultValue: m.backend })}`}
                    {m.port !== undefined && ` – ${t("detail.port", { value: m.port })}`}
                    {m.pid !== undefined && ` – ${t("detail.pid", { value: m.pid })}`}
                  </li>
                ))}
              </ul>
            </Fold>
          )}

          <BenchmarkSection api={api} node={node} />

          <Fold title={t("pressure.heading")}>
            <PressureSettings
              api={api}
              nodeId={node.id}
              config={node.config}
              onSaved={reload}
              dedicatedGpu={node.gpus.some((g) => !g.integrated)}
            />
          </Fold>

          <Fold title={t("yield.heading")}>
            <YieldSettings
              api={api}
              nodeId={node.id}
              config={node.config}
              onSaved={reload}
              dedicatedGpu={node.gpus.some((g) => !g.integrated)}
            />
          </Fold>

          <Fold title={t("power.heading")}>
            <PowerSettings api={api} nodeId={node.id} config={node.config} onSaved={reload} />
          </Fold>
        </>
      )}

      {/* a compute node that has capabilities switched on (the Windows checks, file reading) shows
          them like a tool node does; without any, its page stays as it was */}
      {!isTools && Array.isArray(node.config?.capabilities) && node.config.capabilities.length > 0 && (
        <ToolsSection api={api} node={node} />
      )}

      <HostReadSection api={api} node={node} onSaved={reload} />

      <UpdateSection api={api} node={node} config={node.config} onChanged={reload} />

      <RenameNode api={api} nodeId={node.id} name={node.name} onRenamed={reload} />

      <Fold title={t("config.heading")}>
        <NodeConfigForm api={api} nodeId={node.id} config={node.config} onSaved={reload} />
      </Fold>

      <Fold title={t("detail.events")}>
        <EventList events={node.events} />
      </Fold>
    </>
  );
}

/** The model drive's free space: in the summary while it is low, else among the details. */
function DiskFact({ disk, low }: { disk: string; low: boolean }) {
  const { t } = useTranslation();
  return (
    <>
      <dt>{t("node.disk")}</dt>
      <dd data-testid="node-disk" className={low ? "warn-text" : undefined}>
        {disk}
        {low && ` – ${t("node.diskLow")}`}
      </dd>
    </>
  );
}

/** A section the page keeps closed until asked for: settings that rarely change. */
function Fold({ title, children }: { title: string; children: ReactNode }) {
  return (
    <details className="fold">
      <summary>
        <h2>{title}</h2>
      </summary>
      {children}
    </details>
  );
}

/** Compute node (holds and serves models) or tool node (only capabilities): config.roles. A tool
 *  node is left out of the models page and its counts. */
function NodeKindSwitch({ node, compute }: { node: NodeDetail; compute: NodeActions["compute"] }) {
  const { t, i18n } = useTranslation();
  const roles = node.config?.roles;
  if (!node.config || !Array.isArray(roles)) return null;
  const isCompute = roles.includes("gpu");
  const busy = compute.busy;
  const error = compute.error ? describeError(compute.error, t, i18n) : null;

  return (
    <div className="node-kind">
      <label>
        <input
          type="checkbox"
          checked={isCompute}
          disabled={busy}
          onChange={(e) => void compute.set(e.target.checked)}
        />{" "}
        {t("node.computeToggle")}
      </label>
      <p className="muted small">{t(isCompute ? "node.computeOn" : "node.computeOff")}</p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function ModelRow({
  model,
  resident,
  disabled,
  residentDisabled,
  onAction,
  onResident,
}: {
  model: ModelInfo;
  resident: Resident | undefined;
  disabled: boolean;
  /** Resident settings are stored on the hub, so they work while the node is offline. */
  residentDisabled: boolean;
  onAction: (a: ModelAction) => void;
  onResident: (entry: ResidentUpdate | null) => void;
}) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  return (
    <tr>
      <td className="model-name">
        <a href={modelHref(model.name)}>{model.name}</a>
      </td>
      <td>
        {model.size_bytes !== undefined
          ? t("detail.sizeGb", { value: formatDecimal(bytesToGb(model.size_bytes), locale) })
          : "–"}
      </td>
      <td>{model.format ?? "–"}</td>
      <td>{model.loaded ? t("node.yes") : t("node.no")}</td>
      <td>
        {model.vram_used_mb !== null
          ? t("detail.vramMb", { value: formatInt(model.vram_used_mb, locale) })
          : "–"}
      </td>
      <td>
        {model.ram_used_mb !== undefined
          ? t("detail.ramMb", { value: formatInt(model.ram_used_mb, locale) })
          : "–"}
      </td>
      <td>
        {model.backend !== undefined
          ? t(`backend.${model.backend}`, { defaultValue: model.backend })
          : "–"}
      </td>
      <td>{model.port ?? "–"}</td>
      <td className="resident">
        <ResidentControls
          model={model.name}
          resident={resident}
          disabled={residentDisabled}
          onChange={onResident}
        />
      </td>
      <td className="row-actions">
        {model.loaded ? (
          <button type="button" disabled={disabled} onClick={() => onAction("models.unload")}>
            {t("detail.unload")}
          </button>
        ) : (
          <button type="button" disabled={disabled} onClick={() => onAction("models.load")}>
            {t("detail.load")}
          </button>
        )}
        {model.own !== false && (
          <button
            type="button"
            className="danger"
            disabled={disabled || model.loaded}
            aria-label={t("detail.deleteAria", { model: model.name })}
            onClick={() => onAction("models.delete")}
          >
            {t("detail.deleteModel")}
          </button>
        )}
      </td>
    </tr>
  );
}

/**
 * "Automatisch bereithalten" toggle and priority of one model. Toggling on adds the model to
 * config.residents with the defaults; the priority is saved when the field loses focus.
 */
function ResidentControls({
  model,
  resident,
  disabled,
  onChange,
}: {
  model: string;
  resident: Resident | undefined;
  disabled: boolean;
  onChange: (entry: ResidentUpdate | null) => void;
}) {
  const { t } = useTranslation();
  const priority = resident?.priority ?? DEFAULT_RESIDENT_PRIORITY;
  const [text, setText] = useState(String(priority));
  const [shown, setShown] = useState(priority);
  if (shown !== priority) {
    // The stored priority changed (saved here or elsewhere): show it.
    setShown(priority);
    setText(String(priority));
  }

  function commit() {
    const n = Number(text);
    if (!resident || !Number.isInteger(n) || n < 0 || n > 100) {
      setText(String(priority));
      return;
    }
    if (n === priority) return;
    const entry: ResidentUpdate = { priority: n, auto_reload: resident.auto_reload ?? true };
    if (resident.context_length !== undefined) entry.context_length = resident.context_length;
    onChange(entry);
  }

  return (
    <>
      {/* the label is the larger touch target around the box; the name is the input's own */}
      <label className="resident-toggle">
        <input
          type="checkbox"
          checked={resident !== undefined}
          disabled={disabled}
          aria-label={t("detail.residentToggle", { model })}
          onChange={(e) => onChange(e.target.checked ? {} : null)}
        />
      </label>
      {resident && (
        <input
          type="number"
          className="priority"
          min={0}
          max={100}
          step={1}
          value={text}
          disabled={disabled}
          title={t("detail.residentPriorityHint")}
          aria-label={t("detail.residentPriority", { model })}
          onChange={(e) => setText(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") e.currentTarget.blur();
          }}
        />
      )}
      {resident?.auto_reload === false && (
        <span className="muted small resident-paused">{t("detail.residentPaused")}</span>
      )}
    </>
  );
}

/** "Model – 10 Kerne (2 P + 8 E), 12 Threads"; plain "Model (8 Kerne)" without topology. */
function CpuText({ cpu }: { cpu: CpuInfo | null }) {
  const { t } = useTranslation();
  if (!cpu) return <>{t("node.unknown")}</>;
  const hybrid = (cpu.efficiency_cores ?? 0) > 0 && cpu.performance_cores !== undefined;
  if (!hybrid && cpu.threads === undefined) {
    return <>{t("node.cpuValue", { model: cpu.model, cores: cpu.cores })}</>;
  }
  let topology = t("node.cpuCores", { cores: cpu.cores });
  if (hybrid) {
    topology += ` ${t("node.cpuHybrid", { p: cpu.performance_cores, e: cpu.efficiency_cores })}`;
  }
  if (cpu.threads !== undefined) topology += `, ${t("node.cpuThreads", { threads: cpu.threads })}`;
  return <>{t("node.cpuDetail", { model: cpu.model, topology })}</>;
}

function EventList({ events }: { events: NodeEvent[] }) {
  const { t, i18n } = useTranslation();
  if (events.length === 0) return <p className="muted">{t("detail.noEvents")}</p>;
  return (
    <ol className="events">
      {events.map((e) => (
        <li key={e.id}>
          <time dateTime={e.ts}>{formatDateTime(e.ts, i18n.language)}</time>
          <span className="event-kind">{t(`eventKind.${e.kind}`, { defaultValue: e.kind })}</span>
          <EventDetail event={e} />
          {Object.keys(e.data).length > 0 && (
            <code className="event-data">{JSON.stringify(e.data)}</code>
          )}
        </li>
      ))}
    </ol>
  );
}

/** German summary of an event's reason / source, if the catalogue knows it. */
function EventDetail({ event }: { event: NodeEvent }) {
  const { t, i18n } = useTranslation();
  const key = eventDetailKey(event);
  let text = key && i18n.exists(key) ? t(key) : null;
  if (event.kind === "auto_reload_paused" && typeof event.data.until === "string") {
    text = t("detail.pausedUntil", { time: formatDateTime(event.data.until, i18n.language) });
  }
  // What went wrong (or was tried instead) is the point of these events: show the text.
  const why = loadEventText(event);
  if (why) text = why;
  if (event.kind === "model_load_cancelled" && typeof event.data.reason === "string") {
    const model = typeof event.data.model === "string" ? `${event.data.model}: ` : "";
    text = model + t(`cancelReason.${event.data.reason}`, { defaultValue: event.data.reason });
  }
  if ((event.kind === "paused" || event.kind === "resumed") && Array.isArray(event.data.reasons)) {
    const reasons = event.data.reasons
      .filter((r): r is string => typeof r === "string")
      .map((r) => t(`pauseReason.${r}`, { defaultValue: r }));
    if (reasons.length > 0) text = reasons.join(", ");
  }
  const signal = event.data.signal;
  if (text && typeof signal === "string" && i18n.exists(`eventSignal.${signal}`)) {
    text += ` (${t(`eventSignal.${signal}`)})`;
  }
  return text ? <span className="event-detail">{text}</span> : null;
}
