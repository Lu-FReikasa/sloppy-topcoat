import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import type { CoreSystemRules } from "../types";

interface Props {
  api: ApiClient;
  rules: CoreSystemRules[];
  admin: boolean;
  onChanged: () => void;
  onError: (err: unknown) => void;
}

/** The fixed rule block (evidence discipline): everybody reads it, only an admin changes it (a
 *  new version each time); the assistant never can. */
export function SystemRules({ api, rules, admin, onChanged, onError }: Props) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState<string | null>(null);
  const [text, setText] = useState("");

  async function save(lang: "en" | "de") {
    try {
      await api.putSystemRules(lang, text.trim());
      setEditing(null);
      onChanged();
    } catch (err) {
      onError(err);
    }
  }

  return (
    <section className="core-slice" aria-label={t("instructions.system.heading")}>
      <h2>{t("instructions.system.heading")}</h2>
      <p className="muted small">
        {admin ? t("instructions.system.adminNote") : t("instructions.system.readOnly")}
      </p>
      <ul className="core-items">
        {rules.map((r) => (
          <li key={r.id} className="core-item" data-testid="system-rules">
            <p className="muted small">
              {t(`instructions.system.lang.${r.lang}`)} ·{" "}
              {t("memory.version", { value: r.version })}
            </p>
            {editing === r.lang ? (
              <div className="stack">
                <textarea
                  value={text}
                  rows={8}
                  maxLength={3000}
                  aria-label={t("instructions.system.heading")}
                  onChange={(e) => setText(e.target.value)}
                />
                <div className="row-actions">
                  <button
                    type="button"
                    className="primary"
                    disabled={!text.trim()}
                    onClick={() => void save(r.lang)}
                  >
                    {t("instructions.save")}
                  </button>
                  <button type="button" onClick={() => setEditing(null)}>
                    {t("instructions.cancel")}
                  </button>
                </div>
              </div>
            ) : (
              <>
                <p className="core-text">{r.text}</p>
                {admin && (
                  <div className="row-actions">
                    <button
                      type="button"
                      onClick={() => {
                        setEditing(r.lang);
                        setText(r.text);
                      }}
                    >
                      {t("instructions.edit")}
                    </button>
                  </div>
                )}
              </>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
