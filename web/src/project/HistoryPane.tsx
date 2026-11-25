import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { ProjectDiff, ProjectNode, ProjectVersion } from "./types";

/** The versions of a node's title and body, the difference between two of them, and a way back.
 *  Going back makes a **new** version (the hub never overwrites): both stay in the history. */
export function HistoryPane({
  api,
  node,
  zone,
  onRestored,
  onError,
}: {
  api: ApiClient;
  node: ProjectNode;
  zone: string;
  onRestored: () => void;
  onError: (err: unknown) => void;
}) {
  const { t, i18n } = useTranslation();
  const [versions, setVersions] = useState<ProjectVersion[] | null>(null);
  const [from, setFrom] = useState<number | null>(null);
  const [to, setTo] = useState<number | null>(null);
  const [diff, setDiff] = useState<ProjectDiff | null>(null);

  const load = useCallback(async () => {
    try {
      const list = (await api.projectNodeHistory(node.id)).versions;
      setVersions(list);
      const last = list.at(-1)?.rev ?? null;
      const before = list.at(-2)?.rev ?? last;
      setTo(last);
      setFrom(before);
      setDiff(null);
    } catch (err) {
      onError(err);
    }
  }, [api, node.id, onError]);
  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  if (versions === null) return null;
  const when = (v: ProjectVersion) =>
    v.at
      ? new Intl.DateTimeFormat(i18n.language, {
          timeZone: zone,
          dateStyle: "short",
          timeStyle: "short",
        }).format(new Date(v.at))
      : "";
  const label = (v: ProjectVersion) =>
    `${t("project.versionLabel", { rev: v.rev })} · ${when(v)}`;

  const show = async () => {
    if (from === null || to === null) return;
    try {
      setDiff(await api.projectNodeDiff(node.id, from, to));
    } catch (err) {
      onError(err);
    }
  };
  const restore = async () => {
    if (from === null) return;
    try {
      await api.revertProjectNode(node.id, {
        revision: node.revision,
        to_rev: from,
      });
      onRestored();
    } catch (err) {
      onError(err);
    }
  };
  const same =
    diff !== null &&
    diff.title[0] === diff.title[1] &&
    diff.body.every((r) => r.tag === "equal");
  return (
    <fieldset className="project-extra">
      <legend>{t("project.history")}</legend>
      {versions.length < 2 ? (
        <p className="project-muted">{t("project.historyEmpty")}</p>
      ) : (
        <>
          <div className="project-bar">
            <label>
              {t("project.historyFrom")}
              <select
                value={from ?? ""}
                onChange={(e) => setFrom(Number(e.target.value))}
              >
                {versions.map((v) => (
                  <option key={v.rev} value={v.rev}>
                    {label(v)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              {t("project.historyTo")}
              <select
                value={to ?? ""}
                onChange={(e) => setTo(Number(e.target.value))}
              >
                {versions.map((v) => (
                  <option key={v.rev} value={v.rev}>
                    {label(v)}
                  </option>
                ))}
              </select>
            </label>
            <button type="button" onClick={show}>
              {t("project.showChanges")}
            </button>
            <button type="button" onClick={restore} disabled={from === null}>
              {t("project.restoreVersion")}
            </button>
          </div>
          {diff && (
            <div className="project-diff" data-testid="diff">
              {same && <p className="project-muted">{t("project.diffNone")}</p>}
              {diff.title[0] !== diff.title[1] && (
                <p>
                  <strong>{t("project.diffTitle")}: </strong>
                  <del>{diff.title[0]}</del> <ins>{diff.title[1]}</ins>
                </p>
              )}
              {diff.body.map((run, i) =>
                run.tag === "equal" ? (
                  <pre key={i} className="project-diff-equal">
                    {run.old.join("\n")}
                  </pre>
                ) : (
                  <div key={i}>
                    {run.old.length > 0 && (
                      <pre className="project-diff-del">
                        {run.old.map((l) => `- ${l}`).join("\n")}
                      </pre>
                    )}
                    {run.new.length > 0 && (
                      <pre className="project-diff-ins">
                        {run.new.map((l) => `+ ${l}`).join("\n")}
                      </pre>
                    )}
                  </div>
                ),
              )}
            </div>
          )}
        </>
      )}
    </fieldset>
  );
}
