import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatDateTime } from "../format";
import type { ApiKey } from "../types";
import { askConfirm } from "./askConfirm";

interface Props {
  api: ApiClient;
  /** Origin clients use; defaults to window.location.origin. */
  origin?: string;
}

/** Gateway base URL and the signed-in user's API keys (E1-05). */
export function ApiAccess({ api, origin = window.location.origin }: Props) {
  const { t, i18n } = useTranslation();
  const [keys, setKeys] = useState<ApiKey[] | null>(null);
  const [name, setName] = useState("");
  const [created, setCreated] = useState<ApiKey | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  const fail = useCallback(
    (err: unknown) => {
      if (!(err instanceof UnauthorizedError)) setError(describeError(toActionError(err), t, i18n));
    },
    [t, i18n],
  );

  const reload = useCallback(() => {
    api.listKeys().then(setKeys, fail);
  }, [api, fail]);

  useEffect(reload, [reload]);

  async function create(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setCreated(await api.createKey(name.trim()));
      setCopied(false);
      setName("");
      reload();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  async function revoke(key: ApiKey) {
    if (!await askConfirm(t("access.confirmRevoke", { name: key.name }))) return;
    setError(null);
    try {
      await api.revokeKey(key.id);
      if (created?.id === key.id) setCreated(null);
      reload();
    } catch (err) {
      fail(err);
    }
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  const baseUrl = `${origin}/v1`;
  const locale = i18n.language;
  return (
    <section>
      <h1>{t("access.heading")}</h1>
      <p>{t("access.intro")}</p>
      <p>
        <span className="fact-label">{t("access.baseUrl")}:</span>{" "}
        <code data-testid="base-url">{baseUrl}</code>
      </p>
      <p className="muted small">{t("access.modelHint")}</p>
      <pre className="code-sample">{`curl ${baseUrl}/chat/completions \\
  -H "Authorization: Bearer <API key>" -H "Content-Type: application/json" \\
  -d '{"model": "<model>", "stream": true, "messages": [{"role": "user", "content": "Hallo"}]}'`}</pre>

      <h2>{t("access.keys")}</h2>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {created?.key && (
        <div className="notice new-key" role="status">
          <p>{t("access.createdOnce")}</p>
          <div className="command">
            <code data-testid="new-key">{created.key}</code>
            <button type="button" onClick={() => void copy(created.key ?? "")}>
              {copied ? t("pairing.copied") : t("pairing.copy")}
            </button>
          </div>
        </div>
      )}
      {keys === null ? (
        <p className="muted">{t("app.loading")}</p>
      ) : keys.length === 0 ? (
        <p className="muted">{t("access.noKeys")}</p>
      ) : (
        <div className="table-wrap">
          <table className="models">
            <thead>
              <tr>
                <th>{t("access.colName")}</th>
                <th>{t("access.colKey")}</th>
                <th>{t("access.colCreated")}</th>
                <th>{t("access.colLastUsed")}</th>
                <th>{t("detail.colAction")}</th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.id}>
                  <td>{k.name}</td>
                  <td>
                    <code>{k.prefix}…</code>
                  </td>
                  <td>{formatDateTime(k.created_at, locale) ?? "–"}</td>
                  <td>{formatDateTime(k.last_used_at, locale) ?? t("node.never")}</td>
                  <td className="row-actions">
                    <button
                      type="button"
                      className="danger"
                      aria-label={t("access.revokeKey", { name: k.name })}
                      onClick={() => void revoke(k)}
                    >
                      {t("access.revoke")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <form className="form key-form" onSubmit={(e) => void create(e)}>
        <label>
          <span>{t("access.newKeyName")}</span>
          <input value={name} maxLength={100} onChange={(e) => setName(e.target.value)} />
        </label>
        <div>
          <button type="submit" className="primary" disabled={busy || !name.trim()}>
            {t("access.create")}
          </button>
        </div>
      </form>
    </section>
  );
}
