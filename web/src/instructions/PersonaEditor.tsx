import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { describeError, toActionError } from "../errors";
import type { Persona, PersonaComponents } from "../types";

interface Props {
  api: ApiClient;
  /** Edit this persona; none = a new one built from blocks. */
  persona?: Persona;
  /** An admin's template (shared with everybody) instead of a personal persona. */
  template?: boolean;
  onSaved: () => void;
  onCancel: () => void;
}

const HUMORS = [0, 1, 2] as const;

/** A persona is built from blocks, never free-written by a model: a role, form of address, humor
 *  (always off for errors and risk), length, transparency, how to ask back, emojis and one fixed
 *  example sentence. The old system prompt is a persona that is just a text. */
export function PersonaEditor({ api, persona, template, onSaved, onCancel }: Props) {
  const { t, i18n } = useTranslation();
  const c: PersonaComponents = persona?.components ?? {};
  const freeText = persona != null && c.free_text !== undefined;
  const [name, setName] = useState(persona?.name ?? "");
  const [text, setText] = useState(c.free_text ?? "");
  const [roleDe, setRoleDe] = useState(c.role?.de ?? "");
  const [roleEn, setRoleEn] = useState(c.role?.en ?? "");
  const [address, setAddress] = useState<"du" | "sie">(c.address ?? "du");
  const [humor, setHumor] = useState<0 | 1 | 2>(c.humor ?? 1);
  const [length, setLength] = useState<"short" | "long">(c.length ?? "short");
  const [ask, setAsk] = useState<"ask_first" | "read_first">(c.ask ?? "ask_first");
  const [transparency, setTransparency] = useState(c.transparency ?? true);
  const [emojis, setEmojis] = useState(c.emojis ?? false);
  const [exDe, setExDe] = useState(persona?.example.de ?? "");
  const [exEn, setExEn] = useState(persona?.example.en ?? "");
  // D127: the voice this persona speaks with (a voice of the node's text to speech)
  const [voice, setVoice] = useState(
    typeof persona?.defaults.voice === "string" ? persona.defaults.voice : "",
  );
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function save() {
    setError(null);
    setSaving(true);
    const components: PersonaComponents = freeText
      ? { free_text: text.trim() }
      : {
          role: {
            ...(roleDe.trim() ? { de: roleDe.trim() } : {}),
            ...(roleEn.trim() ? { en: roleEn.trim() } : {}),
          },
          address,
          humor,
          length,
          ask,
          transparency,
          emojis,
          evidence: true,
        };
    const example = {
      ...(exDe.trim() ? { de: exDe.trim() } : {}),
      ...(exEn.trim() ? { en: exEn.trim() } : {}),
    };
    const spoken = voice.trim() || null;
    try {
      if (persona) {
        await api.updatePersona(persona.id, { name: name.trim(), components, example, voice: spoken });
      } else {
        const made = await api.createPersona({
          name: name.trim(),
          components,
          example,
          scope: template ? "household" : "user",
        });
        if (spoken) await api.updatePersona(made.id, { voice: spoken });
      }
      onSaved();
    } catch (err) {
      setError(describeError(toActionError(err), t, i18n));
    } finally {
      setSaving(false);
    }
  }

  const ready = name.trim() && (freeText ? text.trim() : roleDe.trim() || roleEn.trim());

  return (
    <form
      className="persona-editor stack"
      aria-label={t("persona.editor.heading")}
      onSubmit={(e) => {
        e.preventDefault();
        if (ready) void save();
      }}
    >
      <h3>
        {persona ? t("persona.editor.edit", { name: persona.name }) : t("persona.editor.new")}
      </h3>
      <label>
        <span className="fact-label">{t("persona.editor.name")}</span>
        <input value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
      </label>
      {freeText ? (
        <label>
          <span className="fact-label">{t("persona.editor.freeText")}</span>
          <textarea
            value={text}
            rows={6}
            maxLength={4000}
            onChange={(e) => setText(e.target.value)}
          />
        </label>
      ) : (
        <>
          <label>
            <span className="fact-label">{t("persona.editor.roleDe")}</span>
            <textarea
              value={roleDe}
              rows={3}
              maxLength={700}
              onChange={(e) => setRoleDe(e.target.value)}
            />
          </label>
          <label>
            <span className="fact-label">{t("persona.editor.roleEn")}</span>
            <textarea
              value={roleEn}
              rows={3}
              maxLength={700}
              onChange={(e) => setRoleEn(e.target.value)}
            />
          </label>
          <div className="persona-blocks">
            <label className="picker">
              <span className="fact-label">{t("persona.block.address")}</span>
              <select value={address} onChange={(e) => setAddress(e.target.value as "du" | "sie")}>
                <option value="du">{t("persona.option.du")}</option>
                <option value="sie">{t("persona.option.sie")}</option>
              </select>
            </label>
            <label className="picker">
              <span className="fact-label">{t("persona.block.humor")}</span>
              <select value={humor} onChange={(e) => setHumor(Number(e.target.value) as 0 | 1 | 2)}>
                {HUMORS.map((h) => (
                  <option key={h} value={h}>
                    {t(`persona.option.humor${h}`)}
                  </option>
                ))}
              </select>
            </label>
            <label className="picker">
              <span className="fact-label">{t("persona.block.length")}</span>
              <select
                value={length}
                onChange={(e) => setLength(e.target.value as "short" | "long")}
              >
                <option value="short">{t("persona.option.short")}</option>
                <option value="long">{t("persona.option.long")}</option>
              </select>
            </label>
            <label className="picker">
              <span className="fact-label">{t("persona.block.ask")}</span>
              <select
                value={ask}
                onChange={(e) => setAsk(e.target.value as "ask_first" | "read_first")}
              >
                <option value="ask_first">{t("persona.option.ask_first")}</option>
                <option value="read_first">{t("persona.option.read_first")}</option>
              </select>
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={transparency}
                onChange={(e) => setTransparency(e.target.checked)}
              />
              {t("persona.block.transparency")}
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={emojis}
                onChange={(e) => setEmojis(e.target.checked)}
              />
              {t("persona.block.emojis")}
            </label>
          </div>
          <p className="muted small">{t("persona.editor.humorNote")}</p>
          <label>
            <span className="fact-label">{t("persona.editor.exampleDe")}</span>
            <input value={exDe} maxLength={300} onChange={(e) => setExDe(e.target.value)} />
          </label>
          <label>
            <span className="fact-label">{t("persona.editor.exampleEn")}</span>
            <input value={exEn} maxLength={300} onChange={(e) => setExEn(e.target.value)} />
          </label>
        </>
      )}
      <label>
        <span className="fact-label">{t("persona.editor.voice")}</span>
        <input
          value={voice}
          maxLength={128}
          pattern="[A-Za-z0-9][A-Za-z0-9._\-]*"
          spellCheck={false}
          onChange={(e) => setVoice(e.target.value)}
        />
        <span className="muted small">{t("persona.editor.voiceHint")}</span>
      </label>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="row-actions">
        <button type="submit" className="primary" disabled={!ready || saving}>
          {t("instructions.save")}
        </button>
        <button type="button" onClick={onCancel}>
          {t("instructions.cancel")}
        </button>
      </div>
    </form>
  );
}
