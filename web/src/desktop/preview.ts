// The live preview of a theme that is being edited (docs/THEMES.md): the editor puts the draft
// here, the desktop draws with it at once; nothing is stored until the editor saves. One draft at a
// time; `null` ends the preview.
import { useEffect, useState } from "react";
import type { ThemeManifest } from "./theme";

const EVENT = "topcoat:theme-preview";
let current: ThemeManifest | null = null;

export function previewTheme(draft: ThemeManifest | null): void {
  current = draft;
  window.dispatchEvent(new Event(EVENT));
}

export function usePreviewTheme(): ThemeManifest | null {
  const [draft, setDraft] = useState<ThemeManifest | null>(current);
  useEffect(() => {
    const on = () => setDraft(current);
    window.addEventListener(EVENT, on);
    on();
    return () => window.removeEventListener(EVENT, on);
  }, []);
  return draft;
}
