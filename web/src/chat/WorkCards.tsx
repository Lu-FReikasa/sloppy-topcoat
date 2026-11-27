import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { programHue } from "../desktop/programs";
import { requestOpen } from "../layout/intent";
import type { ChatMessage, WorkCard } from "../types";
import { Markdown } from "./Markdown";

/** States in which the employee is still at it: the cards are asked again while one is. */
const OPEN: ReadonlySet<WorkCard["status"]> = new Set(["sent", "queued", "running", "waiting"]);
/** How soon open cards are asked again, at first and at most (a task can stay open for long:
 *  one the queue refused stays "sent"). The conversation moving starts again from the first. */
export const CARD_REFRESH_MS = 10_000;
export const CARD_REFRESH_MAX_MS = 120_000;
const NONE: WorkCard[] = [];

/** The work cards of a conversation (D112, #37): what the secretary handed on in it. Asked again
 *  when the conversation moves (`tick`) and, while a task is open, ever less often. */
export function useWorkCards(
  api: ApiClient | undefined,
  conversationId: string | undefined,
  enabled: boolean,
  tick: number,
): WorkCard[] {
  const { i18n } = useTranslation();
  // the cards with the conversation they belong to: another conversation shows none of them
  const [loaded, setLoaded] = useState<{ of: string; cards: WorkCard[] } | null>(null);
  const language = i18n.language?.startsWith("de") ? "de" : "en";
  useEffect(() => {
    if (!api || !conversationId || !enabled) return;
    let live = true;
    let timer: number | undefined;
    let wait = CARD_REFRESH_MS; // doubles on each look, up to CARD_REFRESH_MAX_MS
    let open = false; // a task was open at the last look that got an answer
    const again = (): void => {
      timer = window.setTimeout(() => void look(), wait);
      wait = Math.min(wait * 2, CARD_REFRESH_MAX_MS);
    };
    const look = async (): Promise<void> => {
      try {
        const r = await api.conversationOrders(conversationId, language);
        if (!live) return;
        setLoaded({ of: conversationId, cards: r });
        open = r.some((c) => OPEN.has(c.status));
      } catch {
        // no cards is no failure of the conversation: they come with the next look, and a
        // task that was open is asked about again (a hub gone for a moment ends no card)
        if (!live) return;
      }
      if (open) again();
    };
    void look();
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [api, conversationId, enabled, tick, language]);
  return enabled && loaded !== null && loaded.of === conversationId ? loaded.cards : NONE;
}

/** Which message each card stands under: the message that said it was handed on, else (an order
 *  from before the hub kept that link, or a message not shown) the last message written before
 *  the card. A card older than every message stands under the first one. */
export function placeCards(
  messages: readonly ChatMessage[],
  cards: readonly WorkCard[],
): Map<number, WorkCard[]> {
  if (messages.length === 0) return new Map();
  const ids = new Set(messages.map((m) => m.id));
  const timeOf = (iso: string | null | undefined): number => (iso ? Date.parse(iso) : NaN);
  const underOf = (card: WorkCard): number => {
    if (card.chat_message_id != null && ids.has(card.chat_message_id)) return card.chat_message_id;
    const at = timeOf(card.created_at);
    if (Number.isNaN(at)) return messages[messages.length - 1]!.id;
    return messages.reduce(
      (under, m) => (timeOf(m.created_at) <= at ? m.id : under),
      messages[0]!.id,
    );
  };
  return cards.reduce((out, card) => {
    const id = underOf(card);
    return out.set(id, [...(out.get(id) ?? []), card]);
  }, new Map<number, WorkCard[]>());
}

/** One task the secretary handed on: who does it, what, how far it is, what came of it, and the
 *  way to the employee's own conversation about it. The button is the person's own click; it
 *  opens (or makes) a conversation and changes nothing else. */
export function WorkCardView({ card, api }: { card: WorkCard; api?: ApiClient }) {
  const { t, i18n } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const language = i18n.language?.startsWith("de") ? "de" : "en";
  const open = async () => {
    if (!api) return;
    setBusy(true);
    setFailed(false);
    try {
      const r = await api.talkToEmployee(card.id, language);
      requestOpen({
        module: "page.chat",
        props: { id: r.conversation_id },
        program: r.application,
      });
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <article
      className={`work-card work-card-${card.status}`}
      data-testid="work-card"
      aria-label={t("workCard.label", { employee: card.employee_title })}
      style={{ ["--pg-hue" as string]: String(programHue(card.employee)) }}
    >
      <header className="work-card-head">
        <span className="work-card-who">{card.employee_title}</span>
        <span className={`work-card-state work-card-state-${card.status}`} data-testid="work-card-state">
          {t(`workCard.status.${card.status}`)}
        </span>
      </header>
      <p className="work-card-topic">{card.topic}</p>
      {card.summary && (
        <div className="work-card-result" data-testid="work-card-result">
          <Markdown text={card.summary} />
        </div>
      )}
      {card.report_when_done && OPEN.has(card.status) && (
        <p className="work-card-note small">{t("workCard.reportWhenDone")}</p>
      )}
      <p className="work-card-actions">
        <button type="button" disabled={busy || !api} onClick={() => void open()}>
          {t("workCard.talkDirectly", { employee: card.employee_title })}
        </button>
      </p>
      {failed && (
        <p className="work-card-note small" role="alert">
          {t("workCard.openFailed")}
        </p>
      )}
    </article>
  );
}
