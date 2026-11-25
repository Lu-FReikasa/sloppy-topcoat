// The tool groups a workspace or a profile can name (hub/src/topcoat_hub/tool_groups.py and
// ui/access.py). `web` is not among them: the internet is its own switch.

/** Groups that exist today. */
export const TOOL_GROUPS = [
  "docker",
  "journal",
  "host",
  "network",
  "monitors",
  "reach",
  "notes",
  "identity",
  "topcoat",
  "github",
  "shell",
  "workshop",
] as const;

/** Groups a workspace may already name although no tool exists yet (the office, music and image
 *  workspaces need them). */
export const PLANNED_GROUPS = ["docs", "mail", "image", "chords"] as const;

export const ALL_GROUPS: readonly string[] = [...TOOL_GROUPS, ...PLANNED_GROUPS];

export function groupLabel(t: (key: string, o?: Record<string, unknown>) => string, key: string): string {
  const planned = (PLANNED_GROUPS as readonly string[]).includes(key);
  return planned ? t(`layout.group.${key}`) : t(`scope.name.${key}`, { defaultValue: key });
}
