import { useTranslation } from "react-i18next";
import { formatDateTime } from "../format";
import type { MemoryItem } from "../types";

/** The care facts of one note (E3-05): how far it can be trusted now (the same three stages the
 *  assistant sees, never a number), when it was last confirmed and how often, and how useful it
 *  has been. Nothing for a hub that does not report them. */
export function NoteCare({ item, locale }: { item: MemoryItem; locale: string }) {
  const { t } = useTranslation();
  if (item.kind === undefined && item.stage === undefined) return null;
  const live = item.status === "active" || item.status === "disputed";
  const verified = item.verified_count ?? 0;
  const confirmations = item.confirmed_count + verified;
  const stamps = [item.last_confirmed_at, item.last_verified_at].filter((s): s is string => !!s);
  const last = stamps.length > 0 ? stamps.sort()[stamps.length - 1] : null;
  const usefulness = Math.round((item.usefulness ?? 0.5) * 100);
  return (
    <p className="muted small memory-care-line" data-testid="note-care">
      {live && item.stage && (
        <>
          <span className={`badge badge-stage-${item.stage}`} data-testid="note-stage">
            {t(`memory.stage.${item.stage}`)}
          </span>{" "}
        </>
      )}
      {item.kind && <span data-testid="note-kind">{t(`memory.kind.${item.kind}`)}</span>}
      {" · "}
      {last
        ? t("memory.lastConfirmation", {
            when: formatDateTime(last, locale) ?? "–",
          })
        : t("memory.neverConfirmed")}
      {" · "}
      <span data-testid="note-confirmations">
        {verified > 0
          ? t("memory.confirmationsLive", {
              count: confirmations,
              live: verified,
            })
          : t("memory.confirmations", { count: confirmations })}
      </span>
      {" · "}
      <span data-testid="note-usefulness">
        {t("memory.usefulness", {
          value: usefulness,
          used: item.used_count ?? 0,
          recalled: item.recalled_count ?? 0,
        })}
      </span>
      {item.expires_at && (
        <>
          {" · "}
          {t("memory.expires", {
            when: formatDateTime(item.expires_at, locale) ?? "–",
          })}
        </>
      )}
      {item.verify_with && (
        <>
          {" · "}
          {t("memory.checkableWith", {
            capability: item.verify_with.capability,
          })}
        </>
      )}
    </p>
  );
}
