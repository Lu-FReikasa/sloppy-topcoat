import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatDateTime, formatInt } from "../format";
import { capabilityStatus, inputTemplate, outputText, parseInput, toggleGrant } from "../tools";
import type {
  GrantRow,
  NodeCapabilities,
  NodeDetail,
  ToolCallRow,
  ToolResult,
  UserRow,
} from "../types";

interface Props {
  api: ApiClient;
  node: NodeDetail;
}

/**
 * What a tool node offers (E2-04): the capabilities with their state for the signed-in user, a
 * form to run one (read-only; every call is logged), the recent calls and — for an admin — who may
 * run what on this node (D20).
 */
export function ToolsSection({ api, node }: Props) {
  const { t, i18n } = useTranslation();
  const [caps, setCaps] = useState<NodeCapabilities | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [calls, setCalls] = useState<ToolCallRow[]>([]);

  const load = useCallback(() => {
    api.nodeCapabilities(node.id).then(
      (c) => {
        setCaps(c);
        setError(null);
      },
      (err: unknown) => {
        if (err instanceof UnauthorizedError) return;
        setError(describeError(toActionError(err), t, i18n));
      },
    );
    api.toolCalls(node.id).then(setCalls, () => undefined);
  }, [api, node.id, t, i18n]);

  // reload when the node connects (it sends its capabilities with its inventory)
  useEffect(() => {
    load();
  }, [load, node.connected]);

  if (error) {
    return (
      <>
        <h2>{t("tools.heading")}</h2>
        <p className="error" role="alert">
          {error}
        </p>
      </>
    );
  }
  if (!caps) return null;
  const offline = !node.connected || node.state === "offline";

  return (
    <>
      <h2>{t("tools.heading")}</h2>
      <p className="muted small">{t("tools.hint")}</p>
      {caps.offered.length === 0 ? (
        <p className="muted">{t("tools.noneOffered")}</p>
      ) : (
        <div className="table-wrap">
          <table className="models capabilities">
            <thead>
              <tr>
                <th>{t("tools.colName")}</th>
                <th>{t("tools.colDescription")}</th>
                <th>{t("tools.colState")}</th>
              </tr>
            </thead>
            <tbody>
              {caps.offered.map((c) => (
                <tr key={c.name}>
                  <td className="model-name">{c.name}</td>
                  <td>{c.description}</td>
                  <td>{t(`tools.status.${capabilityStatus(c, caps.enabled, caps.usable)}`)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <RunForm api={api} node={node} caps={caps} offline={offline} onDone={load} />
      <RecentCalls calls={calls} locale={i18n.language} />
      <Grants api={api} node={node} caps={caps} />
    </>
  );
}

function RunForm({
  api,
  node,
  caps,
  offline,
  onDone,
}: {
  api: ApiClient;
  node: NodeDetail;
  caps: NodeCapabilities;
  offline: boolean;
  onDone: () => void;
}) {
  const { t, i18n } = useTranslation();
  const runnable = caps.offered.filter((c) => caps.usable.includes(c.name));
  const [name, setName] = useState("");
  const [text, setText] = useState("{}");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ToolResult | null>(null);
  const [inputError, setInputError] = useState(false);

  if (runnable.length === 0) return <p className="muted">{t("tools.nothingToRun")}</p>;
  const current = runnable.find((c) => c.name === name) ?? runnable[0]!;

  function choose(next: string) {
    setName(next);
    setText(inputTemplate(runnable.find((c) => c.name === next)));
    setResult(null);
    setInputError(false);
  }

  async function run() {
    const input = parseInput(text);
    if (input === null) {
      setInputError(true);
      return;
    }
    setInputError(false);
    setBusy(true);
    setResult(null);
    try {
      setResult(await api.runCapability(node.id, current.name, input));
      onDone();
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) throw err;
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="run-capability">
      <h3>{t("tools.runHeading")}</h3>
      <label>
        {t("tools.capability")}{" "}
        <select value={current.name} onChange={(e) => choose(e.target.value)} disabled={busy}>
          {runnable.map((c) => (
            <option key={c.name} value={c.name}>
              {c.name}
            </option>
          ))}
        </select>
      </label>
      <textarea
        aria-label={t("tools.input")}
        value={text}
        spellCheck={false}
        rows={Math.min(10, text.split("\n").length + 1)}
        onChange={(e) => setText(e.target.value)}
      />
      {inputError && (
        <p className="error" role="alert">
          {t("tools.inputInvalid")}
        </p>
      )}
      {offline && <p className="muted">{t("tools.offlineHint")}</p>}
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          disabled={busy || offline}
          onClick={() => void run()}
        >
          {t("tools.run")}
        </button>
      </div>
      {result && !result.ok && (
        <p className="error" role="alert">
          {result.error
            ? t(`tools.error.${result.error.code}`, {
                defaultValue: result.error.code,
              }) + (result.error.message ? `: ${result.error.message}` : "")
            : t("node.unknown")}
        </p>
      )}
      {result?.ok && (
        <details open className="tool-result">
          <summary>
            {t("tools.result", {
              bytes: formatInt(result.bytes ?? 0, i18n.language),
              ms: formatInt(result.duration_ms ?? 0, i18n.language),
            })}
          </summary>
          <pre>{outputText(result.output)}</pre>
        </details>
      )}
    </div>
  );
}

function RecentCalls({ calls, locale }: { calls: ToolCallRow[]; locale: string }) {
  const { t } = useTranslation();
  if (calls.length === 0) return null;
  return (
    <>
      <h3>{t("tools.callsHeading")}</h3>
      <ol className="events tool-calls">
        {calls.map((c) => (
          <li key={c.id}>
            {c.created_at && (
              <time dateTime={c.created_at}>{formatDateTime(c.created_at, locale)}</time>
            )}
            <span className="event-kind">{c.capability}</span>
            <span className={c.ok ? "event-detail" : "event-detail error"}>
              {c.ok
                ? t("tools.callOk", { bytes: formatInt(c.output_bytes, locale) })
                : t(`tools.error.${c.code ?? "error"}`, { defaultValue: c.code ?? "error" })}
            </span>
            {c.user && <span className="muted"> {c.user}</span>}
            <details>
              <summary>{t("tools.callDetails")}</summary>
              <code className="event-data">{JSON.stringify(c.input)}</code>
              {c.error && <p className="muted">{c.error}</p>}
              {c.output_excerpt && <pre>{c.output_excerpt}</pre>}
            </details>
          </li>
        ))}
      </ol>
    </>
  );
}

/** Who may run what on this node (admin only: the users list refuses everybody else). */
function Grants({ api, node, caps }: { api: ApiClient; node: NodeDetail; caps: NodeCapabilities }) {
  const { t, i18n } = useTranslation();
  const [users, setUsers] = useState<UserRow[] | null>(null);
  const [rows, setRows] = useState<Record<string, string[]>>({});
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.listUsers(), api.nodeGrants(node.id)]).then(
      ([u, g]: [UserRow[], GrantRow[]]) => {
        if (cancelled) return;
        setUsers(u.filter((x) => x.role !== "admin"));
        setRows(Object.fromEntries(g.map((x) => [x.user_id, x.capabilities])));
      },
      () => undefined, // not an admin: no grants section
    );
    return () => {
      cancelled = true;
    };
  }, [api, node.id]);

  if (users === null) return null;
  const names = caps.offered.filter((c) => c.read_only).map((c) => c.name);

  async function save(userId: string) {
    setError(null);
    try {
      await api.putGrants(node.id, userId, rows[userId] ?? []);
      setSaved(userId);
    } catch (err) {
      if (err instanceof UnauthorizedError) return;
      setError(describeError(toActionError(err), t, i18n));
    }
  }

  return (
    <>
      <h3>{t("tools.grantsHeading")}</h3>
      <p className="muted small">{t("tools.grantsHint")}</p>
      {users.length === 0 ? (
        <p className="muted">{t("tools.noUsers")}</p>
      ) : (
        <div className="table-wrap">
          <table className="models grants">
            <thead>
              <tr>
                <th>{t("tools.colUser")}</th>
                <th>{t("tools.grantAll")}</th>
                {names.map((n) => (
                  <th key={n}>{n}</th>
                ))}
                <th />
              </tr>
            </thead>
            <tbody>
              {users.map((u) => {
                const mine = rows[u.id] ?? [];
                const toggle = (name: string) => {
                  setSaved(null);
                  setRows({ ...rows, [u.id]: toggleGrant(mine, name) });
                };
                return (
                  <tr key={u.id}>
                    <td className="model-name">{u.name}</td>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={t("tools.grantAllFor", { user: u.name })}
                        checked={mine.includes("*")}
                        onChange={() => toggle("*")}
                      />
                    </td>
                    {names.map((n) => (
                      <td key={n}>
                        <input
                          type="checkbox"
                          aria-label={t("tools.grantFor", { user: u.name, capability: n })}
                          checked={mine.includes("*") || mine.includes(n)}
                          disabled={mine.includes("*")}
                          onChange={() => toggle(n)}
                        />
                      </td>
                    ))}
                    <td>
                      <button type="button" onClick={() => void save(u.id)}>
                        {saved === u.id ? t("tools.grantsSaved") : t("tools.grantsSave")}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </>
  );
}
