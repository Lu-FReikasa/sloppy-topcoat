import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { ApiError, type ApiClient } from "../api/client";
import { askConfirm } from "./askConfirm";

const NAME = /^[a-z0-9][a-z0-9-]{0,62}$/;

/** Rename a node (admin): the hub rewrites the living settings to the new name, keeps the
 *  history, and the old name still leads to the node. A name like the machine's host name lets
 *  the person and the assistant mean the same computer. */
export function RenameNode({
  api,
  nodeId,
  name,
  onRenamed,
}: {
  api: ApiClient;
  nodeId: string;
  name: string;
  onRenamed: () => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState(name);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const wanted = value.trim();
  const valid = NAME.test(wanted) && wanted !== name;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!valid || !await askConfirm(t("rename.confirm", { from: name, to: wanted }))) return;
    setBusy(true);
    setMessage(null);
    try {
      await api.renameNode(nodeId, wanted);
      setMessage({ ok: true, text: t("rename.done", { from: name, to: wanted }) });
      setOpen(false);
      onRenamed();
    } catch (err) {
      setMessage({ ok: false, text: err instanceof ApiError ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section data-testid="rename-node">
      <h2>{t("rename.heading")}</h2>
      {!open && (
        <div className="power-actions">
          <button type="button" onClick={() => setOpen(true)}>
            {t("rename.open")}
          </button>
        </div>
      )}
      {open && (
        <form onSubmit={submit}>
          <label>
            {t("rename.label")}
            <input
              value={value}
              onChange={(e) => setValue(e.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </label>
          <p className="muted small">{t("rename.hint")}</p>
          {wanted && !NAME.test(wanted) && (
            <p className="warn-text">{t("rename.invalid")}</p>
          )}
          <div className="power-actions">
            <button type="submit" disabled={!valid || busy}>
              {t("rename.submit")}
            </button>
            <button type="button" onClick={() => setOpen(false)} disabled={busy}>
              {t("rename.cancel")}
            </button>
          </div>
        </form>
      )}
      {message && (
        <p className={message.ok ? "muted" : "error"} role={message.ok ? "status" : "alert"}>
          {message.text}
        </p>
      )}
    </section>
  );
}
