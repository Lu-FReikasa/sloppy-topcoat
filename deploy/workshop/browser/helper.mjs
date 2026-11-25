#!/usr/bin/env node
// The helper inside the workshop browser container (brief E6-02-workshop-browser).
//
// It serves the built GUI of the worktree copy (/work) together with a faked hub on the container's
// own loopback, opens it in a headless Chromium, and then answers the commands the node leaves in
// the mailbox: /box/in/<id>.json in, /box/out/<id>.json out (written to a temporary name first and
// renamed). It opens no port that anything outside the container could reach, there is no network
// in the container at all, and it understands only the fixed operations below - never JavaScript or
// a command line from the model.
//
// UNVERIFIED on real hardware: written against Playwright's API; the pure parts are unit-tested.
import { spawn } from "node:child_process";
import { readdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { createServer as createNetServer } from "node:net";
import { createRequire } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const arg = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : fallback;
};
const BOX = arg("box", "/box");
const WORK = arg("work", "/work");
const MODE = arg("mode", "built");
const START_PATH = arg("path", "/");
const MAX_SECONDS = Number(arg("max-seconds", "1800"));

const here = new URL(".", import.meta.url).pathname;
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const { runAudit, DEFAULT_MATRIX } = await import(pathToFileURL(join(here, "lib", "audit.mjs")).href);
const { snapshotInPage, treeLines, treeDiff } = await import(pathToFileURL(join(here, "lib", "snapshot-page.mjs")).href);
const { serve } = await import(pathToFileURL(join(here, "lib", "fake-hub.mjs")).href);

const reply = async (id, body) => {
  const tmp = join(BOX, "out", `${id}.tmp`);
  await writeFile(tmp, JSON.stringify(body));
  await rename(tmp, join(BOX, "out", `${id}.json`));
};

// --- the page ----------------------------------------------------------------------------------

const logs = []; // console, page errors, failed requests: { n, kind, text }
const note = (kind, text) => logs.push({ n: logs.length + 1, kind, text: String(text).slice(0, 300) });

// the repository's own fake hub when it has one, else the shared one that ships with this image
async function fakeRoutes() {
  try {
    return (await import(pathToFileURL(join(WORK, "web", "scripts", "lib", "fake-hub.mjs")).href)).JSON_ROUTES;
  } catch {
    return (await import(pathToFileURL(join(here, "lib", "fake-hub.mjs")).href)).JSON_ROUTES;
  }
}

// A theme the node left in the mailbox (browser.open with a theme): the faked hub offers it as the
// person's only custom theme and has it chosen, so the GUI is drawn in it from the first frame.
let theme = null;
try {
  theme = JSON.parse(await readFile(join(BOX, "theme.json"), "utf8"));
} catch {
  /* no theme: the GUI's own look */
}
function withTheme(routes) {
  if (!theme?.key) return routes;
  const boot = routes["/api/ui/bootstrap"] ?? {};
  return {
    ...routes,
    "/api/ui/themes": [theme],
    // three programs open, so the theme's layout, bar and title bars have something to arrange
    "/api/ui/bootstrap": {
      ...boot,
      themes: [theme],
      prefs: {
        ...(boot.prefs ?? {}),
        look: theme.key,
        desktop: { ...(boot.prefs?.desktop ?? {}), open: ["chat", "dev", "memory"], minimised: [], focus: "chat", maximised: null },
      },
    },
  };
}

const freePort = () =>
  new Promise((resolve, reject) => {
    const probe = createNetServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });

// Mode dev: the repository's own dev server (vite) on the container's loopback. The container has
// no network, so the dependencies must already be in the worktree copy (web/node_modules). The
// server's /api proxy is never reached: the page's API calls are answered by the faked hub below.
async function startDevServer() {
  const web = join(WORK, "web");
  const vite = join(web, "node_modules", ".bin", "vite");
  try {
    await stat(vite);
  } catch {
    throw new Error("mode dev needs web/node_modules in the worktree (the container cannot install them): use mode built");
  }
  const port = await freePort();
  // /work is read-only: vite would write its bundled config and its dependency cache into the
  // worktree. A wrapper in /tmp loads the repository's config in memory and moves the cache.
  const wrapper = join(tmpdir(), "topcoat-vite.config.mjs");
  const viteApi = pathToFileURL(join(web, "node_modules", "vite", "dist", "node", "index.js")).href;
  await writeFile(
    wrapper,
    `import { loadConfigFromFile, mergeConfig } from ${JSON.stringify(viteApi)};
const loaded = await loadConfigFromFile({ command: "serve", mode: "development" }, ${JSON.stringify(join(web, "vite.config.ts"))}, ${JSON.stringify(web)}, undefined, undefined, "runner");
export default mergeConfig(loaded?.config ?? {}, { root: ${JSON.stringify(web)}, cacheDir: ${JSON.stringify(join(tmpdir(), "topcoat-vite-cache"))} });
`,
  );
  const args = ["--config", wrapper, "--configLoader", "runner", "--host", "127.0.0.1", "--port", String(port), "--strictPort"];
  const child = spawn(vite, args, { cwd: web, stdio: "ignore" });
  let died = false;
  child.once("exit", () => (died = true));
  for (let i = 0; i < 300; i++) {
    if (died) throw new Error("the dev server stopped right after it started");
    try {
      if ((await fetch(`http://127.0.0.1:${port}/`)).ok) {
        return { address: () => ({ port }), close: () => child.kill() };
      }
    } catch {
      /* not listening yet */
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  child.kill();
  throw new Error("the dev server did not answer within 30 seconds");
}

async function startServer() {
  if (MODE === "built") {
    // the worktree's own build, else the GUI the image was built with (a theme looks the same in it)
    for (const dist of [join(WORK, "web", "dist"), join(here, "gui")]) {
      try {
        await stat(join(dist, "index.html"));
      } catch {
        continue;
      }
      if (dist !== join(WORK, "web", "dist")) note("info", "the worktree has no web/dist: showing the GUI of the image");
      return { server: await serve(dist), routes: withTheme(await fakeRoutes()) };
    }
    throw new Error("the worktree has no built GUI (web/dist/index.html): build it first, or use mode dev");
  }
  if (MODE === "dev") return { server: await startDevServer(), routes: withTheme(await fakeRoutes()) };
  throw new Error(`unknown mode: ${MODE}`);
}

const state = { width: 1280, height: 800, dpr: 1, mobile: false, scheme: "light", reducedMotion: false };

// everything that can fail at start-up is in here, so the node is told why instead of waiting
async function boot() {
  const { server, routes } = await startServer();
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  await context.addInitScript((windows) => {
    localStorage.setItem("topcoat.devToken", "t");
    // a theme is about the window desktop; a plain browser would draw the navigation instead
    if (windows) localStorage.setItem("topcoat.shell", "windows");
    window.__tcShifts = [];
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) if (!e.hadRecentInput) window.__tcShifts.push(e.value);
    }).observe({ type: "layout-shift", buffered: true });
  }, Boolean(theme?.key));
  // nothing leaves the box: anything that is not the page itself is refused; the hub's API is faked
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== base) return route.abort();
    if (url.pathname.startsWith("/api/")) {
      if (url.pathname === "/api/stream") return route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
      const body = routes[url.pathname];
      return route.fulfill({ status: body === undefined ? 404 : 200, contentType: "application/json", body: JSON.stringify(body ?? {}) });
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.on("console", (m) => note(m.type(), m.text()));
  page.on("pageerror", (e) => note("pageerror", e.message));
  page.on("requestfailed", (r) => note("requestfailed", `${r.method()} ${r.url()} ${r.failure()?.errorText ?? ""}`));
  page.on("response", (r) => r.status() >= 400 && note("http", `${r.status()} ${r.url()}`));
  const cdp = await context.newCDPSession(page);
  await page.goto(`${base}/#${START_PATH}`);
  await page.waitForLoadState("load");
  // the dev server transforms its modules on the first visit: ready means the app has drawn something
  await page.waitForFunction(() => (document.getElementById("root")?.childElementCount ?? 0) > 0, undefined, { timeout: 30000 });
  await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => undefined);
  return { server, base, browser, context, page, cdp };
}

let booted;
try {
  booted = await boot();
} catch (e) {
  await reply("ready", { ready: false, error: String(e?.message ?? e).split("\n")[0].slice(0, 300) });
  process.exit(1);
}
const { server, browser, context, page, cdp } = booted;

// --- the operations ----------------------------------------------------------------------------

async function setViewport({ width, height, dpr = 1, orientation, scheme, reduced_motion: reduced }) {
  Object.assign(state, { width, height, dpr, mobile: width < 768 });
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width, height, deviceScaleFactor: dpr, mobile: state.mobile,
    screenOrientation: { type: orientation === "landscape" || (!orientation && width > height) ? "landscapePrimary" : "portraitPrimary", angle: 0 },
  });
  if (scheme || reduced !== undefined) {
    state.scheme = scheme ?? state.scheme;
    state.reducedMotion = reduced ?? state.reducedMotion;
    await page.emulateMedia({ colorScheme: state.scheme, reducedMotion: state.reducedMotion ? "reduce" : "no-preference" });
  }
  await page.waitForTimeout(100);
  return page.evaluate(() => ({
    width: innerWidth, height: innerHeight, dpr: devicePixelRatio,
    scheme: matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light",
    reduced_motion: matchMedia("(prefers-reduced-motion: reduce)").matches,
  }));
}

const tree = (args) => page.evaluate(snapshotInPage, { scope: args.scope, layout: !!args.layout, maxNodes: args.max_nodes ?? 200 });

async function handle(refOf) {
  const h = await page.evaluateHandle((ref) => window.__tcRefs?.get(ref) ?? null, refOf);
  const el = h.asElement();
  if (!el) throw new Error(`no element has the ref ${refOf}: take a new snapshot`);
  return el;
}

async function act({ ref, action, value }) {
  const before = treeLines((await tree({ max_nodes: 400 })).nodes);
  const mark = logs.length;
  if (action === "key") {
    await page.keyboard.press(value);
  } else if (action === "scroll") {
    const dx = value === "left" ? -400 : value === "right" ? 400 : 0;
    const dy = value === "up" ? -400 : value === "down" ? 400 : 0;
    if (ref) await (await handle(ref)).evaluate((e, d) => e.scrollBy(d[0], d[1]), [dx, dy]);
    else await page.mouse.wheel(dx, dy);
  } else {
    const el = await handle(ref);
    if (action === "click") await el.click({ timeout: 5000 });
    else if (action === "hover") await el.hover({ timeout: 5000 });
    else if (action === "type") await el.fill(value, { timeout: 5000 });
  }
  await page.waitForTimeout(150);
  const after = treeLines((await tree({ max_nodes: 400 })).nodes);
  return { ...treeDiff(before, after), console: logs.slice(mark, mark + 20) };
}

async function consoleSince({ since = 0 }) {
  const items = logs.filter((l) => l.n > since).slice(0, 100);
  return { messages: items, last: logs.length, more: Math.max(0, logs.filter((l) => l.n > since).length - items.length) };
}

async function animations({ trigger, sample_ms: sampleMs = 1000, properties }) {
  if (trigger) {
    const el = await handle(trigger.ref);
    if (trigger.action === "click") await el.click({ timeout: 5000 });
    else await el.hover({ timeout: 5000 });
  }
  const result = await page.evaluate(
    ({ sampleMs, properties }) => {
      const selector = (el) => (el?.tagName ? el.tagName.toLowerCase() + (el.id ? `#${el.id}` : "") + (typeof el.className === "string" && el.className ? "." + el.className.trim().split(/\s+/)[0] : "") : "");
      const list = document.getAnimations().filter((a) => a.effect?.target);
      const out = [];
      for (const a of list.slice(0, 12)) {
        const t = a.effect.getComputedTiming();
        const target = a.effect.target;
        const keyframes = a.effect.getKeyframes();
        const names = properties?.length ? properties : [...new Set(keyframes.flatMap((k) => Object.keys(k)).filter((k) => !["offset", "easing", "composite", "computedOffset"].includes(k)))].slice(0, 6);
        a.pause();
        const samples = [];
        const steps = 10;
        // an endless animation has no end time: sample one full cycle (two when it alternates)
        const cycle = Number(t.delay) + Number(t.duration) * (/alternate/.test(t.direction) ? 2 : 1);
        const span = Number.isFinite(Number(t.endTime)) && Number(t.endTime) > 0 ? Number(t.endTime) : cycle || sampleMs;
        for (let i = 0; i <= steps; i++) {
          a.currentTime = span * (i / steps);
          const cs = getComputedStyle(target);
          samples.push({ t: Math.round(a.currentTime), values: Object.fromEntries(names.map((n) => [n, cs.getPropertyValue(n.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)) || cs[n]])) });
        }
        out.push({
          target: selector(target), kind: a.constructor.name, name: a.animationName ?? a.transitionProperty ?? a.id ?? "",
          duration_ms: Number(t.duration), delay_ms: Number(t.delay), easing: t.easing, iterations: t.iterations,
          fill: t.fill, endless: !Number.isFinite(Number(t.endTime)), properties: names, samples,
        });
        a.play();
      }
      return { count: list.length, animations: out, layout_shift: (window.__tcShifts ?? []).reduce((s, v) => s + v, 0) };
    },
    { sampleMs, properties },
  );
  // frame times over the first part of the sample window, in real time
  const frames = await page.evaluate(
    (ms) =>
      new Promise((resolve) => {
        const deltas = [];
        let last = performance.now();
        const start = last;
        const tick = (now) => {
          deltas.push(now - last);
          last = now;
          if (now - start < ms) requestAnimationFrame(tick);
          else {
            const sorted = [...deltas].sort((a, b) => a - b);
            resolve({ frames: deltas.length, avg_ms: deltas.reduce((s, v) => s + v, 0) / Math.max(1, deltas.length), p95_ms: sorted[Math.floor(sorted.length * 0.95)] ?? 0, max_ms: sorted[sorted.length - 1] ?? 0 });
          }
        };
        requestAnimationFrame(tick);
      }),
    Math.min(sampleMs, 1000),
  );
  return { ...result, frame_times: frames };
}

const SHOT_BYTES = 34 * 1024; // the node's answer is bounded; a base64 picture has to fit

async function shot({ region, scale = 0.5, filmstrip }) {
  const grab = async (s, quality) => {
    const clip = region ? { x: region.x, y: region.y, width: region.w, height: region.h, scale: s } : undefined;
    const r = await cdp.send("Page.captureScreenshot", {
      format: "jpeg", quality, ...(clip ? { clip } : { clip: { x: 0, y: 0, width: state.width, height: state.height, scale: s } }),
    });
    return r.data;
  };
  let s = scale;
  let quality = 60;
  if (!filmstrip) {
    for (let i = 0; i < 6; i++) {
      const data = await grab(s, quality);
      if (data.length <= SHOT_BYTES) return { format: "jpeg", scale: s, base64: data };
      s = Math.max(0.1, s * 0.7);
      quality = Math.max(25, quality - 10);
    }
    throw new Error("the picture does not fit: ask for a smaller region or scale");
  }
  const frames = [];
  const step = 100;
  for (let i = 0; i < filmstrip; i++) {
    frames.push(await grab(Math.min(s, 0.25), 40));
    await page.waitForTimeout(step);
  }
  const sheet = await context.newPage();
  const data = await sheet.evaluate(async (list) => {
    const imgs = await Promise.all(list.map((b) => new Promise((ok) => { const i = new Image(); i.onload = () => ok(i); i.src = `data:image/jpeg;base64,${b}`; })));
    const w = imgs[0].width;
    const h = imgs[0].height;
    const cols = Math.min(4, imgs.length);
    const rows = Math.ceil(imgs.length / cols);
    const c = document.createElement("canvas");
    c.width = w * cols;
    c.height = h * rows;
    const g = c.getContext("2d");
    imgs.forEach((im, i) => g.drawImage(im, (i % cols) * w, Math.floor(i / cols) * h));
    return c.toDataURL("image/jpeg", 0.5).split(",")[1];
  }, frames);
  await sheet.close();
  if (data.length > SHOT_BYTES) throw new Error("the filmstrip does not fit: use fewer frames or a smaller region");
  return { format: "jpeg", frames: filmstrip, step_ms: step, base64: data };
}

const OPS = {
  viewport: setViewport,
  snapshot: tree,
  act,
  audit: (args) => runAudit(page, args, (size) => setViewport({ width: size.w, height: size.h, dpr: size.dpr ?? 1 })),
  animations,
  console: consoleSince,
  shot,
};

// --- the mailbox ---------------------------------------------------------------------------------

await reply("ready", { ready: true, url: page.url(), default_matrix: DEFAULT_MATRIX.length });
const deadline = Date.now() + MAX_SECONDS * 1000;
let running = true;
const shutdown = async () => {
  running = false;
  await browser.close().catch(() => undefined);
  server.close();
  process.exit(0);
};
process.on("SIGTERM", shutdown);

// commands already taken: a command file that cannot be removed (a mailbox the node made without
// write access for this uid) is not run twice, and does not end the helper
const taken = new Set();
while (running && Date.now() < deadline) {
  let names = [];
  try {
    names = (await readdir(join(BOX, "in"))).filter((n) => n.endsWith(".json") && !taken.has(n)).sort();
  } catch {
    /* the mailbox is gone: the node closed the session */
    break;
  }
  for (const name of names) {
    const file = join(BOX, "in", name);
    let msg;
    try {
      msg = JSON.parse(await readFile(file, "utf8"));
    } catch {
      continue;
    } finally {
      taken.add(name);
      await rm(file, { force: true }).catch(() => undefined);
    }
    if (msg.op === "close") {
      await shutdown();
    }
    const run = OPS[msg.op];
    if (!run) {
      await reply(msg.id, { ok: false, error: `unknown operation: ${msg.op}` });
      continue;
    }
    try {
      await reply(msg.id, { ok: true, result: await run(msg.args ?? {}) });
    } catch (e) {
      await reply(msg.id, { ok: false, error: String(e?.message ?? e).split("\n")[0].slice(0, 400) });
    }
  }
  await new Promise((r) => setTimeout(r, 25));
}
await shutdown();
