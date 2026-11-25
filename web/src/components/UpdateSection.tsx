import { useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type { NodeSummary } from "../types";
import { updateState } from "../update";
import { askConfirm } from "./askConfirm";

interface Props {
  api: ApiClient;
  node: NodeSummary;
  /** The node's desired config (to allow node.update); null when not loaded. */
  config: Record<string, unknown> | null;
  onChanged: () => void;
}

/**
 * Update card of a node (E2-06, D50): installed and available version, what is missing for an
 * update, and the button. The hub only offers builds that were signed on the release machine;
 * the node and its updater verify the signature again, so this card cannot install anything else.
 */
export function UpdateSection({ api, node, config, onChanged }: Props) {
  const { t, i18n } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const u = node.update;
  const state = updateState(node);
  const offline = !node.connected || node.state === "offline";

  async function run(job: () => Promise<string | null>) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      setNotice(await job());
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) {
        setError(describeError(toActionError(err), t, i18n));
      }
    } finally {
      setBusy(false);
    }
  }

  async function start() {
    if (!u?.available || !await askConfirm(t("update.confirm", { node: node.name, version: u.available }))) {
      return;
    }
    void run(async () => {
      const res = await api.runAction(node.id, "node.update", {});
      if (!res.ok) throw res.error ?? { code: "unknown", message: "" };
      onChanged();
      return t("update.started");
    });
  }

  function allow() {
    if (!config) return;
    const actions = Array.isArray(config.allowed_actions) ? (config.allowed_actions as string[]) : [];
    void run(async () => {
      await api.putNodeConfig(node.id, { ...config, allowed_actions: [...actions, "node.update"] });
      onChanged();
      return t("update.allowed");
    });
  }

  if (state === "unknown") return null;
  return (
    <>
      <h2>{t("update.heading")}</h2>
      <dl className="facts" data-testid="update-facts">
        <dt>{t("update.installed")}</dt>
        <dd>{node.agent_version ?? t("node.unknown")}</dd>
        <dt>{t("update.channel")}</dt>
        <dd>{u?.channel}</dd>
        <dt>{t("update.available")}</dt>
        <dd>{u?.available ?? t("update.none")}</dd>
        {u?.updater_version && (
          <>
            <dt>{t("update.updaterVersion")}</dt>
            <dd>{u.updater_version}</dd>
          </>
        )}
      </dl>
      <p className="muted" data-testid="update-state">
        {t(`update.state.${state}`, { version: u?.available ?? "" })}
      </p>
      {u?.updater_behind && (
        <p className="warn-text" data-testid="update-updater-behind">
          {t("update.updaterBehind", {
            updater: u.updater_version ?? "",
            node: node.agent_version ?? "",
          })}
        </p>
      )}
      {state === "available" && (
        <div className="power-actions">
          <button type="button" disabled={busy || offline} onClick={start}>
            {t("update.run", { version: u?.available ?? "" })}
          </button>
        </div>
      )}
      {state === "not_allowed" && config && (
        <div className="power-actions">
          <button type="button" disabled={busy} onClick={allow}>
            {t("update.allow")}
          </button>
        </div>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </>
  );
}
