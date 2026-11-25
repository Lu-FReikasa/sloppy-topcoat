#!/usr/bin/env node
// Self-test of the workshop browser helper against a real Chromium (brief E6-02, 6b).
//
// It builds a tiny fixture GUI in a temporary directory, starts helper.mjs next to it exactly as the
// node does (a mailbox directory, no port), sends it the node's JSON messages and checks the answers.
// Run it where Playwright and a Chromium are installed - inside the image:
//
//   docker run --rm --network none topcoat-workshop-browser:1 node /opt/helper/selftest.mjs
//
// or on a development host with `node deploy/workshop/browser/selftest.mjs` (the helper's `lib/` is
// taken from web/scripts/lib when the file is run from the repository).
import { spawn } from "node:child_process";
import { cp, mkdir, mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = await mkdtemp(join(tmpdir(), "topcoat-browser-selftest-"));

// the helper wants `lib/` next to it (the image copies it there); from a checkout it is in web/scripts
const helperDir = join(root, "helper");
await mkdir(helperDir, { recursive: true });
await cp(join(here, "helper.mjs"), join(helperDir, "helper.mjs"));
await cp(existsSync(join(here, "lib")) ? join(here, "lib") : join(here, "..", "..", "..", "web", "scripts", "lib"), join(helperDir, "lib"), { recursive: true });

const work = join(root, "work");
await mkdir(join(work, "web", "dist"), { recursive: true });
await writeFile(
  join(work, "web", "dist", "index.html"),
  `<!doctype html><meta charset=utf-8><title>fixture</title>
<style>
 #hover{width:100px;height:44px;opacity:1;transition:opacity 400ms ease-in,width 400ms linear}
 #hover:hover{opacity:.2;width:200px}
 #small{width:30px;height:30px}
 #spin{width:20px;height:20px;background:#08f;animation:pulse 800ms ease-in-out infinite alternate}
 @keyframes pulse{from{transform:scale(1)}to{transform:scale(2)}}
</style>
<div id=root><button id=hover>Hover me</button> <button id=small>x</button><div id=spin></div></div>
<script>fetch("/api/ui/bootstrap").then((r) => r.json()).then((b) => {
  const t = document.createElement("button");
  t.style.cssText = "width:120px;height:48px";
  t.textContent = "look " + b.prefs.look + " " + (b.themes ?? []).length;
  document.getElementById("root").append(t);
});</script>`,
);

const box = join(root, "box");
await mkdir(join(box, "in"), { recursive: true });
await mkdir(join(box, "out"), { recursive: true });
// a theme in the mailbox: the faked hub offers it and has it chosen (browser.open with a theme)
await writeFile(join(box, "theme.json"), JSON.stringify({ key: "selftest", name: "Selftest", base: "terminal" }));
// the copy of the helper finds the Playwright installed next to this script (in the image:
// /opt/helper/node_modules), not only one next to the copy (2026-11-15: "Cannot find module")
const modules = [join(here, "node_modules"), process.env.NODE_PATH].filter(Boolean).join(":");
const child = spawn("node", [join(helperDir, "helper.mjs"), "--box", box, "--work", work], {
  stdio: ["ignore", "inherit", "inherit"],
  env: { ...process.env, NODE_PATH: modules },
});

const answer = async (id) => {
  const file = join(box, "out", `${id}.json`);
  for (let t = 0; t < 600; t++) {
    if (existsSync(file)) return JSON.parse(await readFile(file, "utf8"));
    await new Promise((r) => setTimeout(r, 50));
  }
  throw new Error(`no answer for ${id}`);
};
let n = 0;
const call = async (op, args = {}) => {
  const id = String(++n).padStart(4, "0");
  await writeFile(join(box, "in", `${id}.tmp`), JSON.stringify({ id, op, args }));
  await rename(join(box, "in", `${id}.tmp`), join(box, "in", `${id}.json`));
  return answer(id);
};

let failed = 0;
const check = (name, ok, detail = "") => {
  if (!ok) failed++;
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${ok ? "" : `  ${detail}`}`);
};

try {
  const ready = await answer("ready");
  check("starts and loads the fixture", ready.ready === true, JSON.stringify(ready));

  const snap = await call("snapshot", { layout: true });
  const hover = snap.result?.nodes?.find((x) => x.role === "button" && x.name === "Hover me");
  check("snapshot lists the buttons with refs", !!hover?.ref, JSON.stringify(snap).slice(0, 300));
  const look = snap.result?.nodes?.find((x) => x.role === "button" && x.name?.startsWith("look "));
  check("the theme from the mailbox is offered and chosen", look?.name === "look selftest 1", JSON.stringify(look));

  const act = await call("act", { ref: hover?.ref, action: "hover" });
  check("act answers with a tree diff", act.ok === true && Array.isArray(act.result?.added), JSON.stringify(act).slice(0, 300));

  const audit = await call("audit", { checks: ["target_size"], matrix: [{ w: 390, h: 844 }] });
  const small = audit.result?.sizes?.[0]?.violations?.find((v) => v.check === "target_size" && v.selector.includes("#small"));
  check("audit finds the 30 px button", !!small && small.width === 30, JSON.stringify(audit).slice(0, 400));

  const refused = await call("audit", { checks: ["no_such_check"] });
  check("audit refuses an unknown check", refused.ok === false && /unknown check/.test(refused.error ?? ""), JSON.stringify(refused));

  const anim = await call("animations", { trigger: { ref: hover?.ref, action: "hover" }, sample_ms: 600 });
  const opacity = anim.result?.animations?.find((a) => a.properties?.includes("opacity"));
  const values = opacity?.samples?.map((s) => Number(s.values.opacity));
  check(
    "animations samples the hover transition from 1 down to 0.2",
    !!values && Math.abs(values[0] - 1) < 0.01 && Math.abs(values.at(-1) - 0.2) < 0.01 && values[Math.floor(values.length / 2)] > 0.2,
    JSON.stringify(opacity).slice(0, 300),
  );
  const pulse = anim.result?.animations?.find((a) => a.name === "pulse");
  check("animations samples an endless animation over one cycle", pulse?.endless === true && pulse.samples.length > 2, JSON.stringify(pulse).slice(0, 300));

  const unknown = await call("rm", {});
  check("an unknown operation is refused", unknown.ok === false, JSON.stringify(unknown));

  await call("close").catch(() => undefined);
} catch (e) {
  failed++;
  console.log(`FAIL ${e?.message ?? e}`);
} finally {
  child.kill();
  await rm(root, { recursive: true, force: true });
}
console.log(failed === 0 ? "all checks passed" : `${failed} check(s) failed`);
process.exit(failed === 0 ? 0 : 1);
