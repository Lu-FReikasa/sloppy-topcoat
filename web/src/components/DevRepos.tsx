import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatDateTime } from "../format";
import type { DevRepo, DevRepoList } from "../types";
import { CloneLocal, LocalRepoCard, NewLocalRepo } from "./LocalRepos";
import { PushPanel } from "./PushPanel";
import { askConfirm } from "./askConfirm";

interface Props {
  api: ApiClient;
}

/** The repositories of the dev workshop (E6-01, D52, D76), in two plainly separate groups: local
 *  ones, which live in the workshop of a dev node and which the assistant edits and tests, and
 *  external ones, which live elsewhere and which it only reads (a local clone makes one editable).
 *  A token is typed once and never shown again. */
export function DevRepos({ api }: Props) {
  const { t, i18n } = useTranslation();
  const [list, setList] = useState<DevRepoList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const [repo, setRepo] = useState("");
  const [token, setToken] = useState("");
  const [template, setTemplate] = useState("none");
  // the repository whose push panel is open
  const [pushing, setPushing] = useState<string | null>(null);
  // the repository being edited (mode, test command, a new token) and its form
  const [editing, setEditing] = useState<DevRepo | null>(null);
  const [editMode, setEditMode] = useState("read");
  const [editTemplate, setEditTemplate] = useState("none");
  const [editToken, setEditToken] = useState("");

  const fail = useCallback(
    (err: unknown) => {
      if (!(err instanceof UnauthorizedError))
        setError(describeError(toActionError(err), t, i18n));
    },
    [t, i18n],
  );

  const reload = useCallback(() => {
    api.devRepos().then(setList, fail);
  }, [api, fail]);

  useEffect(reload, [reload]);

  async function add(e: FormEvent) {
    e.preventDefault();
    if (!name.trim() || !repo.trim() || !token.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.addDevRepo({
        name: name.trim(),
        repo: repo.trim(),
        token: token.trim(),
        mode: "read", // an external repository is only read (D76): edit a local clone
        test_template: template,
      });
      setName("");
      setRepo("");
      setToken("");
      reload();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  function startEdit(r: DevRepo) {
    setEditing(r);
    setEditMode(r.mode);
    setEditTemplate(r.test_template ?? "none");
    setEditToken("");
  }

  async function saveEdit(e: FormEvent) {
    e.preventDefault();
    if (!editing) return;
    setBusy(true);
    setError(null);
    try {
      await api.editDevRepo(editing.id, {
        mode: editMode,
        test_template: editTemplate,
        ...(editToken.trim() ? { token: editToken.trim() } : {}),
      });
      setEditing(null);
      reload();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  }

  async function refresh(r: DevRepo) {
    setError(null);
    try {
      await api.refreshDevRepo(r.id);
      reload();
    } catch (err) {
      fail(err);
    }
  }

  async function remove(r: DevRepo) {
    if (!await askConfirm(t("repos.confirmRemove", { name: r.name }))) return;
    setError(null);
    try {
      await api.removeDevRepo(r.id);
      reload();
    } catch (err) {
      fail(err);
    }
  }

  const modes = list?.modes ?? ["read", "test-branch"];
  const templates = list?.templates ?? ["none"];
  const locals = (list?.repos ?? []).filter((r) => r.kind === "local");
  const externals = (list?.repos ?? []).filter((r) => r.kind !== "local");
  return (
    <section className="dev-repos">
      <h1>{t("repos.heading")}</h1>
      <p>{t("repos.intro")}</p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {list === null ? (
        <p className="muted">{t("app.loading")}</p>
      ) : (
        <>
          <h2>{t("repos.localHeading")}</h2>
          <p className="muted small">{t("repos.localHint")}</p>
          {locals.length === 0 ? (
            <p className="muted">{t("repos.localNone")}</p>
          ) : (
            <ul className="repo-list" data-testid="local-repos">
              {locals.map((r) => (
                <LocalRepoCard
                  key={r.id}
                  api={api}
                  repo={r}
                  all={list.repos}
                  onChanged={reload}
                  onError={fail}
                />
              ))}
            </ul>
          )}
          <NewLocalRepo
            api={api}
            list={list}
            onCreated={reload}
            onError={fail}
          />
          <h2>{t("repos.externalHeading")}</h2>
          <p className="muted small">{t("repos.externalHint")}</p>
          {externals.length === 0 ? (
            <p className="muted">{t("repos.none")}</p>
          ) : (
            <ul className="repo-list" data-testid="external-repos">
              {externals.map((r) => {
                const st = r.state;
                const checked = formatDateTime(r.checked_at, i18n.language);
                return (
                  <li key={r.id} className="repo-card">
                    <div className="repo-head">
                      <strong>{r.name}</strong>
                      <span className="chip">{t("repos.kindExternal")}</span>
                      <code>{r.full_name}</code>
                      {st?.private && (
                        <span className="chip">{t("repos.private")}</span>
                      )}
                    </div>
                    {(r.clones?.length ?? 0) > 0 && (
                      <p className="small" data-testid="clones">
                        {t("repos.clonesHere", {
                          names: (r.clones ?? [])
                            .map(
                              (id) =>
                                list.repos.find((x) => x.id === id)?.name ?? id,
                            )
                            .join(", "),
                        })}
                      </p>
                    )}
                    {r.mode === "test-branch" && (
                      <p className="muted small">{t("repos.legacy")}</p>
                    )}
                    <dl className="repo-facts">
                      <div>
                        <dt>{t("repos.colBranch")}</dt>
                        <dd>{r.default_branch}</dd>
                      </div>
                      <div>
                        <dt>{t("repos.colMode")}</dt>
                        <dd>
                          {t(`repos.mode${r.mode}`, { defaultValue: r.mode })}
                        </dd>
                      </div>
                      <div>
                        <dt>{t("repos.template")}</dt>
                        <dd>
                          {t(`repos.template${r.test_template ?? "none"}`, {
                            defaultValue: r.test_template ?? "none",
                          })}
                        </dd>
                      </div>
                    </dl>
                    <div className="repo-state">
                      {st?.error ? (
                        <span className="error">
                          {t("repos.failed", {
                            message: st.message ?? st.error,
                          })}
                        </span>
                      ) : (
                        st && (
                          <>
                            {st.open_prs != null && (
                              <div>
                                {t("repos.prs", { count: st.open_prs })}
                              </div>
                            )}
                            <div>
                              {t("repos.branches", { count: st.branches ?? 0 })}
                            </div>
                            {(st.test_branches?.length ?? 0) > 0 && (
                              <div className="small">
                                {t("repos.testBranches", {
                                  names: st.test_branches?.join(", "),
                                })}
                              </div>
                            )}
                          </>
                        )
                      )}
                      {checked && (
                        <div className="muted small">
                          {t("repos.checked", { when: checked })}
                        </div>
                      )}
                    </div>
                    <div className="repo-actions">
                      <CloneLocal
                        api={api}
                        repo={r}
                        list={list}
                        onCloned={reload}
                        onError={fail}
                      />
                      <button type="button" onClick={() => void refresh(r)}>
                        {t("repos.refresh")}
                      </button>
                      {r.mode === "test-branch" && (
                        <button
                          type="button"
                          onClick={() =>
                            setPushing(pushing === r.id ? null : r.id)
                          }
                        >
                          {t("repos.push")}
                        </button>
                      )}
                      <button type="button" onClick={() => startEdit(r)}>
                        {t("repos.edit")}
                      </button>
                      <button type="button" onClick={() => void remove(r)}>
                        {t("repos.remove")}
                      </button>
                    </div>
                    {pushing === r.id && <PushPanel api={api} repo={r} />}
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}

      {editing && (
        <form
          className="form-grid"
          data-testid="repo-edit"
          onSubmit={(e) => void saveEdit(e)}
        >
          <h2>{t("repos.editing", { name: editing.name })}</h2>
          {editing.mode === "test-branch" && (
            // an old registration for work on GitHub branches may still be turned back to read
            <label>
              {t("repos.mode")}
              <select
                value={editMode}
                onChange={(e) => setEditMode(e.target.value)}
              >
                {modes.map((m) => (
                  <option key={m} value={m}>
                    {t(`repos.mode${m}`, { defaultValue: m })}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label>
            {t("repos.template")}
            <select
              value={editTemplate}
              onChange={(e) => setEditTemplate(e.target.value)}
            >
              {templates.map((m) => (
                <option key={m} value={m}>
                  {t(`repos.template${m}`, { defaultValue: m })}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t("repos.newToken")}
            <input
              type="password"
              autoComplete="off"
              value={editToken}
              onChange={(e) => setEditToken(e.target.value)}
            />
          </label>
          <p className="muted small">{t("repos.newTokenHint")}</p>
          <div>
            <button type="submit" className="primary" disabled={busy}>
              {t("repos.save")}
            </button>{" "}
            <button type="button" onClick={() => setEditing(null)}>
              {t("repos.cancel")}
            </button>
          </div>
        </form>
      )}

      <h3>{t("repos.add")}</h3>
      <form className="form-grid" onSubmit={(e) => void add(e)}>
        <label>
          {t("repos.name")}
          <input
            type="text"
            value={name}
            placeholder={t("repos.namePh")}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label>
          {t("repos.repo")}
          <input
            type="text"
            value={repo}
            placeholder={t("repos.repoPh")}
            onChange={(e) => setRepo(e.target.value)}
          />
        </label>
        <label>
          {t("repos.token")}
          <input
            type="password"
            autoComplete="off"
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
        </label>
        <p className="muted small">{t("repos.tokenHint")}</p>
        <label>
          {t("repos.template")}
          <select
            value={template}
            onChange={(e) => setTemplate(e.target.value)}
          >
            {templates.map((m) => (
              <option key={m} value={m}>
                {t(`repos.template${m}`, { defaultValue: m })}
              </option>
            ))}
          </select>
        </label>
        <button
          type="submit"
          className="primary"
          disabled={busy || !name.trim() || !repo.trim() || !token.trim()}
        >
          {t("repos.submit")}
        </button>
      </form>
    </section>
  );
}
