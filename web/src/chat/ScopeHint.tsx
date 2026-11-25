import { useTranslation } from "react-i18next";
import type { ChatToolsOverview, ToolScope } from "../types";

/** The machines this conversation's checks may use (pruefauftrag T2.4): all of them when no choice
 *  of servers is set, else the chosen ones that exist. A line of text, so the person sees what a
 *  check of this chat can reach before they ask. */
export function scopeNodes(overview: ChatToolsOverview, scope: ToolScope | null | undefined): string[] {
  const names = overview.nodes.map((n) => n.name);
  const chosen = scope?.nodes;
  return chosen === null || chosen === undefined ? names : names.filter((n) => chosen.includes(n));
}

export function ScopeHint({
  overview,
  scope,
}: {
  overview: ChatToolsOverview;
  scope: ToolScope | null | undefined;
}) {
  const { t } = useTranslation();
  const inScope = scopeNodes(overview, scope);
  const all = scope?.nodes === null || scope?.nodes === undefined;
  return (
    <p className="scope-hint muted small" data-testid="scope-hint">
      {all
        ? t("serverScope.hint.all")
        : inScope.length > 0
          ? t("serverScope.hint.some", { nodes: inScope.join(", ") })
          : t("serverScope.hint.none")}
    </p>
  );
}
