import { useTranslation } from "react-i18next";
import type { ChatToolsOverview, ToolScope } from "../types";

/** Is the internet on in a conversation: its own choice, else the user's default; never on when no
 *  node is released for web search. */
export function internetOn(
  overview: ChatToolsOverview | undefined,
  scope: ToolScope | null | undefined,
  defaultOn: boolean,
): boolean {
  if (overview && overview.internet.node === null) return false;
  return scope?.internet ?? defaultOn;
}

interface Props {
  overview: ChatToolsOverview;
  scope: ToolScope | null | undefined;
  /** The user's default for conversations that have no choice of their own. */
  defaultOn: boolean;
  onChange: (scope: ToolScope) => void;
}

/** The "Internet" switch of a conversation: web search and reading public pages. It is separate
 *  from the servers (deselecting those never removes it); the hub picks the node it searches from. */
export function InternetSwitch({ overview, scope, defaultOn, onChange }: Props) {
  const { t } = useTranslation();
  const node = overview.internet.node;
  const on = internetOn(overview, scope, defaultOn);
  const state = t(on ? "internet.on" : "internet.off");
  const title =
    node === null
      ? t("internet.unavailable")
      : on
        ? t("internet.via", { node })
        : t("internet.hint");
  return (
    <button
      type="button"
      className={`internet-switch${on ? " on" : ""}`}
      aria-pressed={on}
      disabled={node === null}
      title={title}
      onClick={() =>
        onChange({
          nodes: scope?.nodes ?? null,
          groups: scope?.groups ?? null,
          internet: !on,
        })
      }
    >
      <span className="internet-dot" aria-hidden="true" />
      {t("internet.label")}: {state}
    </button>
  );
}
