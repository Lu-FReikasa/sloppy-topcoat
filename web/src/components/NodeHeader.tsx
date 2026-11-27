import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { NodeActions } from "../data/useNodeActions";
import { describeError } from "../errors";
import { formatDecimal, formatInt, mbToGb } from "../format";
import { nodeGlance } from "../summaries";
import type { NodeDetail, NodeSummary } from "../types";
import { askConfirm } from "./askConfirm";
import { diskLow, useDiskText, useRamText } from "./NodeFacts";
import { StateBadge } from "./StateBadge";

/** The installed agent version as the node reported it, and the newer release the hub holds. The
 *  string is shown as it is: no parsing, no shortening (issue #175: "0.2.18" must not turn into
 *  "0.2.8"). */
export function nodeVersion(node: Pick<NodeSummary, "agent_version" | "update">): {
  installed: string | null;
  newer: string | null;
} {
  const installed = node.agent_version?.trim() || null;
  const u = node.update;
  return { installed, newer: u?.newer && u.available ? u.available : null };
}

/** May the page offer waking (offline, the hub knows a network card) and sleeping (online, the
 *  node allows node.sleep)? */
export function powerOffers(node: NodeDetail): { canWake: boolean; canSleep: boolean } {
  const offline = !node.connected || node.state === "offline";
  const allowed = node.config?.allowed_actions;
  return {
    canWake: offline && (node.network?.length ?? 0) > 0,
    canSleep: !offline && Array.isArray(allowed) && allowed.includes("node.sleep"),
  };
}

/**
 * Top of the node page (issue #175): online state, what is loaded, the load of the machine
 * (RAM, VRAM, CPU), the agent version with a hint when an update is there, and the actions a
 * person reaches for most: wake, sleep, load a model. The rest of the page is the body.
 */
export function NodeHeader({
  node,
  actions,
  isTools,
}: {
  node: NodeDetail;
  actions: NodeActions;
  /** A tool or dev node holds no models: no loaded line, no load control. */
  isTools: boolean;
}) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const ram = useRamText(node);
  const disk = useDiskText(node);
  const { pending } = actions;
  const offline = !node.connected || node.state === "offline";
  const { canWake, canSleep } = powerOffers(node);
  const glance = nodeGlance(node);
  const gb = (mb: number) => formatDecimal(mbToGb(mb), locale);
  const version = nodeVersion(node);
  const actionError = actions.error ? describeError(actions.error, t, i18n) : null;
  const notice = actions.notice ? t(`power.${actions.notice}`) : null;

  // an offline node loads nothing: no picker then (the body says actions are not possible)
  const loadable = isTools || offline ? [] : node.models.filter((m) => !m.loaded).map((m) => m.name);
  const [choice, setChoice] = useState("");
  const pick = loadable.includes(choice) ? choice : (loadable[0] ?? "");

  async function sleepNow() {
    if (!(await askConfirm(t("power.confirmSleep", { node: node.name })))) return;
    await actions.sleep();
  }

  return (
    <header className="node-header" data-testid="node-header">
      <div className="detail-head">
        <h1>{node.name}</h1>
        <StateBadge state={node.state} />
      </div>
      <dl className="facts" data-testid="node-glance">
        <dt>{t("node.ram")}</dt>
        <dd>{offline ? "–" : ram}</dd>
        {glance.live && typeof node.cpu_util === "number" && (
          <>
            <dt>{t("node.cpu")}</dt>
            <dd data-testid="glance-cpu">
              {t("nodeHeader.cpuLoad", { value: formatInt(Math.round(node.cpu_util), locale) })}
            </dd>
          </>
        )}
        {glance.gpus.length > 0 && (
          <>
            <dt>{t("detail.glanceGpu")}</dt>
            <dd data-testid="glance-gpus">
              {glance.gpus.map((g, i) => (
                <span className="fact-item" key={i}>
                  {t("detail.glanceGpuValue", {
                    name: g.name,
                    used: g.usedMb === null ? "?" : gb(g.usedMb),
                    total: gb(g.totalMb),
                  }) +
                    (g.shared
                      ? `, ${t("detail.glanceGpuShared", {
                          used: g.shared.usedMb === null ? "?" : gb(g.shared.usedMb),
                          total: gb(g.shared.totalMb),
                        })}`
                      : "")}
                </span>
              ))}
            </dd>
          </>
        )}
        {!isTools && glance.live && (
          <>
            <dt>{t("detail.glanceLoaded")}</dt>
            <dd data-testid="glance-loaded">
              {glance.loaded.length === 0
                ? t("detail.glanceNone")
                : glance.loaded.map((m) => (
                    <span className="fact-item" key={m.name}>
                      {m.vramMb === null
                        ? m.name
                        : t("detail.glanceLoadedValue", { name: m.name, vram: gb(m.vramMb) })}
                    </span>
                  ))}
            </dd>
          </>
        )}
        {disk && diskLow(node) && (
          <>
            <dt>{t("node.disk")}</dt>
            <dd data-testid="node-disk" className="warn-text">
              {disk} – {t("node.diskLow")}
            </dd>
          </>
        )}
      </dl>
      <p className="node-header-version muted small" data-testid="node-version">
        {t("nodeHeader.version", { version: version.installed ?? t("node.unknown") })}
        {/* a space, so a screen reader does not read "0.2.18Update" */}
        {version.newer && " "}
        {version.newer && (
          <span className="badge node-header-update" data-testid="node-update-hint">
            {t("nodeHeader.updateAvailable", { version: version.newer })}
          </span>
        )}
      </p>
      {(canWake || canSleep || loadable.length > 0) && (
        <div className="power-actions node-header-actions">
          {canWake && (
            <button type="button" disabled={pending !== null} onClick={() => void actions.wake()}>
              {t("power.wake")}
            </button>
          )}
          {canSleep && (
            <button type="button" disabled={pending !== null} onClick={() => void sleepNow()}>
              {t("power.sleepNow")}
            </button>
          )}
          {loadable.length > 0 && (
            <span className="node-header-load">
              <select
                aria-label={t("nodeHeader.loadPick")}
                value={pick}
                disabled={pending !== null}
                onChange={(e) => setChoice(e.target.value)}
              >
                {loadable.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                disabled={pending !== null || pick === ""}
                onClick={() => void actions.runModelAction("models.load", pick)}
              >
                {t("nodeHeader.load")}
              </button>
            </span>
          )}
        </div>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {actionError && (
        <p className="error" role="alert">
          {actionError}
        </p>
      )}
    </header>
  );
}
