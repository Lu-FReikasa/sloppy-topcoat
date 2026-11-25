// The interface as data (docs/DESKTOP.md, D60, D70): what the hub serves about the programs the
// person is offered and their choices (hub/src/topcoat_hub/api/ui.py, proto/ui/modules.json).

export type PropValue = string | number | boolean | null;

/** One open view, as a module renders it. */
export interface LayoutWindow {
  id: string;
  module: string;
  docked: boolean;
  title?: string;
  props?: Record<string, PropValue>;
}

export interface UiAgentDefaults {
  tool_scope: {
    nodes: string[] | null;
    groups: string[] | null;
    internet: boolean | null;
    memory?: boolean | null;
  };
  persona: string | null;
  model_role: "fast" | "deep" | null;
  /** The frame of the program (D74): the tone of its assistant and the rules it keeps. */
  frame?: { tone: string; rules: string[] } | null;
}

/** The contract of a program's employee (hub agent_contract.py, plan step 1.9), in German. */
export interface AgentContractView {
  key: string;
  title: string;
  purpose: string;
  tasks: { text: string; example: string }[];
  tools: string[];
  never: string[];
  needs_click: string[];
  talks_to: { to: string; for: string }[];
}

/** A program of the desktop (a workspace template, D60). */
export interface Workspace {
  key: string;
  title: string;
  /** An i18n key for the built-in ones; the title is the fallback. */
  title_key: string | null;
  icon: string;
  position: number;
  /** Its views, in the order of its navigation. */
  modules: string[];
  /** The view it starts with (the first of the list); null = its first module. */
  open?: string[] | null;
  agent: UiAgentDefaults;
  requires_role: "admin" | null;
  builtin: boolean;
  rev: number;
}

export interface UiModuleInfo {
  id: string;
  kind: "page" | "widget";
  admin: boolean;
  needs: string | null;
  /** The person may add and use it. */
  allowed: boolean;
}

export interface DesktopPrefs {
  open: string[];
  minimised: string[];
  focus: string | null;
  maximised: string | null;
  views: Record<string, { module: string; props: Record<string, PropValue> }>;
}

import type { ThemeManifest } from "../desktop/theme";

/** The look of the desktop is the key of a theme (docs/THEMES.md): a built-in one or one of the
 *  custom themes the hub stores. */
export type Look = string;

export interface UiPrefs {
  /** The hue (0–359): the accent of the terminal, the housing of the control room. */
  hue: number;
  look: Look;
  desktop: DesktopPrefs;
}

export interface UiBootstrap {
  /** The custom themes of this hub (docs/THEMES.md); the built-in ones come with the web build. */
  themes?: ThemeManifest[];
  v: 1;
  access: { admin: boolean; profile: string | null; denied_tools: string[] };
  prefs: UiPrefs;
  modules: UiModuleInfo[];
  workspaces: Workspace[];
}

// --- the admin side ----------------------------------------------------------------------------

export interface UiTemplateView {
  key: string;
  title: string;
  title_key: string | null;
  icon: string;
  position: number;
  modules: string[];
  agent: UiAgentDefaults;
  requires_role: "admin" | null;
  builtin: boolean;
  rev: number;
}

export interface UiGrant {
  kind: "workspace" | "tool";
  key: string;
  effect: "allow" | "deny";
}

export interface UiProfileView {
  name: string;
  description: string;
  default: boolean;
  builtin: boolean;
  grants: UiGrant[];
}

export interface UiUserView {
  id: string;
  name: string;
  role: "admin" | "user";
  profile: string | null;
}
