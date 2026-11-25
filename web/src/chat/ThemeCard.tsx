import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { UnauthorizedError, type ApiClient } from "../api/client";
import { previewTheme, usePreviewTheme } from "../desktop/preview";
import { cssWarnings, themeProblems, type ThemeManifest } from "../desktop/theme";
import { describeError, toActionError } from "../errors";

/** The api a theme card in an answer uses to store the theme. Set where a chat has one; without
 *  it a theme in a code block is only text. */
export const ThemeCardContext = createContext<ApiClient | null>(null);

/** A code block that holds one whole theme document (docs/THEMES.md): the same shape the themes
 *  page stores. Anything else, including a theme with problems, is not a card. */
export function parseTheme(code: string): ThemeManifest | null {
  const text = code.trim();
  if (!text.startsWith("{")) return null;
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof doc !== "object" || doc === null || Array.isArray(doc)) return null;
  const m = doc as Record<string, unknown>;
  // only a document that claims to be a theme gets a card; its problems are then shown
  if (!("key" in m && "layout" in m && "bar" in m && "chrome" in m && "motion" in m)) return null;
  return normaliseTheme(m) as unknown as ThemeManifest;
}

const SHAPES: Record<string, [string, Record<string, unknown>]> = {
  layout: ["kind", {}],
  bar: ["position", { items: ["start", "programs", "clock", "status", "me"] }],
  motion: ["style", { ms: 150 }],
};
const ALLOWED: Record<string, string[]> = {
  layout: ["kind"],
  bar: ["position", "items"],
  chrome: ["buttons", "titlebar"],
  motion: ["style", "ms"],
};

/** The theme in the shape the schema wants, from the shapes small models write (`"layout":
 *  "tile"`, extra keys in `layout`): the same rule as the hub's `ui.themes.normalise`, so the
 *  preview and the stored theme are the same. A value that is still wrong stays and is named. */
export function normaliseTheme(doc: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...doc };
  for (const [field, [key, rest]] of Object.entries(SHAPES)) {
    const value = out[field];
    if (typeof value === "string") out[field] = { [key]: value, ...rest };
  }
  for (const [field, keys] of Object.entries(ALLOWED)) {
    const value = out[field];
    if (value && typeof value === "object" && !Array.isArray(value)) {
      out[field] = Object.fromEntries(
        Object.entries(value as Record<string, unknown>).filter(([k]) => keys.includes(k)),
      );
    }
  }
  return out;
}

/** Under a theme in an answer: what the theme is, a preview on this desktop that stores nothing,
 *  and the one click that stores it for everybody to choose (D15: the person clicks, the model
 *  never does; the hub checks the document again and only an admin may store it). */
export function ThemeCard({ theme }: { theme: ThemeManifest }) {
  const { t, i18n } = useTranslation();
  const api = useContext(ThemeCardContext);
  const shown = usePreviewTheme();
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const problems = useMemo(() => themeProblems(theme), [theme]);
  const warnings = useMemo(() => cssWarnings(theme.css ?? ""), [theme]);
  const paper = theme.base === "schreibtisch";
  const fontVar = (theme.css ?? "").includes("--font-family");
  const previewing = shown?.key === theme.key && shown.name === theme.name;

  // the preview belongs to this card: it ends when the card goes away
  useEffect(
    () => () => {
      if (previewing) previewTheme(null);
    },
    [previewing],
  );

  async function store() {
    if (!api) return;
    setBusy(true);
    setError(null);
    try {
      const doc: ThemeManifest = { ...theme };
      if (!doc.css) delete doc.css;
      await api.putTheme(doc.key, doc);
      setSaved(true);
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) setError(describeError(toActionError(err), t, i18n));
    } finally {
      setBusy(false);
    }
  }

  const css = theme.css?.length ?? 0;
  return (
    <div className="theme-card" data-testid="theme-card">
      <p>
        <strong>{theme.name}</strong> <code>{theme.key}</code>
      </p>
      <p className="muted small">
        {t("themeCard.summary", {
          layout: theme.layout?.kind,
          bar: theme.bar?.position,
          motion: theme.motion?.style,
          css,
        })}
      </p>
      {(warnings.classes.length > 0 || warnings.lines.length > 0 || paper || fontVar) && (
        <ul className="small" data-testid="theme-warnings">
          {paper && <li>{t("themeCard.warnPaper")}</li>}
          {fontVar && <li>{t("themeCard.warnFontVar")}</li>}
          {warnings.classes.length > 0 && (
            <li>{t("themeCard.warnClasses", { list: warnings.classes.join(", ") })}</li>
          )}
          {warnings.lines.length > 0 && (
            <li>{t("themeCard.warnSemicolon", { lines: warnings.lines.slice(0, 8).join(", ") })}</li>
          )}
        </ul>
      )}
      {problems.length > 0 ? (
        <ul className="error small" role="alert">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      ) : (
        api && (
          <div className="theme-card-actions">
            <button
              type="button"
              onClick={() => previewTheme(previewing ? null : theme)}
              aria-pressed={previewing}
            >
              {previewing ? t("themeCard.previewEnd") : t("themeCard.preview")}
            </button>
            <button type="button" disabled={busy || saved} onClick={() => void store()}>
              {t("themeCard.create")}
            </button>
          </div>
        )
      )}
      {saved && <p role="status">{t("themeCard.saved", { name: theme.name })}</p>}
      {error && (
        <p className="error small" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
