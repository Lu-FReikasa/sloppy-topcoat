import { useEffect, type RefObject } from "react";

/** The nearest ancestor of `el` that scrolls vertically (the window's view in the desktop, the
 *  page in the nav shell); null when only the document scrolls. */
export function scrollParent(el: HTMLElement | null): HTMLElement | null {
  let at = el?.parentElement ?? null;
  while (at) {
    const { overflowY } = getComputedStyle(at);
    if (overflowY === "auto" || overflowY === "scroll") return at;
    at = at.parentElement;
  }
  return null;
}

/** While `active`, keeps the height of `ref`'s scrolling ancestor in the CSS variable `name` on
 *  `ref`, so a sticky side panel can be as tall as the visible part of the page and scroll on its
 *  own (taller, its lower part would stay out of reach while the tree scrolls). */
export function useScrollportHeight(
  ref: RefObject<HTMLElement | null>,
  name: string,
  active: boolean,
) {
  useEffect(() => {
    const el = ref.current;
    if (!active || !el) return;
    const port = scrollParent(el);
    const measure = () =>
      el.style.setProperty(
        name,
        `${port ? port.clientHeight : window.innerHeight}px`,
      );
    measure();
    if (!port || typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const watch = new ResizeObserver(measure);
    watch.observe(port);
    return () => watch.disconnect();
  }, [ref, name, active]);
}
