import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { ActivityItem } from "../types";

/** What the person can do while a check runs (E7-01): pause it, or send it a note that it reads
 *  before its next turn. A check never gets wider rights from a note: it is text, nothing else. */

/** Is the check held right now? The chain says so: its last pause or resume decides. */
export function isPaused(items: ActivityItem[]): boolean {
  for (let i = items.length - 1; i >= 0; i -= 1) {
    const item = items[i];
    if (item?.kind === "steer" && typeof item.data.event === "string") {
      return item.data.event === "pause";
    }
  }
  return false;
}

export function CheckControls({
  api,
  messageId,
  items,
}: {
  api: ApiClient;
  messageId: number;
  items: ActivityItem[];
}) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const paused = isPaused(items);

  async function run(action: () => Promise<unknown>, done?: () => void) {
    setBusy(true);
    setFailed(false);
    try {
      await action();
      done?.();
    } catch {
      setFailed(true); // the check ended in the meantime, or it is not yours
    } finally {
      setBusy(false);
    }
  }

  function send(e: FormEvent) {
    e.preventDefault();
    const note = text.trim();
    if (!note) return;
    void run(
      () => api.steerCheck(messageId, note),
      () => setText(""),
    );
  }

  return (
    <div className="check-controls" data-testid="check-controls">
      <button
        type="button"
        disabled={busy}
        onClick={() => void run(() => api.pauseCheck(messageId, !paused))}
      >
        {paused ? t("chain.control.resume") : t("chain.control.pause")}
      </button>
      {paused && (
        <span className="notice-line" data-testid="check-paused">
          {t("chain.control.paused")}
        </span>
      )}
      <form onSubmit={send} className="check-note-form">
        <input
          type="text"
          value={text}
          maxLength={500}
          disabled={busy}
          aria-label={t("chain.control.noteLabel")}
          placeholder={t("chain.control.notePlaceholder")}
          onChange={(e) => setText(e.target.value)}
        />
        <button type="submit" disabled={busy || text.trim() === ""}>
          {t("chain.control.send")}
        </button>
      </form>
      {failed && (
        <p className="error small" role="alert">
          {t("chain.control.failed")}
        </p>
      )}
    </div>
  );
}
