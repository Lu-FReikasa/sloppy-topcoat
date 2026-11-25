import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { programHue } from "../desktop/programs";
import { requestOpen } from "../layout/intent";
import type { Conversation } from "../types";
import { CONVERSATIONS_EVENT, openGroups } from "./openChats";

/** How often the list looks again without a reason (a day ends, another device talked). */
const REFRESH_MS = 60_000;

/** Start's open conversations in its side rail (2026-11-13), grouped by application: what is still
 *  going on anywhere, whatever window it was begun at. Pin keeps one open; ✓ marks it done (a day
 *  without activity does that by itself); the rest is in the archive. */
export function OpenChatsList({
  api,
  program,
  current,
  tick,
}: {
  api: ApiClient;
  program: string;
  /** The conversation the window shows. */
  current?: string;
  /** Bumped on chat activity: a new message may open a conversation again. */
  tick: number;
}) {
  const { t } = useTranslation();
  const [chats, setChats] = useState<Conversation[]>([]);
  const load = useCallback(() => {
    api.listConversations().then(setChats, () => undefined);
  }, [api]);
  useEffect(load, [load, tick]);
  useEffect(() => {
    window.addEventListener(CONVERSATIONS_EVENT, load);
    const timer = window.setInterval(load, REFRESH_MS);
    return () => {
      window.removeEventListener(CONVERSATIONS_EVENT, load);
      window.clearInterval(timer);
    };
  }, [load]);

  async function change(c: Conversation, patch: { pinned?: boolean; done?: boolean }) {
    try {
      await api.updateConversation(c.id, patch);
    } finally {
      load();
    }
  }

  const groups = openGroups(chats);
  return (
    <div className="open-chats" aria-label={t("openChats.title")} role="group">
      <p className="open-chats-head small muted">{t("openChats.title")}</p>
      {groups.length === 0 && <p className="small muted">{t("openChats.none")}</p>}
      {groups.map((g) => (
        <div
          key={g.application}
          className="open-chats-group"
          style={{ ["--pg-hue" as string]: String(programHue(g.application)) }}
        >
          <p className="open-chats-app small">{t(`workspace.${g.application}`, { defaultValue: g.application })}</p>
          <ul>
            {g.chats.map((c) => (
              <li key={c.id} aria-current={c.id === current ? "page" : undefined}>
                <button
                  type="button"
                  className="open-chats-title"
                  title={c.title || t("chat.untitled")}
                  onClick={() => requestOpen({ module: "page.chat", props: { id: c.id }, program })}
                >
                  {c.running && <span className="open-chats-run" aria-label={t("openChats.running")} />}
                  {c.title || t("chat.untitled")}
                </button>
                <button
                  type="button"
                  className="open-chats-act"
                  aria-pressed={c.pinned === true}
                  title={c.pinned ? t("openChats.unpin") : t("openChats.pin")}
                  aria-label={c.pinned ? t("openChats.unpin") : t("openChats.pin")}
                  onClick={() => void change(c, { pinned: !c.pinned })}
                >
                  📌
                </button>
                <button
                  type="button"
                  className="open-chats-act"
                  title={t("openChats.done")}
                  aria-label={t("openChats.done")}
                  onClick={() => void change(c, { done: true })}
                >
                  ✓
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
