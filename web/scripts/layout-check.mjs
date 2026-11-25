#!/usr/bin/env node
// Layout check of the chat in a real browser (E3-04): the chat view is the window's height, the
// message list scrolls inside, the message box is full width (3 to 10 lines) with its controls on
// their own row below it, every click target is at least 40 px, nothing scrolls sideways.
// E3-05 adds the memory page (tabs, notes with their care facts, the care history opened up) and
// the feedback buttons under "n notes used": same rules, 40 px targets, no sideways scrolling.
//
//   npm run build && npm run layout          (needs the `playwright` package and a Chromium)
//
// E3-06 adds the instructions page (budget bars, entries, proposals, personas, preview), the
// first-start wizard (every step) and the persona bar of the chat (chip, prompt view): same rules.
//
// The hub is faked (all /api calls are answered from memory); nothing else is needed. It is not
// part of CI (browsers are not installed there) lists it as a row.
import { createRequire } from "node:module";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { JSON_ROUTES, long, serve as serveDist } from "./lib/fake-hub.mjs";
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
  console.error("layout-check needs the `playwright` package (npm i -g playwright) and a Chromium.");
  process.exit(2);
}
const { chromium } = loadPlaywright();

const dist = join(fileURLToPath(new URL("..", import.meta.url)), "dist");

// LOOK=glas|kontrollraum|werkstatt|terminal picks the look to check (default: the shipped one).
const LOOK = process.env.LOOK ?? "glas";
// CHATLIST=liste|dicht|karten picks the variant of the conversation list (default: the shipped one).
const CHATLIST = process.env.CHATLIST ?? "liste";
const WIDTHS = (process.env.WIDTHS ?? "360,412,600,768,1024,1280,1920,2560").split(",").map(Number);
const HEIGHTS = (process.env.HEIGHTS ?? "600,800,1000,1400").split(",").map(Number);
const MIN_TARGET = 40; // px
// ONLY=machines checks the Machines page alone.
const ONLY = process.env.ONLY;
const SHOTS = process.env.SHOTS; // a directory: screenshots of every combination go there

// the faked hub (data and server) lives in ./lib/fake-hub.mjs
const serve = () => serveDist(dist);

// --- measuring ---------------------------------------------------------------------------------

/** Runs in the page: what the layout looks like right now. */
function measure() {
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height };
  };
  const q = (s) => document.querySelector(s);
  const root = document.documentElement;
  const out = {
    vw: window.innerWidth, vh: window.innerHeight,
    pageScrollX: root.scrollWidth - window.innerWidth,
    pageScrollY: root.scrollHeight - window.innerHeight,
    bodyScrollY: document.body.scrollHeight - window.innerHeight,
    scrollTop: root.scrollTop,
    inner: Object.fromEntries(
      [".app", ".content", ".chat", ".chat-main", ".conversation"].map((s) => {
        const e = document.querySelector(s);
        return [s, e ? `${e.clientHeight}/${e.scrollHeight}` : "-"];
      }),
    ),
  };
  const composer = q(".composer");
  const input = q(".composer-input");
  const bar = q(".composer-bar");
  const messagesEl = q(".messages");
  const head = q(".topbar");
  const view = q(".conversation");
  if (!composer || !input || !bar || !messagesEl || !head || !view) return { ...out, missing: true };
  out.composer = rect(composer);
  out.input = rect(input);
  out.bar = rect(bar);
  out.messages = { ...rect(messagesEl), scrollHeight: messagesEl.scrollHeight, clientHeight: messagesEl.clientHeight };
  out.topbar = rect(head);
  const nav = q(".areanav");
  if (nav) out.nav = { ...rect(nav), fixed: getComputedStyle(nav).position === "fixed" };
  out.view = rect(view);
  out.empty = view.classList.contains("new-chat");
  const cs = getComputedStyle(input);
  out.line = Number.parseFloat(cs.lineHeight) || Number.parseFloat(cs.fontSize) * 1.2;
  out.pad = Number.parseFloat(cs.paddingTop) + Number.parseFloat(cs.paddingBottom) +
    Number.parseFloat(cs.borderTopWidth) + Number.parseFloat(cs.borderBottomWidth);
  const placeholder = q(".messages > p.muted");
  if (placeholder) out.placeholder = rect(placeholder);
  // every control of the composer bar and of the chat page
  const targets = [...document.querySelectorAll(
    ".composer-bar button, .composer-bar select, .composer-bar input, .composer-bar label, .composer-bar summary, " +
      ".chat-list button, .chat-list input, .chat-list li a, .conversation-head button, " +
      ".conversation-head select, .conversation-head input, .conversation-head summary",
  )].filter((el) => {
    // content of a closed <details> (export menu, options chip) is not on screen
    if (el.closest("details:not([open])") && el.tagName !== "SUMMARY") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  // a label that wraps a select, a text field or a button is not a target itself (what is inside
  // is); a check box is clicked through its label
  const scrollsSideways = (el) => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX;
      if ((o === "auto" || o === "scroll") && p.scrollWidth > p.clientWidth) return true;
    }
    return false;
  };
  out.targets = targets
    .filter((el) => !(el.tagName === "LABEL" && el.querySelector("select, button, input:not([type=checkbox])")))
    .filter((el) => !(el.tagName === "INPUT" && el.type === "checkbox"))
    .map((el) => ({
      tag: el.tagName.toLowerCase(),
      name: (el.getAttribute("aria-label") || el.textContent || el.className).trim().slice(0, 28),
      scrolls: scrollsSideways(el),
      ...rect(el),
    }));
  const panel = q(".tool-picker-panel");
  if (panel) out.panel = { ...rect(panel), scrollHeight: panel.scrollHeight, clientHeight: panel.clientHeight };
  return out;
}

/** Runs in the page: the memory page (or the feedback row of the chat) as the rules see it. */
function measureTargets(selector) {
  const root = document.documentElement;
  const targets = [...document.querySelectorAll(selector)].filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  return {
    vw: window.innerWidth, vh: window.innerHeight,
    pageScrollX: root.scrollWidth - window.innerWidth,
    count: targets.length,
    targets: targets.map((el) => {
      const r = el.getBoundingClientRect();
      return { name: (el.getAttribute("aria-label") || el.textContent || el.className).trim().slice(0, 28),
        tag: el.tagName.toLowerCase(), left: r.left, right: r.right, width: r.width, height: r.height };
    }),
  };
}

const MEMORY_TARGETS = ".memory button, .memory select, .memory input:not([type=checkbox]), " +
  ".memory textarea, .memory summary, .memory [role=tab]";

const INSTRUCTION_TARGETS = ".instructions button, .instructions select, .instructions summary, " +
  ".instructions input:not([type=checkbox]), .instructions textarea, .instructions label.check, " +
  ".persona-banner button, .persona-banner a";
const WIZARD_TARGETS = ".wizard button, .wizard select, .wizard summary, .wizard label.wizard-answer";
const PERSONA_BAR_TARGETS = ".persona-bar button, .persona-bar select, .answer-verdict button";

function checkTargets(where, m, { min = 1 } = {}) {
  if (m.count < min) fail(where, `only ${m.count} click targets found (wanted ${min})`);
  if (m.pageScrollX > 1) fail(where, `the page scrolls sideways by ${m.pageScrollX}px`);
  for (const t of m.targets) {
    if (t.height < MIN_TARGET - 0.5 || t.width < MIN_TARGET - 0.5) fail(where, `target "${t.name}" is ${Math.round(t.width)}x${Math.round(t.height)}px`);
    if (t.right > m.vw + 0.5 || t.left < -0.5) fail(where, `target "${t.name}" is outside the window`);
  }
}


// --- the Machines page (UI-01): three machines, a VM, two nodes on one machine, long names ------
const gpuNode = (id, name, extra = {}) => ({
  id, name, os: "linux", arch: "amd64", state: "available", connected: true, last_seen: "2026-01-01T12:00:00Z",
  agent_version: "0.2.0", cpu: { model: "CPU", cores: 8 }, ram_total_mb: 32768, ram_used_mb: 28415,
  gpus: [{ index: 0, name: "Radeon RX 7900 XTX", vendor: "amd", vram_total_mb: 24576, vram_used_mb: 23800 }],
  runner: { type: "llama", available: true }, models: [],
  loaded: [{ name: "gemma-3-12b-it-qat", vram_used_mb: 9000, context_length: 8192 }, { name: "qwen3-coder-30b-a3b", vram_used_mb: 14000, context_length: 8192 }],
  roles: ["gpu"], ...extra,
});
const toolNode = (id, name, extra = {}) => ({
  ...gpuNode(id, name), gpus: [], loaded: [], roles: ["tools"], ram_used_mb: 4000, ram_total_mb: 8192,
  capabilities: ["docker.ps", "logs.read", "network.ping"], ...extra,
});
const MACHINE_NODES = [
  gpuNode("n1", "node-amd"),
  gpuNode("n2", "node-laptop-with-a-very-long-name", { gpus: [], ram_used_mb: 9000, loaded: [{ name: "qwen-14b", vram_used_mb: 0, context_length: 4096 }] }),
  toolNode("n3", "tools-server-a"),
  toolNode("n4", "tools-vm-b"),
  toolNode("n5", "dev-server-a", { roles: ["dev"], capabilities: [] }),
  toolNode("n6", "tools-gone", { state: "offline" }),
];
const MACHINE_VIEW = {
  machines: [
    { key: "amd", name: "amd", kind: "physical", host: null, configured: true, nodes: ["node-amd"] },
    { key: "laptop", name: "laptop-with-a-very-long-name", kind: "physical", host: null, configured: true, nodes: ["node-laptop-with-a-very-long-name"] },
    { key: "server-a", name: "server-a", kind: "physical", host: null, configured: true, nodes: ["tools-server-a", "dev-server-a"] },
    { key: "vm-b", name: "vm-b", kind: "vm", host: "server-a", configured: true, nodes: ["tools-vm-b"] },
    { key: "gone", name: "gone", kind: "physical", host: null, configured: true, nodes: ["tools-gone"] },
  ],
  node_machine: {}, assign: {},
};
const series = (f) => Array.from({ length: 30 }, (_, i) => ({ t: i, ram_used_mb: 5000, cpu_util: f(i), gpu_util: f(i + 3), vram_used_mb: 1000 }));
// the desktop with the administration program open (the shipped bootstrap of the fake hub has none)
const MACHINE_BOOTSTRAP = {
  v: 1,
  access: { admin: true, profile: null, denied_tools: [] },
  prefs: { hue: 140, look: "terminal", desktop: { open: ["admin"], minimised: [], focus: "admin", maximised: "admin", views: {} } },
  modules: ["page.overview", "page.node"].map((id) => ({ id, kind: "page", admin: true, needs: null, allowed: true })),
  workspaces: [{
    key: "admin", title: "Administration", title_key: null, icon: "", position: 10, modules: ["page.overview", "page.node"],
    agent: { tool_scope: { nodes: null, groups: null, internet: null }, flow: null, persona: null, model_role: null },
    requires_role: null, builtin: true, rev: 1,
  }],
};
const MACHINE_ROUTES = {
  "/api/ui/bootstrap": MACHINE_BOOTSTRAP,
  "/api/nodes": MACHINE_NODES,
  "/api/machines": MACHINE_VIEW,
  "/api/nodes/metrics": { window_seconds: 1800, nodes: { n1: series((i) => (i * 7) % 100), n2: series((i) => (i * 3) % 60), n3: series(() => 4), n4: series(() => 6) } },
  "/api/activity": {
    nodes: [
      { name: "node-amd", state: "available", work: [{ node: "node-amd", model: "gemma-3-12b-it-qat", kind: "chat", seconds: 12 }, { node: "node-amd", model: "qwen3-coder-30b-a3b", kind: "digest_check", seconds: 95 }], queued: { chat: 1, answer: 0, idle: 2 } },
      { name: "node-laptop-with-a-very-long-name", state: "available", work: [] },
    ],
    roles: [{ key: "chat", model: "gemma-3-12b-it-qat", node: null }], background_nodes: [], digest: { running: 1, waiting: 2 },
  },
};
let machinesPage = false;

/** Runs in the page: does any row of the table stick out of the window or out of the content? */
function measureMachines() {
  const root = document.documentElement;
  const rows = [...document.querySelectorAll(".machine-table tbody tr")].map((r) => {
    const b = r.getBoundingClientRect();
    return { id: r.dataset.testid, left: b.left, right: b.right, width: b.width };
  });
  const cells = [...document.querySelectorAll(".machine-table td, .machine-table th")].filter((c) => c.getBoundingClientRect().width > 0)
    .map((c) => ({ right: c.getBoundingClientRect().right, left: c.getBoundingClientRect().left }));
  return { vw: window.innerWidth, pageScrollX: root.scrollWidth - window.innerWidth, rows, cells,
    table: !!document.querySelector(".machine-table"), standing: !!document.querySelector("details.activity-standing") };
}

const problems = [];
const fail = (where, what) => problems.push(`${where}: ${what}`);

function check(where, m, { long }) {
  if (m.missing) return fail(where, "chat elements not found");
  if (m.pageScrollX > 1) fail(where, `the page scrolls sideways by ${m.pageScrollX}px`);
  if (m.pageScrollY > 1 || m.bodyScrollY > 1) fail(where, `the page scrolls vertically (${m.pageScrollY}px; client/scroll heights ${JSON.stringify(m.inner)})`);
  // the composer is fully in the window, at its bottom
  if (m.composer.bottom > m.vh + 0.5 || m.composer.top < 0) fail(where, `composer outside the window (${m.composer.top}..${m.composer.bottom} of ${m.vh})`);
  if (m.bar.right > m.vw + 0.5 || m.bar.left < -0.5) fail(where, "controls stick out sideways");
  // the menu is in the window and does not cover the composer
  if (m.nav) {
    if (m.nav.left < -0.5 || m.nav.right > m.vw + 0.5) fail(where, `the menu leaves the window sideways (${Math.round(m.nav.left)}..${Math.round(m.nav.right)} of ${m.vw})`);
    if (m.nav.fixed && m.nav.top < m.composer.bottom - 0.5 && m.nav.bottom > m.composer.top) fail(where, "the menu covers the composer");
  }
  // the box: full width of the composer, at least 3 lines
  if (m.input.width < m.composer.width * 0.98) fail(where, `message box is ${Math.round(m.input.width)}px of ${Math.round(m.composer.width)}px wide`);
  if (m.input.height < m.line * 3 + m.pad - 1) fail(where, `message box has only ${((m.input.height - m.pad) / m.line).toFixed(1)} lines`);
  // the controls are on a row of their own, below the box
  if (m.bar.top < m.input.bottom - 0.5) fail(where, "controls are not below the message box");
  for (const t of m.targets) {
    if (t.height < MIN_TARGET - 0.5 || t.width < MIN_TARGET - 0.5) fail(where, `target "${t.name}" is ${Math.round(t.width)}x${Math.round(t.height)}px`);
    if (!t.scrolls && (t.right > m.vw + 0.5 || t.left < -0.5)) fail(where, `target "${t.name}" is outside the window`);
  }
  if (m.panel) {
    if (m.panel.left < -0.5 || m.panel.right > m.vw + 0.5) fail(where, "the server choice leaves the window sideways");
    if (m.panel.top < -0.5) fail(where, `the server choice starts above the window (${Math.round(m.panel.top)}px)`);
    if (m.panel.bottom > m.vh + 0.5) fail(where, `the server choice ends below the window (${Math.round(m.panel.bottom)} of ${m.vh}px)`);
  }
  if (long) {
    if (m.messages.scrollHeight <= m.messages.clientHeight) fail(where, "message list does not scroll inside");
    if (process.env.DBG) console.log(where, "messages", Math.round(m.messages.clientHeight), "bar", Math.round(m.bar.height));
    if (m.messages.clientHeight < 120) fail(where, `message list is only ${Math.round(m.messages.clientHeight)}px high`);
    if (m.composer.bottom < (m.nav && m.nav.fixed ? m.nav.top : m.vh) - 40) fail(where, `composer is not at the bottom (ends ${Math.round(m.vh - m.composer.bottom)}px above it)`);
  } else {
    if (!m.empty) fail(where, "a new chat is not marked as new");
    if (m.placeholder && m.composer.top - m.placeholder.bottom > 40) fail(where, `composer is ${Math.round(m.composer.top - m.placeholder.bottom)}px below the placeholder`);
    // known limit: in the glas look a window of 600 px or less has no room for the strip, the head,
    // the box and the dock together; the page scrolls there and the box is pinned over the hint
    const tight = LOOK === "glas" && m.vh <= 600 && m.vw <= 900;
    if (!tight && m.placeholder && m.composer.top < m.placeholder.bottom - 0.5) fail(where, "composer overlaps the placeholder");
  }
}

/** The Machines page: one row per machine; at phone width a block per machine, no page scroll. */
async function checkMachines(page, base, w, h) {
  const where = `${w}x${h} machines`;
  machinesPage = true;
  await page.goto(`${base}?machines=1#/admin/nodes`); // a new address: the page loads again, with its own bootstrap
  await page.waitForSelector(".machine-table tbody tr", { timeout: 10000 }).catch(() => undefined);
  await page.waitForSelector("[data-testid=machine-work]", { timeout: 5000 }).catch(() => undefined);
  await page.waitForTimeout(150);
  const mm = await page.evaluate(measureMachines);
  if (!mm.table) fail(where, "the machine table is missing");
  if (mm.rows.length !== 5) fail(where, `${mm.rows.length} rows instead of 5`);
  if (mm.pageScrollX > 1) fail(where, `the page scrolls sideways by ${mm.pageScrollX}px`);
  for (const r of mm.rows) if (r.right > mm.vw + 0.5 || r.left < -0.5) fail(where, `row ${r.id} leaves the window`);
  for (const c of mm.cells) if (c.right > mm.vw + 0.5) fail(where, "a cell leaves the window");
  if (!mm.standing) fail(where, "the collapsed standing tasks are missing");
  if (SHOTS) await page.screenshot({ path: join(SHOTS, `${w}x${h}-machines.png`), fullPage: true });
  machinesPage = false;
  runs++;
}

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM ?? undefined,
  args: ["--no-sandbox"],
});
const server = await serve();
const base = `http://127.0.0.1:${server.address().port}/`;
let runs = 0;
try {
  for (const w of WIDTHS) {
    for (const h of HEIGHTS) {
      const ctx = await browser.newContext({ viewport: { width: w, height: h } });
      await ctx.addInitScript(
        ([look, chatlist]) => {
          localStorage.setItem("topcoat.devToken", "t");
          localStorage.setItem("topcoat.look", look);
          localStorage.setItem("topcoat.chatlist", chatlist);
        },
        [LOOK, CHATLIST],
      );
      const page = await ctx.newPage();
      await page.route("**/api/**", (route) => {
        const path = new URL(route.request().url()).pathname;
        if (path === "/api/stream") return route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
        const snapshot = /^\/api\/chat\/prompt-snapshots\/([0-9a-f]{64})$/.exec(path);
        if (snapshot) {
          const text = "# Core — applies to every answer\n\n" + long("Du bist mein Haus-Assistent: aufmerksam, loyal.", 30);
          return route.fulfill({ status: 200, contentType: "application/json",
            body: JSON.stringify({ hash: snapshot[1], text, persona_ref: "t1@2", persona: { id: "t1", name: "Butler" }, core_version: "s", created_at: null }) });
        }
        const body = machinesPage && path in MACHINE_ROUTES ? MACHINE_ROUTES[path] : JSON_ROUTES[path];
        return route.fulfill({ status: body === undefined ? 404 : 200, contentType: "application/json", body: JSON.stringify(body ?? {}) });
      });
      if (ONLY === "machines") {
        await checkMachines(page, base, w, h);
        await ctx.close();
        continue;
      }
      if (SHOTS) {
        // the page of old chats, nothing open
        await page.goto(`${base}#/chat`);
        await page.waitForSelector(".chat-list li", { timeout: 10000 }).catch(() => undefined);
        await page.waitForTimeout(150);
        await page.screenshot({ path: join(SHOTS, `${w}x${h}-list.png`) });
      }
      for (const id of ["empty", "long"]) {
        const where = `${w}x${h} ${id}`;
        await page.goto(`${base}#/chat/${id}`);
        await page.waitForSelector(".composer-input", { timeout: 10000 }).catch(() => undefined);
        // the line under the head comes with the tools overview: measure with it, not before it
        await page.waitForSelector("[data-testid=quality-line]", { timeout: 3000 }).catch(() => undefined);
        await page.waitForTimeout(150);
        check(where, await measure_in(page), { long: id === "long" });
        runs++;
        if (id === "empty") {
          // typing grows the box up to about 10 lines, then it scrolls inside; the page does not
          for (const [lines, label] of [[1, "1 line"], [30, "30 lines"]]) {
            await page.fill(".composer-input", Array.from({ length: lines }, (_, i) => `Zeile ${i + 1}`).join("\n"));
            await page.waitForTimeout(50);
            const g = await measure_in(page);
            const shown = (g.input.height - g.pad) / g.line;
            if (lines === 1 && g.input.height < g.line * 3 + g.pad - 1) fail(`${where} ${label}`, "box shrank below 3 lines");
            if (lines === 30 && (shown > 10.5 || shown < 9 && g.vh > 800)) fail(`${where} ${label}`, `box shows ${shown.toFixed(1)} lines (wanted about 10)`);
            if (g.pageScrollY > 1) fail(`${where} ${label}`, "typing made the page scroll");
            if (g.composer.bottom > g.vh + 0.5) fail(`${where} ${label}`, "composer left the window while typing");
            runs++;
          }
        }
        if (id === "empty") {
          // the server choice opens above the bar, inside the window, with 40 px targets
          await page.fill(".composer-input", "");
          await page.click(".composer-options > summary"); // the controls sit behind the options chip
          await page.click(".tool-picker > button");
          await page.waitForTimeout(50);
          const open = await measure_in(page);
          check(`${where} server choice open`, open, { long: false });
          runs++;
          if (SHOTS) await page.screenshot({ path: join(SHOTS, `${w}x${h}-${id}-panel.png`) });
          await page.keyboard.press("Escape"); // closes it (so does the button inside, or a click outside)
        }
        if (id === "long") {
          // "n notes used" unfolds to the feedback buttons: 40 px each, inside the window
          await page.locator(".memory-used summary").first().click();
          await page.waitForTimeout(50);
          checkTargets(`${where} feedback`, await page.evaluate(measureTargets, ".memory-used summary, .memory-feedback button"), { min: 5 });
          runs++;
        }
        if (id === "long") {
          // the persona bar: chip, "View prompt" opened, a change notice; verdict buttons under answers
          await page.click("[data-testid=prompt-toggle]");
          await page.waitForSelector("[data-testid=prompt-view] pre", { timeout: 5000 }).catch(() => undefined);
          const pm = await page.evaluate(measureTargets, PERSONA_BAR_TARGETS);
          checkTargets(`${where} persona bar`, pm, { min: 3 });
          if (!(await page.locator("[data-testid=prompt-changed]").count())) fail(`${where} persona bar`, "no change notice");
          runs++;
          await page.click("[data-testid=prompt-toggle]");
        }
        if (SHOTS) await page.screenshot({ path: join(SHOTS, `${w}x${h}-${id}.png`) });
      }
      // the memory page: every tab, the care history opened up
      await page.goto(`${base}#/memory`);
      await page.waitForSelector(".memory-tabs", { timeout: 10000 }).catch(() => undefined);
      for (const tab of ["personal", "infra", "archive"]) {
        const where = `${w}x${h} memory ${tab}`;
        await page.click(`#memory-tab-${tab}`);
        await page.waitForTimeout(100);
        if (tab === "personal") {
          await page.locator(".care-run summary").first().click();
          await page.locator(".care-settings summary").first().click();
          await page.locator(".memory-item .row-actions button").first().click(); // history
          await page.waitForTimeout(50);
        }
        checkTargets(where, await page.evaluate(measureTargets, MEMORY_TARGETS), { min: 8 });
        if (SHOTS) await page.screenshot({ path: join(SHOTS, `${w}x${h}-memory-${tab}.png`), fullPage: true });
        runs++;
      }
      // the instructions page: budget, entries, proposals, personas, with the reminder banner
      {
        const where = `${w}x${h} instructions`;
        await page.goto(`${base}#/instructions`);
        await page.waitForSelector(".instructions .core-budget", { timeout: 10000 }).catch(() => undefined);
        await page.waitForSelector(".persona-banner", { timeout: 5000 }).catch(() => undefined);
        await page.locator(".core-preview summary").first().click();
        await page.locator(".persona-card summary").first().click();
        await page.locator(".core-item .row-actions button").first().click(); // edit form of an entry
        await page.waitForTimeout(100);
        checkTargets(where, await page.evaluate(measureTargets, INSTRUCTION_TARGETS), { min: 25 });
        for (const bar of await page.locator(".budget-bar").all()) {
          const box = await bar.boundingBox();
          if (!box || box.width < 40) fail(where, "a budget bar is squeezed");
        }
        if (SHOTS) await page.screenshot({ path: join(SHOTS, `${w}x${h}-instructions.png`), fullPage: true });
        runs++;
        // the wizard, every step
        await page.click(".persona-banner button.primary");
        await page.waitForSelector(".wizard", { timeout: 5000 }).catch(() => undefined);
        for (const step of ["scene 1", "scene 2", "scene 3", "questions", "preview"]) {
          await page.waitForTimeout(80);
          const wm = await page.evaluate(measureTargets, WIZARD_TARGETS);
          checkTargets(`${w}x${h} wizard ${step}`, wm, { min: 4 });
          const dialog = await page.locator(".dialog").boundingBox();
          if (!dialog || dialog.x < -0.5 || dialog.x + dialog.width > w + 0.5) fail(`${w}x${h} wizard ${step}`, "the dialog leaves the window sideways");
          if (SHOTS) await page.screenshot({ path: join(SHOTS, `${w}x${h}-wizard-${step.replace(" ", "")}.png`) });
          runs++;
          if (step.startsWith("scene")) await page.locator(".wizard-answer input").first().check();
          if (step !== "preview") await page.getByRole("button", { name: "Weiter" }).click();
        }
      }
      await checkMachines(page, base, w, h);
      await ctx.close();
    }
  }
} finally {
  await browser.close();
  server.close();
}

function measure_in(page) {
  return page.evaluate(measure);
}

if (problems.length) {
  console.error(`${problems.length} problem(s) in ${runs} checks:`);
  for (const p of problems) console.error(" - " + p);
  process.exit(1);
}
console.log(`layout ok: ${runs} checks, widths ${WIDTHS.join("/")}, heights ${HEIGHTS.join("/")}`);
