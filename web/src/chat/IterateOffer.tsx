// The next iteration of a task, prepared by the Projekte chat ("mach eine v5 von 1.8.2"): the
// message is the feedback, to trim before the person's one click starts it (D15). The task text
// stays; the feedback may become a requirement of every further iteration (project_iterations.py).
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type { IterateOfferData } from "../types";

export function IterateOffer({ api, offer }: { api: ApiClient; offer: IterateOfferData }) {
  const { t, i18n } = useTranslation();
  const [feedback, setFeedback] = useState(offer.feedback);
  const [keep, setKeep] = useState(true);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      await api.iterateProjectNode(offer.task_id, {
        based_on: offer.based_on,
        feedback: feedback.trim(),
        keep: keep && !!feedback.trim(),
      });
      setDone(true);
    } catch (err) {
      setError(describeError(toActionError(err), t, i18n));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="iterate-offer" data-testid="iterate-offer">
      <p>
        <strong>{t("iterations.offerTitle", { n: offer.iteration, number: offer.task_number })}</strong>{" "}
        {offer.task_title}
        {offer.based_on_number && (
          <span className="muted"> · {t("iterations.basedOn", { number: offer.based_on_number })}</span>
        )}
      </p>
      <label>
        {t("iterations.feedback")}
        <textarea rows={4} value={feedback} maxLength={4000} disabled={done} onChange={(e) => setFeedback(e.target.value)} />
      </label>
      {offer.requirements.length > 0 && (
        <p className="muted small">
          {t("iterations.requirements")}: {offer.requirements.join("; ")}
        </p>
      )}
      <label className="small">
        <input type="checkbox" checked={keep} disabled={done} onChange={(e) => setKeep(e.target.checked)} />{" "}
        {t("iterations.keep")}
      </label>
      <div>
        <button type="button" disabled={busy || done} onClick={() => void start()}>
          {t("iterations.offerStart")}
        </button>
      </div>
      {done && <p role="status">{t("iterations.started")}</p>}
      {error && (
        <p className="error small" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
