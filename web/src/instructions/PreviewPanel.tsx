import { useTranslation } from "react-i18next";

/** "This is what the assistant sees now": the core exactly as a new conversation gets it,
 *  read-only. */
export function PreviewPanel({ text }: { text: string }) {
  const { t } = useTranslation();
  return (
    <details className="core-preview" data-testid="core-preview">
      <summary>{t("instructions.preview.heading")}</summary>
      <p className="muted small">{t("instructions.preview.note")}</p>
      <pre className="plain-pre" aria-label={t("instructions.preview.heading")}>
        {text}
      </pre>
    </details>
  );
}
