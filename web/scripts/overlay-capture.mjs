#!/usr/bin/env node
// Screenshots and the layout audit of the REAL desktop overlay, for the maintainer's machine. The
// client must run with the developer's switch (desktop/src-tauri/src/devtools.rs):
//
//   $env:TOPCOAT_DEVTOOLS_PORT = "9222"; & "<path>\topcoat-desktop.exe"     (PowerShell)
//   node web/scripts/overlay-capture.mjs                                    (another terminal)
//
// It connects to the client's web views over the DevTools protocol on 127.0.0.1, finds the overlay
// pages (`?overlay=1`, one per screen), and for each one that is shown takes a screenshot as the
// page draws itself and runs the same audit as the simulated check (lib/overlay-audit.mjs).
//
// It only looks: it does not click, type or navigate, so the person's desktop on the hub stays as
// it is. Show the overlay first (the key, or AltGr+F for every screen), or pass SHOW=1: the script
// then presses the client's key itself (Windows, through PowerShell) and waits for the pages.
// WAIT=<seconds> waits that long for a shown overlay (default 20).
//
// PORT=9222 picks the port, SHOTS=<dir> where the screenshots go (default ./overlay-shots).
// Needs `playwright` or `playwright-core` (no browser download: it attaches to the client's own).
//
// UNVERIFIED on real hardware: attaching to the client's WebView2 and the screenshot of a
// see-through window.
import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";
import { overlayAudit } from "./lib/overlay-audit.mjs";

const require = createRequire(import.meta.url);
function loadPlaywright() {
  const paths = [process.cwd(), ...(process.env.NODE_PATH ?? "").split(/[:;]/)];
  for (const name of ["playwright", "playwright-core"])
    for (const p of paths.filter(Boolean)) {
      try {
        return require(require.resolve(name, { paths: [p] }));
      } catch {
        /* try the next place */
      }
    }
  console.error(
    "overlay-capture needs `playwright` or `playwright-core` (npm i -g playwright-core).",
  );
  process.exit(2);
}
const { chromium } = loadPlaywright();

const PORT = Number(process.env.PORT ?? 9222);
const SHOTS = resolve(process.env.SHOTS ?? "overlay-shots");
const WAIT = Number(process.env.WAIT ?? 20) * 1000;
mkdirSync(SHOTS, { recursive: true });

/** Press the client's overlay key for every screen (default AltGr+F = Ctrl+Alt+F) on Windows. */
function pressShowKey() {
  const script =
    "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('^%f')";
  execFileSync(
    "powershell.exe",
    ["-NoProfile", "-NonInteractive", "-Command", script],
    { stdio: "ignore" },
  );
}

let browser;
try {
  browser = await chromium.connectOverCDP(`http://127.0.0.1:${PORT}`);
} catch (e) {
  console.error(
    `no client on 127.0.0.1:${PORT}: start it with TOPCOAT_DEVTOOLS_PORT=${PORT} (${e.message.split("\n")[0]})`,
  );
  process.exit(2);
}

const overlays = () =>
  browser
    .contexts()
    .flatMap((c) => c.pages())
    .filter((p) => {
      try {
        return new URL(p.url()).searchParams.get("overlay") === "1";
      } catch {
        return false;
      }
    });

if (process.env.SHOW === "1") pressShowKey();

// wait for at least one overlay page that is shown
const until = Date.now() + WAIT;
let shown = [];
while (Date.now() < until) {
  shown = [];
  for (const p of overlays()) {
    if (
      (await p
        .evaluate(() => document.visibilityState)
        .catch(() => "hidden")) === "visible"
    )
      shown.push(p);
  }
  if (shown.length) break;
  await new Promise((r) => setTimeout(r, 500));
}
if (!shown.length) {
  console.error(
    `no overlay shown within ${WAIT / 1000} s (found ${overlays().length} overlay page(s)); press the overlay key, or SHOW=1`,
  );
  await browser.close();
  process.exit(1);
}
await new Promise((r) => setTimeout(r, 600)); // the theme's arrival

const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
let problems = 0;
for (const page of shown) {
  const screen = new URL(page.url()).searchParams.get("screen") ?? "main";
  const audit = await page.evaluate(overlayAudit);
  const file = join(
    SHOTS,
    `${stamp}-screen-${screen}-${audit.vw}x${audit.vh}@${audit.dpr}.png`,
  );
  await page.screenshot({ path: file, omitBackground: true });
  console.log(
    `screen ${screen}: ${audit.vw}x${audit.vh}@${audit.dpr}, ${audit.programs} program(s) -> ${file}`,
  );
  for (const p of audit.problems) console.log(`  - ${p}`);
  problems += audit.problems.length;
}
// detach only: closing a CDP connection to the client must not close its web views
await browser.close();
console.log(
  problems
    ? `${problems} problem(s) on ${shown.length} screen(s)`
    : `overlay ok on ${shown.length} screen(s)`,
);
process.exit(problems ? 1 : 0);
