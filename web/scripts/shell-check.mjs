#!/usr/bin/env node
// Layout check of the navigation shell (webshell/WebShell.tsx) in a real browser, issue #74:
// - the shell fills the window: `.ws-body` ends at the bottom of the viewport, and a chat's view
//   fills the space below the crumb without overflowing it (the message box stays in the window);
// - nothing in the header scrolls sideways or is cut, at 320 and 360 px too, English and German;
// - the phone drawer opens below the header, closes on Escape and on a tap outside, and gives the
//   focus back to the menu button;
// - the user menu wraps its items on its own surface, stays inside the window, closes on Escape and
//   on a click outside, and gives the focus back to its button.
//
//   npm run build && npm run shell-layout      (needs the `playwright` package and a Chromium)
//
// The hub is faked (./lib/fake-hub.mjs). Like `npm run layout` it is not part of CI (no browsers).
// SIZES=320x640,1280x800, LANGS=en and LOOKS=terminal limit the run; DIST=<dir> checks another build; SHOTS=<dir> keeps a screenshot of each step.
import { createRequire } from "node:module";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { JSON_ROUTES, serve, watchCsp } from "./lib/fake-hub.mjs";

const require = createRequire(import.meta.url);
function loadPlaywright() {
  const paths = [process.cwd(), "/opt/node22/lib/node_modules", ...(process.env.NODE_PATH ?? "").split(":")];
  for (const p of paths.filter(Boolean)) {
    try {
      return require(require.resolve("playwright", { paths: [p] }));
    } catch {
      /* try the next place */
    }
  }
  console.error("shell-check needs the `playwright` package (npm i -g playwright) and a Chromium.");
  process.exit(2);
}
const { chromium } = loadPlaywright();

const dist = process.env.DIST ?? join(fileURLToPath(new URL("..", import.meta.url)), "dist");
// 400 px is the first width where the search shows its text: the tightest header
const SIZES = (process.env.SIZES ?? "320x640,360x740,390x844,400x800,768x1024,1280x800,1366x768")
  .split(",")
  .map((s) => s.split("x").map(Number));
const LANGS = (process.env.LANGS ?? "en,de").split(",");
const LOOKS = (process.env.LOOKS ?? "terminal,leitstand,schreibtisch").split(",");
const SHOTS = process.env.SHOTS;
// a long name, as a person may have one: it must shorten, not push the header wider
const NAME = "Maximiliane Ostermann-Brüggemeier";
const NARROW_PX = 760; // layout/useNarrow.ts

const problems = [];
let runs = 0;
const fail = (where, what) => problems.push(`${where}: ${what}`);

/** Runs in the page: the shell's boxes and what scrolls or is cut. */
function measure() {
  const box = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height };
  };
  const q = (s) => document.querySelector(s);
  const cut = [];
  // inside a box that shortens its text with an ellipsis, the rest is hidden on purpose
  const shortened = (el) => {
    for (let p = el.parentElement; p && !p.matches(".ws-top"); p = p.parentElement)
      if (getComputedStyle(p).textOverflow === "ellipsis" && getComputedStyle(p).overflowX !== "visible") return true;
    return false;
  };
  for (const el of document.querySelectorAll(".ws-top, .ws-top *")) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || getComputedStyle(el).visibility === "hidden" || shortened(el)) continue;
    if (r.right > window.innerWidth + 0.5 || r.left < -0.5) cut.push(`${el.className || el.tagName} leaves the window`);
    // text that shortens with an ellipsis is meant to; anything else wider than its box is cut
    if (el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).textOverflow !== "ellipsis" && el.clientWidth > 0)
      cut.push(`${el.className || el.tagName} is cut (${el.scrollWidth} > ${el.clientWidth})`);
  }
  return {
    vw: window.innerWidth,
    vh: window.innerHeight,
    pageScrollX: document.documentElement.scrollWidth - window.innerWidth,
    pageScrollY: document.documentElement.scrollHeight - window.innerHeight,
    top: box(q(".ws-top")),
    body: box(q(".ws-body")),
    main: q(".ws-main") && { overflow: q(".ws-main").scrollHeight - q(".ws-main").clientHeight },
    composer: box(q(".talk-composer")),
    side: box(q(".ws-side")),
    sideShown: !!q(".ws-side") && getComputedStyle(q(".ws-side")).display !== "none",
    burger: box(q(".ws-burger")),
    backdrop: box(q(".ws-backdrop")),
    cut,
  };
}

/** Runs in the page: the user menu's surface against its items. */
function measureMenu() {
  const menu = document.querySelector(".ws-me-menu");
  if (!menu) return null;
  const r = menu.getBoundingClientRect();
  const items = [...menu.children].map((c) => c.getBoundingClientRect());
  return {
    vw: window.innerWidth,
    vh: window.innerHeight,
    menu: { top: r.top, bottom: r.bottom, left: r.left, right: r.right },
    outside: items.filter((i) => i.top < r.top - 0.5 || i.bottom > r.bottom + 0.5 || i.left < r.left - 0.5 || i.right > r.right + 0.5).length,
    // what a person sees in the middle of the menu is the menu, not the page under it
    hitsMenu: menu.contains(document.elementFromPoint((r.left + r.right) / 2, r.bottom - 3)),
  };
}

/** Clicks like a person; false when something else covers it (that is the finding). */
const tap = (page, sel) => page.click(sel, { timeout: 2000 }).then(() => true, () => false);
const focusOn = (page, sel) => page.evaluate((s) => !!document.activeElement?.matches(s), sel);

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM ?? undefined, args: ["--no-sandbox"] });
const server = await serve(dist);
const base = `http://127.0.0.1:${server.address().port}/`;
try {
  for (const look of LOOKS) for (const lang of LANGS) {
    for (const [w, h] of SIZES) {
      const where = `${w}x${h} ${lang} ${look}`;
      const narrow = w < NARROW_PX;
      const ctx = await browser.newContext({ viewport: { width: w, height: h }, locale: lang });
      await ctx.addInitScript(() => {
        localStorage.setItem("topcoat.devToken", "t");
        localStorage.setItem("topcoat.shell", "nav");
      });
      const page = await ctx.newPage();
      watchCsp(page, (m) => problems.push(`content security policy: ${m}`));
      await page.route("**/api/**", (route) => {
        const path = new URL(route.request().url()).pathname;
        if (path === "/api/stream") return route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
        let body = JSON_ROUTES[path];
        if (path === "/api/me") body = { ...body, name: NAME, language: lang };
        if (path === "/api/ui/bootstrap") body = { ...body, prefs: { ...body.prefs, look } };
        return route.fulfill({ status: body === undefined ? 404 : 200, contentType: "application/json", body: JSON.stringify(body ?? {}) });
      });
      await page.goto(`${base}#/w/dev`);
      await page.waitForSelector(".ws-body", { timeout: 10000 }).catch(() => undefined);
      await page.waitForTimeout(250);
      const shot = async (name) => SHOTS && page.screenshot({ path: join(SHOTS, `${w}x${h}-${lang}-${look}-${name}.png`) });
      await shot("shell");

      // the frame: full height, no sideways scroll, nothing cut in the header
      const m = await page.evaluate(measure);
      runs++;
      if (!m.body) {
        fail(where, "no navigation shell drawn");
        await ctx.close();
        continue;
      }
      if (Math.abs(m.body.bottom - m.vh) > 1) fail(where, `.ws-body ends at ${m.body.bottom.toFixed(0)}, the window at ${m.vh}`);
      if (m.pageScrollX > 0) fail(where, `the page scrolls ${m.pageScrollX} px sideways`);
      if (m.pageScrollY > 0) fail(where, `the page scrolls ${m.pageScrollY} px down (the shell scrolls inside)`);
      for (const c of m.cut) fail(where, c);

      // a chat: the view fills the space below the crumb, the message box stays in the window
      await page.goto(`${base}#/w/chat`);
      await page.waitForSelector(".talk-composer", { timeout: 10000 }).catch(() => undefined);
      await page.waitForTimeout(250);
      const c = await page.evaluate(measure);
      await shot("chat");
      runs++;
      if (!c.composer) fail(`${where} chat`, "no message box");
      else if (c.composer.bottom > c.vh + 0.5) fail(`${where} chat`, `the message box ends at ${c.composer.bottom.toFixed(0)}, below the window (${c.vh})`);
      if (c.main && c.main.overflow > 0) fail(`${where} chat`, `the view overflows the shell's main area by ${c.main.overflow} px`);
      await page.goto(`${base}#/w/dev`);
      await page.waitForSelector(".ws-body", { timeout: 10000 }).catch(() => undefined);
      await page.waitForTimeout(150);

      if (narrow) {
        // the drawer: below the header, closes on Escape and on a tap outside, focus back on ☰
        await tap(page, ".ws-burger");
        await page.waitForTimeout(50);
        const open = await page.evaluate(measure);
        await shot("drawer");
        runs++;
        if (!open.sideShown) fail(`${where} drawer`, "does not open");
        else if (open.side.top < open.top.bottom - 0.5) fail(`${where} drawer`, "covers the header (and the menu button)");
        if (!open.backdrop) fail(`${where} drawer`, "has no backdrop to tap");
        await page.keyboard.press("Escape");
        await page.waitForTimeout(50);
        if ((await page.evaluate(measure)).sideShown) fail(`${where} drawer`, "stays open on Escape");
        if (!(await focusOn(page, ".ws-burger"))) fail(`${where} drawer`, "Escape does not return the focus to ☰");
        if (!(await tap(page, ".ws-burger"))) fail(`${where} drawer`, "☰ cannot be tapped");
        await page.waitForTimeout(50);
        await page.mouse.click(w - 10, h - 10); // on the backdrop, beside the drawer
        await page.waitForTimeout(50);
        if ((await page.evaluate(measure)).sideShown) fail(`${where} drawer`, "stays open on a tap outside");
        if (!(await focusOn(page, ".ws-burger"))) fail(`${where} drawer`, "a tap outside does not return the focus to ☰");
        runs++;
      }

      // the user menu: its own surface around its items, inside the window, Escape and outside click
      if (!(await tap(page, ".ws-me > button"))) fail(`${where} menu`, "its button cannot be clicked");
      await page.waitForTimeout(50);
      const mm = await page.evaluate(measureMenu);
      await shot("menu");
      runs++;
      if (!mm) fail(`${where} menu`, "does not open");
      else {
        if (mm.outside) fail(`${where} menu`, `${mm.outside} item(s) hang outside its surface`);
        if (!mm.hitsMenu) fail(`${where} menu`, "the page shows through the menu");
        if (mm.menu.left < -0.5 || mm.menu.right > mm.vw + 0.5 || mm.menu.bottom > mm.vh + 0.5)
          fail(`${where} menu`, "leaves the window");
      }
      await page.keyboard.press("Escape");
      await page.waitForTimeout(50);
      if (await page.evaluate(measureMenu)) fail(`${where} menu`, "stays open on Escape");
      if (!(await focusOn(page, ".ws-me > button"))) fail(`${where} menu`, "Escape does not return the focus to its button");
      await tap(page, ".ws-me > button");
      await page.waitForTimeout(50);
      await page.mouse.click(Math.round(w / 2), h - 10); // the page below
      await page.waitForTimeout(50);
      if (await page.evaluate(measureMenu)) fail(`${where} menu`, "stays open on a click outside");
      runs++;
      await ctx.close();
    }
  }
} finally {
  await browser.close();
  server.close();
}

if (problems.length) {
  console.error(`${problems.length} problem(s) in ${runs} checks:`);
  for (const p of problems) console.error(" - " + p);
  process.exit(1);
}
console.log(`shell layout ok: ${runs} checks, ${SIZES.map((s) => s.join("x")).join(" ")}, ${LANGS.join("/")}, ${LOOKS.join("/")}`);
