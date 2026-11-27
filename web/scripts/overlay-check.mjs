#!/usr/bin/env node
// Layout check of the desktop overlay, simulated in a browser. The overlay is where the GUI is used
// first (desktop/README.md): the Tauri client lays one see-through window per screen and loads the
// hub's desktop with `?overlay=1&screen=<id>`, after putting its settings into
// `window.topcoatClient.config` (desktop/src-tauri/src/overlay.rs, `client_script`). This check
// does the same in Chromium against the faked hub, so it runs without the client and without a hub:
// - the page is in overlay mode and paints no opaque ground over the screen;
// - nothing scrolls, the taskbar and every program that takes input stay on the screen, no program
//   reaches under the taskbar, no text is cut in title bars, taskbar or navigation;
// - for one, two and three programs, a maximised one, and with the command palette open;
// - per screen size and pixel ratio (the maintainer's two screens differ in DPI), look and language.
//
//   npm run build && npm run overlay-layout   (needs `playwright` or `playwright-core` and a Chromium)
//
// SCREENS=1920x1080@1,2560x1440@1.5 picks the screens, LANGS=de, LOOKS=terminal limit the run.
// SHOTS=<dir> keeps two screenshots of each step: `*.png` as the page draws itself (see-through)
// and `*-desk.png` over a busy desktop with the client's dark tint, as a person sees it.
// CHANNEL=msedge (or chrome) uses an installed browser instead of Playwright's own Chromium;
// CHROMIUM=<path> a browser binary. DIST=<dir> checks another build. Not part of CI (no browsers).
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { JSON_ROUTES, serve } from "./lib/fake-hub.mjs";
import { overlayAudit } from "./lib/overlay-audit.mjs";

const require = createRequire(import.meta.url);
function loadPlaywright() {
  const paths = [
    process.cwd(),
    "/opt/node22/lib/node_modules",
    ...(process.env.NODE_PATH ?? "").split(/[:;]/),
  ];
  for (const name of ["playwright", "playwright-core"])
    for (const p of paths.filter(Boolean)) {
      try {
        return require(require.resolve(name, { paths: [p] }));
      } catch {
        /* try the next place */
      }
    }
  console.error(
    "overlay-check needs `playwright` or `playwright-core` (npm i -g playwright-core) and a Chromium (CHANNEL=msedge works on Windows).",
  );
  process.exit(2);
}
const { chromium } = loadPlaywright();

const dist =
  process.env.DIST ??
  join(fileURLToPath(new URL("..", import.meta.url)), "dist");
// a full-HD screen, a larger one with Windows' 150 % scaling, a laptop and a tablet in landscape
const SCREENS = (
  process.env.SCREENS ?? "1920x1080@1,2560x1440@1.5,1366x768@1,1280x800@1"
)
  .split(",")
  .map((s) => {
    const [size, dpr] = s.split("@");
    const [w, h] = size.split("x").map(Number);
    return { w, h, dpr: Number(dpr ?? 1) };
  });
const LANGS = (process.env.LANGS ?? "de,en").split(",");
const LOOKS = (process.env.LOOKS ?? "terminal,leitstand,schreibtisch").split(
  ",",
);
const SHOTS = process.env.SHOTS;
if (SHOTS) mkdirSync(SHOTS, { recursive: true });

// what the client tells its page (overlay.rs, client_config)
const CLIENT = {
  version: "0.2.0",
  key: "ctrl+alt+Space",
  allKey: "AltGr+F",
  clipperKey: "ctrl+alt+C",
  effect: "blur",
  screens: 2,
};

const desk = (open, extra = {}) => ({
  open,
  minimised: [],
  focus: open[open.length - 1] ?? null,
  maximised: null,
  views: {},
  ...extra,
});
const SCENES = [
  { name: "one", desk: desk(["chat"]) },
  { name: "two", desk: desk(["chat", "dev"]) },
  { name: "three", desk: desk(["chat", "dev", "memory"]) },
  { name: "maximised", desk: desk(["chat", "dev"], { maximised: "dev" }) },
  { name: "palette", desk: desk(["chat", "dev"]), palette: true },
];

// the client's blur with its dark tint (overlay.rs: Color(8, 10, 14, 150)) over a busy desktop
const DESK_CSS = `html.tc-overlay{background:linear-gradient(rgba(8,10,14,.59),rgba(8,10,14,.59)),
  repeating-linear-gradient(35deg,#3a6ea5 0 80px,#d98c3f 80px 160px,#6aa84f 160px 240px,#ece9e2 240px 320px) !important}`;

const problems = [];
let runs = 0;

const launch = { args: ["--no-sandbox"] };
if (process.env.CHROMIUM) launch.executablePath = process.env.CHROMIUM;
else if (process.env.CHANNEL) launch.channel = process.env.CHANNEL;
const browser = await chromium.launch(launch);
const server = await serve(dist);
const base = `http://127.0.0.1:${server.address().port}/`;
try {
  for (const look of LOOKS)
    for (const lang of LANGS)
      for (const { w, h, dpr } of SCREENS)
        for (const scene of SCENES) {
          const where = `${w}x${h}@${dpr} ${lang} ${look} ${scene.name}`;
          const ctx = await browser.newContext({
            viewport: { width: w, height: h },
            deviceScaleFactor: dpr,
            locale: lang,
          });
          await ctx.addInitScript((client) => {
            localStorage.setItem("topcoat.devToken", "t");
            localStorage.setItem("topcoat.shell", "windows");
            window.topcoatClient = Object.assign(window.topcoatClient || {}, {
              config: client,
            });
          }, CLIENT);
          const page = await ctx.newPage();
          const errors = [];
          page.on("pageerror", (e) => errors.push(e.message));
          await page.route("**/api/**", (route) => {
            const path = new URL(route.request().url()).pathname;
            // the stream stays open, as with a live hub (a closed one shows "hub not reachable")
            if (path === "/api/stream") return undefined;
            let body = JSON_ROUTES[path];
            if (path === "/api/me") body = { ...body, language: lang };
            if (path === "/api/ui/bootstrap")
              body = {
                ...body,
                prefs: { ...body.prefs, look, desktop: scene.desk },
              };
            return route.fulfill({
              status: body === undefined ? 404 : 200,
              contentType: "application/json",
              body: JSON.stringify(body ?? {}),
            });
          });
          // the primary screen: the person's desktop from the hub, as the client opens it
          await page.goto(`${base}?overlay=1&screen=s1`);
          await page
            .waitForSelector(".dt-taskbar", { timeout: 10000 })
            .catch(() => undefined);
          await page.waitForTimeout(400); // the theme's arrival
          if (scene.palette) {
            await page.keyboard.press("Control+k");
            await page
              .waitForSelector(".dt-palette", { timeout: 2000 })
              .catch(() =>
                problems.push(`${where}: the palette does not open on Ctrl+K`),
              );
            await page.waitForTimeout(150);
          }
          const m = await page.evaluate(overlayAudit);
          runs++;
          for (const p of m.problems) problems.push(`${where}: ${p}`);
          for (const e of errors) problems.push(`${where}: page error: ${e}`);
          if (!m.client)
            problems.push(
              `${where}: the page did not see window.topcoatClient`,
            );
          const expected = scene.desk.maximised ? 1 : scene.desk.open.length;
          if (m.programs < expected)
            problems.push(
              `${where}: ${m.programs} program window(s) shown, ${expected} expected`,
            );
          if (scene.palette) {
            const pal = await page.evaluate(() => {
              const p = document.querySelector(".dt-palette");
              if (!p) return null;
              const r = p.getBoundingClientRect();
              return {
                inside:
                  r.left >= 0 &&
                  r.top >= 0 &&
                  r.right <= innerWidth &&
                  r.bottom <= innerHeight,
                focus: p.contains(document.activeElement),
              };
            });
            if (pal && !pal.inside)
              problems.push(`${where}: the palette leaves the screen`);
            if (pal && !pal.focus)
              problems.push(`${where}: the palette does not hold the focus`);
          }
          if (SHOTS) {
            const file = join(
              SHOTS,
              `${w}x${h}@${dpr}-${lang}-${look}-${scene.name}`,
            );
            await page.screenshot({
              path: `${file}.png`,
              omitBackground: true,
            });
            await page.addStyleTag({ content: DESK_CSS });
            await page.screenshot({ path: `${file}-desk.png` });
          }
          await ctx.close();
        }
} finally {
  await browser.close();
  server.close();
}

if (problems.length) {
  console.error(`${problems.length} problem(s) in ${runs} overlay checks:`);
  for (const p of problems) console.error(" - " + p);
  process.exit(1);
}
console.log(
  `overlay layout ok: ${runs} checks, ${SCREENS.map((s) => `${s.w}x${s.h}@${s.dpr}`).join(" ")}, ${LANGS.join("/")}, ${LOOKS.join("/")}`,
);
