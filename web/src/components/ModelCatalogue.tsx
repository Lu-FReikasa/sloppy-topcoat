import { Fragment, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { bytesToGb, formatDecimal } from "../format";
import { modelHref } from "../route";
import type { Backend, NodeState, NodeSummary } from "../types";
import { ModelNodes } from "./ModelNodes";

/** One model on one node. */
export interface Placement {
  nodeId: string;
  nodeName: string;
  state: NodeState;
  connected: boolean;
  loaded: boolean;
  vramUsedMb: number | null;
  backend?: Backend;
}

/** Compute nodes, the ones that can hold models: by the node's role ("gpu"), else by whether it
 *  may load models at all. Tool nodes (tools only) never count or show up on the models page. */
export function isComputeNode(n: NodeSummary): boolean {
  return n.roles && n.roles.length > 0
    ? n.roles.includes("gpu")
    : !n.allowed_actions || n.allowed_actions.includes("models.load");
}

export function modelNodes(nodes: NodeSummary[]): NodeSummary[] {
  return nodes.filter(isComputeNode);
}

/** One model name across all nodes. */
export interface CatalogueEntry {
  name: string;
  sizeBytes?: number;
  placements: Placement[];
}

/** Group the models of all nodes by name; sorted by model, then node name. */
export function buildCatalogue(nodes: NodeSummary[]): CatalogueEntry[] {
  const byName = new Map<string, CatalogueEntry>();
  for (const node of nodes) {
    for (const m of node.models) {
      let entry = byName.get(m.name);
      if (!entry) {
        entry = { name: m.name, sizeBytes: m.size_bytes, placements: [] };
        byName.set(m.name, entry);
      }
      entry.sizeBytes ??= m.size_bytes;
      entry.placements.push({
        nodeId: node.id,
        nodeName: node.name,
        state: node.state,
        connected: node.connected,
        loaded: m.loaded,
        vramUsedMb: m.vram_used_mb,
        backend: m.backend,
      });
    }
  }
  const entries = [...byName.values()];
  for (const e of entries) e.placements.sort((a, b) => a.nodeName.localeCompare(b.nodeName));
  return entries.sort((a, b) => a.name.localeCompare(b.name));
}

interface Props {
  api: ApiClient;
  nodes: NodeSummary[];
  loaded: boolean;
  error: string | null;
  onChanged?: () => void;
}

export function ModelCatalogue({ api, nodes, loaded, error, onChanged }: Props) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const [open, setOpen] = useState<Set<string>>(new Set());
  const holders = new Set(modelNodes(nodes).map((n) => n.id));
  const total = holders.size;
  // only compute nodes count: tool nodes (alpha, ...) hold no models and are not listed
  const entries = buildCatalogue(nodes.filter((n) => holders.has(n.id)));
  const loadedCount = entries.reduce((n, e) => n + e.placements.filter((p) => p.loaded).length, 0);

  function toggle(name: string) {
    setOpen((cur) => {
      const next = new Set(cur);
      if (!next.delete(name)) next.add(name);
      return next;
    });
  }

  return (
    <section>
      <h1>{t("catalogue.heading")}</h1>
      {error && (
        <p className="error" role="alert">
          {t("overview.loadError", { message: error })}
        </p>
      )}
      {!loaded && !error && <p className="muted">{t("app.loading")}</p>}
      {loaded && entries.length === 0 && <p className="empty">{t("catalogue.empty")}</p>}
      {entries.length > 0 && (
        <>
          <p className="muted" data-testid="catalogue-summary">
            {t("catalogue.summary", {
              models: entries.length,
              nodes: total,
              loaded: loadedCount,
            })}
          </p>
          <div className="table-wrap">
            <table className="models catalogue">
              <thead>
                <tr>
                  <th>{t("catalogue.model")}</th>
                  <th>{t("catalogue.size")}</th>
                  <th>{t("catalogue.storedOn")}</th>
                  <th>{t("catalogue.loadedOn")}</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e) => {
                  const here = e.placements;
                  const expanded = open.has(e.name);
                  return (
                    <Fragment key={e.name}>
                      <tr data-testid={`catalogue-row-${e.name}`}>
                        <td className="model-name">
                          <button
                            type="button"
                            className="link"
                            aria-expanded={expanded}
                            aria-label={t(expanded ? "catalogue.collapse" : "catalogue.expand", {
                              model: e.name,
                            })}
                            onClick={() => toggle(e.name)}
                          >
                            <span aria-hidden="true">{expanded ? "▾" : "▸"}</span> {e.name}
                          </button>{" "}
                          <a
                            className="small"
                            href={modelHref(e.name)}
                            aria-label={t("catalogue.openPage", { model: e.name })}
                          >
                            {t("catalogue.page")}
                          </a>
                        </td>
                        <td>
                          {e.sizeBytes !== undefined
                            ? t("detail.sizeGb", {
                                value: formatDecimal(bytesToGb(e.sizeBytes), locale),
                              })
                            : "–"}
                        </td>
                        <td data-testid="catalogue-stored">
                          {t("catalogue.countOf", { count: here.length, total })}
                        </td>
                        <td data-testid="catalogue-loaded">
                          {t("catalogue.countOf", {
                            count: here.filter((p) => p.loaded).length,
                            total,
                          })}
                        </td>
                      </tr>
                      {expanded && (
                        <tr data-testid={`catalogue-detail-${e.name}`}>
                          <td colSpan={4}>
                            <ModelNodes api={api} name={e.name} nodes={nodes} onChanged={onChanged} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
