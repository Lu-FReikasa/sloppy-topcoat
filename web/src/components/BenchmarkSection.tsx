import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatDateTime, formatDecimal } from "../format";
import { benchmarkFromEvents, bestTgPerModel, rowKey } from "../nodeActivity";
import type { NodeDetail } from "../types";
import { Dialog } from "./Dialog";

interface Props {
  api: ApiClient;
  node: NodeDetail;
}

/** Benchmark results table plus the "run benchmark" dialog (action node.benchmark). */
export function BenchmarkSection({ api, node }: Props) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const [open, setOpen] = useState(false);
  const [now] = useState(() => Date.now());
  const activity = useMemo(() => benchmarkFromEvents(node.events, now), [node.events, now]);
  const best = useMemo(() => bestTgPerModel(node.benchmarks), [node.benchmarks]);

  const offline = !node.connected || node.state === "offline";
  const gaming = node.state === "busy_gaming";
  const noModels = node.models.length === 0;
  const running = activity.running && !offline;
  const blocked = offline || gaming || running || noModels;

  let hint: string | null = null;
  if (offline) hint = t("benchmark.offlineHint");
  else if (gaming) hint = t("benchmark.gamingHint");
  else if (noModels) hint = t("benchmark.noModelsHint");

  return (
    <>
      <div className="section-head">
        <h2>{t("benchmark.heading")}</h2>
        <button type="button" disabled={blocked} onClick={() => setOpen(true)}>
          {t("benchmark.start")}
        </button>
      </div>
      {hint && <p className="muted">{hint}</p>}
      {running && (
        <p className="notice" role="status">
          {t("benchmark.running", { results: activity.results })}
        </p>
      )}
      {activity.stale && <p className="muted">{t("benchmark.stale")}</p>}
      {!activity.running && activity.error !== undefined && (
        <p className="error">
          {t("benchmark.failed", { message: activity.error || t("node.unknown") })}
        </p>
      )}
      {activity.failures.length > 0 && (
        <ul className="error-list" data-testid="benchmark-failures">
          {activity.failures.map((f, i) => (
            <li key={i} className="notice-line">
              {t("benchmark.runFailed", { model: f.model, backend: f.backend, message: f.error })}
            </li>
          ))}
        </ul>
      )}
      {node.benchmarks.length === 0 ? (
        <p className="muted">{t("benchmark.empty")}</p>
      ) : (
        <div className="table-wrap">
          <table className="models benchmarks">
            <thead>
              <tr>
                <th>{t("benchmark.colModel")}</th>
                <th>{t("benchmark.colBackend")}</th>
                <th>{t("benchmark.colThreads")}</th>
                <th>{t("benchmark.colGpuLayers")}</th>
                <th>{t("benchmark.colPp")}</th>
                <th>{t("benchmark.colTg")}</th>
                <th>{t("benchmark.colTime")}</th>
              </tr>
            </thead>
            <tbody>
              {node.benchmarks.map((r) => {
                const key = rowKey(r);
                const isBest = best.has(key);
                return (
                  <tr key={key} className={isBest ? "best" : undefined}>
                    <td className="model-name">{r.model}</td>
                    <td>{t(`backend.${r.backend}`, { defaultValue: r.backend })}</td>
                    <td className="num">{r.threads}</td>
                    <td className="num">{r.n_gpu_layers ?? "–"}</td>
                    <td className="num">{formatDecimal(r.pp_tps, locale)}</td>
                    <td className="num">
                      {formatDecimal(r.tg_tps, locale)}
                      {isBest && (
                        <>
                          {" "}
                          <span
                            className="best-mark"
                            role="img"
                            aria-label={t("benchmark.bestTitle")}
                            title={t("benchmark.bestTitle")}
                          >
                            ★
                          </span>
                        </>
                      )}
                    </td>
                    <td>{formatDateTime(r.ts, locale)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {open && <BenchmarkDialog api={api} node={node} onClose={() => setOpen(false)} />}
    </>
  );
}

function BenchmarkDialog({
  api,
  node,
  onClose,
}: {
  api: ApiClient;
  node: NodeDetail;
  onClose: () => void;
}) {
  const { t, i18n } = useTranslation();
  const [selected, setSelected] = useState<string[]>([]);
  const [quick, setQuick] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function toggle(name: string) {
    setSelected((s) => (s.includes(name) ? s.filter((n) => n !== name) : [...s, name]));
  }

  async function start() {
    setBusy(true);
    setError(null);
    try {
      // Keep the inventory order, not the click order.
      const models = node.models.map((m) => m.name).filter((n) => selected.includes(n));
      const res = await api.runAction(node.id, "node.benchmark", { models, quick });
      if (res.ok) {
        onClose();
        return;
      }
      setError(describeError(res.error ?? { code: "unknown", message: "" }, t, i18n));
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) {
        setError(describeError(toActionError(err), t, i18n));
      }
    }
    setBusy(false);
  }

  return (
    <Dialog title={t("benchmark.dialogTitle")} onClose={onClose}>
      <p>{t("benchmark.dialogIntro")}</p>
      <fieldset className="checks">
        <legend>{t("benchmark.models")}</legend>
        {node.models.map((m) => (
          <label key={m.name}>
            <input
              type="checkbox"
              checked={selected.includes(m.name)}
              onChange={() => toggle(m.name)}
            />
            <span className="model-name">{m.name}</span>
          </label>
        ))}
      </fieldset>
      <label className="check">
        <input type="checkbox" checked={quick} onChange={(e) => setQuick(e.target.checked)} />
        {t("benchmark.quick")}
      </label>
      <p className="muted small">{t("benchmark.quickHint")}</p>
      {selected.length === 0 && <p className="muted small">{t("benchmark.selectOne")}</p>}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="dialog-actions">
        <button type="button" onClick={onClose}>
          {t("dialog.cancel")}
        </button>
        <button
          type="button"
          className="primary"
          disabled={busy || selected.length === 0}
          onClick={() => void start()}
        >
          {t("benchmark.run")}
        </button>
      </div>
    </Dialog>
  );
}
