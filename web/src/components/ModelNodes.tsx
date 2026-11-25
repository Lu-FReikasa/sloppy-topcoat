import { useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatInt } from "../format";
import { nodeHref } from "../route";
import type { ModelInfo, NodeSummary } from "../types";
import { modelNodes } from "./ModelCatalogue";
import { StateBadge } from "./StateBadge";
import { TransferDialog } from "./TransferDialog";
import { TransferList } from "./TransferList";
import { askConfirm } from "./askConfirm";

type ModelAction = "models.load" | "models.unload" | "models.delete";

type DialogState = { node: NodeSummary; mode: "to" | "from"; move: boolean; model: ModelInfo };

interface Props {
  api: ApiClient;
  name: string;
  nodes: NodeSummary[];
  /** Called after a successful action (the live stream also updates the list). */
  onChanged?: () => void;
  /** Called after a transfer was created (the detail page refreshes its transfer list). */
  onTransferCreated?: () => void;
}

/** The nodes that can hold one model, with stored/loaded state and the actions per node. Used
 *  on the model page and unfolded in the model list. */
export function ModelNodes({ api, name, nodes, onChanged, onTransferCreated }: Props) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const [pending, setPending] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<DialogState | null>(null);
  // a copy started here, where no page lists the transfers (the model list): its state shows below
  const [started, setStarted] = useState(0);

  const rows = modelNodes(nodes)
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name));
  const infoOf = (n: NodeSummary) => n.models.find((m) => m.name === name);
  const anyInfo = nodes.flatMap((n) => n.models).find((m) => m.name === name);

  async function run(node: NodeSummary, action: ModelAction) {
    if (action === "models.delete" && !await askConfirm(t("detail.confirmDelete", { model: name }))) {
      return;
    }
    setPending(node.id);
    setActionError(null);
    try {
      const args: Record<string, unknown> = { model: name };
      if (action === "models.delete") args.confirm = true;
      const res = await api.runAction(node.id, action, args);
      if (!res.ok) {
        const message = describeError(res.error ?? { code: "unknown", message: "" }, t, i18n);
        setActionError(t("catalogue.actionError", { node: node.name, model: name, message }));
      } else {
        onChanged?.();
      }
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) {
        const message = describeError(toActionError(err), t, i18n);
        setActionError(t("catalogue.actionError", { node: node.name, model: name, message }));
      }
    } finally {
      setPending(null);
    }
  }


  if (!anyInfo) return null;

  return (
    <div data-testid={`model-nodes-${name}`}>
      {actionError && (
        <p className="error" role="alert">
          {actionError}
        </p>
      )}
      {rows.length === 0 ? (
        <p className="muted">{t("model.noNodes")}</p>
      ) : (
        <div className="table-wrap">
          <table className="models">
            <thead>
              <tr>
                <th>{t("model.colNode")}</th>
                <th>{t("model.colState")}</th>
                <th>{t("model.colStored")}</th>
                <th>{t("model.colLoaded")}</th>
                <th>{t("model.colActions")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((n) => {
                const info = infoOf(n);
                const offline = !n.connected || n.state === "offline";
                const disabled = offline || pending !== null;
                const label = { model: name, node: n.name };
                return (
                  <tr key={n.id} data-testid={`model-row-${n.id}`}>
                    <td className="model-name">
                      <a href={nodeHref(n.id)}>{n.name}</a>
                    </td>
                    <td>
                      <StateBadge state={n.state} />
                    </td>
                    <td data-testid="model-stored">
                      <span
                        role="img"
                        aria-label={t(info ? "model.stored" : "model.notStored")}
                      >
                        {info ? "✓" : "–"}
                      </span>
                    </td>
                    <td data-testid="model-loaded">
                      {info?.loaded ? (
                        <span role="img" aria-label={t("model.loadedMark")}>
                          ✓{" "}
                          {info.vram_used_mb !== null && (
                            <span className="muted">
                              {t("detail.vramMb", { value: formatInt(info.vram_used_mb, locale) })}
                            </span>
                          )}
                        </span>
                      ) : (
                        <span role="img" aria-label={t("model.notLoadedMark")}>
                          –
                        </span>
                      )}
                    </td>
                    <td className="row-actions">
                      {offline && <span className="muted small">{t("catalogue.offline")} </span>}
                      {info ? (
                        <>
                          <button
                            type="button"
                            disabled={disabled}
                            aria-label={t(info.loaded ? "catalogue.unloadOn" : "catalogue.loadOn", label)}
                            onClick={() => void run(n, info.loaded ? "models.unload" : "models.load")}
                          >
                            {info.loaded ? t("detail.unload") : t("detail.load")}
                          </button>
                          <button
                            type="button"
                            disabled={disabled}
                            aria-label={t("model.copyFrom", label)}
                            onClick={() => setDialog({ node: n, mode: "to", move: false, model: info })}
                          >
                            {t("transfer.copy")}
                          </button>
                          {info.own !== false && (
                            <>
                              <button
                                type="button"
                                disabled={disabled || info.loaded}
                                aria-label={t("model.moveFrom", label)}
                                onClick={() =>
                                  setDialog({ node: n, mode: "to", move: true, model: info })
                                }
                              >
                                {t("transfer.move")}
                              </button>
                              <button
                                type="button"
                                className="danger"
                                disabled={disabled || info.loaded}
                                aria-label={t("model.deleteFrom", label)}
                                onClick={() => void run(n, "models.delete")}
                              >
                                {t("detail.delete")}
                              </button>
                            </>
                          )}
                        </>
                      ) : (
                        <button
                          type="button"
                          disabled={disabled}
                          aria-label={t("model.copyHereAria", label)}
                          onClick={() =>
                            setDialog({ node: n, mode: "from", move: false, model: anyInfo })
                          }
                        >
                          {t("model.copyHere")}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {dialog && (
        <TransferDialog
          api={api}
          node={dialog.node}
          mode={dialog.mode}
          model={dialog.model}
          move={dialog.move}
          onClose={() => setDialog(null)}
          onCreated={() => {
            if (onTransferCreated) onTransferCreated();
            else setStarted((n) => n + 1);
          }}
        />
      )}
      {!onTransferCreated && started > 0 && (
        <TransferList api={api} model={name} events={[]} refreshKey={started} />
      )}
    </div>
  );
}
