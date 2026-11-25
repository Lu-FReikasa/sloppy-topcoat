import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { DevBranch, DevRepo, DevRepoList } from "../types";
import { askConfirm } from "./askConfirm";

interface CardProps {
  api: ApiClient;
  repo: DevRepo;
  /** every repository of the person: the external ones it may be connected to */
  all: DevRepo[];
  onChanged: () => void;
  onError: (err: unknown) => void;
}

/** One local repository (ARCHITECTURE D76): it lives in the workshop of a dev node, where the
 *  assistant edits and tests it on work branches. Its branches are listed under it, each named
 *  with the repository, so it is plain which repository a branch belongs to; taking a branch into
 *  the default branch and publishing it are the person's clicks. */
export function LocalRepoCard({
  api,
  repo,
  all,
  onChanged,
  onError,
}: CardProps) {
  const { t } = useTranslation();
  const [branches, setBranches] = useState<DevBranch[] | null>(null);
  const [open, setOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [from, setFrom] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const upstream = all.find((r) => r.id === repo.upstream_id) ?? null;
  const externals = all.filter((r) => (r.kind ?? "external") === "external");

  async function load() {
    try {
      setBranches((await api.repoBranches(repo.id)).branches);
    } catch (err) {
      onError(err);
    }
  }

  async function toggle() {
    if (!open) await load();
    setOpen(!open);
  }

  async function step(
    fn: () => Promise<Record<string, unknown>>,
    done: (r: Record<string, unknown>) => string,
  ) {
    setNote(null);
    try {
      const r = await fn();
      setNote(done(r));
      if (open) await load();
      onChanged();
    } catch (err) {
      onError(err);
    }
  }

  async function merge(b: DevBranch) {
    const into = repo.default_branch;
    if (
      !await askConfirm(
        t("repos.confirmMerge", { branch: b.name, into, name: repo.name }),
      )
    )
      return;
    void step(
      () => api.mergeRepoBranch(repo.id, b.name),
      (r) => {
        const conflicts = r.conflicts as string[] | undefined;
        if (conflicts?.length)
          return t("repos.conflict", {
            branch: b.name,
            into,
            files: conflicts.join(", "),
          });
        return r.merged
          ? t("repos.merged", { branch: b.name, into })
          : t("repos.nothingToMerge", { into });
      },
    );
  }

  async function publish(b: DevBranch) {
    if (!upstream) return;
    if (
      !await askConfirm(
        t("repos.confirmPublish", {
          branch: b.name,
          repo: repo.name,
          name: upstream.full_name,
        }),
      )
    )
      return;
    void step(
      () => api.publishRepoBranch(repo.id, b.name),
      () => t("repos.published", { branch: b.name }),
    );
  }

  async function addBranch(e: FormEvent) {
    e.preventDefault();
    const name = newName.trim();
    if (!name) return;
    await step(
      () => api.createRepoBranch(repo.id, name, from || undefined),
      () => "",
    );
    setNewName("");
    setOpen(true);
    await load();
  }

  async function remove() {
    if (!await askConfirm(t("repos.confirmRemoveLocal", { name: repo.name })))
      return;
    try {
      await api.removeDevRepo(repo.id);
      onChanged();
    } catch (err) {
      onError(err);
    }
  }

  async function connect(id: string) {
    try {
      await api.setRepoUpstream(repo.id, id || null);
      onChanged();
    } catch (err) {
      onError(err);
    }
  }

  return (
    <li className="repo-card repo-local" data-testid="local-repo">
      <div className="repo-head">
        <strong>{repo.name}</strong>
        <span className="chip">{t("repos.kindLocal")}</span>
        {repo.node && (
          <span className="muted small">
            {t("repos.onNode", { node: repo.node })}
          </span>
        )}
        {upstream && (
          <span className="chip" data-testid="clone-of">
            {t("repos.cloneOf", { name: upstream.full_name })}
          </span>
        )}
      </div>
      <dl className="repo-facts">
        <div>
          <dt>{t("repos.colBranch")}</dt>
          <dd>{repo.default_branch}</dd>
        </div>
        <div>
          <dt>{t("repos.template")}</dt>
          <dd>
            {t(`repos.template${repo.test_template ?? "none"}`, {
              defaultValue: repo.test_template ?? "none",
            })}
          </dd>
        </div>
        <div>
          <dt>{t("repos.connect")}</dt>
          <dd>
            <select
              aria-label={t("repos.connect")}
              value={repo.upstream_id ?? ""}
              onChange={(e) => void connect(e.target.value)}
            >
              <option value="">{t("repos.notConnected")}</option>
              {externals.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.full_name}
                </option>
              ))}
            </select>
          </dd>
        </div>
      </dl>
      <div className="repo-actions">
        <button
          type="button"
          onClick={() => void toggle()}
          aria-expanded={open}
        >
          {open ? t("repos.hideBranches") : t("repos.showBranches")}
        </button>
        {upstream && (
          <button
            type="button"
            onClick={() =>
              void step(
                () => api.pullRepoBranch(repo.id),
                () =>
                  t("repos.pulled", {
                    branch: repo.default_branch,
                    name: upstream.full_name,
                  }),
              )
            }
          >
            {t("repos.pull", { name: upstream.full_name })}
          </button>
        )}
        <button type="button" onClick={() => void remove()}>
          {t("repos.remove")}
        </button>
      </div>
      {note && (
        <p className="notice small" role="status">
          {note}
        </p>
      )}
      {open && (
        <div className="repo-branches" data-testid="repo-branches">
          <h3>{t("repos.branchesOf", { name: repo.name })}</h3>
          <ul>
            {(branches ?? []).map((b) => (
              <li key={b.name} data-testid="repo-branch">
                <code>
                  <span className="muted">{repo.name}/</span>
                  {b.name}
                </code>{" "}
                {b.default && (
                  <span className="chip">{t("repos.defaultBranch")}</span>
                )}
                {b.work && (
                  <span className="chip">{t("repos.workBranch")}</span>
                )}
                {!b.default && (
                  <span className="muted small">
                    {" "}
                    {t("repos.ahead", { count: b.ahead })} ·{" "}
                    {t("repos.behind", { count: b.behind })}
                  </span>
                )}
                {b.subject && (
                  <span className="muted small"> — {b.subject}</span>
                )}{" "}
                {!b.default && b.ahead > 0 && (
                  <button type="button" onClick={() => merge(b)}>
                    {t("repos.merge", { branch: repo.default_branch })}
                  </button>
                )}{" "}
                {upstream && (
                  <button type="button" onClick={() => publish(b)}>
                    {t("repos.publish", { name: upstream.full_name })}
                  </button>
                )}
              </li>
            ))}
          </ul>
          <form className="repo-new-branch" onSubmit={(e) => void addBranch(e)}>
            <label>
              {t("repos.newBranch", { name: repo.name })}{" "}
              <input
                type="text"
                value={newName}
                maxLength={100}
                onChange={(e) => setNewName(e.target.value)}
              />
            </label>{" "}
            <label>
              {t("repos.newBranchFrom")}{" "}
              <select value={from} onChange={(e) => setFrom(e.target.value)}>
                <option value="">{repo.default_branch}</option>
                {(branches ?? [])
                  .filter((b) => !b.default)
                  .map((b) => (
                    <option key={b.name} value={b.name}>
                      {b.name}
                    </option>
                  ))}
              </select>
            </label>{" "}
            <button type="submit" disabled={!newName.trim()}>
              {t("repos.createBranch")}
            </button>
          </form>
        </div>
      )}
    </li>
  );
}

interface NewProps {
  api: ApiClient;
  list: DevRepoList;
  onCreated: () => void;
  onError: (err: unknown) => void;
}

/** A new local repository in the workshop of a dev node (D76). */
export function NewLocalRepo({ api, list, onCreated, onError }: NewProps) {
  const { t } = useTranslation();
  const nodes = list.nodes ?? [];
  const [name, setName] = useState("");
  const [node, setNode] = useState("");
  const [branch, setBranch] = useState("main");
  const [template, setTemplate] = useState("none");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);

  async function create(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      await api.createLocalRepo({
        name: name.trim(),
        branch: branch.trim() || "main",
        test_template: template,
        ...(node ? { node_id: node } : {}),
        ...(title.trim() ? { title: title.trim() } : {}),
        ...(description.trim() ? { description: description.trim() } : {}),
      });
      setName("");
      setTitle("");
      setDescription("");
      onCreated();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  }

  if (nodes.length === 0)
    return <p className="muted small">{t("repos.noDevNode")}</p>;
  return (
    <form
      className="form-grid"
      onSubmit={(e) => void create(e)}
      aria-label={t("repos.createLocal")}
    >
      <h3>{t("repos.createLocal")}</h3>
      <label>
        {t("repos.name")}
        <input
          type="text"
          value={name}
          placeholder={t("repos.namePh")}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      {nodes.length > 1 && (
        <label>
          {t("repos.node")}
          <select value={node} onChange={(e) => setNode(e.target.value)}>
            <option value="">–</option>
            {nodes.map((n) => (
              <option key={n.id} value={n.id}>
                {n.name}
              </option>
            ))}
          </select>
        </label>
      )}
      <label>
        {t("repos.branch")}
        <input
          type="text"
          value={branch}
          maxLength={100}
          onChange={(e) => setBranch(e.target.value)}
        />
      </label>
      <label>
        {t("repos.template")}
        <select value={template} onChange={(e) => setTemplate(e.target.value)}>
          {list.templates.map((m) => (
            <option key={m} value={m}>
              {t(`repos.template${m}`, { defaultValue: m })}
            </option>
          ))}
        </select>
      </label>
      <label>
        {t("repos.title")}
        <input
          type="text"
          value={title}
          maxLength={200}
          onChange={(e) => setTitle(e.target.value)}
        />
      </label>
      <label>
        {t("repos.description")}
        <textarea
          value={description}
          maxLength={2000}
          rows={2}
          onChange={(e) => setDescription(e.target.value)}
        />
      </label>
      <button
        type="submit"
        className="primary"
        disabled={busy || !name.trim() || (nodes.length > 1 && !node)}
      >
        {t("repos.create")}
      </button>
    </form>
  );
}

interface CloneProps {
  api: ApiClient;
  repo: DevRepo;
  list: DevRepoList;
  onCloned: () => void;
  onError: (err: unknown) => void;
}

/** The button and small form that make a local clone of an external repository (D76). */
export function CloneLocal({ api, repo, list, onCloned, onError }: CloneProps) {
  const { t } = useTranslation();
  const nodes = list.nodes ?? [];
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(`${repo.name}-local`);
  const [node, setNode] = useState("");
  const [busy, setBusy] = useState(false);

  async function clone(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api.cloneLocalRepo(repo.id, {
        name: name.trim(),
        ...(node ? { node_id: node } : {}),
      });
      setOpen(false);
      onCloned();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  }

  if (nodes.length === 0) return null;
  if (!open)
    return (
      <button type="button" onClick={() => setOpen(true)}>
        {t("repos.cloneLocal")}
      </button>
    );
  return (
    <form className="repo-clone" onSubmit={(e) => void clone(e)}>
      <label>
        {t("repos.cloneName")}{" "}
        <input
          type="text"
          value={name}
          maxLength={63}
          onChange={(e) => setName(e.target.value)}
        />
      </label>{" "}
      {nodes.length > 1 && (
        <select
          aria-label={t("repos.node")}
          value={node}
          onChange={(e) => setNode(e.target.value)}
        >
          <option value="">–</option>
          {nodes.map((n) => (
            <option key={n.id} value={n.id}>
              {n.name}
            </option>
          ))}
        </select>
      )}{" "}
      <button
        type="submit"
        className="primary"
        disabled={busy || !name.trim() || (nodes.length > 1 && !node)}
      >
        {t("repos.clone")}
      </button>{" "}
      <button type="button" onClick={() => setOpen(false)}>
        {t("repos.cancel")}
      </button>
    </form>
  );
}
