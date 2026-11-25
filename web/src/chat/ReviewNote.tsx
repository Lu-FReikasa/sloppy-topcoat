import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { Light } from "../talk/evidence";
import type { ChatReview } from "../types";

/** The reviews of one conversation by message id; reloaded when the conversation grows. */
export function useReviews(
  api: ApiClient | undefined,
  conversationId: string,
  messageCount: number,
): Record<number, ChatReview> {
  const [state, setState] = useState<{
    id: string;
    byMessage: Record<number, ChatReview>;
  } | null>(null);
  useEffect(() => {
    if (!api || !conversationId) return; // no chat open yet: nothing to ask about
    let live = true;
    api.conversationReviews(conversationId).then(
      (list) => {
        if (!live) return;
        const byMessage: Record<number, ChatReview> = {};
        for (const r of list) byMessage[r.message_id] = r;
        setState({ id: conversationId, byMessage });
      },
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [api, conversationId, messageCount]);
  return state?.id === conversationId ? state.byMessage : {};
}

/** The traffic light of each answer of a conversation by message id: the verdict of its second
 *  look, or "waiting" while it is queued or running (then asked again every few seconds). */
export function useReviewLights(
  api: ApiClient | undefined,
  conversationId: string,
  messageCount: number,
): Record<number, Light> {
  const [state, setState] = useState<{
    id: string;
    by: Record<number, Light>;
  } | null>(null);
  useEffect(() => {
    if (!api || !conversationId) return; // no chat open yet: nothing to ask about
    let live = true;
    let timer: number | undefined;
    const look = () =>
      api.reviewLights(conversationId).then(
        (r) => {
          if (!live) return;
          const by: Record<number, Light> = {};
          for (const [id, verdict] of Object.entries(r.lights))
            by[Number(id)] = verdict;
          for (const id of r.waiting) by[id] = "waiting";
          setState({ id: conversationId, by });
          if (r.waiting.length > 0) timer = window.setTimeout(look, 4000);
        },
        () => undefined,
      );
    void look();
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [api, conversationId, messageCount]);
  return state?.id === conversationId ? state.by : {};
}

/** The second model's finding about an answer: an error has evidence (a note of the memory
 *  contradicts the claim), a hint only says that something looks wrong. */
export function ReviewNote({
  review,
  api,
}: {
  review: ChatReview;
  api?: ApiClient;
}) {
  const { t } = useTranslation();
  const [seen, setSeen] = useState(review.seen);
  const error = review.verdict === "error";
  function acknowledge() {
    if (!api) return;
    setSeen(true);
    void api.markReviewSeen(review.id).catch(() => setSeen(false));
  }
  return (
    <aside
      className={`review-note ${error ? "review-error" : "review-hint"}`}
      data-testid="review-note"
    >
      <p className="small">
        <strong>{t(error ? "review.error" : "review.hint")}</strong>{" "}
        <span className="muted">
          {t(review.same_model ? "review.bySame" : "review.by", {
            model: review.reviewer_model ?? "?",
          })}
        </span>
      </p>
      <ul className="small">
        {review.findings.map((f, i) => (
          <li key={i}>
            {f.claim} — {f.reason}
            {f.evidence && (
              <span className="muted">
                {" "}
                (
                {t(
                  f.evidence.note_id
                    ? "review.evidence"
                    : "review.evidenceOther",
                  { note: f.evidence.text },
                )}
                )
              </span>
            )}
          </li>
        ))}
      </ul>
      {!seen && api && (
        <button type="button" className="small" onClick={acknowledge}>
          {t("review.ok")}
        </button>
      )}
    </aside>
  );
}
