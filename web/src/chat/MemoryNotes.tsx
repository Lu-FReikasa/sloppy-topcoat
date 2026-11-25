import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { memoryHref } from "../route";
import type { MemoryRef } from "../types";
import type { MessageNotes, NoteItem, NoteKind } from "./state";

/** Which notes of its memory the assistant had in front of it for an answer. With a client the
 *  user can say, per note, that it helped or did not fit (E3-05): a quiet word that counts heavily
 *  in the note's usefulness and deletes nothing. Unfolded only on request. */
export function MemoryUsed({ refs, api }: { refs: MemoryRef[]; api?: ApiClient }) {
  const { t } = useTranslation();
  const [rated, setRated] = useState<Record<string, "helped" | "unfit" | "error">>({});

  async function rate(id: string, helped: boolean) {
    if (!api) return;
    try {
      await api.memoryFeedback(id, helped);
      setRated((r) => ({ ...r, [id]: helped ? "helped" : "unfit" }));
    } catch {
      setRated((r) => ({ ...r, [id]: "error" }));
    }
  }

  return (
    <details className="memory-used" data-testid="memory-used">
      <summary>{t("memory.used", { count: refs.length })}</summary>
      <ul>
        {refs.map((r) => (
          <li key={r.id}>
            <span className={`tag tag-${r.evidence}`}>[{r.evidence}]</span> {r.text}
            {r.status === "disputed" && (
              <span className="muted small"> ({t("memory.status.disputed")})</span>
            )}
            {api && (
              <span className="memory-feedback" data-testid="memory-feedback">
                {rated[r.id] === "helped" || rated[r.id] === "unfit" ? (
                  <span className="muted small" role="status">
                    {t(
                      rated[r.id] === "helped"
                        ? "memory.feedback.thanksHelped"
                        : "memory.feedback.thanksUnfit",
                    )}
                  </span>
                ) : (
                  <>
                    <button type="button" className="link" onClick={() => void rate(r.id, true)}>
                      {t("memory.feedback.helped")}
                    </button>
                    <button type="button" className="link" onClick={() => void rate(r.id, false)}>
                      {t("memory.feedback.unfit")}
                    </button>
                    {rated[r.id] === "error" && (
                      <span className="error small" role="alert">
                        {t("memory.feedback.failed")}
                      </span>
                    )}
                  </>
                )}
              </span>
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}

function kindOf(n: NoteItem): NoteKind {
  return n.kind ?? (n.isNew ? "remembered" : "confirmed");
}

/** "Remembered: …", "Corrected: … (replaces: …)" or "Withdrawn: …", each with undo (a
 *  confirmation has nothing to undo), and "not remembered" for refused secrets. */
export function RememberedNotes({
  notes,
  onUndo,
}: {
  notes: MessageNotes;
  onUndo: (id: string, kind: NoteKind) => void;
}) {
  const { t } = useTranslation();
  if (notes.items.length === 0 && notes.blocked === 0 && !notes.notNoted) return null;
  return (
    <div className="memory-notes" data-testid="memory-notes">
      {notes.items.map((n) => (
        <div key={n.id} className="memory-note-block">
          <p className="notice-line memory-note">
            <span>
              {t(`memory.${kindOf(n)}`)}: {n.text}
              {n.replaced && n.replaced.length > 0 && (
                <span className="muted" data-testid="memory-replaced">
                  {" ("}
                  {t(n.withdrawn ? "memory.withdrawnNote" : "memory.replaces", {
                    text: n.replaced.map((r) => r.text).join(", "),
                  })}
                  {")"}
                </span>
              )}
            </span>{" "}
            {(n.isNew || n.kind === "retracted") && (
              <button type="button" className="link" onClick={() => onUndo(n.id, kindOf(n))}>
                {t("memory.undo")}
              </button>
            )}
          </p>
          {n.contradicts?.map((c) => (
            <p key={c.id} className="notice-line" data-testid="memory-contradiction">
              {t("memory.contradictsEarlier", { text: c.text })}{" "}
              <a href={memoryHref}>{t("memory.decide")}</a>
            </p>
          ))}
        </div>
      ))}
      {notes.notNoted && (
        <p className="notice-line" data-testid="memory-not-noted">
          {t("memory.notNoted", {
            reason: t(`memory.notNotedReason.${notes.notNoted}`, { defaultValue: notes.notNoted }),
          })}
        </p>
      )}
      {notes.blocked > 0 && notes.notNoted !== "secret" && (
        <p className="notice-line" data-testid="memory-blocked">
          {t("memory.notStored", { count: notes.blocked })}
        </p>
      )}
      <p className="muted small">
        <a href={memoryHref}>{t("memory.open")}</a>
      </p>
    </div>
  );
}
