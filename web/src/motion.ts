/** Does the person ask their system for less motion? Without a way to ask, things move. The
 *  desktop's stylesheet stops every CSS animation then; this is for the motion made by script
 *  (the idle art of an empty chat, the burning of an incognito tab). */
export function reducedMotion(): boolean {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}
