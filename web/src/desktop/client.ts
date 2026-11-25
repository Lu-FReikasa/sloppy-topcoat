/** The desktop client, seen from its overlay's page (desktop/src-tauri/src/overlay.rs). The client
 *  puts its settings into `window.topcoatClient.config` before the page starts; the page asks it
 *  for something by going to `?client=<verb>&...`, an address the client stops and reads (a fixed
 *  list, nav.rs); the client answers through `window.topcoatClient.onResult`. In a browser there
 *  is no `topcoatClient` and nothing here does anything. */

export interface ClientConfig {
  version: string;
  /** The key that shows the overlay, as the client registers it (`ctrl+alt+Space`). */
  key: string;
  effect: "blur" | "none" | string;
  /** The key that shows the overlay on every screen (client 0.2 on; AltGr+F). */
  allKey?: string;
  /** The key that attaches Clipper to the window in front. */
  clipperKey?: string;
  /** How many screens there are (client 0.2 on): a program can go to another one. */
  screens?: number;
}

export interface ClientResult {
  what: "key" | "look" | "send" | string;
  ok: boolean;
  error: string | null;
}

interface ClientGlobal {
  config?: ClientConfig;
  onResult?: (r: ClientResult) => void;
  escape?: () => void;
  /** The Clipper key (pressed while the overlay was hidden too). */
  attachClipper?: () => void;
  wantClipper?: boolean;
  /** The herald's message was clicked: open this view. */
  onOpen?: (o: { module: string; props: Record<string, string> }) => void;
  wantOpen?: { module: string; props: Record<string, string> };
  /** The client has just shown the overlay (a client from 0.1.1 on): the theme's arrival plays. */
  onShow?: () => void;
  /** The key asks the overlay to go (a client from 0.1.1 on): the page plays the theme's leaving
   *  and then asks to hide; a page without it is hidden at once. */
  leave?: () => void;
}

function global(): ClientGlobal | null {
  const c = (window as unknown as { topcoatClient?: ClientGlobal }).topcoatClient;
  return c && typeof c === "object" ? c : null;
}

/** The client's settings, or null in a browser (or a client too old to tell). */
export function clientConfig(): ClientConfig | null {
  return global()?.config ?? null;
}

/** Ask the client for something (only inside the client). */
export function clientCommand(
  verb: "hide" | "set-key" | "set-look" | "herald-show" | "herald-hide" | "open-task" | "send",
  params: Record<string, string> = {},
): void {
  if (!global()) return;
  const q = new URLSearchParams({ client: verb, ...params });
  window.location.assign(`?${q.toString()}`);
}

/** Hear the client's answers; returns the way to stop. */
export function onClientResult(fn: (r: ClientResult) => void): () => void {
  const c = global();
  if (!c) return () => undefined;
  c.onResult = fn;
  return () => {
    if (c.onResult === fn) delete c.onResult;
  };
}

/** What stands open on the page and closes with Esc before the overlay does. */
const CLOSES_FIRST = "[aria-modal='true'], .dt-palette-backdrop, .dt-me-menu, [data-esc-first]";

/** Esc while the overlay is up is the client's key; it gives it to the page here: something open
 *  closes (the page's own Esc handling gets the key), else the overlay goes. */
export function installEscape(): () => void {
  const c = global();
  if (!c) return () => undefined;
  const escape = () => {
    if (document.querySelector(CLOSES_FIRST)) {
      const target = document.activeElement instanceof HTMLElement ? document.activeElement : document.body;
      target.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
      return;
    }
    if (c.leave) c.leave();
    else clientCommand("hide");
  };
  c.escape = escape;
  return () => {
    if (c.escape === escape) delete c.escape;
  };
}

const MODS = ["ctrl", "alt", "shift", "super"] as const;

/** The key code a global key may end in (what the client's parser takes). */
function plainKey(code: string): boolean {
  return (
    /^Key[A-Z]$/.test(code) ||
    /^Digit[0-9]$/.test(code) ||
    /^F([1-9]|1[0-9]|2[0-4])$/.test(code) ||
    ["Space", "Backquote", "Insert", "Home", "End", "PageUp", "PageDown", "Pause"].includes(code)
  );
}

/** A key press as the client's global key (`ctrl+alt+KeyC`), or null when it is not one: a lone
 *  modifier, or no ctrl, alt or super (a plain letter would be taken from every program). AltGr
 *  arrives on Windows as ctrl+alt. */
export function accelerator(e: Pick<KeyboardEvent, "code" | "ctrlKey" | "altKey" | "shiftKey" | "metaKey">): string | null {
  if (!plainKey(e.code)) return null;
  if (!e.ctrlKey && !e.altKey && !e.metaKey) return null;
  const mods = [e.ctrlKey && "ctrl", e.altKey && "alt", e.shiftKey && "shift", e.metaKey && "super"].filter(Boolean);
  return [...mods, e.code].join("+");
}

type T = (key: string) => string;

/** A key as people read it: `Strg+Alt+Leertaste`, `Ctrl+Alt+C`. */
export function keyLabel(acc: string, t: T): string {
  return acc
    .split("+")
    .map((part) => {
      const lower = part.toLowerCase();
      if ((MODS as readonly string[]).includes(lower)) return t(`client.mod.${lower}`);
      if (lower === "space") return t("client.key.space");
      return part.replace(/^Key/, "").replace(/^Digit/, "");
    })
    .join("+");
}

/** The client's Clipper key and the herald's "open this task": the overlay's page answers both.
 *  A wish that came while the page was loading is taken now. */
/** The overlay comes and goes with the theme's motion. `ms()` is the length of the leaving as it
 *  is now (0: it goes at once); `mark` puts the root's class (`arriving`, `leaving` or none). The
 *  leaving class is taken off a moment after the client hid the window, so an older client that
 *  never calls `onShow` cannot show an overlay that stayed faded out. Returns the way to stop. */
export function installOverlayMotion(
  ms: () => number,
  mark: (state: "arriving" | "leaving" | null) => void,
): () => void {
  const c = global();
  if (!c) return () => undefined;
  const timers: number[] = [];
  const later = (fn: () => void, wait: number) => timers.push(window.setTimeout(fn, wait));
  let going = false;
  const onShow = () => {
    going = false;
    const wait = ms();
    if (!wait) return mark(null);
    mark("arriving");
    later(() => mark(null), wait);
  };
  const leave = () => {
    if (going) return;
    going = true;
    const wait = ms();
    mark(wait ? "leaving" : null);
    later(() => {
      clientCommand("hide");
      later(() => {
        going = false;
        mark(null);
      }, 150);
    }, wait);
  };
  c.onShow = onShow;
  c.leave = leave;
  return () => {
    timers.forEach((t) => window.clearTimeout(t));
    if (c.onShow === onShow) delete c.onShow;
    if (c.leave === leave) delete c.leave;
  };
}

export function installClientHandlers(attach: () => void, open: (o: { module: string; props: Record<string, string> }) => void): () => void {
  const c = global();
  if (!c) return () => undefined;
  c.attachClipper = attach;
  c.onOpen = open;
  if (c.wantClipper) {
    delete c.wantClipper;
    attach();
  }
  if (c.wantOpen) {
    const o = c.wantOpen;
    delete c.wantOpen;
    open(o);
  }
  return () => {
    if (c.attachClipper === attach) delete c.attachClipper;
    if (c.onOpen === open) delete c.onOpen;
  };
}
