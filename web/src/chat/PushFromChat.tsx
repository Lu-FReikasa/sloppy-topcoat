import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { PushPanel } from "../components/PushPanel";
import type { DevRepo, ReportEvidence } from "../types";

// what a check can have changed in the workshop's scratch copy
const CHANGES = ["repo.start", "repo.write", "repo.edit", "repo.apply_patch"];

/** The registered repository a check changed in the workshop (by the name its calls carried), or
 *  null when it changed none. */
export function changedRepo(evidence: ReportEvidence[]): string | null {
  for (const e of evidence) {
    if (e.capability && CHANGES.includes(e.capability)) {
      const name = e.input?.["repo"];
      if (typeof name === "string" && name) return name;
    }
  }
  return null;
}

/** Under a report that changed files in the workshop: the same push step as on the Repositories
 *  page (commit message, the diff on a card, one click), so the user need not leave the chat. */
export function PushFromChat({
  api,
  evidence,
}: {
  api: ApiClient;
  evidence: ReportEvidence[];
}) {
  const { t } = useTranslation();
  const name = changedRepo(evidence);
  const [repo, setRepo] = useState<DevRepo | null | undefined>(undefined);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!name) return;
    let live = true;
    api.devRepos().then(
      (list) => {
        if (live) setRepo(list.repos.find((r) => r.name === name) ?? null);
      },
      () => {
        if (live) setRepo(null);
      },
    );
    return () => {
      live = false;
    };
  }, [api, name]);

  if (!name || !repo) return null;
  if (repo.mode !== "test-branch")
    return <p className="muted small">{t("report.pushReadOnly", { name })}</p>;
  return (
    <div className="report-push">
      <button type="button" onClick={() => setOpen(!open)}>
        {t("report.pushChange", { name })}
      </button>
      {open && <PushPanel api={api} repo={repo} />}
    </div>
  );
}
