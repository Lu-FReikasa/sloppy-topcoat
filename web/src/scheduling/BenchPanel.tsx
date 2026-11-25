import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type {
  BenchRating,
  BenchRun,
  BenchSuggestion,
  BenchSuite,
  BenchVariant,
  PoolsView,
} from "../types";

const POLL_MS = 3000;

/** Rate the models of the pool with small tests of fixed rules (PRG-00 R6): start a run, watch it,
 *  read the ratings. A rating of an older version of a suite is shown in grey and does not rank. */
export function BenchPanel({ api }: { api: ApiClient }) {
  const { t, i18n } = useTranslation();
  const [suites, setSuites] = useState<BenchSuite[]>([]);
  const [ratings, setRatings] = useState<BenchRating[]>([]);
  const [runs, setRuns] = useState<BenchRun[]>([]);
  const [pools, setPools] = useState<PoolsView | null>(null);
  const [node, setNode] = useState("");
  const [model, setModel] = useState("");
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [variants, setVariants] = useState<BenchVariant[]>([]);
  const [variant, setVariant] = useState("");
  const [suggestion, setSuggestion] = useState<BenchSuggestion | null>(null);

  const fail = useCallback(
    (err: unknown) => setError(describeError(toActionError(err), t, i18n)),
    [t, i18n],
  );

  const refresh = useCallback(() => {
    api.benchRuns().then(setRuns, fail);
    api.benchRatings().then(setRatings, fail);
    api.benchSuggestion().then((s) => setSuggestion(s.suggestion), () => undefined);
  }, [api, fail]);

  useEffect(() => {
    api.benchSuites().then(
      (list) => {
        setSuites(list);
        setChosen(new Set(list.map((s) => s.key)));
      },
      fail,
    );
    api.getPools().then(setPools, fail);
    api.benchVariants().then(setVariants, () => undefined);
    refresh();
  }, [api, fail, refresh]);

  const running = runs.some((r) => r.status === "running");
  useEffect(() => {
    if (!running) return undefined;
    const id = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(id);
  }, [running, refresh]);

  const nodes = (pools?.nodes ?? []).filter(
    (n) => n.connected && n.models.some((m) => m.available),
  );
  const activeNode = node || nodes[0]?.node || "";
  const models = (nodes.find((n) => n.node === activeNode)?.models ?? [])
    .filter((m) => m.available)
    .map((m) => m.name);
  const activeModel = models.includes(model) ? model : (models[0] ?? "");

  async function start() {
    setError(null);
    try {
      await api.startBench(activeModel, activeNode, [...chosen], variant);
      refresh();
    } catch (err) {
      fail(err);
    }
  }

  async function stop(runId: string) {
    try {
      await api.stopBench(runId);
      refresh();
    } catch (err) {
      fail(err);
    }
  }

  // the setting taken from the bench for a model and a kind of work (undefined = the model as is)
  const profileOf = (m: string, fn: string): string | undefined =>
    pools?.nodes.flatMap((n) => n.models).find((x) => x.name === m)?.profile?.[fn];

  async function applySetting(model: string, fn: string, variant: string | null) {
    try {
      setPools(await api.putModelProfile(model, fn, variant));
    } catch (err) {
      fail(err);
    }
  }

  const toggle = (key: string) => {
    const next = new Set(chosen);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    setChosen(next);
  };

  // one row per model and node, one column per suite
  const label = (r: { model: string; node: string; variant: string }) =>
    `${r.model}@${r.node}${r.variant ? ` (${r.variant})` : ""}`;
  const rows = [...new Set(ratings.map(label))].sort();
  const cell = (row: string, suite: string) =>
    ratings.find((r) => label(r) === row && r.suite === suite);
  // the kinds of work a row was rated for (answer, tools ...), with its model and setting
  const kindsOf = (row: string) => {
    const mine = ratings.filter((r) => label(r) === row);
    return {
      model: mine[0]?.model ?? "",
      variant: mine[0]?.variant ?? "",
      functions: [...new Set(mine.map((r) => r.function))].sort(),
    };
  };

  const line = (r: BenchRun) => {
    const names = { model: r.model, node: r.node };
    if (r.status === "running")
      return t("bench.running", {
        ...names,
        suite: r.progress.suite ?? "",
        case: r.progress.case ?? 0,
        of: r.progress.of ?? 0,
      });
    if (r.status === "failed") return t("bench.failed", { ...names, error: r.error ?? "" });
    return t(r.status === "done" ? "bench.done" : "bench.stopped", names);
  };

  return (
    <section className="card" data-testid="bench">
      <h2>{t("bench.title")}</h2>
      <p className="muted small">{t("bench.hint")}</p>
      {nodes.length === 0 ? (
        <p className="muted small">{t("bench.noModels")}</p>
      ) : (
        <>
          <div className="form-grid">
            <label>
              <span>{t("bench.node")}</span>
              <select value={activeNode} onChange={(e) => setNode(e.target.value)}>
                {nodes.map((n) => (
                  <option key={n.node} value={n.node}>
                    {n.node}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>{t("bench.model")}</span>
              <select value={activeModel} onChange={(e) => setModel(e.target.value)}>
                {models.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </label>
            {variants.length > 1 && (
              <label>
                <span>{t("bench.variant")}</span>
                <select value={variant} onChange={(e) => setVariant(e.target.value)}>
                  {variants.map((v) => (
                    <option key={v.key} value={v.key}>
                      {v.key === "" ? t("bench.variantDefault") : v.key}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
          <p>
            <span className="small">{t("bench.suites")}: </span>
            {suites.map((s) => (
              <label key={s.key} className="small">
                <input
                  type="checkbox"
                  checked={chosen.has(s.key)}
                  onChange={() => toggle(s.key)}
                  aria-label={s.title}
                />{" "}
                {s.title} <span className="muted">({t("bench.cases", { count: s.cases })})</span>{" "}
              </label>
            ))}
          </p>
          <div className="actions">
            <button
              type="button"
              className="primary"
              disabled={!activeModel || chosen.size === 0}
              onClick={() => void start()}
            >
              {t("bench.start")}
            </button>
          </div>
        </>
      )}
      {error && (
        <p className="bad" role="alert">
          {error}
        </p>
      )}
      {runs.length > 0 && (
        <ul className="plain" data-testid="bench-runs">
          {runs.map((r) => (
            <li key={r.run_id}>
              {line(r)}{" "}
              {r.status === "running" && (
                <button type="button" onClick={() => void stop(r.run_id)}>
                  {t("bench.stop")}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {suggestion && (
        <p data-testid="bench-suggestion">
          {suggestion.default_score == null
            ? t("bench.suggestUnrated", {
                model: suggestion.model,
                node: suggestion.node,
                score: Math.round(suggestion.score * 100),
                current: suggestion.default_model,
              })
            : t("bench.suggest", {
                model: suggestion.model,
                node: suggestion.node,
                score: Math.round(suggestion.score * 100),
                current: suggestion.default_model,
                currentScore: Math.round(suggestion.default_score * 100),
              })}{" "}
          <span className="muted small">{t("bench.suggestHint")}</span>
        </p>
      )}
      <h3>{t("bench.ratings")}</h3>
      {rows.length === 0 ? (
        <p className="muted small">{t("bench.none")}</p>
      ) : (
        <table className="plain" data-testid="bench-ratings">
          <thead>
            <tr>
              <th />
              {suites.map((s) => (
                <th key={s.key}>{s.title}</th>
              ))}
              <th>{t("bench.setting")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row}>
                <th scope="row">{row}</th>
                {suites.map((s) => {
                  const r = cell(row, s.key);
                  return (
                    <td
                      key={s.key}
                      className={r && !r.current ? "muted" : undefined}
                      title={
                        r
                          ? [
                              !r.current ? t("bench.old") : "",
                              r.failed.length
                                ? t("bench.failedCases", {
                                    cases: r.failed.map((f) => f.case).join(", "),
                                  })
                                : "",
                            ]
                              .filter(Boolean)
                              .join(" · ")
                          : undefined
                      }
                    >
                      {r ? `${Math.round(r.score * 100)} % (${r.passed}/${r.total})` : "–"}
                    </td>
                  );
                })}
                <td>
                  {(() => {
                    const { model: m, variant: v, functions } = kindsOf(row);
                    // a tried setting can be taken for a kind of work; the plain row gives it back
                    return functions.map((fn) => {
                      const inUse = profileOf(m, fn);
                      if (v) {
                        return inUse === v ? (
                          <span key={fn} className="muted small">
                            {t("bench.inUse", { function: fn })}{" "}
                          </span>
                        ) : (
                          <button
                            key={fn}
                            type="button"
                            onClick={() => void applySetting(m, fn, v)}
                            aria-label={`${row}: ${t("bench.useFor", { function: fn })}`}
                          >
                            {t("bench.useFor", { function: fn })}
                          </button>
                        );
                      }
                      return inUse ? (
                        <button
                          key={fn}
                          type="button"
                          onClick={() => void applySetting(m, fn, null)}
                          aria-label={`${row}: ${t("bench.giveBack", { function: fn })}`}
                        >
                          {t("bench.giveBack", { function: fn })}
                        </button>
                      ) : null;
                    });
                  })()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
