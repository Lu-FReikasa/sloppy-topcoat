import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import de from "./de.json";
import en from "./en.json";

// English is the default GUI language, German is fully supported. Both catalogues must
// have the same keys (keys.test.ts checks it in CI).
export const resources = {
  en: { translation: en },
  de: { translation: de },
} as const;

export type Language = keyof typeof resources;
export const LANGUAGES: readonly Language[] = ["en", "de"];
export const DEFAULT_LANGUAGE: Language = "en";

/** Cached choice so the login page already speaks the user's language. */
export const LANGUAGE_KEY = "topcoat.language";

export function isLanguage(value: unknown): value is Language {
  return typeof value === "string" && (LANGUAGES as readonly string[]).includes(value);
}

/** First supported browser language (de-AT → de), else English. */
export function browserLanguage(languages: readonly string[] = navigatorLanguages()): Language {
  for (const tag of languages) {
    const base = tag.toLowerCase().split("-")[0];
    if (isLanguage(base)) return base;
  }
  return DEFAULT_LANGUAGE;
}

function navigatorLanguages(): readonly string[] {
  if (typeof navigator === "undefined") return [];
  return navigator.languages?.length ? navigator.languages : [navigator.language ?? ""];
}

export function cachedLanguage(): Language | null {
  try {
    const v = window.localStorage.getItem(LANGUAGE_KEY);
    return isLanguage(v) ? v : null;
  } catch {
    return null;
  }
}

/** Switch the GUI language and remember it in this browser. */
export function applyLanguage(lang: Language): void {
  try {
    window.localStorage.setItem(LANGUAGE_KEY, lang);
  } catch {
    // ignore: the choice then only lives for this page load
  }
  if (typeof document !== "undefined") document.documentElement.lang = lang;
  void i18n.changeLanguage(lang);
}

const initial = cachedLanguage() ?? browserLanguage();
if (typeof document !== "undefined") document.documentElement.lang = initial;

void i18n.use(initReactI18next).init({
  resources,
  lng: initial,
  fallbackLng: DEFAULT_LANGUAGE,
  supportedLngs: [...LANGUAGES],
  interpolation: { escapeValue: false },
  initAsync: false,
});

export default i18n;
