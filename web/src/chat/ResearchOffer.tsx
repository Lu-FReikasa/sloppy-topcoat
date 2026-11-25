import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type { ChatMessage, ResearchOfferInfo } from "../types";

/** "Als Recherche starten" at an answer of a Research chat (2026-11-13): one click starts a
 *  research of its own in this conversation; its progress and then the summary of its report
 *  follow as messages. A taken offer says so instead of offering again. */
export function ResearchOffer({
  api,
  conversationId,
  messageId,
  offer,
  onStarted,
}: {
  api: ApiClient;
  conversationId: string;
  messageId: number;
  offer: ResearchOfferInfo;
  onStarted?: (message: ChatMessage) => void;
}) {
  const { t, i18n } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [started, setStarted] = useState(Boolean(offer.started));
  const [error, setError] = useState<string | null>(null);
  if (started) return <p className="muted small research-offer">{t("researchOffer.started")}</p>;
  return (
    <p className="research-offer">
      <button
        type="button"
        className="primary"
        disabled={busy}
        onClick={() => {
          setBusy(true);
          setError(null);
          api.startResearchInChat(conversationId, offer.question, messageId > 0 ? messageId : undefined).then(
            (m) => {
              setStarted(true);
              onStarted?.(m);
            },
            (err) => {
              setError(describeError(toActionError(err), t, i18n));
              setBusy(false);
            },
          );
        }}
      >
        {t("researchOffer.start")}
      </button>
      {error && (
        <span className="error small" role="alert">
          {" "}
          {error}
        </span>
      )}
    </p>
  );
}
