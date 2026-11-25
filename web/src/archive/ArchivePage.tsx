import { applicationCounts, applicationOf } from "../chat/applications";
import { programHue } from "../desktop/programs";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { dayGroup, type DayGroup } from "../chat/chatListModel";
import { describeError, toActionError } from "../errors";
import { formatDateTime } from "../format";
import { chatHref } from "../route";
import type { Conversation } from "../types";

const GROUPS: DayGroup[] = ["today", "yesterday", "week", "older"];

/** Every conversation, found by a word, read in place. The sidebar shows the latest ones; this is
 *  the rest. Continuing an old conversation is just asking again in the current one. */
/** `application` preselects the filter: the shelf of an application opens on its own conversations
 *  (Chat, the archive of all of them, opens on all). */
export function ArchivePage({ api, application }: { api: ApiClient; application?: string }) {
  const { t, i18n } = useTranslation();
  const [query, setQuery] = useState("");
  // one application or all (the desktop plan, phase 6: the archive of every conversation)
  const [app, setApp] = useState<string>(application && application !== "chat" ? application : "all");
  const [chats, setChats] = useState<Conversation[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fail = useCallback(
    (err: unknown) => {
      if (!(err instanceof UnauthorizedError))
        setError(describeError(toActionError(err), t, i18n));
    },
    [t, i18n],
  );

  useEffect(() => {
    const timer = setTimeout(() => {
      api.listConversations(query.trim() || undefined).then((list) => {
        setError(null);
        setChats(list);
      }, fail);
    }, 200);
    return () => clearTimeout(timer);
  }, [api, query, fail]);

  const groups = useMemo(() => {
    const now = new Date();
    return GROUPS.map((group) => ({
      group,
      items: (chats ?? []).filter(
        (c) => dayGroup(c.updated_at, now) === group && (app === "all" || applicationOf(c) === app),
      ),
    })).filter((g) => g.items.length > 0);
  }, [chats, app]);
  const apps = useMemo(() => applicationCounts(chats ?? []), [chats]);

  return (
    <section className="archive">
      <h1>{t("archive.heading")}</h1>
      <p className="muted">{t("archive.intro")}</p>
      <input
        type="search"
        aria-label={t("archive.search")}
        placeholder={t("archive.search")}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {(apps.length > 1 || app !== "all") && (
        <div className="archive-apps" role="group" aria-label={t("archive.apps")}>
          {[{ application: "all", count: chats?.length ?? 0 }, ...apps].map((a) => (
            <button
              key={a.application}
              type="button"
              aria-pressed={app === a.application}
              onClick={() => setApp(a.application)}
              style={{ ["--pg-hue" as string]: String(programHue(a.application)) }}
            >
              {a.application === "all" ? t("archive.all") : t(`workspace.${a.application}`, { defaultValue: a.application })}{" "}
              <span className="muted small">{a.count}</span>
            </button>
          ))}
        </div>
      )}
      {chats === null ? (
        <p className="muted">{t("app.loading")}</p>
      ) : chats.length === 0 ? (
        <p className="muted">
          {query ? t("archive.noMatch") : t("archive.none")}
        </p>
      ) : (
        groups.map(({ group, items }) => (
          <div key={group}>
            <h2>{t(`chat.group.${group}`)}</h2>
            <ul className="archive-list">
              {items.map((c) => (
                <li key={c.id}>
                  <span
                    className="archive-app"
                    title={t(`workspace.${applicationOf(c)}`, { defaultValue: applicationOf(c) })}
                    style={{ ["--pg-hue" as string]: String(programHue(applicationOf(c))) }}
                    aria-hidden="true"
                  />
                  <a href={chatHref(c.id)}>{c.title || t("chat.untitled")}</a>{" "}
                  <span className="muted small">
                    {formatDateTime(c.updated_at, i18n.language)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ))
      )}
    </section>
  );
}
