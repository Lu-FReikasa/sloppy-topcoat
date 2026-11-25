import { createContext, useContext } from "react";
import type { Language } from "../i18n";
import type { ThemeManifest } from "../desktop/theme";

/** What the shell around the pages (the desktop or the web navigation) lets Settings › General
 *  change: the look, the hue and the stored language. The shell owns these preferences; a page
 *  outside a shell (a test, the login) gets null and leaves the look out. */
export interface LookControls {
  theme: ThemeManifest;
  themes: readonly ThemeManifest[];
  hue: number;
  onTheme: (key: string) => void;
  onHue: (hue: number) => void;
  onSaveLanguage?: (lang: Language) => void;
}

export const LookContext = createContext<LookControls | null>(null);

export function useLook(): LookControls | null {
  return useContext(LookContext);
}
