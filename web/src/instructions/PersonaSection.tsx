import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { Persona, PersonaLibrary, PersonaStat } from "../types";
import { PersonaEditor } from "./PersonaEditor";
import { announceInstructionsChange, personaName } from "./personaUi";

interface Props {
  api: ApiClient;
  library: PersonaLibrary;
  admin: boolean;
  onChanged: () => void;
  onError: (err: unknown) => void;
  onWizard: () => void;
}

/** The voice of the assistant: the persona new conversations start with, the shared templates, the
 *  user's own personas and how each one did (the user's own count, nobody else's). */
export function PersonaSection({ api, library, admin, onChanged, onError, onWizard }: Props) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState<{ persona?: Persona; template?: boolean } | null>(null);
  const [stats, setStats] = useState<PersonaStat[]>([]);

  useEffect(() => {
    api.personaStats().then(setStats, () => undefined);
  }, [api, library]);

  const statOf = (p: Persona) => stats.find((s) => s.persona_id === p.id);

  async function run(fn: () => Promise<unknown>) {
    try {
      await fn();
      announceInstructionsChange();
      onChanged();
    } catch (err) {
      onError(err);
    }
  }

  function card(p: Persona) {
    const stat = statOf(p);
    const isDefault = library.default_id === p.id;
    const mayEdit = p.scope === "user" || admin;
    return (
      <li key={p.id} className="persona-card" data-testid="persona-card">
        <p className="persona-head">
          <strong>{personaName(t, p)}</strong>{" "}
          <span className="muted small">
            v{p.version} · {t("persona.tokens", { count: p.tokens })}
            {p.scope === "household" ? ` · ${t("persona.shared")}` : ""}
          </span>
          {isDefault && (
            <span className="tag" data-testid="persona-default">
              {t("persona.default")}
            </span>
          )}
        </p>
        {p.truncated && (
          <p className="notice-line small" role="status" data-testid="persona-truncated">
            {t("persona.truncated", { max: library.max_tokens })}
          </p>
        )}
        <details>
          <summary>{t("persona.showText")}</summary>
          <pre className="plain-pre persona-text">{p.text}</pre>
        </details>
        {stat && (
          <p className="muted small" data-testid="persona-stat">
            {t("persona.stat", { answers: stat.answers, helped: stat.helped, unfit: stat.unfit })}
          </p>
        )}
        <div className="row-actions">
          {!isDefault && (
            <button type="button" onClick={() => void run(() => api.setDefaultPersona(p.id))}>
              {t("persona.useDefault")}
            </button>
          )}
          {mayEdit && (
            <button
              type="button"
              onClick={() => setEditing({ persona: p, template: p.scope === "household" })}
            >
              {t("instructions.edit")}
            </button>
          )}
          {mayEdit && (
            <button
              type="button"
              className="danger"
              onClick={() => void run(() => api.archivePersona(p.id))}
            >
              {t("instructions.remove")}
            </button>
          )}
        </div>
      </li>
    );
  }

  return (
    <section className="persona-section" aria-label={t("persona.heading")}>
      <h2>{t("persona.heading")}</h2>
      <p className="muted small">{t("persona.intro")}</p>
      <label className="picker">
        <span className="fact-label">{t("persona.defaultLabel")}</span>
        <select
          value={library.default_id ?? ""}
          aria-label={t("persona.defaultLabel")}
          onChange={(e) => void run(() => api.setDefaultPersona(e.target.value || null))}
        >
          <option value="">{t("persona.none")}</option>
          {[...library.templates, ...library.own].map((p) => (
            <option key={p.id} value={p.id}>
              {personaName(t, p)}
            </option>
          ))}
        </select>
      </label>
      <p className="muted small">{t("persona.defaultNote")}</p>
      <div className="row-actions">
        <button type="button" onClick={onWizard}>
          {t("persona.wizard.start")}
        </button>
        <button type="button" onClick={() => setEditing({})}>
          {t("persona.new")}
        </button>
        {admin && (
          <button type="button" onClick={() => setEditing({ template: true })}>
            {t("persona.newTemplate")}
          </button>
        )}
      </div>
      {editing && (
        <PersonaEditor
          api={api}
          persona={editing.persona}
          template={editing.template}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            announceInstructionsChange();
            onChanged();
          }}
        />
      )}
      <h3>{t("persona.templates")}</h3>
      <ul className="persona-list">{library.templates.map(card)}</ul>
      <h3>{t("persona.own")}</h3>
      {library.own.length === 0 ? (
        <p className="muted small">{t("persona.noOwn")}</p>
      ) : (
        <ul className="persona-list">{library.own.map(card)}</ul>
      )}
    </section>
  );
}
