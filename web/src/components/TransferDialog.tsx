import { useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatBytes } from "../format";
import type { ModelInfo, ModelTransfer, NodeSummary } from "../types";
import { Dialog } from "./Dialog";

/** The target needs the file size plus this much free space (node: size + 1 GiB). */
const DISK_RESERVE_BYTES = 1024 ** 3;

export type SourceProblem = "offline" | "noAction" | null;

/** Why a node cannot send a model right now (null = it can). */
export function sourceProblem(node: NodeSummary): SourceProblem {
  if (!node.connected || node.state === "offline") return "offline";
  if (node.allowed_actions && !node.allowed_actions.includes("models.send")) return "noAction";
  return null;
}

export type TargetProblem = "offline" | "noAction" | "noSpace" | null;

/** Why a node cannot be the target of a transfer of `sizeBytes` bytes (null = it can). */
export function targetProblem(node: NodeSummary, sizeBytes: number | undefined): TargetProblem {
  if (!node.connected || node.state === "offline") return "offline";
  if (node.allowed_actions && !node.allowed_actions.includes("models.receive")) return "noAction";
  if (sizeBytes !== undefined && node.disk_free_mb != null) {
    if (node.disk_free_mb * 1024 * 1024 < sizeBytes + DISK_RESERVE_BYTES) return "noSpace";
  }
  return null;
}

/**
 * Copy or move one model. `mode "to"`: `node` is the fixed SOURCE and the target is picked.
 * `mode "from"`: `node` is the fixed TARGET and the source is picked among the nodes that
 * store the model (copy only).
 */
export function TransferDialog({
  api,
  node,
  mode = "to",
  model,
  move = false,
  onClose,
  onCreated,
}: {
  api: ApiClient;
  node: NodeSummary;
  mode?: "to" | "from";
  model: ModelInfo;
  move?: boolean;
  onClose: () => void;
  onCreated?: (transfer: ModelTransfer) => void;
}) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const from = mode === "from";
  const doMove = move && !from;
  const [nodes, setNodes] = useState<NodeSummary[] | null>(null);
  const [picked, setPicked] = useState("");
  const [name, setName] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api.listNodes().then(
      (list) => {
        if (!alive) return;
        const others = list.filter((n) => n.id !== node.id);
        const options = from ? others.filter((n) => n.models.some((m) => m.name === model.name)) : others;
        setNodes(options);
        if (from) {
          const usable = options.filter((n) => sourceProblem(n) === null);
          if (options.length === 1 && usable.length === 1) setPicked(usable[0]!.id);
        }
      },
      (err: unknown) => {
        if (alive && !(err instanceof UnauthorizedError)) {
          setNodes([]);
          setError(describeError(toActionError(err), t, i18n));
        }
      },
    );
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, node.id, mode, model.name]);

  const fixedProblem = from ? targetProblem(node, model.size_bytes) : null;
  const valid = picked !== "" && fixedProblem === null && (!doMove || confirmed);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!valid) return;
    setBusy(true);
    setError(null);
    try {
      const body = {
        source_node_id: from ? picked : node.id,
        target_node_id: from ? node.id : picked,
        model: model.name,
        ...(name.trim() ? { target_name: name.trim() } : {}),
        ...(doMove ? { move: true } : {}),
      };
      const created = await api.createModelTransfer(body);
      onCreated?.(created);
      onClose();
      return;
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) {
        const ae = toActionError(err);
        // "busy" is also used by other hub calls with their own message; here it means a transfer.
        setError(ae.code === "busy" ? t("transfer.errorCode.busy") : describeError(ae, t, i18n));
      }
    }
    setBusy(false);
  }

  return (
    <Dialog
      title={t(doMove ? "transfer.moveTitle" : "transfer.copyTitle", { model: model.name })}
      onClose={onClose}
    >
      <form className="form" onSubmit={(e) => void submit(e)}>
        <p className="muted small">
          {t(from ? "transfer.toNode" : "transfer.from", { node: node.name })}
          {model.size_bytes !== undefined && ` – ${formatBytes(model.size_bytes, locale)}`}
        </p>
        {fixedProblem && (
          <p className="error small" role="alert">
            {t("transfer.fixedTargetProblem", { problem: t(`transfer.problem.${fixedProblem}`) })}
          </p>
        )}
        <fieldset className="transfer-targets">
          <legend>{t(from ? "transfer.source" : "transfer.target")}</legend>
          {nodes === null && <p className="muted">{t("transfer.loading")}</p>}
          {nodes !== null && nodes.length === 0 && (
            <p className="muted">{t(from ? "transfer.noSources" : "transfer.noTargets")}</p>
          )}
          {nodes?.map((n) => {
            const problem = from ? sourceProblem(n) : targetProblem(n, model.size_bytes);
            const key = from ? "transfer.sourceProblem" : "transfer.problem";
            const free =
              n.disk_free_mb != null
                ? t("transfer.free", { value: formatBytes(n.disk_free_mb * 1024 * 1024, locale) })
                : t("transfer.freeUnknown");
            return (
              <label key={n.id} className="transfer-target">
                <input
                  type="radio"
                  name="transfer-target"
                  value={n.id}
                  checked={picked === n.id}
                  disabled={problem !== null}
                  onChange={() => setPicked(n.id)}
                />{" "}
                <span className="model-name">{n.name}</span>{" "}
                {!from && <span className="muted small">{free}</span>}{" "}
                <span className={problem ? "error small" : "muted small"}>
                  {problem ? t(`${key}.${problem}`) : from ? "" : t("transfer.fits")}
                </span>
              </label>
            );
          })}
        </fieldset>
        <label>
          {t("transfer.name")}
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder={model.name} />
        </label>
        {doMove && (
          <label className="transfer-confirm">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />{" "}
            {t("transfer.moveConfirm", { model: model.name, node: node.name })}
          </label>
        )}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="dialog-actions">
          <button type="button" onClick={onClose}>
            {t("dialog.cancel")}
          </button>
          <button type="submit" className="primary" disabled={busy || !valid}>
            {t(doMove ? "transfer.submitMove" : "transfer.submitCopy")}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
