import { useCallback, useEffect, useState } from "react";
import type { TFunction } from "i18next";
import type { ApiClient } from "../api/client";
import type { Persona, PersonaLibrary } from "../types";

/** Fired when something changed the instructions (a proposal decided, a persona chosen): the
 *  persona prompt and the instructions page look again. */
export const INSTRUCTIONS_CHANGED = "instructions-changed";

export function announceInstructionsChange(): void {
  window.dispatchEvent(new Event(INSTRUCTIONS_CHANGED));
}

/** The persona library, loaded once and again whenever the instructions changed. */
export function usePersonas(api: ApiClient): {
  library: PersonaLibrary | null;
  reload: () => void;
} {
  const [library, setLibrary] = useState<PersonaLibrary | null>(null);
  const reload = useCallback(() => {
    api.personas().then(setLibrary, () => undefined);
  }, [api]);
  useEffect(() => {
    reload();
    window.addEventListener(INSTRUCTIONS_CHANGED, reload);
    return () => window.removeEventListener(INSTRUCTIONS_CHANGED, reload);
  }, [reload]);
  return { library, reload };
}

/** The shipped templates have a translated name; anything else shows the name it was given. */
export function personaName(t: TFunction, p: Pick<Persona, "key" | "name" | "scope">): string {
  if (p.scope === "household" && p.key) {
    return t(`persona.template.${p.key}`, { defaultValue: p.name });
  }
  return p.key === "legacy" ? t("persona.legacyName", { defaultValue: p.name }) : p.name;
}

/** "id@version" of a persona reference as stored on a message. */
export function splitRef(ref: string | null | undefined): { id: string; version: number } | null {
  if (!ref) return null;
  const [id, v] = ref.split("@");
  return id ? { id, version: Number(v) || 1 } : null;
}

export function pickLang(lang: string | undefined): "de" | "en" {
  return lang?.toLowerCase().startsWith("de") ? "de" : "en";
}
