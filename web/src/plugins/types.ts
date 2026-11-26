/** The registry of data plug-ins as the hub sends it (`/api/plugins`, D105). */

export interface PluginRow {
  key: string;
  version: string;
  enabled: boolean;
  builtin: boolean;
  title_key: string | null;
  updated_at: string | null;
}

export interface PluginVersionView {
  version: string;
  sha256: string;
  installed_by: string | null;
  installed_at: string | null;
  manifest: Record<string, unknown>;
}

export interface PluginChangeView {
  action: "register" | "install" | "update" | "rollback" | "enable" | "disable" | "remove";
  from_version: string | null;
  to_version: string | null;
  by: string | null;
  at: string | null;
}

export interface PluginDetail extends PluginRow {
  /** newest first */
  versions: PluginVersionView[];
  /** newest first */
  changes: PluginChangeView[];
}
