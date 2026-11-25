import { useState } from "react";
import { useTranslation } from "react-i18next";
import { formatDecimal, formatInt } from "../format";
import { formatClock, formatFull } from "./time";
import type { ChatMessage } from "../types";
import { copyToClipboard, Markdown } from "./Markdown";
import { MemoryUsed, RememberedNotes } from "./MemoryNotes";
import type { ApiClient } from "../api/client";
import { ThemeCardContext } from "./ThemeCard";
import { StoredCheck } from "./CheckPanel";
import { ActivityTimeline } from "./ActivityTimeline";
import { HandoffCard } from "./HandoffCard";
import { ReportCardView } from "./ReportCard";
import type { CheckState, MessageNotes, NoteKind } from "./state";

interface Props {
  message: ChatMessage;
  /** Only the last answer can be replaced. */
  onRegenerate?: () => void;
  /** Only the last answer, and only when it was cut off. */
  onContinue?: () => void;
  /** "remembered: …" lines for this answer, with undo. */
  notes?: MessageNotes;
  onUndoNote?: (noteId: string, kind: NoteKind) => void;
  /** The server check this answer is based on (E3-01d): what this session saw of it, and the
   *  client to load the rest when the user opens it. */
  check?: CheckState;
  api?: ApiClient;
  /** The conversation the answer belongs to: needed to give the answer a verdict (E3-06). */
  conversationId?: string;
  /** The server check behind this answer has its own report card in the conversation (E3-07 D3):
   *  the closing bubble does not show the check a second time. */
  checkInCard?: boolean;
  /** The report card stands under the answer it belongs to: closed until the reader opens it. */
  folded?: boolean;
  busy: boolean;
}

/** The line that shows which tool runs while a check is under way (E3-07 D3); it goes when the
 *  result is there. The text is "capability · node — why", an empty one means "nothing known
 *  yet". */
export function ProgressLine({ text }: { text: string }) {
  const { t } = useTranslation();
  return (
    <p
      className="progress-line notice-line"
      role="status"
      data-testid="progress-line"
    >
      <span className="running-dot" aria-hidden="true" />
      <span>
        {text ? t("chat.progress", { what: text }) : t("chat.progressWaiting")}
      </span>
    </p>
  );
}

/** What the check has done so far, one line per finished tool call (a tick, or a cross when the
 *  tool failed); the line that runs now is the progress line below it. */
export function DoneSteps({
  steps,
}: {
  steps: { capability?: string; node?: string; ok?: boolean }[];
}) {
  if (steps.length === 0) return null;
  return (
    <ul className="done-steps muted small" data-testid="done-steps">
      {steps.map((s, i) => (
        <li key={i}>
          <span aria-hidden="true">{s.ok === false ? "✗" : "✓"}</span>{" "}
          {[s.capability, s.node].filter(Boolean).join(" · ")}
        </li>
      ))}
    </ul>
  );
}

/** The time of a message ("14:32"); the full date and time with the zone is its hover text. */
export function MessageTime({ at }: { at: string | null | undefined }) {
  const { i18n } = useTranslation();
  const clock = formatClock(at, i18n.language);
  if (!at || !clock) return null;
  return (
    <time
      className="message-time"
      dateTime={at}
      title={formatFull(at, i18n.language) ?? undefined}
    >
      {clock}
    </time>
  );
}

/** "node-amd · Qwen2.5-7B · 42 tokens · 3,1 s" under an answer. */
export function MessageMeta({ message }: { message: ChatMessage }) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const parts: string[] = [];
  if (message.node) parts.push(message.node);
  if (message.model) parts.push(message.model);
  if (message.completion_tokens !== null) {
    parts.push(
      t("chat.tokens", { value: formatInt(message.completion_tokens, locale) }),
    );
  }
  if (message.duration_ms !== null) {
    parts.push(
      t("chat.seconds", {
        value: formatDecimal(message.duration_ms / 1000, locale),
      }),
    );
  }
  return parts.length > 0 ? <span>{parts.join(" · ")}</span> : null;
}

export function MessageView({
  message,
  onRegenerate,
  onContinue,
  notes,
  onUndoNote,
  check,
  api,
  conversationId,
  checkInCard,
  folded,
  busy,
}: Props) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const [verdict, setVerdict] = useState<"helped" | "unfit" | null>(
    message.feedback ?? null,
  );
  const [verdictFailed, setVerdictFailed] = useState(false);

  async function judge(next: "helped" | "unfit") {
    if (!api || !conversationId) return;
    const value = verdict === next ? null : next;
    try {
      await api.messageFeedback(conversationId, message.id, value);
      setVerdict(value);
      setVerdictFailed(false);
    } catch {
      setVerdictFailed(true);
    }
  }
  const isUser = message.role === "user";
  if (message.kind === "progress") {
    return (
      <article
        className="message message-assistant message-progress"
        data-testid="message-progress"
      >
        <ProgressLine text={message.content} />
      </article>
    );
  }
  if (message.kind === "reaction") {
    // an emoji instead of a text block: small, under the message it answers
    return (
      <article
        className="message message-assistant message-reaction"
        data-testid="message-reaction"
        aria-label={t("chat.reaction")}
      >
        <span className="reaction-emoji">{message.content}</span>
      </article>
    );
  }
  if (message.kind === "handoff") {
    return <HandoffCard message={message} api={api} />;
  }
  if (message.kind === "report" && message.report) {
    return (
      <article
        className="message message-assistant message-report"
        data-testid="message-report"
      >
        <ReportCardView card={message.report} api={api} folded={folded} />
        <footer className="message-foot muted small">
          <MessageTime at={message.created_at} />
        </footer>
      </article>
    );
  }
  return (
    <article
      className={`message message-${message.role}`}
      data-testid={`message-${message.role}`}
    >
      {!isUser && message.activity && message.activity.length > 0 && (
        <ActivityTimeline
          items={message.activity}
          live={message.status === "running"}
        />
      )}
      <div className="message-body">
        {isUser ? (
          <p className="user-text">{message.content}</p>
        ) : message.content ? (
          <ThemeCardContext.Provider value={api ?? null}>
            <Markdown text={message.content} />
          </ThemeCardContext.Provider>
        ) : null}
        {message.status === "stopped" && (
          <p className="muted small">{t("chat.stopped")}</p>
        )}
        {message.status === "running" && (
          <p className="notice-line" data-testid="running-note">
            {t("chat.runningBackground")}
          </p>
        )}
        {message.status === "interrupted" && (
          <p className="notice-line" data-testid="interrupted-note">
            {t("chat.hubInterrupted")}
          </p>
        )}
        {message.status === "truncated" && (
          <p className="notice-line" data-testid="truncated-note">
            {t("chat.truncated")}
          </p>
        )}
        {message.status === "error" && (
          <p className="error small" role="alert">
            {t("chat.interrupted", { message: message.error ?? "" })}
          </p>
        )}
      </div>
      {!isUser && message.agent_task_id && api && !checkInCard && (
        <StoredCheck
          api={api}
          taskId={message.agent_task_id}
          snapshot={check}
        />
      )}
      {!isUser && message.verification && (
        <div
          className={`notice-line verification ${message.verification.ok ? "ok" : "issues"}`}
          data-testid="verification"
        >
          <p>
            {message.verification.ok
              ? t("quality.verified", { model: message.verification.model })
              : t("quality.notVerified", { model: message.verification.model })}
          </p>
          {message.verification.same_model && (
            <p className="muted small">{t("quality.sameModel")}</p>
          )}
          {message.verification.issues.length > 0 && (
            <ul className="small">
              {message.verification.issues.map((i, n) => (
                <li key={n}>{i}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {!isUser && message.memory_refs && message.memory_refs.length > 0 && (
        <MemoryUsed refs={message.memory_refs} api={api} />
      )}
      {!isUser && notes && onUndoNote && (
        <RememberedNotes notes={notes} onUndo={onUndoNote} />
      )}
      {!isUser && message.fallback_from && message.node && (
        <p className="notice-line" data-testid="fallback-note">
          {t("chat.fallbackStored", {
            node: message.node,
            from: message.fallback_from,
          })}
        </p>
      )}
      {!isUser && message.context_trimmed > 0 && (
        <p className="notice-line" data-testid="trim-note">
          {t("chat.trimmed", { count: message.context_trimmed })}
        </p>
      )}
      <footer className="message-foot muted small">
        <MessageTime at={message.created_at} />
        {!isUser && <MessageMeta message={message} />}
        <span className="spacer" />
        <button
          type="button"
          onClick={() => void copyToClipboard(message.content).then(setCopied)}
          aria-label={t("chat.copyMessage")}
        >
          {copied ? t("chat.copied") : t("chat.copy")}
        </button>
        {api &&
          conversationId &&
          !isUser &&
          message.id > 0 &&
          message.persona_ref && (
            <span className="answer-verdict" data-testid="answer-verdict">
              <button
                type="button"
                aria-pressed={verdict === "helped"}
                onClick={() => void judge("helped")}
              >
                {t("persona.verdict.helped")}
              </button>
              <button
                type="button"
                aria-pressed={verdict === "unfit"}
                onClick={() => void judge("unfit")}
              >
                {t("persona.verdict.unfit")}
              </button>
              {verdictFailed && (
                <span className="error small" role="alert">
                  {t("memory.feedback.failed")}
                </span>
              )}
            </span>
          )}
        {onContinue &&
          (message.status === "truncated" ||
            message.status === "interrupted") && (
            <button
              type="button"
              className="primary"
              disabled={busy}
              onClick={onContinue}
            >
              {t("chat.continue")}
            </button>
          )}
        {onRegenerate && (
          <button type="button" disabled={busy} onClick={onRegenerate}>
            {t("chat.regenerate")}
          </button>
        )}
      </footer>
    </article>
  );
}
