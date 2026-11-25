import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { WikiTakeOver } from "../project/WikiTakeOver";
import { ArtifactView } from "./ArtifactView";
import { ReportView } from "./ReportsPage";

/** One document of the Ablage in a window of its own (desktop/docs.ts): a research report with its
 *  versions and comments, or the report of a task. */
export function DocumentPage({ api, kind, id }: { api: ApiClient; kind?: string; id?: string }) {
  const { t, i18n } = useTranslation();
  const [gone, setGone] = useState(false);
  const [error, setError] = useState("");
  if (!id) return <p className="muted">{t("shelf.noDocument")}</p>;
  if (gone) return <p className="muted">{t("shelf.deleted")}</p>;
  const doc = { kind: kind === "report" ? ("report" as const) : ("artifact" as const), id };
  return (
    <div className="doc-window">
      <WikiTakeOver api={api} doc={doc} onError={(e) => setError(describeError(toActionError(e), t, i18n))} />
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {doc.kind === "report" ? (
        <ReportView api={api} id={id} onChanged={() => undefined} onDeleted={() => setGone(true)} />
      ) : (
        <ArtifactView api={api} id={id} />
      )}
    </div>
  );
}
