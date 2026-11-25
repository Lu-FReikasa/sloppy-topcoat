import { useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import { formatDateTime } from "../format";
import type { MemoryQuestion, QuestionAnswer } from "../types";
import { announceMemoryChange } from "./useQuestions";

const TOPICS = ["residence", "birthday", "age", "name", "work", "pet", "family", "care"];

interface Props {
  api: ApiClient;
  question: MemoryQuestion;
  /** Called after the question was answered or put aside (the caller reloads). */
  onDone: () => void;
}

/** A question the memory could not settle by itself: which of the contradicting notes is
 *  right? One button per note, "none of them", free text, or "later". The answer is applied
 *  like a statement of the user (D19 extended). */
export function QuestionCard({ api, question, onDone }: Props) {
  const { t, i18n } = useTranslation();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const topic = TOPICS.includes(question.topic) ? question.topic : "other";

  async function send(fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      announceMemoryChange();
      onDone();
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) setError(describeError(toActionError(err), t, i18n));
    } finally {
      setBusy(false);
    }
  }

  const answer = (a: QuestionAnswer) => void send(() => api.answerQuestion(question.id, a));

  return (
    <article className="question-card card" data-testid="question-card" aria-busy={busy}>
      <h2>{t(`memory.question.topic.${topic}`)}</h2>
      {question.question ? (
        <>
          <p data-testid="question-concrete">
            <strong>{question.question}</strong>
          </p>
          <p className="muted small">{t("memory.question.notesAbout")}</p>
        </>
      ) : (
        <p>{t(question.origin === "care" ? "memory.question.careIntro" : "memory.question.intro")}</p>
      )}
      {question.origin === "care" && question.observed && (
        <p data-testid="care-observed">
          {t("memory.question.observed", { text: question.observed })}
        </p>
      )}
      <ul className="question-candidates">
        {question.candidates.map((c) => (
          <li key={c.id}>
            {t("memory.question.said", {
              when: formatDateTime(c.at, i18n.language) ?? "–",
              text: c.text,
            })}
            {c.confirmed_count > 0 && (
              <span className="muted small">
                {" "}
                (
                {t("memory.confirmedCount", {
                  count: c.confirmed_count,
                  when: formatDateTime(c.last_confirmed_at, i18n.language) ?? "–",
                })}
                )
              </span>
            )}
          </li>
        ))}
      </ul>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="question-answers">
        {question.candidates.map((c) => (
          <button
            key={c.id}
            type="button"
            className="primary"
            disabled={busy}
            onClick={() => answer({ note_id: c.id })}
          >
            {t("memory.question.isRight", { text: c.text })}
          </button>
        ))}
        <button type="button" disabled={busy} onClick={() => answer({ none: true })}>
          {t("memory.question.none")}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => void send(() => api.dismissQuestion(question.id))}
        >
          {t("memory.question.later")}
        </button>
      </div>
      <form
        className="question-text"
        onSubmit={(e) => {
          e.preventDefault();
          const value = text.trim();
          if (value) answer({ text: value });
        }}
      >
        <label>
          <span className="fact-label">{t("memory.question.write")}</span>
          <input
            value={text}
            maxLength={300}
            placeholder={t("memory.question.placeholder")}
            onChange={(e) => setText(e.target.value)}
          />
        </label>
        <button type="submit" disabled={busy || !text.trim()}>
          {t("memory.question.send")}
        </button>
      </form>
    </article>
  );
}

/** All open questions, one card each (nothing when there are none). */
export function QuestionCards({
  api,
  questions,
  onDone,
}: {
  api: ApiClient;
  questions: MemoryQuestion[];
  onDone: () => void;
}) {
  if (questions.length === 0) return null;
  return (
    <div className="question-cards" data-testid="question-cards">
      {questions.map((q) => (
        <QuestionCard key={q.id} api={api} question={q} onDone={onDone} />
      ))}
    </div>
  );
}
