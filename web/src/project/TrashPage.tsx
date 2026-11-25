import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { ProjectPurgeMode, ProjectTrash } from "./types";

interface Target {
  kind: "project" | "node";
  id: string;
  title: string;
}

/** The trash: what was moved there stays whole until the person deletes it for good, by typing its
 *  title. No agent has a tool for any of this. A project can leave its timeline behind (without the
 *  texts) or take everything with it; an entry always leaves the timeline. */
export function TrashPage({
  api,
  tick,
  onChanged,
  onError,
}: {
  api: ApiClient;
  tick: number;
  onChanged: (message: string) => void;
  onError: (err: unknown) => void;
}) {
  const { t } = useTranslation();
  const [trash, setTrash] = useState<ProjectTrash | null>(null);
  const [purging, setPurging] = useState<Target | null>(null);
  const [typed, setTyped] = useState("");
  const [mode, setMode] = useState<ProjectPurgeMode>("keep_timeline");

  const load = useCallback(async () => {
    try {
      setTrash(await api.projectTrash());
    } catch (err) {
      onError(err);
    }
  }, [api, onError]);
  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load, tick]);

  const act = async (run: () => Promise<unknown>, message: string) => {
    try {
      await run();
      setPurging(null);
      setTyped("");
      await load();
      onChanged(message);
    } catch (err) {
      onError(err);
    }
  };

  const empty =
    trash !== null && trash.projects.length === 0 && trash.nodes.length === 0;
  return (
    <section className="project-trash" aria-label={t("project.trash")}>
      <h2>{t("project.trash")}</h2>
      {empty && <p className="project-muted">{t("project.trashEmpty")}</p>}
      {trash && trash.projects.length > 0 && (
        <>
          <h3>{t("project.trashedProjects")}</h3>
          <ul className="project-list">
            {trash.projects.map((p) => (
              <li key={p.id} className="project-row">
                <span className="project-name">{p.title}</span>
                <button
                  type="button"
                  onClick={() =>
                    act(() => api.restoreProject(p.id), t("project.restored"))
                  }
                >
                  {t("project.restore")}: {p.title}
                </button>
                <button
                  type="button"
                  onClick={() =>
                    setPurging({ kind: "project", id: p.id, title: p.title })
                  }
                >
                  {t("project.deleteForGood")}: {p.title}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
      {trash && trash.nodes.length > 0 && (
        <>
          <h3>{t("project.trashedNodes")}</h3>
          <ul className="project-list">
            {trash.nodes.map((n) => (
              <li key={n.id} className="project-row">
                <span className="project-name">
                  {n.ref} {n.title}
                </span>
                <button
                  type="button"
                  onClick={() =>
                    act(
                      () => api.restoreProjectNode(n.id),
                      t("project.restored"),
                    )
                  }
                >
                  {t("project.restore")}: {n.ref}
                </button>
                <button
                  type="button"
                  onClick={() =>
                    setPurging({ kind: "node", id: n.id, title: n.title })
                  }
                >
                  {t("project.deleteForGood")}: {n.ref}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
      {purging && (
        <form
          className="project-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (typed !== purging.title) return;
            void act(
              () =>
                purging.kind === "project"
                  ? api.purgeProject(purging.id, typed, mode)
                  : api.purgeProjectNode(purging.id, typed),
              t("project.purged"),
            );
          }}
        >
          <p role="alert">
            {t("project.purgeConfirm", { title: purging.title })}
          </p>
          <input
            aria-label={t("project.purgeConfirm", { title: purging.title })}
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
          />
          {purging.kind === "project" && (
            <label>
              {t("project.purgeMode")}
              <select
                value={mode}
                onChange={(e) => setMode(e.target.value as ProjectPurgeMode)}
              >
                <option value="keep_timeline">{t("project.purgeKeep")}</option>
                <option value="remove_everything">
                  {t("project.purgeAll")}
                </option>
              </select>
            </label>
          )}
          <div className="project-actions">
            <button type="submit" disabled={typed !== purging.title}>
              {t("project.deleteForGood")}
            </button>
            <button
              type="button"
              onClick={() => {
                setPurging(null);
                setTyped("");
              }}
            >
              {t("project.cancel")}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
