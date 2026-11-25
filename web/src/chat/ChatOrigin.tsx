import { useTranslation } from "react-i18next";
import { programHue } from "../desktop/programs";
import type { ChatToolsOverview } from "../types";

/** Which application a conversation belongs to and what its assistant may use (2026-11-13): a talk
 *  begun at a window and opened in Start says whose rights it has. */
export function ChatOrigin({
  application,
  overview,
  internet,
  pinned,
  onPin,
  onDone,
}: {
  application: string;
  overview: ChatToolsOverview | undefined;
  internet: boolean;
  pinned?: boolean;
  onPin?: (pinned: boolean) => void;
  onDone?: () => void;
}) {
  const { t } = useTranslation();
  const groups = overview?.groups.map((g) => g.label) ?? [];
  return (
    <p
      className="chat-origin small"
      data-testid="chat-origin"
      style={{ ["--pg-hue" as string]: String(programHue(application)) }}
    >
      <span className="chat-origin-app">{t(`workspace.${application}`, { defaultValue: application })}</span>
      <span className="muted">
        {" · "}
        {groups.length > 0 ? t("chatOrigin.tools", { tools: groups.join(", ") }) : t("chatOrigin.noTools")}
        {" · "}
        {internet ? t("chatOrigin.internetOn") : t("chatOrigin.internetOff")}
      </span>
      {onPin && (
        <button type="button" className="linklike small" aria-pressed={pinned === true} onClick={() => onPin(!pinned)}>
          {pinned ? t("openChats.unpin") : t("openChats.pin")}
        </button>
      )}
      {onDone && (
        <button type="button" className="linklike small" onClick={onDone}>
          {t("openChats.done")}
        </button>
      )}
    </p>
  );
}
