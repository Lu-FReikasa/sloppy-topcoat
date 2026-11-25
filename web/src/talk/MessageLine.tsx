import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { MessageTime } from "../chat/MessageView";
import { requestCompose } from "../desktop/compose";
import type { ChatMessage } from "../types";

/** Under a message: when it was written, its number, and two ways to point at it. The reference
 *  is signed by the hub; the development assistant reads how the answer was handled from it.
 *  `children`: what can be done with the message itself, shown at all times (the footer of an
 *  answer carries "Have it checked", #77). */
export function MessageLine({
  api,
  conversationId,
  message,
  children,
}: {
  api: ApiClient;
  conversationId: string;
  message: ChatMessage;
  children?: ReactNode;
}) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  async function reference(): Promise<string | null> {
    try {
      return (await api.messageReference(conversationId, message.id)).reference;
    } catch {
      return null;
    }
  }

  async function copy() {
    const ref = await reference();
    if (ref === null) return;
    try {
      await navigator.clipboard.writeText(ref);
    } catch {
      return;
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  }

  async function investigate() {
    const ref = await reference();
    if (ref === null) return;
    requestCompose("dev", t("talk.investigateText", { reference: ref }));
  }

  return (
    <div className="message-line">
      <MessageTime at={message.created_at} />
      <span className="message-number">#{message.id}</span>
      {children}
      <button type="button" onClick={() => void copy()}>
        {copied ? t("talk.copied") : t("talk.reference")}
      </button>
      <button type="button" onClick={() => void investigate()}>
        {t("talk.investigate")}
      </button>
    </div>
  );
}
