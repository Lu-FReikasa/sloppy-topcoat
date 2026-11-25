/** A change to a project, from the hub's stream (``project_changed``, sent after the change is
 *  committed, only to the person's own streams). The app announces it, an open project page
 *  reloads (the maintainer, 2026-11-17: an agent's change showed only after leaving the project). */

const PROJECT_CHANGED = "topcoat:project-changed";

export function announceProjectChanged(projectId: string): void {
  window.dispatchEvent(new CustomEvent<string>(PROJECT_CHANGED, { detail: projectId }));
}

/** The project id of a ``project_changed`` event's data, or null. */
export function projectOfEvent(data: string): string | null {
  try {
    const parsed = JSON.parse(data) as { project_id?: unknown };
    return typeof parsed.project_id === "string" ? parsed.project_id : null;
  } catch {
    return null;
  }
}

/** Calls ``onChange`` with the project id for each announced change; returns the unsubscribe. */
export function onProjectChanged(onChange: (projectId: string) => void): () => void {
  const listener = (ev: Event) => onChange((ev as CustomEvent<string>).detail);
  window.addEventListener(PROJECT_CHANGED, listener);
  return () => window.removeEventListener(PROJECT_CHANGED, listener);
}
