import { useState } from "react";
import { useTranslation } from "react-i18next";
import { ApiError, UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";

interface Props {
  api: ApiClient;
  nodeId: string;
  config: Record<string, unknown> | null;
  onSaved: () => void;
}

/** Raw JSON editor for a node's config; the hub validates it against the schema. */
export function ConfigEditor({ api, nodeId, config, onSaved }: Props) {
  const { t, i18n } = useTranslation();
  const [text, setText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  function open() {
    setText(JSON.stringify(config ?? {}, null, 2));
    setError(null);
    setSaved(false);
  }

  async function save() {
    if (text === null) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch (err) {
      setError(t("config.invalidJson", { message: err instanceof Error ? err.message : "" }));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.putNodeConfig(nodeId, parsed);
      setText(null);
      setSaved(true);
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

  if (text === null) {
    return (
      <>
        <button type="button" onClick={open} disabled={config === null}>
          {t("config.edit")}
        </button>
        {saved && (
          <p className="notice" role="status">
            {t("config.saved")}
          </p>
        )}
      </>
    );
  }

  return (
    <div className="config-editor">
      <p className="muted small">{t("config.hintBackend")}</p>
      <p className="muted small">
        {t("config.hintWatcher")} <code>{'{"process": "spiel.exe"}'}</code>{" "}
        {t("config.hintOr")}{" "}
        <code>{'{"path_prefix": "D:\\\\SteamLibrary\\\\steamapps\\\\common\\\\"}'}</code>
      </p>
      <textarea
        aria-label={t("config.label")}
        value={text}
        spellCheck={false}
        rows={Math.min(30, text.split("\n").length + 1)}
        onChange={(e) => setText(e.target.value)}
      />
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="dialog-actions">
        <button type="button" onClick={() => setText(null)} disabled={busy}>
          {t("dialog.cancel")}
        </button>
        <button type="button" className="primary" onClick={() => void save()} disabled={busy}>
          {t("config.save")}
        </button>
      </div>
    </div>
  );
}
