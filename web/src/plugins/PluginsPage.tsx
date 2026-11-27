import { useEffect, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { ApiError, type ApiClient } from "../api/client";
import type { HubEvents } from "../data/hubEvents";
import { usePlugin, usePlugins } from "../data/usePlugins";
import { useResource } from "../data/useResource";
import type { PluginDetail, PluginRow } from "./types";

/** What the hub said is wrong: its list of problems, else its message. */
export function problemsOf(err: unknown): string[] {
  if (err instanceof ApiError) {
    const listed = err.detail?.problems;
    if (Array.isArray(listed) && listed.length > 0) return listed.map(String);
    return [err.message];
  }
  return [err instanceof Error ? err.message : String(err)];
}

/** The manifest typed or loaded into the form: its id, or the reason it cannot be sent. */
export function parseManifest(
  text: string,
):
  | { manifest: Record<string, unknown>; id: string }
  | { error: "json" | "id" } {
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    return { error: "json" };
  }
  if (!doc || typeof doc !== "object" || Array.isArray(doc))
    return { error: "json" };
  const id = (doc as Record<string, unknown>).id;
  if (typeof id !== "string" || !id) return { error: "id" };
  return { manifest: doc as Record<string, unknown>, id };
}

/** The class of one line of a unified diff. */
export function diffLineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---"))
    return "plugin-diff-head";
  if (line.startsWith("@@")) return "plugin-diff-hunk";
  if (line.startsWith("+")) return "plugin-diff-add";
  if (line.startsWith("-")) return "plugin-diff-del";
  return "";
}

/** How many characters of a version's hash the table shows (the whole one is its title). */
const HASH_SHOWN = 12;

type PluginState = "builtin" | "enabled" | "disabled";

const STATE_KEY: Record<PluginState, string> = {
  builtin: "plugins.builtin",
  enabled: "plugins.enabled",
  disabled: "plugins.disabled",
};

function stateOf(r: PluginRow): PluginState {
  if (r.builtin) return "builtin";
  return r.enabled ? "enabled" : "disabled";
}

function when(iso: string | null, lang: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(lang);
}

interface Props {
  api: ApiClient;
  /** The stream bus; the app's by default, a fake one in tests. */
  events?: HubEvents;
}

/** Administration: the installed data plug-ins (D105). Install or update by a manifest, look at
 *  the versions and how they differ, roll back, switch off, remove. Every change reaches open
 *  pages at once (the hub publishes it on the stream). */
export function PluginsPage({ api, events }: Props) {
  const { t, i18n } = useTranslation();
  const list = usePlugins(api, events);
  const [picked, setPicked] = useState<string | null>(null);
  const rows = list.data ?? [];
  // a plug-in removed meanwhile (here or by another admin) is no longer picked
  const selected =
    picked !== null && rows.some((r) => r.key === picked) ? picked : null;
  const detail = usePlugin(api, selected, events);
  const [text, setText] = useState("");
  const [problems, setProblems] = useState<string[]>([]);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null);
  // the answer to an action is shown where it was asked: at the list or at the form
  const [where, setWhere] = useState<"list" | "form">("list");

  const title = (r: { key: string; title_key: string | null }): string =>
    r.title_key && i18n.exists(r.title_key) ? t(r.title_key) : r.key;

  async function run(
    what: () => Promise<unknown>,
    done: string,
    at: "list" | "form" = "list",
  ): Promise<boolean> {
    setWhere(at);
    setBusy(true);
    setNote(null);
    setProblems([]);
    try {
      await what();
      setNote(done);
      await Promise.all([list.reload(), detail.reload()]);
      return true;
    } catch (err) {
      setProblems(problemsOf(err));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function submit(e: FormEvent): Promise<void> {
    e.preventDefault();
    const parsed = parseManifest(text);
    if ("error" in parsed) {
      setWhere("form");
      setNote(null);
      setProblems([
        t(parsed.error === "json" ? "plugins.notJson" : "plugins.noId"),
      ]);
      return;
    }
    const installed = rows.some((r) => r.key === parsed.id);
    const ok = await run(
      () =>
        installed
          ? api.updatePlugin(parsed.id, parsed.manifest)
          : api.installPlugin(parsed.manifest),
      t(installed ? "plugins.updated" : "plugins.installed", {
        key: parsed.id,
      }),
      "form",
    );
    if (ok) {
      setText("");
      setPicked(parsed.id);
    }
  }

  async function loadFile(file: File | undefined): Promise<void> {
    if (!file) return;
    setText(await file.text());
  }

  async function remove(key: string): Promise<void> {
    if (await run(() => api.removePlugin(key), t("plugins.removed", { key })))
      setConfirmRemove(null);
  }

  const feedback = (
    <>
      {note && (
        <p className="muted" role="status">
          {note}
        </p>
      )}
      {problems.length > 0 && (
        <div className="error" role="alert" data-testid="plugin-problems">
          <p>{t("plugins.refused")}</p>
          <ul>
            {problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </div>
      )}
    </>
  );

  return (
    <section data-testid="plugins" className="plugins-page">
      <h2>{t("plugins.title")}</h2>
      <p className="muted small">{t("plugins.hint")}</p>
      {list.error && (
        <p className="error" role="alert">
          {list.error.message}
        </p>
      )}
      <div className="table-wrap">
        <table className="ws-admin-table">
          <thead>
            <tr>
              <th>{t("plugins.name")}</th>
              <th className="plugin-col">{t("plugins.version")}</th>
              <th className="plugin-col">{t("plugins.state")}</th>
              <th>
                <span className="sr-only">{t("plugins.actions")}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.key}
                data-testid="plugin-row"
                aria-selected={r.key === selected}
              >
                <td>
                  <button
                    type="button"
                    className="link"
                    onClick={() => setPicked(r.key === selected ? null : r.key)}
                  >
                    {title(r)}
                  </button>
                  {title(r) !== r.key && (
                    <>
                      {" "}
                      <code className="muted small">{r.key}</code>
                    </>
                  )}
                  {/* a narrow page folds the version and state columns into this line */}
                  <span className="plugin-facts muted small">
                    {r.version} · {t(STATE_KEY[stateOf(r)])}
                  </span>
                </td>
                <td className="plugin-col">{r.version}</td>
                <td className="plugin-col">{t(STATE_KEY[stateOf(r)])}</td>
                <td>
                  <div className="plugin-actions">
                    {!r.builtin && (
                      <>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() =>
                            void run(
                              () => api.setPluginEnabled(r.key, !r.enabled),
                              t(
                                r.enabled
                                  ? "plugins.disabledNote"
                                  : "plugins.enabledNote",
                                { key: r.key },
                              ),
                            )
                          }
                        >
                          {r.enabled
                            ? t("plugins.disable")
                            : t("plugins.enable")}
                        </button>
                        <button
                          type="button"
                          className="danger"
                          disabled={busy}
                          onClick={() => setConfirmRemove(r.key)}
                        >
                          {t("plugins.remove")}
                        </button>
                      </>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {confirmRemove && (
        <p
          role="alertdialog"
          aria-label={t("plugins.confirmRemove", { key: confirmRemove })}
        >
          {t("plugins.confirmRemove", { key: confirmRemove })}{" "}
          <button
            type="button"
            className="danger"
            disabled={busy}
            onClick={() => void remove(confirmRemove)}
          >
            {t("plugins.remove")}
          </button>{" "}
          <button type="button" onClick={() => setConfirmRemove(null)}>
            {t("plugins.cancel")}
          </button>
        </p>
      )}
      {where === "list" && feedback}
      {selected && detail.data && (
        <PluginVersions
          api={api}
          detail={detail.data}
          busy={busy}
          events={events}
          title={title(detail.data)}
          onRollback={(v) =>
            void run(
              () => api.rollbackPlugin(detail.data!.key, v),
              t("plugins.rolledBack", { key: detail.data!.key, version: v }),
            )
          }
          lang={i18n.language}
        />
      )}
      <form className="plugin-install" onSubmit={(e) => void submit(e)}>
        <h3>{t("plugins.installTitle")}</h3>
        <p className="muted small">{t("plugins.installHint")}</p>
        <label>
          {t("plugins.manifest")}
          <textarea
            value={text}
            rows={10}
            spellCheck={false}
            onChange={(e) => setText(e.target.value)}
            placeholder='{"id": "…", "version": "1.0.0", "title_key": "…"}'
          />
        </label>
        <div className="plugin-install-row">
          <label>
            {t("plugins.fromFile")}
            <input
              type="file"
              accept="application/json,.json"
              onChange={(e) => void loadFile(e.target.files?.[0])}
            />
          </label>
          <button
            type="submit"
            className="primary"
            disabled={busy || !text.trim()}
          >
            {t("plugins.installOrUpdate")}
          </button>
        </div>
        {where === "form" && feedback}
      </form>
    </section>
  );
}

function PluginVersions({
  api,
  detail,
  busy,
  events,
  title,
  onRollback,
  lang,
}: {
  api: ApiClient;
  detail: PluginDetail;
  busy: boolean;
  events?: HubEvents;
  title: string;
  onRollback: (version: string) => void;
  lang: string;
}) {
  const { t } = useTranslation();
  // compared: an earlier or later version against the one in force
  const [other, setOther] = useState<string | null>(null);
  const compared =
    other && detail.versions.some((v) => v.version === other) ? other : null;
  const [a, b] = compared ? ordered(detail, compared) : [null, null];
  // the versions open under the whole list: bring them into view and move focus to their
  // heading, or a click on a plug-in shows nothing new on a long list (UI review 2, #174)
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    const el = heading.current;
    if (!el) return;
    el.focus({ preventScroll: true });
    el.scrollIntoView?.({ block: "start" });
  }, [detail.key]);
  const diff = useResource(
    `plugin-diff:${detail.key}:${a ?? ""}:${b ?? ""}`,
    async () => (a && b ? (await api.pluginDiff(detail.key, a, b)).lines : []),
    { events },
  );

  return (
    <section
      className="plugin-detail"
      data-testid="plugin-detail"
      aria-label={title}
    >
      <h3 ref={heading} tabIndex={-1}>
        {t("plugins.versionsOf", { name: title })}
      </h3>
      <ul className="plugin-versions">
        {detail.versions.map((v) => {
          const current = v.version === detail.version;
          return (
            <li key={v.version} data-testid="plugin-version">
              <div>
                <strong>{v.version}</strong> {current && t("plugins.inForce")}{" "}
                <code
                  className="small muted"
                  title={v.sha256}
                  aria-label={`${t("plugins.hash")} ${v.sha256}`}
                >
                  {v.sha256.slice(0, HASH_SHOWN)}
                </code>
                <br />
                <span className="small">
                  {t("plugins.installedBy")}:{" "}
                  {v.installed_by ?? t("plugins.byHub")}
                  {v.installed_at && (
                    <span className="muted">
                      , {when(v.installed_at, lang)}
                    </span>
                  )}
                </span>
              </div>
              {!current && (
                <div className="plugin-actions">
                  <button
                    type="button"
                    aria-pressed={compared === v.version}
                    onClick={() =>
                      setOther(compared === v.version ? null : v.version)
                    }
                  >
                    {t("plugins.compare")}
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onRollback(v.version)}
                  >
                    {t("plugins.rollBack")}
                  </button>
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {a && b && (
        <figure className="plugin-diff-box">
          <figcaption className="small">
            {t("plugins.diffOf", { a, b })}
          </figcaption>
          {diff.data && diff.data.length === 0 && !diff.loading ? (
            <p className="muted small">{t("plugins.same")}</p>
          ) : (
            <pre className="plugin-diff" data-testid="plugin-diff">
              {(diff.data ?? []).map((line, i) => (
                <span key={i} className={diffLineClass(line)}>
                  {line}
                  {"\n"}
                </span>
              ))}
            </pre>
          )}
        </figure>
      )}
      {detail.changes.length > 0 && (
        <>
          <h4>{t("plugins.changes")}</h4>
          <ul className="small plugin-changes">
            {detail.changes.map((c, i) => (
              <li key={i}>
                {when(c.at, lang)}:{" "}
                {t(`plugins.action.${c.action}`, {
                  from: c.from_version ?? "",
                  to: c.to_version ?? "",
                })}
                {" · "}
                {c.by ?? t("plugins.byHub")}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

/** The two versions of a diff, older first. */
function ordered(detail: PluginDetail, other: string): [string, string] {
  const index = (v: string) =>
    detail.versions.findIndex((x) => x.version === v);
  // versions are newest first: a higher index is older
  return index(other) > index(detail.version)
    ? [other, detail.version]
    : [detail.version, other];
}
