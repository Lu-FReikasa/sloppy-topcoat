// browser.snapshot (E6-02): the page as a compact tree of what a user can perceive and operate.
// One self-contained function, run inside the page by Playwright (the unit tests run it in jsdom).
//
// Every element that appears in the tree gets a ref (e1, e2 …) that stays with the element while it
// exists; browser.act takes the ref. The refs live in the page (window.__tcRefs), not in the DOM, so
// the page's own CSS and scripts never see them.

/**
 * @param {{scope?: string, layout?: boolean, maxNodes: number}} arg
 */
export function snapshotInPage({ scope, layout, maxNodes }) {
  window.__tcRefs ??= new Map();
  window.__tcOf ??= new WeakMap();
  window.__tcN ??= 0;
  for (const [ref, el] of window.__tcRefs) if (!el.isConnected) window.__tcRefs.delete(ref);
  const refFor = (el) => {
    let ref = window.__tcOf.get(el);
    if (!ref) {
      ref = `e${++window.__tcN}`;
      window.__tcOf.set(el, ref);
    }
    window.__tcRefs.set(ref, el);
    return ref;
  };

  const root = scope ? window.__tcRefs.get(scope) : document.body;
  if (!root) throw new Error(`no element has the ref ${scope}`);

  const shorten = (s, n = 80) => {
    const t = (s ?? "").replace(/\s+/g, " ").trim();
    return t.length > n ? `${t.slice(0, n - 1)}…` : t;
  };
  const hidden = (el) => {
    if (el.hidden || el.getAttribute("aria-hidden") === "true") return true;
    const s = getComputedStyle(el);
    return s.display === "none" || s.visibility === "hidden";
  };
  const INPUT_ROLE = {
    checkbox: "checkbox", radio: "radio", range: "slider", search: "searchbox", button: "button",
    submit: "button", reset: "button", image: "button", number: "spinbutton",
  };
  const TAG_ROLE = {
    a: (el) => (el.hasAttribute("href") ? "link" : null),
    button: () => "button", select: () => "combobox", textarea: () => "textbox", img: () => "img",
    h1: () => "heading", h2: () => "heading", h3: () => "heading", h4: () => "heading", h5: () => "heading", h6: () => "heading",
    ul: () => "list", ol: () => "list", li: () => "listitem", nav: () => "navigation", main: () => "main",
    header: () => "banner", footer: () => "contentinfo", aside: () => "complementary", form: () => "form",
    table: () => "table", dialog: () => "dialog", summary: () => "button", progress: () => "progressbar",
    meter: () => "meter", section: (el) => (el.hasAttribute("aria-label") || el.hasAttribute("aria-labelledby") ? "region" : null),
    input: (el) => INPUT_ROLE[el.type] ?? "textbox",
  };
  const roleOf = (el) => {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit.split(/\s+/)[0];
    const fn = TAG_ROLE[el.tagName.toLowerCase()];
    return fn ? fn(el) : null;
  };
  const labelText = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return l.textContent;
    }
    const wrap = el.closest("label");
    return wrap ? wrap.textContent : "";
  };
  const nameOf = (el, role) => {
    const aria = el.getAttribute("aria-label");
    if (aria) return shorten(aria);
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const text = by.split(/\s+/).map((id) => document.getElementById(id)?.textContent ?? "").join(" ");
      if (text.trim()) return shorten(text);
    }
    if (role === "img") return shorten(el.getAttribute("alt") ?? el.getAttribute("title"));
    if (["textbox", "searchbox", "combobox", "checkbox", "radio", "slider", "spinbutton"].includes(role)) {
      return shorten(labelText(el) || el.getAttribute("title") || el.getAttribute("placeholder"));
    }
    if (el.tagName === "INPUT" && ["submit", "reset", "button"].includes(el.type)) return shorten(el.value);
    return shorten(el.textContent);
  };
  const stateOf = (el, role) => {
    const st = {};
    if (el.disabled || el.getAttribute("aria-disabled") === "true") st.disabled = true;
    if (el.checked === true || el.getAttribute("aria-checked") === "true") st.checked = true;
    for (const a of ["expanded", "selected", "pressed", "invalid", "current"]) {
      const v = el.getAttribute(`aria-${a}`);
      if (v !== null && v !== "false") st[a] = v === "true" ? true : v;
    }
    if (el.required) st.required = true;
    if (role === "heading") st.level = Number(el.tagName.slice(1)) || Number(el.getAttribute("aria-level")) || undefined;
    if (["textbox", "searchbox", "combobox", "slider", "spinbutton"].includes(role) && el.type !== "password") {
      const v = shorten(el.value ?? "", 40);
      if (v) st.value = v;
    }
    return Object.keys(st).length ? st : undefined;
  };
  const directText = (el) =>
    shorten([...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(" "));

  const nodes = [];
  let total = 0;
  const walk = (el, depth) => {
    if (hidden(el)) return;
    const role = roleOf(el);
    let nextDepth = depth;
    if (role) {
      total++;
      if (nodes.length < maxNodes) {
        const node = { ref: refFor(el), role, name: nameOf(el, role), d: depth };
        const state = stateOf(el, role);
        if (state) node.state = state;
        if (layout) {
          const r = el.getBoundingClientRect();
          node.box = [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
        }
        nodes.push(node);
      }
      nextDepth = depth + 1;
    } else {
      const text = directText(el);
      if (text) {
        total++;
        if (nodes.length < maxNodes) {
          const node = { ref: refFor(el), role: "text", name: text, d: depth };
          if (layout) {
            const r = el.getBoundingClientRect();
            node.box = [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
          }
          nodes.push(node);
        }
      }
    }
    // an img, a form control or a button already says what it holds
    if (["img", "textbox", "searchbox", "combobox", "slider", "checkbox", "radio", "button", "link"].includes(role ?? "")) return;
    for (const child of el.children) walk(child, nextDepth);
  };
  walk(root, 0);

  return {
    url: location.pathname + location.search + location.hash,
    title: document.title,
    viewport: [window.innerWidth, window.innerHeight],
    nodes,
    shown: nodes.length,
    omitted: Math.max(0, total - nodes.length),
  };
}

/** A line per node, without refs: what the tree says, to compare two trees. Pure. */
export function treeLines(nodes) {
  return nodes.map((n) => `${n.role}|${n.name}${n.state ? `|${JSON.stringify(n.state)}` : ""}`);
}

/** The lines that are in `after` but not in `before` and the other way round (multiset). Pure. */
export function treeDiff(before, after, cap = 30) {
  const left = new Map();
  for (const l of before) left.set(l, (left.get(l) ?? 0) + 1);
  const added = [];
  for (const l of after) {
    const n = left.get(l) ?? 0;
    if (n > 0) left.set(l, n - 1);
    else added.push(l);
  }
  const removed = [];
  for (const [l, n] of left) for (let i = 0; i < n; i++) removed.push(l);
  return {
    added: added.slice(0, cap),
    removed: removed.slice(0, cap),
    more: Math.max(0, added.length - cap) + Math.max(0, removed.length - cap),
  };
}
