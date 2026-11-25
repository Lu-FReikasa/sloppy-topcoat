import { useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import {
  isProfileRoot,
  readRootProblem,
  readRootsOf,
  suggestedRoots,
  windowsChecksOn,
  withReadRoots,
  withWindowsChecks,
} from "../hostRead";
import type { NodeDetail } from "../types";

interface Props {
  api: ApiClient;
  node: NodeDetail;
  onSaved: () => void;
}

/**
 * Reading files on a machine (HOST-01, D89): the folders this node may read from, and on a Windows
 * node the switch of the fixed Windows checks. The folders are the person's decision: nothing is
 * released until they save, the suggestions only fill the input, and saving a folder is what
 * switches the file capabilities on for the node.
 */
export function HostReadSection({ api, node, onSaved }: Props) {
  const { t, i18n } = useTranslation();
  const config = node.config;
  const saved = readRootsOf(config);
  const [roots, setRoots] = useState<string[]>(saved);
  const [draft, setDraft] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const savedKey = saved.join("\n");

  // another admin or a refetch changed the stored list: show it (state derived while rendering)
  const [seenKey, setSeenKey] = useState(savedKey);
  if (seenKey !== savedKey) {
    setSeenKey(savedKey);
    setRoots(savedKey === "" ? [] : savedKey.split("\n"));
  }

  if (!config) return null;
  const isWindows = node.os.toLowerCase().startsWith("windows");
  const dirty = roots.join("\n") !== savedKey;

  function add(raw: string) {
    const path = raw.trim();
    const p = readRootProblem(path);
    if (p !== null) {
      setProblem(t(`hostRead.problem.${p}`));
      return;
    }
    if (roots.includes(path)) {
      setProblem(t("hostRead.problem.duplicate"));
      return;
    }
    setProblem(null);
    setDone(false);
    setRoots([...roots, path]);
    setDraft("");
  }

  async function put(next: Record<string, unknown>) {
    setBusy(true);
    setError(null);
    setDone(false);
    try {
      await api.putNodeConfig(node.id, next);
      setDone(true);
      onSaved();
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) setError(describeError(toActionError(err), t, i18n));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="host-read">
      <h2>{t("hostRead.heading")}</h2>
      <p className="muted small">{t("hostRead.intro")}</p>
      {roots.length === 0 ? (
        <p className="muted">{t("hostRead.none")}</p>
      ) : (
        <ul className="host-roots">
          {roots.map((r) => (
            <li key={r}>
              <code>{r}</code>{" "}
              <button
                type="button"
                disabled={busy}
                aria-label={t("hostRead.remove", { path: r })}
                onClick={() => {
                  setDone(false);
                  setRoots(roots.filter((x) => x !== r));
                }}
              >
                {t("hostRead.removeShort")}
              </button>
            </li>
          ))}
        </ul>
      )}
      <label>
        {t("hostRead.addLabel")}{" "}
        <input
          type="text"
          value={draft}
          spellCheck={false}
          disabled={busy}
          onChange={(e) => {
            setDraft(e.target.value);
            setProblem(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") add(draft);
          }}
        />
      </label>{" "}
      <button type="button" disabled={busy || draft.trim() === ""} onClick={() => add(draft)}>
        {t("hostRead.add")}
      </button>
      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}
      {isProfileRoot(draft) && <p className="muted small">{t("hostRead.profileWarning")}</p>}
      <p className="muted small">
        {t("hostRead.suggestions")}{" "}
        {suggestedRoots(node.os).map((s) => (
          <button key={s.path} type="button" disabled={busy} onClick={() => setDraft(s.path)}>
            {t(`hostRead.suggest.${s.key}`)}: {s.path}
          </button>
        ))}
      </p>
      <p className="muted small">{t("hostRead.profileNote")}</p>
      <p className="muted small">{t("hostRead.saveNote")}</p>
      <div className="dialog-actions">
        <button
          type="button"
          className="primary"
          disabled={busy || !dirty}
          onClick={() => void put(withReadRoots(config, roots))}
        >
          {t("hostRead.save")}
        </button>
      </div>
      {done && (
        <p className="notice" role="status">
          {t("hostRead.saved")}
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {isWindows && (
        <div className="windows-checks">
          <h3>{t("hostRead.windowsHeading")}</h3>
          <p className="muted small">{t("hostRead.windowsHint")}</p>
          <label>
            <input
              type="checkbox"
              checked={windowsChecksOn(config)}
              disabled={busy}
              onChange={(e) => void put(withWindowsChecks(config, e.target.checked))}
            />{" "}
            {t("hostRead.windowsToggle")}
          </label>
        </div>
      )}
    </section>
  );
}
