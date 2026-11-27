/**
 * Names the hub and the nodes send in English (issue #11): the titles of the block programs that
 * ship with the hub and the descriptions of node capabilities. The GUI translates them by a stable
 * id (program name, capability name) and shows the sent text for an id it does not know, e.g. a
 * capability of a newer node. The node protocol stays as it is.
 */

type T = (key: string, o?: Record<string, unknown>) => string;

/** One segment of a key: lower case, digits and underscores; a dot would nest the key. */
const SEGMENT = /^[a-z][a-z0-9_]*$/;

/** Catalogue lookups only for own keys: `constructor` and friends are not translations. */
const RESERVED = new Set(Object.getOwnPropertyNames(Object.prototype));

function safe(segment: string | undefined): segment is string {
  return segment !== undefined && SEGMENT.test(segment) && !RESERVED.has(segment);
}

/** The title of a block program: translated for one that ships, as saved for a person's own. */
export function blockProgramTitle(p: { name: string; title: string; builtin: boolean }, t: T): string {
  if (!p.builtin || !safe(p.name)) return p.title;
  return t(`blocksPage.programs.${p.name}`, { defaultValue: p.title });
}

/** A program's key as the hub allows it (`hub/src/topcoat_hub/ui/store.py`, KEY). */
const PROGRAM_KEY = /^[a-z0-9][a-z0-9-]{0,39}$/;

/** The title of a program (an application of the desktop, issue #181): by its `title_key`, else,
 *  for one that ships, by `workspace.<key>` (the key the hub gives a built-in program, ui/seeds.py),
 *  else as the hub sent it. A key the catalogue lacks falls through to the next step, so a row of
 *  an older hub or a program of a newer one still reads in the GUI's language where it can. */
export function programTitle(
  w: { key: string; title: string; title_key: string | null; builtin: boolean },
  t: T,
): string {
  const byKey =
    w.builtin && PROGRAM_KEY.test(w.key) && !RESERVED.has(w.key)
      ? t(`workspace.${w.key}`, { defaultValue: w.title })
      : w.title;
  return w.title_key ? t(w.title_key, { defaultValue: byKey }) : byKey;
}

/** What a capability does, in the GUI's language; the node's own text for an unknown name. */
export function capabilityDescription(c: { name: string; description: string }, t: T): string {
  const parts = c.name.split(".");
  const [group, action] = parts;
  if (parts.length !== 2 || !safe(group) || !safe(action)) return c.description;
  return t(`tools.cap.${group}.${action}`, { defaultValue: c.description });
}
