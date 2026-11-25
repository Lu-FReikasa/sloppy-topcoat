import { useState } from "react";
import { useTranslation } from "react-i18next";
import { memoryHref } from "../route";
import type { ChatMessage } from "../types";
import { evidenceOf, hasMark, type Light } from "./evidence";

/** The quiet mark under an answer: the traffic light of its second look (yellow while it waits,
 *  green when another model found nothing, orange for small flaws, red for a finding that a
 *  source backs), and which node answered when that was not the expected one. A click opens the
 *  sources; nothing else about the run shows. */
export function EvidenceMark({
  message,
  light,
}: {
  message: ChatMessage;
  light: Light | null;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const e = evidenceOf(message);
  if (!hasMark(e, light)) return null;
  const parts: string[] = [];
  if (light) parts.push(t(`talk.light.${light}`));
  if (e.via) parts.push(t("talk.via", { node: e.via }));
  return (
    <div className="talk-evidence">
      <button
        type="button"
        className={`talk-mark talk-mark-${light ?? "plain"}`}
        aria-expanded={open}
        aria-label={t("talk.evidence.open")}
        onClick={() => setOpen((v) => !v)}
        data-testid="evidence-mark"
        data-light={light ?? ""}
      >
        <span className="talk-mark-dot" aria-hidden="true" />
        {parts.join(" · ")}
      </button>
      {open && (
        <div
          className="talk-sheet"
          role="region"
          aria-label={t("talk.evidence.title")}
        >
          {light && <p className="small">{t(`talk.lightWhy.${light}`)}</p>}
          {e.via && (
            <p className="small">
              {t("talk.viaWhy", { node: e.via, from: e.viaFrom ?? "" })}
            </p>
          )}
          {e.issues.length > 0 && (
            <ul>
              {e.issues.map((issue) => (
                <li key={issue}>{issue}</li>
              ))}
            </ul>
          )}
          {e.refs.length > 0 && (
            <ul>
              {e.refs.map((r) => (
                <li key={r.id}>
                  {r.text}
                  <span className="muted small">
                    {" "}
                    · {t(`talk.kind.${r.evidence}`)}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {e.refs.length === 0 && e.issues.length === 0 && (
            <p className="muted small">{t("talk.evidence.none")}</p>
          )}
          <a className="small" href={memoryHref}>
            {t("talk.evidence.memory")}
          </a>
        </div>
      )}
    </div>
  );
}
