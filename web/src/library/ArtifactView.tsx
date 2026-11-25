import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { Markdown } from "../chat/Markdown";
import { setPendingPrompt } from "../chat/pending";
import { describeError, toActionError } from "../errors";
import { formatDateTime } from "../format";
import { chatHref } from "../route";
import type { Artifact } from "../types";
import { askConfirm } from "../components/askConfirm";

/** Longest part of a report that goes into the first message of a "ask about this" chat. */
const ASK_CHARS = 6000;

/** The report of a task (a document of the Ablage), read in a window of its own: with a note which
 *  task and chat made it, a chat about it, and delete. */
export function ArtifactView({ api, id }: { api: ApiClient; id: string }) {
  const { t, i18n } = useTranslation();
  const [open, setOpen] = useState<Artifact | null>(null);
  const [gone, setGone] = useState(false);
  const [error, setError] = useState("");
  const fail = useCallback((e: unknown) => setError(describeError(toActionError(e), t, i18n)), [t, i18n]);

  useEffect(() => {
    let live = true;
    api.artifact(id).then((a) => {
      if (!live) return;
      setOpen(a);
      if (a.unread) void api.markArtifactRead(a.id).catch(() => undefined);
    }, (e) => live && fail(e));
    return () => {
      live = false;
    };
  }, [api, id, fail]);

  async function remove(a: Artifact) {
    if (!await askConfirm(t("library.confirmDelete"))) return;
    try {
      await api.deleteArtifact(a.id);
      setGone(true);
    } catch (e) {
      fail(e);
    }
  }

  async function ask(a: Artifact) {
    try {
      const conversation = await api.createConversation({ application: "chat" });
      setPendingPrompt(
        conversation.id,
        t("library.askPrompt", { title: a.title, body: (a.body ?? "").slice(0, ASK_CHARS) }),
      );
      window.location.hash = chatHref(conversation.id);
    } catch (e) {
      fail(e);
    }
  }

  if (gone) return <p className="muted">{t("shelf.deleted")}</p>;
  return (
    <article className="library-open doc-window" data-testid="library-open">
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!open ? (
        !error && <p className="muted">{t("app.loading")}</p>
      ) : (
        <>
          <h2>{open.title}</h2>
          <p className="muted small">
            {t("library.madeBy", { when: formatDateTime(open.updated_at, i18n.language) })}
            {open.task_id && (
              <>
                {" · "}
                <a href={`#/tasks/${encodeURIComponent(open.task_id)}`}>{t("library.task")}</a>
              </>
            )}
            {open.conversation_id && (
              <>
                {" · "}
                <a href={chatHref(open.conversation_id)}>{t("library.fromChat")}</a>
              </>
            )}
          </p>
          <div className="memory-actions">
            <button type="button" onClick={() => void ask(open)}>
              {t("library.ask")}
            </button>
            <button type="button" className="danger" onClick={() => void remove(open)}>
              {t("library.delete")}
            </button>
          </div>
          <Markdown text={open.body ?? ""} />
        </>
      )}
    </article>
  );
}
