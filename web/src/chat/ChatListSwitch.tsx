import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  applyChatList,
  CHAT_LIST_EVENT,
  CHAT_LISTS,
  isChatList,
  loadChatList,
  type ChatListVariant,
} from "./chatListModel";

export function ChatListSwitch() {
  const { t } = useTranslation();
  const [variant, setVariant] = useState<ChatListVariant>(() => loadChatList());
  useEffect(() => {
    const sync = () => setVariant(loadChatList());
    window.addEventListener(CHAT_LIST_EVENT, sync);
    return () => window.removeEventListener(CHAT_LIST_EVENT, sync);
  }, []);
  return (
    <select
      className="language-switch"
      aria-label={t("chatlist.heading")}
      value={variant}
      onChange={(e) => {
        const v = e.target.value;
        if (!isChatList(v)) return;
        applyChatList(v);
        setVariant(v);
      }}
    >
      {CHAT_LISTS.map((v) => (
        <option key={v} value={v}>
          {t(`chatlist.${v}`)}
        </option>
      ))}
    </select>
  );
}
