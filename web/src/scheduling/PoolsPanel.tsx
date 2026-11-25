import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { DEFAULT_RESIDENT_PRIORITY } from "../pressure";
import type { PoolsView } from "../types";

const KINDS = ["chat", "embedding", "rerank", "tts", "stt", "vision"];
/** The label of the option that shows what the hub found out about the kind (not set by hand). */
const KIND_SOURCE_LABEL: Record<string, string> = {
  detected: "pools.kindDetected",
  proven: "pools.kindProven",
  gguf: "pools.kindGguf",
};

/** Per node, which models programs may use (PRG-00 R1). All switched on = no pool for that node.
 *  Also the default of the pool, how many models a node holds at once and the kinds set by hand. */
export function PoolsPanel({ api }: { api: ApiClient }) {
  const { t, i18n } = useTranslation();
  const [view, setView] = useState<PoolsView | null>(null);
  // node -> models switched on; only filled for nodes the admin touched
  const [draft, setDraft] = useState<Record<string, Set<string>>>({});
  // node -> text of the "most models at once" field ("" = no limit); model -> kind set by hand
  // ("" = detected from the name); both only for what the admin touched
  const [limits, setLimits] = useState<Record<string, string>>({});
  const [kinds, setKinds] = useState<Record<string, string>>({});
  // node -> house model ("" = none) and node -> worker, only what the admin touched
  const [house, setHouse] = useState<Record<string, string>>({});
  const [workers, setWorkers] = useState<Record<string, boolean>>({});
  // the house order the admin set by "First" (null: unchanged; the hub keeps the stored one)
  const [order, setOrder] = useState<string[] | null>(null);
  // the tags the list is filtered by (a model shows when it has all of them)
  const [filter, setFilter] = useState<string[]>([]);
  const [state, setState] = useState<"idle" | "saved" | string>("idle");
  const [probing, setProbing] = useState<string | null>(null); // "node/model" being tested

  const load = useCallback(() => {
    api.getPools().then(
      (v) => {
        setView(v);
        setDraft({});
        setLimits({});
        setKinds({});
        setHouse({});
        setWorkers({});
        setOrder(null);
      },
      (err: unknown) => setState(describeError(toActionError(err), t, i18n)),
    );
  }, [api, t, i18n]);
  useEffect(load, [load]);

  if (!view) return null;

  const current = (node: string): Set<string> =>
    draft[node] ??
    new Set(
      view.nodes
        .find((n) => n.node === node)
        ?.models.filter((m) => m.available)
        .map((m) => m.name),
    );


  const toggle = (node: string, model: string) => {
    const next = new Set(current(node));
    if (next.has(model)) next.delete(model);
    else next.add(model);
    setDraft({ ...draft, [node]: next });
  };

  const houseOf = (node: string): string =>
    house[node] ?? view.nodes.find((n) => n.node === node)?.house ?? "";
  // the nodes with a house model in the house order: the stored ranks, or the order set by "First"
  const houseOrder = (): string[] =>
    order ??
    view.nodes
      .filter((n) => n.house_rank != null)
      .sort((a, b) => (a.house_rank ?? 0) - (b.house_rank ?? 0))
      .map((n) => n.node);
  const rankOf = (node: string): number | null => {
    const i = houseOrder().indexOf(node);
    return i < 0 ? null : i + 1;
  };
  const putFirst = (node: string) => setOrder([node, ...houseOrder().filter((n) => n !== node)]);
  const workerOf = (node: string): boolean =>
    workers[node] ?? view.nodes.find((n) => n.node === node)?.worker ?? false;
  const allTags = [...new Set(view.nodes.flatMap((n) => n.models.flatMap((m) => m.tags ?? [])))].sort();
  const shown = (tags: string[] | undefined) => filter.every((f) => (tags ?? []).includes(f));

  const limitOf = (node: string): string =>
    limits[node] ?? String(view.nodes.find((n) => n.node === node)?.max_loaded ?? "");

  const proofText = (value: boolean | null): string =>
    t(value === null ? "pools.proofUnknown" : value ? "pools.proofYes" : "pools.proofNo");

  async function probe(node: string, model: string) {
    setProbing(`${node}/${model}`);
    try {
      await api.runModelProbes(model, node);
      load();
      setState("idle");
    } catch (err) {
      setState(describeError(toActionError(err), t, i18n));
    } finally {
      setProbing(null);
    }
  }

  async function save() {
    // every model on (or none on) is no pool: the node offers all of its models
    const pools: Record<string, string[]> = {};
    for (const n of view?.nodes ?? []) {
      const on = current(n.node);
      const all = n.models.map((m) => m.name);
      if (on.size > 0 && on.size < all.length) pools[n.node] = all.filter((m) => on.has(m));
    }
    try {
      let next = await api.putPools(pools);
      if (Object.keys(limits).length > 0) {
        const maxLoaded: Record<string, number> = {};
        for (const n of next.nodes) {
          const text = limitOf(n.node).trim();
          const count = Number(text);
          if (text !== "" && Number.isInteger(count) && count >= 1) maxLoaded[n.node] = count;
        }
        next = await api.putMaxLoaded(maxLoaded);
      }
      if (Object.keys(kinds).length > 0) {
        // what was set by hand stays, what the admin touched is replaced (or cleared)
        const byHand: Record<string, string[]> = {};
        for (const n of next.nodes)
          for (const m of n.models) if (m.kinds_source === "manual") byHand[m.name] = m.kinds;
        for (const [model, kind] of Object.entries(kinds)) {
          if (kind) byHand[model] = [kind];
          else delete byHand[model];
        }
        next = await api.putModelKinds(byHand);
      }
      if (Object.keys(house).length > 0 || Object.keys(workers).length > 0 || order) {
        const chosen: Record<string, string> = {};
        const working: string[] = [];
        for (const n of next.nodes) {
          if (houseOf(n.node)) chosen[n.node] = houseOf(n.node);
          if (workerOf(n.node)) working.push(n.node);
        }
        // the order names only nodes that keep a house model; new ones follow on the hub
        const ranked = order ? order.filter((n) => chosen[n]) : undefined;
        next = await api.putHouse(chosen, working, ranked);
        // the house model stays loaded: it is the node's resident; the one it replaces is not
        for (const n of view?.nodes ?? []) {
          const before = n.house ?? "";
          const after = houseOf(n.node);
          if (!n.node_id || before === after) continue;
          if (before) await api.deleteResident(n.node_id, before);
          if (after)
            await api.putResident(n.node_id, after, {
              priority: DEFAULT_RESIDENT_PRIORITY,
              auto_reload: true,
            });
        }
      }
      setView(next);
      setDraft({});
      setLimits({});
      setKinds({});
      setHouse({});
      setWorkers({});
      setState("saved");
    } catch (err) {
      setState(describeError(toActionError(err), t, i18n));
    }
  }

  return (
    <section className="card" data-testid="pools">
      <h2>{t("pools.title")}</h2>
      <p className="muted small">{t("pools.hint")}</p>
      <p className="muted small">{t("pools.houseHint")}</p>
      {allTags.length > 0 && (
        <div className="tag-filter" role="group" aria-label={t("pools.tagFilter")}>
          {allTags.map((tag) => (
            <button
              key={tag}
              type="button"
              className={filter.includes(tag) ? "tag-chip on" : "tag-chip"}
              aria-pressed={filter.includes(tag)}
              onClick={() =>
                setFilter(filter.includes(tag) ? filter.filter((f) => f !== tag) : [...filter, tag])
              }
            >
              {tag}
            </button>
          ))}
        </div>
      )}
      {view.nodes.map((n) => {
        const on = current(n.node);
        const total = n.models.length;
        const everyModel = on.size === 0 || on.size === total;
        return (
          <div key={n.node} data-testid={`pool-${n.node}`}>
            <h3>
              {n.node}
              {!n.connected && <span className="muted small"> ({t("pools.disconnected")})</span>}
            </h3>
            {total === 0 ? (
              <p className="muted small">{t("pools.noModels")}</p>
            ) : (
              <>
                <p className="muted small">
                  {everyModel ? t("pools.everyModel") : t("pools.poolOf", { count: on.size, total })}
                </p>
                <label className="form-grid">
                  <span>{t("pools.house")}</span>
                  <select
                    value={houseOf(n.node)}
                    onChange={(e) => setHouse({ ...house, [n.node]: e.target.value })}
                    aria-label={`${n.node}: ${t("pools.house")}`}
                  >
                    <option value="">{t("pools.houseNone")}</option>
                    {n.models
                      .filter((m) => m.kinds.includes("chat"))
                      .map((m) => (
                        <option key={m.name} value={m.name}>
                          {m.name}
                        </option>
                      ))}
                  </select>
                </label>
                {houseOf(n.node) && n.house && (
                  <p className="muted small">
                    {rankOf(n.node) != null && t("pools.houseRank", { rank: rankOf(n.node) })}{" "}
                    {rankOf(n.node) !== 1 && (
                      <button
                        type="button"
                        onClick={() => putFirst(n.node)}
                        aria-label={t("pools.houseFirstLabel", { node: n.node })}
                      >
                        {t("pools.houseFirst")}
                      </button>
                    )}
                  </p>
                )}
                <label>
                  <input
                    type="checkbox"
                    checked={workerOf(n.node)}
                    onChange={(e) => setWorkers({ ...workers, [n.node]: e.target.checked })}
                    aria-label={`${n.node}: ${t("pools.worker")}`}
                  />{" "}
                  {t("pools.worker")}
                </label>
                <label className="form-grid">
                  <span>{t("pools.maxLoaded")}</span>
                  <input
                    type="number"
                    min={1}
                    placeholder={t("pools.noLimit")}
                    value={limitOf(n.node)}
                    onChange={(e) => setLimits({ ...limits, [n.node]: e.target.value })}
                    aria-label={`${n.node}: ${t("pools.maxLoaded")}`}
                  />
                </label>
                <div className="actions">
                  <button
                    type="button"
                    onClick={() => setDraft({ ...draft, [n.node]: new Set(n.models.map((m) => m.name)) })}
                  >
                    {t("pools.allOn")}
                  </button>
                  <button type="button" onClick={() => setDraft({ ...draft, [n.node]: new Set() })}>
                    {t("pools.allOff")}
                  </button>
                </div>
                <ul className="plain">
                  {n.models.filter((m) => shown(m.tags)).map((m) => (
                    <li key={m.name}>
                      <label>
                        <input
                          type="checkbox"
                          checked={on.has(m.name)}
                          aria-label={`${n.node}: ${m.name} ${t("pools.available")}`}
                          onChange={() => toggle(n.node, m.name)}
                        />{" "}
                        {m.name}
                      </label>{" "}
                      <select
                        value={kinds[m.name] ?? (m.kinds_source === "manual" ? (m.kinds[0] ?? "") : "")}
                        onChange={(e) => setKinds({ ...kinds, [m.name]: e.target.value })}
                        aria-label={`${m.name}: ${t("pools.kindLabel")}`}
                      >
                        <option value="">
                          {t(KIND_SOURCE_LABEL[m.kinds_source] ?? "pools.kindDetected", {
                            kinds: m.kinds.map((k) => t(`pools.kind.${k}`, { defaultValue: k })).join(", "),
                          })}
                        </option>
                        {KINDS.map((k) => (
                          <option key={k} value={k}>
                            {t(`pools.kind.${k}`, { defaultValue: k })}
                          </option>
                        ))}
                      </select>{" "}
                      {n.connected && (
                        <button
                          type="button"
                          title={t("pools.probeHint")}
                          disabled={probing === `${n.node}/${m.name}`}
                          onClick={() => void probe(n.node, m.name)}
                          aria-label={`${n.node}: ${m.name} ${t("pools.probe")}`}
                        >
                          {probing === `${n.node}/${m.name}` ? t("pools.probing") : t("pools.probe")}
                        </button>
                      )}{" "}
                      {m.capabilities && (
                        <span className="muted small" data-testid={`proof-${m.name}`}>
                          {t("pools.proof", {
                            tools: proofText(m.capabilities.tools),
                            embedding: proofText(m.capabilities.embedding),
                          })}
                        </span>
                      )}{" "}
                      <span className="muted small">
                        {m.loaded && ` · ${t("pools.loaded")}`}
                        {m.size_mb != null && ` · ${(m.size_mb / 1024).toFixed(1)} GB`}
                        {m.tps != null && ` · ${t("pools.speed", { tps: m.tps })}`}
                      </span>
                      {(m.tags ?? []).length > 0 && (
                        <span className="tag-row" data-testid={`tags-${n.node}-${m.name}`}>
                          {(m.tags ?? []).map((tag) => (
                            <span key={tag} className="tag-chip small">
                              {tag}
                            </span>
                          ))}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        );
      })}
      <div className="actions">
        <button type="button" className="primary" onClick={() => void save()}>
          {t("pools.save")}
        </button>
        {state === "saved" && (
          <span className="muted small" role="status">
            {t("pools.saved")}
          </span>
        )}
      </div>
      {state !== "idle" && state !== "saved" && (
        <p className="bad" role="alert">
          {state}
        </p>
      )}
    </section>
  );
}
