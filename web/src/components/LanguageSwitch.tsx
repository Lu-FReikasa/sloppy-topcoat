import { useTranslation } from "react-i18next";
import { applyLanguage, isLanguage, LANGUAGES, type Language } from "../i18n";

// Language names in their own language, so everyone finds theirs.
const NAMES: Record<Language, string> = { en: "English", de: "Deutsch" };

interface Props {
  /** Stores the choice for the signed-in user; without it only this browser remembers. */
  onSave?: (lang: Language) => void;
}

export function LanguageSwitch({ onSave }: Props) {
  const { t, i18n } = useTranslation();
  const current = isLanguage(i18n.resolvedLanguage) ? i18n.resolvedLanguage : "en";
  return (
    <select
      className="language-switch"
      aria-label={t("header.language")}
      value={current}
      onChange={(e) => {
        const lang = e.target.value;
        if (!isLanguage(lang)) return;
        applyLanguage(lang);
        onSave?.(lang);
      }}
    >
      {LANGUAGES.map((l) => (
        <option key={l} value={l} lang={l}>
          {NAMES[l]}
        </option>
      ))}
    </select>
  );
}
