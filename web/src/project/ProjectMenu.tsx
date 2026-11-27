import { useEffect, useRef, useState, type KeyboardEvent } from "react";

export interface MenuItem {
  key: string;
  label: string;
  onSelect: () => void;
  /** A toggle (scope editor, template form): shown as a checked menu entry while it is on. */
  checked?: boolean;
}

/** Where the focus goes in a menu of `count` entries for `key`, from entry `at`; null when the key
 *  does not move it. Arrows wrap around, Home and End go to the ends. */
export function menuFocus(
  at: number,
  key: string,
  count: number,
): number | null {
  if (count === 0) return null;
  const moves: Record<string, () => number> = {
    ArrowDown: () => (at + 1) % count,
    ArrowUp: () => (at <= 0 ? count - 1 : at - 1),
    Home: () => 0,
    End: () => count - 1,
  };
  return moves[key]?.() ?? null;
}

/** A "⋯" button that opens the project's actions as a menu. Opening it focuses the first entry;
 *  arrows, Home and End move between entries; Escape closes it and gives the focus back to the
 *  button (and is kept from the desktop, which would minimise the window, and from the overlay's
 *  client, which would hide the overlay); Tab and a click outside
 *  close it. */
export function ProjectMenu({
  label,
  items,
}: {
  label: string;
  items: MenuItem[];
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const entries = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    if (!open) return;
    entries.current[0]?.focus();
    const outside = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, [open]);

  const close = () => {
    setOpen(false);
    trigger.current?.focus();
  };

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close();
      return;
    }
    if (e.key === "Tab") {
      setOpen(false);
      return;
    }
    const at = entries.current.findIndex((el) => el === document.activeElement);
    const next = menuFocus(at, e.key, items.length);
    if (next !== null) {
      e.preventDefault();
      entries.current[next]?.focus();
    }
  };

  return (
    <div className="project-menu" ref={root}>
      <button
        type="button"
        ref={trigger}
        className="project-menu-button"
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        ⋯
      </button>
      {open && (
        <div
          className="project-menu-list"
          role="menu"
          aria-label={label}
          // in the desktop overlay Esc is the client's key: it hides the whole overlay unless
          // something open asks to close first (desktop/client.ts, CLOSES_FIRST)
          data-esc-first=""
          onKeyDown={onKey}
        >
          {items.map((item, i) => (
            <button
              key={item.key}
              ref={(el) => {
                entries.current[i] = el;
              }}
              type="button"
              role={
                item.checked === undefined ? "menuitem" : "menuitemcheckbox"
              }
              aria-checked={item.checked}
              tabIndex={-1}
              onClick={() => {
                close();
                item.onSelect();
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
