// The measurements of browser.audit (E6-02), written as ONE self-contained function: Playwright
// serialises it and runs it inside the page, so it must not use anything from this module. The unit
// tests (audit-page.test.ts) run the same function in jsdom with a stubbed layout.
//
// It returns violations only, each with a selector and the numbers that make it one. Nothing here
// returns a DOM or a picture.

/**
 * @param {{checks: string[], opts: {minTarget: number, minFont: number, minContrast: number, maxPerCheck: number}}} arg
 *   checks: which checks to run (see CHECKS in audit.mjs). One argument, because that is what
 *   page.evaluate passes.
 */
export function auditInPage({ checks, opts }) {
  const { minTarget, minFont, minContrast, maxPerCheck } = opts;
  const out = [];
  const counts = {};
  const add = (check, el, data) => {
    counts[check] = (counts[check] ?? 0) + 1;
    if (counts[check] <= maxPerCheck) out.push({ check, selector: selectorOf(el), ...data });
  };

  function selectorOf(el) {
    if (!el || !el.tagName) return "";
    const parts = [];
    let node = el;
    for (let depth = 0; node && node.nodeType === 1 && depth < 3; depth++) {
      let part = node.tagName.toLowerCase();
      if (node.id) {
        parts.unshift(`${part}#${node.id}`);
        break;
      }
      const cls = typeof node.className === "string" ? node.className.trim().split(/\s+/).filter(Boolean) : [];
      if (cls.length) part += "." + cls.slice(0, 2).join(".");
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(" > ");
  }

  const round = (n) => Math.round(n * 100) / 100;
  const visible = (el) => {
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden" || Number(s.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const INTERACTIVE = 'a[href], button, input:not([type="hidden"]), select, textarea, summary, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [tabindex]:not([tabindex="-1"])';
  const interactive = () =>
    [...document.querySelectorAll(INTERACTIVE)].filter((el) => !el.disabled && visible(el));
  const hasText = (el) => [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim().length > 0);

  const run = new Set(checks);

  if (run.has("page_scroll")) {
    const root = document.documentElement;
    const over = root.scrollWidth - window.innerWidth;
    if (over > 1) add("page_scroll", root, { overflow_x: over, viewport: window.innerWidth });
  }

  if (run.has("inner_scroll")) {
    const intended = (el) =>
      !!el.closest("[data-scroll]") ||
      ["log", "list", "listbox", "grid", "tree", "textbox", "feed"].includes(el.getAttribute("role") ?? "") ||
      ["TEXTAREA", "PRE", "CODE", "SELECT", "TABLE"].includes(el.tagName) ||
      el === document.body ||
      el === document.documentElement;
    for (const el of document.querySelectorAll("*")) {
      const s = getComputedStyle(el);
      const scrollsY = /(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 1;
      const scrollsX = /(auto|scroll)/.test(s.overflowX) && el.scrollWidth > el.clientWidth + 1;
      if ((scrollsY || scrollsX) && !intended(el) && visible(el)) {
        add("inner_scroll", el, {
          scroll_height: el.scrollHeight,
          client_height: el.clientHeight,
          scroll_width: el.scrollWidth,
          client_width: el.clientWidth,
        });
      }
    }
  }

  if (run.has("clipped_text")) {
    for (const el of document.querySelectorAll("*")) {
      if (!hasText(el) || !visible(el)) continue;
      const s = getComputedStyle(el);
      const clips = /(hidden|clip)/.test(s.overflowX) || /(hidden|clip)/.test(s.overflowY);
      if (!clips || s.textOverflow === "ellipsis") continue; // an ellipsis is a decision, not an accident
      if (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1) {
        add("clipped_text", el, {
          scroll_width: el.scrollWidth,
          client_width: el.clientWidth,
          scroll_height: el.scrollHeight,
          client_height: el.clientHeight,
        });
      }
    }
  }

  if (run.has("target_size") || run.has("overlap")) {
    const items = interactive();
    if (run.has("target_size")) {
      for (const el of items) {
        const r = el.getBoundingClientRect();
        if (r.width < minTarget || r.height < minTarget) {
          add("target_size", el, { width: round(r.width), height: round(r.height), min: minTarget });
        }
      }
    }
    if (run.has("overlap")) {
      const list = items.slice(0, 300);
      for (let i = 0; i < list.length; i++) {
        for (let j = i + 1; j < list.length; j++) {
          const a = list[i];
          const b = list[j];
          if (a.contains(b) || b.contains(a)) continue;
          const ra = a.getBoundingClientRect();
          const rb = b.getBoundingClientRect();
          const w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
          const h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
          if (w > 2 && h > 2) add("overlap", a, { with: selectorOf(b), overlap_width: round(w), overlap_height: round(h) });
        }
      }
    }
  }

  if (run.has("font_size")) {
    for (const el of document.querySelectorAll("body *")) {
      if (!hasText(el) || !visible(el)) continue;
      const size = parseFloat(getComputedStyle(el).fontSize);
      if (size < minFont) add("font_size", el, { font_size: round(size), min: minFont });
    }
  }

  if (run.has("contrast")) {
    const parse = (c) => {
      const m = /rgba?\(([^)]+)\)/.exec(c);
      if (!m) return null;
      const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
      return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
    };
    const over = (top, under) => ({
      r: top.r * top.a + under.r * (1 - top.a),
      g: top.g * top.a + under.g * (1 - top.a),
      b: top.b * top.a + under.b * (1 - top.a),
      a: 1,
    });
    const lum = ({ r, g, b }) => {
      const f = (v) => {
        const c = v / 255;
        return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    const background = (el) => {
      const layers = [];
      for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
        const c = parse(getComputedStyle(n).backgroundColor);
        if (c && c.a > 0) layers.push(c);
        if (c && c.a >= 1) break;
      }
      let base = { r: 255, g: 255, b: 255, a: 1 }; // the canvas
      for (const layer of layers.reverse()) base = over(layer, base);
      return base;
    };
    for (const el of document.querySelectorAll("body *")) {
      if (!hasText(el) || !visible(el)) continue;
      const s = getComputedStyle(el);
      const fg = parse(s.color);
      if (!fg) continue;
      const bg = background(el);
      const text = over(fg, bg);
      const l1 = lum(text);
      const l2 = lum(bg);
      const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
      const size = parseFloat(s.fontSize);
      const large = size >= 24 || (size >= 18.66 && Number(s.fontWeight) >= 700);
      const need = large ? Math.min(minContrast, 3) : minContrast;
      if (ratio < need) add("contrast", el, { ratio: round(ratio), need, font_size: round(size) });
    }
  }

  if (run.has("focus")) {
    const items = interactive();
    for (const el of items) {
      if (Number(el.getAttribute("tabindex")) > 0) add("focus", el, { problem: "positive tabindex changes the focus order" });
    }
    const before = (el) => {
      const s = getComputedStyle(el);
      return `${s.outlineStyle}|${s.outlineWidth}|${s.outlineColor}|${s.boxShadow}|${s.borderColor}|${s.backgroundColor}`;
    };
    const active = document.activeElement;
    for (const el of items.slice(0, 120)) {
      const rest = before(el);
      el.focus({ preventScroll: true });
      const s = getComputedStyle(el);
      const focused = before(el);
      const outline = s.outlineStyle !== "none" && parseFloat(s.outlineWidth) > 0;
      if (document.activeElement === el && !outline && focused === rest) {
        add("focus", el, { problem: "no visible focus indication" });
      }
    }
    if (active && active.focus) active.focus({ preventScroll: true });
  }

  return { violations: out, counts };
}
