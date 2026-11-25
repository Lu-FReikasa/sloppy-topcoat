import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { MessageTime } from "../chat/MessageView";
import { requestCompose } from "../desktop/compose";
import type { ChatMessage } from "../types";

/** Under a message: when it was written, its number, and two ways to point at it. The reference
 *  is signed by the hub; the development assistant reads how the answer was handled from it. */
export function MessageLine({
  api,
  conversationId,
  message,
}: {
  api: ApiClient;
  conversationId: string;
  message: ChatMessage;
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
      <button type="button" onClick={() => void copy()}>
        {copied ? t("talk.copied") : t("talk.reference")}
      </button>
      <button type="button" onClick={() => void investigate()}>
        {t("talk.investigate")}
      </button>
    </div>
  );
}
