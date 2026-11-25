// Reading files on a host (HOST-01, ARCHITECTURE D89): the folders a person releases per node and
// the capabilities they switch on. Pure functions, so the page stays thin and the rules are tested.

/** The two capabilities the folders of `tools.read_roots` switch on. */
export const FILE_CAPABILITIES = ["files.list", "files.read"] as const;

/** The fixed read-only checks of a Windows node (node/internal/tools/cap_winread.go). */
export const WINDOWS_CAPABILITIES = [
  "windows.system",
  "windows.power",
  "windows.events",
  "windows.services",
  "windows.devices",
  "windows.storage",
  "windows.network",
  "windows.startup",
  "windows.programs",
  "windows.defender",
  "windows.processes",
] as const;

export type ReadRootProblem =
  | "empty"
  | "notAbsolute"
  | "unc"
  | "filesystemRoot"
  | "dots"
  | "stream"
  | "tooLong";

/** Why a folder cannot be a read root (the node and the config schema refuse the same), or null. */
export function readRootProblem(raw: string): ReadRootProblem | null {
  const p = raw.trim();
  if (p === "") return "empty";
  if (p.length > 512) return "tooLong";
  if (p.startsWith("//") || p.startsWith("\\\\") || p.startsWith("/\\") || p.startsWith("\\/")) {
    return "unc";
  }
  const drive = /^[A-Za-z]:[\\/]/.test(p);
  if (!p.startsWith("/") && !drive) return "notAbsolute";
  const body = drive ? p.slice(2) : p;
  if (drive && body.includes(":")) return "stream";
  const segments = body.split(/[\\/]/).filter((s) => s !== "");
  if (segments.some((s) => s === "." || s === "..")) return "dots";
  if (segments.length === 0) return "filesystemRoot";
  return null;
}

/** A whole user profile folder (C:\Users\name, /home/name, /root): browsers, keys and mail live in it. */
export function isProfileRoot(raw: string): boolean {
  return /^([A-Za-z]:[\\/]users[\\/][^\\/]+|\/home\/[^/]+|\/root)[\\/]?$/i.test(raw.trim());
}

/** The folders the GUI suggests for a node. Only a suggestion: nothing is on until it is saved. */
export function suggestedRoots(os: string): { key: "logs"; path: string }[] {
  if (os.toLowerCase().startsWith("windows")) {
    return [
      { key: "logs", path: "C:\\ProgramData\\topcoat-node\\logs" },
    ];
  }
  return [{ key: "logs", path: "/var/lib/topcoat-node/logs" }];
}

type Config = Record<string, unknown>;

function strings(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

/** The released folders of a node's config. */
export function readRootsOf(config: Config | null): string[] {
  const tools = config?.tools;
  return tools !== null && typeof tools === "object" ? strings((tools as Config).read_roots) : [];
}

function withInvoke(config: Config): string[] {
  const actions = strings(config.allowed_actions);
  return actions.includes("capability.invoke") ? actions : [...actions, "capability.invoke"];
}

/**
 * The config with these folders released. Saving folders is what switches `files.list` and
 * `files.read` on; an empty list switches them off again. Nothing is on before a person saves.
 */
export function withReadRoots(config: Config, roots: string[]): Config {
  const tools: Config = { ...((config.tools as Config | undefined) ?? {}) };
  const caps = strings(config.capabilities).filter(
    (c) => !(FILE_CAPABILITIES as readonly string[]).includes(c),
  );
  if (roots.length === 0) {
    delete tools.read_roots;
    return { ...config, tools, capabilities: caps };
  }
  tools.read_roots = roots;
  return {
    ...config,
    tools,
    capabilities: [...caps, ...FILE_CAPABILITIES],
    allowed_actions: withInvoke(config),
  };
}

/** The config with the Windows read checks switched on or off. */
export function withWindowsChecks(config: Config, on: boolean): Config {
  const caps = strings(config.capabilities).filter(
    (c) => !(WINDOWS_CAPABILITIES as readonly string[]).includes(c),
  );
  if (!on) return { ...config, capabilities: caps };
  return {
    ...config,
    capabilities: [...caps, ...WINDOWS_CAPABILITIES],
    allowed_actions: withInvoke(config),
  };
}

/** Are all Windows read checks switched on in this config? */
export function windowsChecksOn(config: Config | null): boolean {
  const caps = strings(config?.capabilities);
  return WINDOWS_CAPABILITIES.every((c) => caps.includes(c));
}
