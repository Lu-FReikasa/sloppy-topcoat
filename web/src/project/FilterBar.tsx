import { useState, type FormEvent } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { ProjectSavedView } from "./types";

/** The sort presets of the menu: the value is the hub's sort rule ("" = the order of the numbers). */
export const SORTS = [
  "",
  "dueDate",
  "priority:desc",
  "status",
  "title",
  "updated:desc",
  "done:asc",
  "percent:desc",
] as const;

/** The name a saved view is shown under: the built-in ones in the person's language. */
export function viewName(view: ProjectSavedView, t: TFunction): string {
  if (!view.builtin) return view.title;
  const slug = view.title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_|_$/g, "");
  return t(`project.builtinViews.${slug}`, { defaultValue: view.title });
}

export function FilterBar({
  api,
  filter,
  sort,
  views,
  error,
  onApply,
  onPickView,
  onSaveView,
  onDeleteView,
}: {
  api: ApiClient;
  filter: string;
  sort: string;
  views: ProjectSavedView[];
  /** The hub's reason when the last filter was refused. */
  error: string | null;
  onApply: (filter: string, sort: string) => void;
  onPickView: (view: ProjectSavedView) => void;
  onSaveView: (title: string) => void;
  onDeleteView: (view: ProjectSavedView) => void;
}) {
  const { t } = useTranslation();
  const [text, setText] = useState(filter);
  const [help, setHelp] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [picked, setPicked] = useState("");
  const chosen = views.find((v) => v.id === picked);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    onApply(text.trim(), sort);
  };
  const showHelp = async () => {
    if (help !== null) return setHelp(null);
    try {
      setHelp((await api.projectFilterHelp()).help);
    } catch {
      setHelp("");
    }
  };
  return (
    <div className="project-filterbar">
      <form onSubmit={submit} className="project-bar">
        <input
          aria-label={t("project.filterLabel")}
          placeholder={t("project.filterPlaceholder")}
          value={text}
          onChange={(e) => setText(e.target.value)}
          maxLength={1000}
        />
        <button type="submit">{t("project.filterApply")}</button>
        <button
          type="button"
          onClick={() => {
            setText("");
            setPicked("");
            onApply("", "");
          }}
          disabled={!filter && !text && !sort}
        >
          {t("project.filterClear")}
        </button>
        <button type="button" onClick={showHelp} aria-expanded={help !== null}>
          {t("project.filterHelp")}
        </button>
      </form>
      {error && (
        <p role="alert" className="project-error">
          {t("project.filterError", { message: error })}
        </p>
      )}
      {help !== null && <pre className="project-help">{help}</pre>}
      <div className="project-bar">
        <label>
          {t("project.sortLabel")}
          <select
            value={SORTS.includes(sort as (typeof SORTS)[number]) ? sort : ""}
            onChange={(e) => onApply(filter, e.target.value)}
          >
            {SORTS.map((s) => (
              <option key={s || "default"} value={s}>
                {t(`project.sorts.${s || "default"}`)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("project.savedViews")}
          <select
            value={picked}
            onChange={(e) => {
              setPicked(e.target.value);
              const v = views.find((x) => x.id === e.target.value);
              if (v) {
                setText(v.filter);
                onPickView(v);
              }
            }}
          >
            <option value="">{t("project.noSavedView")}</option>
            {views.map((v) => (
              <option key={v.id} value={v.id}>
                {viewName(v, t)}
              </option>
            ))}
          </select>
        </label>
        {chosen && !chosen.builtin && (
          <button type="button" onClick={() => onDeleteView(chosen)}>
            {t("project.deleteView")}
          </button>
        )}
      </div>
      <form
        className="project-bar"
        onSubmit={(e) => {
          e.preventDefault();
          if (!title.trim()) return;
          onSaveView(title.trim());
          setTitle("");
        }}
      >
        <input
          aria-label={t("project.viewName")}
          placeholder={t("project.viewName")}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          maxLength={80}
        />
        <button type="submit" disabled={!title.trim()}>
          {t("project.saveView")}
        </button>
      </form>
    </div>
  );
}
