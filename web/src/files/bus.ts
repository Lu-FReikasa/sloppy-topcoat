// "Show this place in the file reader": the development assistant asks for it (repo.show), the
// reader is another window. The request is kept until the reader takes it, so it also works when
// the reader had to be opened first.
import { requestOpen } from "../layout/intent";

export interface ShowRequest {
  repo?: string;
  /** A file or a folder, relative to the repository. */
  path: string;
  from?: number;
  to?: number;
  /** Instead of lines: a text to jump to (its first place in the file). */
  find?: string;
}

/** Fired when a request is waiting. */
export const SHOW_EVENT = "topcoat:files-show";

let pending: ShowRequest | null = null;

export function requestShow(request: ShowRequest): void {
  pending = request;
  requestOpen({ module: "page.files", props: {} });
  window.dispatchEvent(new Event(SHOW_EVENT));
}

/** The waiting request, once. */
export function takeShow(): ShowRequest | null {
  const request = pending;
  pending = null;
  return request;
}

/** What the assistant's `repo.show` call said (the input of the activity item), or null when it
 *  is not a usable request. Never trusted: the reader checks the path against the tree. */
export function showFromInput(input: unknown): ShowRequest | null {
  if (!input || typeof input !== "object") return null;
  const o = input as Record<string, unknown>;
  if (typeof o.path !== "string" || o.path.trim() === "") return null;
  const line = (v: unknown) =>
    typeof v === "number" && Number.isInteger(v) && v >= 1 ? v : undefined;
  return {
    repo: typeof o.repo === "string" ? o.repo : undefined,
    path: o.path.trim().replace(/\\/g, "/").replace(/^\/+/, ""),
    from: line(o.from),
    to: line(o.to),
    find: typeof o.find === "string" && o.find ? o.find : undefined,
  };
}
