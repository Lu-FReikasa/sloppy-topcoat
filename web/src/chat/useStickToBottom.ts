import { useCallback, useEffect, useRef, useState, type DependencyList } from "react";

/** How close to the end (px) still counts as "reading the latest". */
export const NEAR_PX = 80;

export function nearBottom(
  el: { scrollHeight: number; scrollTop: number; clientHeight: number },
  px: number = NEAR_PX,
): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= px;
}

/** Keeps a scrolling list at its end while new text arrives, but only as long as the user is at
 *  the end: scroll up to read and the list stays where it is. ``unseen`` says that something
 *  new came in while the user was away from the end; ``jump`` goes there. ``stick`` forces
 *  the end (the user just sent a message). */
export function useStickToBottom<T extends HTMLElement>(deps: DependencyList) {
  const ref = useRef<T>(null);
  const stuck = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const [unseen, setUnseen] = useState(false);

  const toEnd = useCallback(() => {
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  const onScroll = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const near = nearBottom(el);
    stuck.current = near;
    setAtBottom(near);
    if (near) setUnseen(false);
  }, []);

  const stick = useCallback(() => {
    stuck.current = true;
    setAtBottom(true);
    setUnseen(false);
    toEnd();
  }, [toEnd]);

  useEffect(() => {
    if (stuck.current) toEnd();
    else setUnseen(true);
    // the caller names what counts as "something new"
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { ref, onScroll, atBottom, unseen, jump: stick, stick };
}
