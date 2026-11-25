import type { ToolCapability } from "./types";

/** A JSON template for a capability's input: its required fields, empty. */
export function inputTemplate(cap: ToolCapability | undefined): string {
  const props = cap?.input_schema.properties ?? {};
  const out: Record<string, unknown> = {};
  for (const key of cap?.input_schema.required ?? []) {
    const type = props[key]?.type;
    out[key] = type === "integer" || type === "number" ? 0 : type === "boolean" ? false : "";
  }
  return JSON.stringify(out, null, 2);
}

/** The input object typed into the box; anything but a JSON object is refused. */
export function parseInput(text: string): Record<string, unknown> | null {
  const trimmed = text.trim();
  if (trimmed === "") return {};
  try {
    const v: unknown = JSON.parse(trimmed);
    return v !== null && typeof v === "object" && !Array.isArray(v)
      ? (v as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

export type CapabilityStatus = "usable" | "not_granted" | "off" | "needs_confirmation";

/** Where a capability stands for the signed-in user. */
export function capabilityStatus(
  cap: ToolCapability,
  enabled: string[],
  usable: string[],
): CapabilityStatus {
  if (!enabled.includes(cap.name)) return "off";
  if (!cap.read_only) return "needs_confirmation";
  return usable.includes(cap.name) ? "usable" : "not_granted";
}

/** The text of a capability's output: a log as plain lines, anything else as JSON. */
export function outputText(output: unknown): string {
  if (output !== null && typeof output === "object" && !Array.isArray(output)) {
    const o = output as Record<string, unknown>;
    if (typeof o.log === "string" && Object.keys(o).every((k) => typeof o[k] !== "object")) {
      const head = Object.entries(o)
        .filter(([k]) => k !== "log")
        .map(([k, v]) => `${k}: ${String(v)}`)
        .join("  ·  ");
      return `${head}\n\n${o.log}`;
    }
    if (typeof o.content === "string") {
      const head = Object.entries(o)
        .filter(([k, v]) => k !== "content" && typeof v !== "object")
        .map(([k, v]) => `${k}: ${String(v)}`)
        .join("  ·  ");
      return `${head}\n\n${o.content}`;
    }
  }
  return JSON.stringify(output, null, 2);
}

/** Toggle a capability in a grant list; "*" (all) excludes the single names. */
export function toggleGrant(current: string[], name: string): string[] {
  if (name === "*") return current.includes("*") ? [] : ["*"];
  const base = current.filter((c) => c !== "*");
  return base.includes(name) ? base.filter((c) => c !== name) : [...base, name].sort();
}
