import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { parseQuickAdd } from "./quickAdd";
import type { ProjectNodeCreate } from "./types";

/** One line makes a task: `Pay the invoice *money !3 tomorrow 17:00 every week`. The labels come
 *  back by name; the page finds or makes them. */
export function QuickAdd({
  zone,
  parentId,
  onCreate,
}: {
  zone: string;
  parentId: string | null;
  onCreate: (body: ProjectNodeCreate, labels: string[]) => void;
}) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const [help, setHelp] = useState(false);
  const [problem, setProblem] = useState(false);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!text.trim()) return;
    const q = parseQuickAdd(text, new Date(), zone);
    if (!q.title) return setProblem(true);
    setProblem(false);
    onCreate(
      {
        kind: "task",
        title: q.title,
        parent_id: parentId,
        ...(q.priority !== null ? { priority: q.priority } : {}),
        ...(q.due_at ? { due_at: q.due_at } : {}),
        ...(q.repeat_rule ? { repeat_rule: q.repeat_rule } : {}),
      },
      q.labels,
    );
    setText("");
  };
  return (
    <form className="project-form project-bar" onSubmit={submit}>
      <input
        aria-label={t("project.quickAdd")}
        placeholder={t("project.quickAddPlaceholder")}
        value={text}
        onChange={(e) => setText(e.target.value)}
        maxLength={300}
      />
      <button type="submit" disabled={!text.trim()}>
        {t("project.add")}
      </button>
      <button
        type="button"
        onClick={() => setHelp((v) => !v)}
        aria-expanded={help}
      >
        ?
      </button>
      {help && <p className="project-muted">{t("project.quickAddHelp")}</p>}
      {problem && (
        <p role="alert" className="project-error">
          {t("project.quickAddNeedsTitle")}
        </p>
      )}
    </form>
  );
}
