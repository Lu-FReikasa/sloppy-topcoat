import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { fromInputValue } from "./time";
import type { Project, ProjectTemplate } from "./types";

/** The templates of the person: the structure of a project (topics, tasks, labels, open relations,
 *  dates and reminders relative to the earliest one), never its reports, status or history. A new
 *  project from a template is made all or nothing. */
export function TemplatesPage({
  api,
  zone,
  tick,
  onCreated,
  onChanged,
  onError,
}: {
  api: ApiClient;
  zone: string;
  tick: number;
  onCreated: (project: Project) => void;
  onChanged: (message: string) => void;
  onError: (err: unknown) => void;
}) {
  const { t } = useTranslation();
  const [templates, setTemplates] = useState<ProjectTemplate[] | null>(null);
  const [using, setUsing] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [start, setStart] = useState("");

  const load = useCallback(async () => {
    try {
      setTemplates((await api.projectTemplates()).templates);
    } catch (err) {
      onError(err);
    }
  }, [api, onError]);
  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load, tick]);

  const create = async (e: FormEvent, template: ProjectTemplate) => {
    e.preventDefault();
    if (!title.trim()) return;
    try {
      const startAt = start ? fromInputValue(start, zone) : null;
      const project = await api.createProjectFromTemplate(template.id, {
        title: title.trim(),
        ...(startAt ? { start_at: startAt } : {}),
      });
      setUsing(null);
      setTitle("");
      setStart("");
      onCreated(project);
    } catch (err) {
      onError(err);
    }
  };
  const remove = async (template: ProjectTemplate) => {
    try {
      await api.deleteProjectTemplate(template.id);
      await load();
      onChanged(t("project.templateDeleted"));
    } catch (err) {
      onError(err);
    }
  };

  return (
    <section className="project-templates" aria-label={t("project.templates")}>
      <h2>{t("project.templates")}</h2>
      {templates !== null && templates.length === 0 && (
        <p className="project-muted">{t("project.templatesEmpty")}</p>
      )}
      <ul className="project-list">
        {(templates ?? []).map((tpl) => (
          <li key={tpl.id}>
            <div className="project-row">
              <span className="project-name">{tpl.name}</span>
              <span className="project-muted">
                {t("project.templateNodes", { count: tpl.nodes })}
                {tpl.has_dates ? ` · ${t("project.templateDates")}` : ""}
              </span>
              <button
                type="button"
                onClick={() => setUsing(using === tpl.id ? null : tpl.id)}
                aria-expanded={using === tpl.id}
              >
                {t("project.fromTemplate")}: {tpl.name}
              </button>
              <button type="button" onClick={() => void remove(tpl)}>
                {t("project.deleteTemplate")}: {tpl.name}
              </button>
            </div>
            {tpl.description && (
              <p className="project-muted">{tpl.description}</p>
            )}
            {using === tpl.id && (
              <form
                className="project-form"
                onSubmit={(e) => void create(e, tpl)}
              >
                <label>
                  {t("project.newProjectTitle")}
                  <input
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                    maxLength={200}
                  />
                </label>
                <label>
                  {t("project.startAt")}
                  <input
                    type="datetime-local"
                    value={start}
                    onChange={(e) => setStart(e.target.value)}
                  />
                </label>
                <p className="project-muted">{t("project.startHelp")}</p>
                <button type="submit" disabled={!title.trim()}>
                  {t("project.createFromTemplate")}
                </button>
              </form>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
