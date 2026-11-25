import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ConversationDetail } from "../types";
import { conversationToMarkdown, exportFileName, type ExportLabels } from "./exportMarkdown";

/** "Export" menu of the open conversation: copy or download it as Markdown. Nothing leaves the
 *  browser. */
export function ExportButtons({ conversation }: { conversation: ConversationDetail }) {
  const { t, i18n } = useTranslation();
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const empty = conversation.messages.length === 0;

  function markdown(): string {
    const labels: ExportLabels = {
      you: t("export.you"),
      assistant: t("export.assistant"),
      report: t("export.report"),
      assumptions: t("export.assumptions"),
      proposal: t("export.proposal"),
      impact: t("export.impact"),
      rollback: t("export.rollback"),
      incomplete: t("export.incomplete"),
    };
    return conversationToMarkdown(conversation, labels, i18n.language, t("chat.untitled"));
  }

  async function copy() {
    try {
      if (!navigator.clipboard) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(markdown());
      setState("copied");
    } catch {
      setState("failed");
    }
  }

  function download() {
    const blob = new Blob([markdown()], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = exportFileName(conversation.title || t("chat.untitled"));
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  return (
    <details className="export-menu">
      <summary>{t("export.title")}</summary>
      <div className="export-panel">
        <button type="button" disabled={empty} onClick={() => void copy()}>
          {t("export.copy")}
        </button>
        <button type="button" disabled={empty} onClick={download}>
          {t("export.download")}
        </button>
        <span role="status" className="muted small">
          {state === "copied" ? t("export.copied") : state === "failed" ? t("export.failed") : ""}
        </span>
      </div>
    </details>
  );
}
