// What the overlay checks look at in a page, shared by the simulated check (overlay-check.mjs) and
// the capture of the real client (overlay-capture.mjs). `overlayAudit` runs inside the page: it
// must not use anything from this module's scope.

/** Runs in the page: the desktop's frame on an overlay page, and what is wrong with it. */
export function overlayAudit() {
  const problems = [];
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const root = document.documentElement;
  const box = (el) => el && el.getBoundingClientRect();
  const visible = (el) => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return (
      r.width > 0 &&
      r.height > 0 &&
      s.visibility !== "hidden" &&
      s.display !== "none"
    );
  };
  const alpha = (c) => {
    const m = /rgba?\(([^)]+)\)/.exec(c);
    if (!m) return c === "transparent" ? 0 : 1;
    const parts = m[1].split(/[,/ ]+/).filter(Boolean);
    return parts.length > 3 ? Number(parts[3]) : 1;
  };

  if (!root.classList.contains("tc-overlay"))
    problems.push("the page is not in overlay mode (html.tc-overlay missing)");
  // the client's window is see-through: the page must not paint an opaque ground over the screen
  for (const el of [root, document.body]) {
    const s = getComputedStyle(el);
    if (
      alpha(s.backgroundColor) >= 0.99 ||
      (s.backgroundImage && s.backgroundImage !== "none")
    )
      problems.push(
        `${el.tagName.toLowerCase()} paints an opaque background (${s.backgroundColor} ${s.backgroundImage})`,
      );
  }
  // the desktop's own ground over the whole screen: a tint lets the screen through, a look's paper
  // or board must not cover it (overlay.rs: only programs, taskbar and Clipper stand on the screen)
  for (const sel of [".dt-root", ".dt-screen", ".dt-stage"]) {
    const el = document.querySelector(sel);
    if (!el) continue;
    const s = getComputedStyle(el);
    if (
      alpha(s.backgroundColor) >= 0.9 ||
      (s.backgroundImage && s.backgroundImage !== "none")
    )
      problems.push(
        `${sel} covers the screen (${s.backgroundColor}${s.backgroundImage !== "none" ? " with an image" : ""})`,
      );
  }
  const sx = root.scrollWidth - vw;
  const sy = root.scrollHeight - vh;
  if (sx > 0) problems.push(`the page scrolls ${sx} px sideways`);
  if (sy > 0) problems.push(`the page scrolls ${sy} px down`);

  const bar = document.querySelector(".dt-taskbar");
  if (!bar || !visible(bar)) problems.push("no taskbar");
  else {
    const r = box(bar);
    if (r.bottom > vh + 0.5 || r.left < -0.5 || r.right > vw + 0.5)
      problems.push("the taskbar leaves the screen");
  }

  const windows = [...document.querySelectorAll(".dt-program")].filter(visible);
  for (const w of windows) {
    const r = box(w);
    const name =
      w.getAttribute("aria-label") || w.dataset.program || "a program";
    // a nest layout lays programs off the stage on purpose (inert): only those that take input count
    if (w.closest("[inert]") || w.hasAttribute("inert")) continue;
    if (
      r.left < -0.5 ||
      r.top < -0.5 ||
      r.right > vw + 0.5 ||
      r.bottom > vh + 0.5
    )
      problems.push(
        `${name} leaves the screen (${Math.round(r.left)},${Math.round(r.top)} ${Math.round(r.width)}x${Math.round(r.height)})`,
      );
    if (
      bar &&
      visible(bar) &&
      r.bottom > box(bar).top + 0.5 &&
      !w.classList.contains("dt-maxi")
    )
      problems.push(`${name} reaches under the taskbar`);
  }

  // text cut without an ellipsis, in title bars, the taskbar and the navigation of the programs
  const shortened = (el) => {
    for (let p = el; p && p !== document.body; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (s.textOverflow === "ellipsis" && s.overflowX !== "visible")
        return true;
    }
    return false;
  };
  const seen = new Set();
  for (const el of document.querySelectorAll(
    ".dt-taskbar *, .dt-program > header *, .dt-program-title, .dt-nav *",
  )) {
    if (!visible(el) || shortened(el) || el.closest("[inert]")) continue;
    if (
      el.scrollWidth > el.clientWidth + 1 &&
      el.clientWidth > 0 &&
      getComputedStyle(el).overflowX !== "auto"
    ) {
      const what = `${el.className || el.tagName} is cut (${el.scrollWidth} > ${el.clientWidth})`;
      if (!seen.has(what)) problems.push(what);
      seen.add(what);
    }
  }

  return {
    vw,
    vh,
    dpr: window.devicePixelRatio,
    programs: windows.length,
    client: !!window.topcoatClient?.config,
    problems,
  };
}
