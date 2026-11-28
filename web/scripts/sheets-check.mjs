#!/usr/bin/env node
// Layout check of Sheets (sheets/SheetsPage.tsx) in a real browser, issue #168:
// - the library table: no column title breaks inside a word, the key ("B · Capo 2") stays on one
//   line, a song with two versions is one row without version links (#159), and the table fits its
//   column (no sideways scroll);
// - the stage: every tool, "Leave" last, ends inside the window (German "Verlassen" ended at
//   453 px on a 320-430 px phone) and is not covered (the windows shell's taskbar lay over it),
//   and on a touch screen the tools are not dimmed.
// Both shells, English and German, phone widths up to 430 and desktop widths.
//
//   npm run build && npm run sheets-layout      (needs the `playwright` package and a Chromium)
//
// The hub is faked (./lib/fake-hub.mjs). CI runs it in the web job (Chromium installed by the job).
// SIZES=320x640,1280x800, LANGS=en, SHELLS=nav and LOOKS=terminal limit the run; DIST=<dir> checks another
// build; SHOTS=<dir> keeps a screenshot of each step.
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
  console.error("sheets-check needs the `playwright` package (`npm ci` installs it) and a Chromium (`npx playwright install chromium`).");
  process.exit(2);
}
const { chromium } = loadPlaywright();

const dist = process.env.DIST ?? join(fileURLToPath(new URL("..", import.meta.url)), "dist");
const SIZES = (process.env.SIZES ?? "320x640,360x740,390x844,430x932,768x1024,1280x800")
  .split(",")
  .map((s) => s.split("x").map(Number));
const LANGS = (process.env.LANGS ?? "en,de").split(",");
const SHELLS = (process.env.SHELLS ?? "nav,windows").split(",");
const LOOKS = (process.env.LOOKS ?? "terminal").split(",");
const SHOTS = process.env.SHOTS;
const NARROW_PX = 760; // touch.css, layout/useNarrow.ts

// synthetic songs: a long title, a long artist, a capo key and a song with two versions
const summary = (id, title, artist, key, capo, created) => ({
  id, title, artist, instrument: "guitar", key, capo, capo_view: true, version_note: "", sources: 1,
  report_id: null, updated_at: null, created_at: created,
});
const SHEETS = [
  summary("s1", "A Rather Long Song Title For Measuring Narrow Columns", "Example Band H", "C", 0, "2026-11-20T10:00:00Z"),
  summary("s2", "Copper Lights", "Example Band B", "B", 2, "2026-11-20T10:00:00Z"),
  summary("s3", "Copper Lights", "Example Band B", "E", 0, "2026-11-21T10:00:00Z"),
  summary("s4", "Distant Tide", "Examplebandwithaverylongname", "F#m", 7, "2026-11-20T10:00:00Z"),
  summary("s5", "Grüße über die Brücke", "Example Band C", "Bb", 0, "2026-11-20T10:00:00Z"),
];
const line = (a, b) => ({ segments: [{ chord: a, text: "Walking down the " }, { chord: b, text: "road tonight" }] });
const DETAIL = {
  ...SHEETS[0],
  view: {
    title: SHEETS[0].title, artist: SHEETS[0].artist, instrument: "guitar", key: "C", key_estimated: false, shown_key: "C",
    capo: 0, capo_view: true, transpose: 0,
    flow: [
      { index: 0, kind: "verse", label: "Verse 1", repeat_of: null },
      { index: 1, kind: "chorus", label: "Chorus", repeat_of: null },
      { index: 2, kind: "chorus", label: "Chorus", repeat_of: 1 },
    ],
    sections: [
      { index: 0, kind: "verse", label: "Verse 1", repeat_of: null, lines: [line("C", "G"), line("D", "C")] },
      { index: 1, kind: "chorus", label: "Chorus", repeat_of: null, lines: [line("F", "C"), line("Am", "G")] },
      { index: 2, kind: "chorus", label: "Chorus", repeat_of: 1, lines: [] },
    ],
    chordpro: "{title: x}\n",
  },
  source_list: [],
};
const SHEET_ROUTES = {
  "/api/sheets": { sheets: SHEETS },
  "/api/sheets/research": { jobs: [] },
  "/api/sheets/setlists": { setlists: [] },
  "/api/sheets/s1": DETAIL,
};

// the fake hub's start data plus the Sheets program
const withSheets = (b, look) => ({
  ...b,
  // the windows shell keeps its windows on the hub: Sheets is open and in front
  prefs: { ...b.prefs, look, desktop: { ...b.prefs.desktop, open: ["sheets"], focus: "sheets" } },
  modules: [...b.modules, { id: "page.sheets", kind: "page", admin: false, needs: null, allowed: true }],
  workspaces: [...b.workspaces, { ...b.workspaces[0], key: "sheets", title: "Sheets", modules: ["page.sheets"], position: 28 }],
});

const problems = [];
let runs = 0;
const fail = (where, what) => problems.push(`${where}: ${what}`);

/** Runs in the page: the library table. */
function measureLibrary() {
  const table = document.querySelector("[data-testid=sheets-list]");
  if (!table) return null;
  const shown = (el) => el.getClientRects().length > 0;
  const lines = (el) => {
    const r = document.createRange();
    r.selectNodeContents(el);
    return new Set([...r.getClientRects()].map((q) => Math.round(q.top))).size;
  };
  const wrap = table.parentElement.getBoundingClientRect();
  return {
    vw: window.innerWidth,
    pageScrollX: document.documentElement.scrollWidth - window.innerWidth,
    tableRight: table.getBoundingClientRect().right,
    wrapRight: Math.min(wrap.right, window.innerWidth),
    heads: [...table.querySelectorAll("th")].filter(shown).filter((th) => lines(th) > 1).map((th) => th.textContent.trim()),
    keys: [...table.querySelectorAll(".sheets-col-key")].filter((td) => lines(td) > 1).map((td) => td.textContent.trim()),
    // one row per song, its title the only link (#159)
    rows: table.querySelectorAll("tbody tr").length,
    links: [...table.querySelectorAll("tbody tr")].filter((tr) => tr.querySelectorAll("a").length !== 1).length,
  };
}

/** Runs in the page: the stage's bar. */
function measureStage() {
  const tools = document.querySelector(".sheet-stage-tools");
  if (!tools) return null;
  return {
    vw: window.innerWidth,
    opacity: Number(getComputedStyle(tools).opacity),
    out: [...tools.querySelectorAll("button")]
      .map((b) => ({ text: b.textContent.trim(), r: b.getBoundingClientRect() }))
      .filter(({ r }) => r.right > window.innerWidth + 0.5 || r.left < -0.5 || r.bottom > window.innerHeight + 0.5)
      .map(({ text, r }) => `"${text}" ends at ${Math.round(r.right)}`),
    // inside the window but under something else (the windows shell's taskbar covered "Leave")
    covered: [...tools.querySelectorAll("button")]
      .filter((b) => {
        const r = b.getBoundingClientRect();
        const x = Math.min(r.left + r.width / 2, window.innerWidth - 1);
        const y = Math.min(r.top + r.height / 2, window.innerHeight - 1);
        const hit = document.elementFromPoint(x, y);
        return !hit || !b.contains(hit);
      })
      .map((b) => b.textContent.trim()),
  };
}

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM ?? undefined, args: ["--no-sandbox"] });
const server = await serve(dist);
const base = `http://127.0.0.1:${server.address().port}/`;
try {
  for (const look of LOOKS) for (const shell of SHELLS) for (const lang of LANGS) {
    for (const [w, h] of SIZES) {
      const where = `${w}x${h} ${lang} ${shell} ${look}`;
      const touch = w < NARROW_PX;
      const ctx = await browser.newContext({ viewport: { width: w, height: h }, locale: lang, hasTouch: touch, isMobile: touch, reducedMotion: "reduce" });
      await ctx.addInitScript((s) => {
        localStorage.setItem("topcoat.devToken", "t");
        localStorage.setItem("topcoat.shell", s);
      }, shell);
      const page = await ctx.newPage();
      watchCsp(page, (m) => problems.push(`content security policy: ${m}`));
      let prefs = withSheets(JSON_ROUTES["/api/ui/bootstrap"], look).prefs;
      await page.route("**/api/**", (route) => {
        const path = new URL(route.request().url()).pathname;
        // the windows shell saves where its windows are (and what they show) on the hub and reads it back
        if (path === "/api/ui/prefs" && route.request().method() === "PUT") {
          prefs = { ...prefs, ...route.request().postDataJSON() };
          return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(prefs) });
        }
        if (path === "/api/stream") return route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
        let body = SHEET_ROUTES[path] ?? JSON_ROUTES[path];
        if (path === "/api/me") body = { ...body, language: lang };
        if (path === "/api/ui/workspaces/sheets/agent") body = JSON_ROUTES["/api/ui/workspaces/chat/agent"];
        if (path === "/api/ui/bootstrap") body = { ...withSheets(body, look), prefs };
        return route.fulfill({ status: body === undefined ? 404 : 200, contentType: "application/json", body: JSON.stringify(body ?? {}) });
      });
      const shot = async (name) => SHOTS && page.screenshot({ path: join(SHOTS, `${w}x${h}-${lang}-${shell}-${look}-${name}.png`) });
      // a full load lands on the start page in some shells: load "/", then set the address (deep links)
      await page.goto(base);
      await page.waitForTimeout(600);
      await page.keyboard.press("Escape");
      await page.evaluate(() => (location.hash = "#/sheets"));
      await page.waitForSelector("[data-testid=sheets-list]", { timeout: 10000 }).catch(() => undefined);
      await page.waitForTimeout(250);
      await shot("library");
      const lib = await page.evaluate(measureLibrary);
      runs++;
      if (!lib) fail(where, "no library table");
      else {
        if (lib.pageScrollX > 0) fail(`${where} library`, `the page scrolls ${lib.pageScrollX} px sideways`);
        if (lib.tableRight > lib.wrapRight + 0.5) fail(`${where} library`, `the table ends at ${Math.round(lib.tableRight)}, its column at ${Math.round(lib.wrapRight)}`);
        for (const th of lib.heads) fail(`${where} library`, `the column title "${th}" breaks`);
        for (const k of lib.keys) fail(`${where} library`, `the key "${k}" breaks`);
        if (lib.rows !== 4) fail(`${where} library`, `${lib.rows} rows for 4 songs`);
        if (lib.links) fail(`${where} library`, `${lib.links} row(s) with more links than the title`);
      }

      // open the song like a person does: its title in the library
      await page.click(".sheets-col-title a[href='#/sheets/s1']", { timeout: 2000 }).catch(() => undefined);
      await page.waitForSelector("[data-testid=sheet-stage-open]", { timeout: 10000 }).catch(() => undefined);
      if (!(await page.click("[data-testid=sheet-stage-open]", { timeout: 2000 }).then(() => true, () => false))) {
        fail(where, "the stage cannot be opened");
        await ctx.close();
        continue;
      }
      await page.waitForTimeout(400);
      await shot("stage");
      const st = await page.evaluate(measureStage);
      runs++;
      if (!st) fail(`${where} stage`, "no tools");
      else {
        for (const o of st.out) fail(`${where} stage`, `${o}, the window at ${st.vw}`);
        for (const c of st.covered) fail(`${where} stage`, `"${c}" is covered by another element`);
        if (touch && st.opacity < 1) fail(`${where} stage`, `the tools are dimmed (${st.opacity}) on a touch screen`);
      }
      await ctx.close();
    }
  }
} finally {
  await browser.close();
  server.close();
}

if (problems.length) {
  console.error(`sheets-check: ${problems.length} problem(s) in ${runs} measurements`);
  for (const p of problems) console.error(`  ${p}`);
  process.exit(1);
}
console.log(`sheets-check: ${runs} measurements, no problems`);
