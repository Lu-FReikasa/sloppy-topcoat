// The faked hub of the layout checks and of the workshop browser (E6-02): the pages of
// the GUI are served from the built `dist` directory and every /api call is answered from memory,
// so a page can be measured without a hub. Shared on purpose: the layout checks (`shell-check.mjs`,
// `sheets-check.mjs`, `overlay-check.mjs`) and the helper of
// the browser container (`deploy/workshop/browser`) use the same data.
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";

// The hub's security headers (hub/src/topcoat_hub/security_headers.py; a hub test keeps the policy
// equal): the checks run the GUI under the same policy, so a change that needs another source fails
// here, not on the hub.
export const CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' data: blob:; connect-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";
export const SECURITY_HEADERS = {
  "content-security-policy": CSP,
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
};

/** Collects the policy's refusals on a page (Chromium reports each one on the console). */
export function watchCsp(page, sink) {
  page.on("console", (m) => {
    if (/Content Security Policy/i.test(m.text())) sink(m.text().split("\n")[0].slice(0, 300));
  });
}

const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };

// --- the faked hub -----------------------------------------------------------------------------

const para = (i) =>
  `Absatz ${i}: ` + "Der Container web startet neu, weil ihm der Speicher ausgeht. ".repeat(3 + (i % 4));
const messages = Array.from({ length: 40 }, (_, i) => ({
  id: i + 1,
  role: i % 2 ? "assistant" : "user",
  content: i % 2 ? para(i) + "\n\n- ein Punkt\n- noch ein Punkt\n\n```\ndocker ps\n```" : para(i),
  model: "qwen-7b",
  node: "node-amd",
  fallback_from: null,
  status: "complete",
  error: null,
  prompt_tokens: 100,
  completion_tokens: 50,
  first_token_ms: 400,
  duration_ms: 3000,
  context_trimmed: 0,
  // a new day every ten messages: the date separators are part of the measured page
  created_at: new Date(Date.UTC(2026, 8, 28 + Math.floor(i / 10), 9, i, 0)).toISOString(),
  verification: null,
  memory_refs: i === 3
    ? [
        { id: "n1", text: "Wohnt in Hamburg und hat einen sehr langen Notiztext für den Test", evidence: "doc", status: "active" },
        { id: "n2", text: "Der Container web läuft auf Port 8080", evidence: "live", status: "active" },
      ]
    : [],
  agent_task_id: null,
  created_at: "2026-10-01T10:00:00Z",
}));
const note = (i, kind, status = "active") => ({
  id: `m${i}`, text: `Notiz ${i}: ` + "Der Container web läuft auf Port 8080 und startet nachts neu. ".repeat(1 + (i % 3)),
  status, evidence: i % 2 ? "doc" : "live", confidence: 0.9, source_type: "chat", source_ref: { conversation_id: "long" },
  version: 2, confirmed_count: i % 4, last_confirmed_at: "2026-09-20T10:00:00Z", disputed_with: null, superseded_by: null,
  embedder: "bge-m3", created_at: "2026-09-01T10:00:00Z", updated_at: "2026-09-02T10:00:00Z",
  kind, stage: ["sure", "older", "stale"][i % 3], reliability: 0.5, usefulness: 0.6,
  last_verified_at: i % 2 ? null : "2026-09-25T10:00:00Z", verified_count: i % 2 ? 0 : 1, expires_at: null,
  verify_with: kind === "operational" ? { capability: "docker.ps", input: {} } : null,
  recalled_count: 5, used_count: 3, last_used_at: "2026-09-28T10:00:00Z", feedback_positive: 0, feedback_negative: 0,
  halflife_days: 21,
});
const MEMORY_ITEMS = [
  ...Array.from({ length: 6 }, (_, i) => note(i, "person")),
  ...Array.from({ length: 6 }, (_, i) => note(10 + i, "operational")),
  note(20, "person", "archived"),
];
const careChange = (action, undo) => ({
  action, item_id: "m10", text: "Der Container web läuft auf Port 8080", kind: "operational", version: 3,
  reason: "care: a live check shows a different state (E1): " + "web is on 8081 ".repeat(6), undo,
  from_version: 2, new_text: action === "changed" ? "Der Container web läuft auf Port 8081" : undefined,
});
const MEMORY = {
  items: MEMORY_ITEMS,
  counts: { active: 12, disputed: 0, superseded: 0, deleted: 0, archived: 1 },
  blocked: 0, enabled: true, embedder: { id: "bge-m3", transitional: false }, running: false, runs: [],
  questions: [], kinds: { person: 6, preference: 0, operational: 6 },
  care: {
    running: false,
    settings: { hour: 3, interval_seconds: 900, halflife_days: { person: 365, preference: 180, operational: 14, volatile: 1 },
      volatile_ttl_hours: 24, check_threshold: 0.5, archive: { usefulness: 0.3, reliability: 0.25, idle_days: 90 } },
    runs: [{ id: "r1", kind: "care", trigger: "nightly", status: "ok", started_at: "2026-10-01T01:00:00Z",
      finished_at: "2026-10-01T01:05:00Z",
      stats: { checked: 3, unchanged: 1, changed: 1, asked: 0, expired: 1, archived: 0, unverifiable: 0,
        changes: [careChange("changed", "revert"), careChange("expired", "restore"), careChange("verified", null)] } }],
  },
};
const HASH1 = "a1".repeat(32);
const HASH2 = "b2".repeat(32);
const long = (text, n = 3) => (text + " ").repeat(n).trim();
const ci = (id, kind, text, o = {}) => ({
  id, text, status: "active", evidence: "doc", confidence: 1, source_type: "user", source_ref: {}, version: 2,
  confirmed_count: 0, last_confirmed_at: null, disputed_with: null, superseded_by: null, embedder: null,
  created_at: "2026-09-01T10:00:00Z", updated_at: "2026-09-02T10:00:00Z", kind, core: true, scope: "user",
  in_core: true, example: null, weak: false, ...o,
});
const CORE = {
  budget: { total: 1000, used: 640, context: 16384, cut: [], examples_dropped: false, parts: {
    system: { budget: 200, used: 190 }, persona: { budget: 400, used: 180 }, rules: { budget: 300, used: 120 },
    profile: { budget: 250, used: 100 }, household: { budget: 250, used: 30 }, episodes: { budget: 150, used: 20 } } },
  rules: [
    ci("r1", "rule", long("Nenne immer zuerst das Ergebnis und danach die Begründung in höchstens zwei Sätzen.")),
    ci("r2", "style", "freundlich, knapp", { weak: true }),
    ci("r3", "style", long("Antworte trocken und knapp."), { example: "Läuft. CPU hoch, kein Defekt." }),
  ],
  profile: Array.from({ length: 4 }, (_, i) => ci(`p${i}`, "person", long(`Wohnt in Hamburg und mag Espresso ohne Milch Nr. ${i}.`, 2), { core: i === 0, in_core: i < 3 })),
  household: [ci("h1", "operational", long("Das Haus hat zwei Etagen und einen Keller mit dem Serverschrank."), { scope: "household" })],
  system: [
    { id: "s1", lang: "en", text: long("Stick to evidence. Say only what you can support.", 6), version: 1 },
    { id: "s2", lang: "de", text: long("Halte dich an Belege. Sage nur, was du belegen kannst.", 6), version: 1 },
  ],
  episodes: [ci("e1", "episode", "Thema: Backups. Entschieden: nachts. Offen: Aufbewahrung.", { core: false })],
  proposals: [
    { id: "pr1", action: "add", kind: "style", text: "Antworte knapp, Ergebnis zuerst.", example: "Läuft. CPU hoch.", core: true, weak: false,
      reason: "the user asked for it in a conversation", evidence: { quote: "antworte bitte immer knapp, das ist mir wichtig" },
      diff: { before: {}, after: {} }, target_id: null, origin: "chat", status: "proposed", created_at: "2026-10-01T10:00:00Z", decided_at: null },
    { id: "pr2", action: "kb_edit", kind: "operational", text: "node-amd: llama-server is stopped", example: null, core: false, weak: false,
      reason: "care: a live check shows something else (E1): the unit is inactive", evidence: { path: "steckbriefe/node-amd.md",
        markdown: "<!-- steckbriefe/node-amd.md -->\nnode-amd: llama-server is stopped, " + long("seit gestern abend", 6) + "\n" },
      diff: { before: {}, after: {} }, target_id: "x", origin: "care", status: "proposed", created_at: "2026-10-01T10:00:00Z", decided_at: null },
    { id: "pr3", action: "change", kind: "rule", text: "Sprich mich mit du an.", example: null, core: true, weak: false,
      reason: "contradicts another core entry: " + long("du or Sie", 8), evidence: { review: true },
      diff: { before: { text: "Sprich mich mit Sie an." }, after: {} }, target_id: "r1", origin: "care", status: "proposed",
      created_at: "2026-10-01T10:00:00Z", decided_at: null },
  ],
  preview: "# Core — applies to every answer, before anything else\n\n## System rules (fixed)\n" + long("Stick to evidence.", 40),
  admin: true,
};
const persona = (id, key, name, scope, extra = {}) => ({
  id, key, name, version: 2, scope, status: "active", components: { role: { de: "Du bist ruhig." }, humor: 1 },
  example: {}, defaults: {}, text: long("Du bist mein Haus-Assistent: aufmerksam, loyal, nie steif.", 6), tokens: 120,
  truncated: false, ref: `${id}@2`, ...extra,
});
const PERSONAS = {
  templates: [persona("t1", "butler", "Butler", "household"), persona("t2", "friend", "Freund", "household"),
    persona("t3", "copilot", "Co-Pilot", "household"), persona("t4", "neutral", "Sachlich", "household")],
  own: [persona("o1", "legacy", "Eigener Text", "user", { truncated: true, components: { free_text: long("Antworte in Reimen.", 5) } })],
  default_id: "t1", max_tokens: 400, wizard: { state: null, show: false, banner: true },
  blocks: { humor: [0, 1, 2], length: ["short", "long"], ask: ["ask_first", "read_first"], address: ["du", "sie"] },
};
const loc = (de, en) => ({ de, en });
const QUESTIONNAIRE = {
  version: 1, roles: ["butler", "friend", "copilot"], none_role: "neutral",
  scenes: ["plex", "network", "everything"].map((id, n) => ({ id, question: loc(`Frage ${n + 1}: Läuft Plex?`, `Question ${n + 1}`),
    answers: ["a", "b", "c"].map((a, k) => ({ id: a, role: ["butler", "friend", "copilot"][k],
      text: loc(long(`Antwort ${a.toUpperCase()}${n + 1}: Ja, es läuft, nur ausgelastet.`, 3), `Answer ${a}${n + 1}`) })) })),
  questions: [["address", "du", ["du", "sie"]], ["humor", "1", ["0", "1", "2"]], ["length", "short", ["short", "long"]],
    ["transparency", "true", ["true", "false"]], ["ask", "ask_first", ["ask_first", "read_first"]], ["emojis", "false", ["false", "true"]]]
    .map(([id, def, vals]) => ({ id, default: def, text: loc(`Kurzfrage ${id}?`, `Question ${id}?`),
      options: vals.map((v) => ({ value: v, label: loc(`Option ${v} mit etwas längerem Text`, `Option ${v}`) })) })),
};
const QPREVIEW = { role: "butler", components: {}, text: long("Du bist mein Haus-Assistent.", 6), tokens: 100,
  samples: [long("Der Backup-Job ist durch. Er hat sich Zeit gelassen.", 3), long("Das weiß ich noch nicht, ich sehe kurz nach.", 3)] };
const conv = (id, title, running = false, hoursAgo = 0, count = 0) => {
  const at = new Date(Date.now() - hoursAgo * 3600_000).toISOString();
  return {
    id, title, running, model: null, node: null, quality: null, tool_scope: null,
    created_at: at, updated_at: at, message_count: count,
  };
};
// a list as in daily use: several days, many titles that start alike
const LIST = [conv("long", "Ein langes Gespräch", false, 0.2, 24), conv("empty", "Neuer Chat", false, 0.5),
  ...Array.from({ length: 14 }, (_, i) =>
    conv(`o${i}`, i % 3 === 0 ? `Gib mir bitte einen kurzen Lagebericht, Nummer ${i + 1}` : `Gespräch Nummer ${i + 1} mit einem langen Titel`,
      i < 2, 1 + i * 9, 2 + i))];
// E3-07 D3: the messages of a finding request — the hub's confirmation, a progress line, the report
// card with long claims, marks, an assumption, a proposal and long evidence, the closing bubble
const FINDING = (() => {
  const base = { ...messages[1], prompt_hash: null, persona_ref: null, memory_refs: [], kind: "say" };
  const at = (n) => new Date(Date.UTC(2026, 9, 2, 9, n, 0)).toISOString();
  const excerpt = Array.from({ length: 60 }, (_, i) => `2026-10-02T09:0${i % 10}:00Z worker ${i}: ` + "x".repeat(120)).join("\n");
  const card = {
    version: 1, task_id: "00000000-0000-4000-8000-0000000000aa", status: "done", incomplete: true,
    incomplete_reason: long("stopped early", 4), calls: 3,
    summary: long("Der Container web wurde wegen Speicher beendet und startet seither neu.", 3),
    findings: ["E1", "E2", "E3", "E4"].map((ref, i) => ({
      claim: long(`Befund ${i + 1}: der Prozess wurde vom Kernel beendet, weil das Speicherlimit erreicht war.`, 2),
      tag: i % 2 ? "doc" : "live", evidence: [ref, "E1", "E2"], ...(i === 1 ? { contradicts: ["E3", "E4"] } : {}) })),
    assumptions: [{ claim: long("Das Limit ist vermutlich zu niedrig.", 3), confirm_with: "docker.inspect web-mit-einem-sehr-langen-container-namen", evidence: [] }],
    proposal: { text: long("Speicherlimit des Containers erhöhen und neu starten.", 3), impact: long("web startet einmal neu.", 4), rollback: "", advice_only: true },
    evidence: ["E1", "E2", "E3", "E4"].map((ref) => ({ ref, tag: "live", node: "vm-docker-mit-langem-namen", capability: "docker.logs",
      input: { container: "web" }, excerpt, truncated: true })),
    notes: [long("a claim tagged live cited no live evidence and became an assumption", 2)],
  };
  return [
    { ...base, id: 900, content: "Schau ich mir an.", created_at: at(1), duration_ms: null, completion_tokens: null },
    { ...base, id: 901, content: "docker.logs · vm-docker — " + long("see why web restarts", 4), kind: "progress", status: "running", created_at: at(2) },
    { ...base, id: 902, content: "card", kind: "report", report: card, agent_task_id: card.task_id, created_at: at(3) },
    { ...base, id: 903, content: "Fertig: web braucht mehr Speicher.", agent_task_id: card.task_id, created_at: at(4) },
  ];
})();
// The desktop (D70) asks the hub which programs and views the person has before it draws anything.
const AGENT = { tool_scope: { nodes: null, groups: null, internet: null }, flow: null, persona: null, model_role: null };
const program = (key, title, modules, position) => ({
  key, title, title_key: null, icon: "", position, modules, agent: AGENT, requires_role: null, builtin: true, rev: 1,
});
const BOOTSTRAP = {
  v: 1,
  access: { admin: true, profile: null, denied_tools: [] },
  prefs: { hue: 140, look: "terminal", desktop: { open: ["chat"], minimised: [], focus: "chat", maximised: null, views: {} } },
  modules: ["page.chat", "page.chat.dev", "page.memory", "page.library"].map((id) => ({
    id, kind: "page", admin: false, needs: null, allowed: true,
  })),
  workspaces: [
    program("chat", "Chat", ["page.chat"], 10),
    program("dev", "Development", ["page.chat.dev"], 20),
    program("memory", "Memory", ["page.memory"], 30),
  ],
};

export const JSON_ROUTES = {
  "/api/ui/bootstrap": BOOTSTRAP,
  "/api/ui/workspaces/chat/agent": AGENT,
  "/api/ui/workspaces/dev/agent": AGENT,
  "/api/ui/workspaces/memory/agent": AGENT,
  "/api/inbox/counts": { unread: 0, urgent: 0, applications: {} },
  "/api/me": { id: "u", name: "Alex", role: "admin", language: "de" },
  "/api/nodes": [],
  "/api/memory/questions": [],
  "/api/memory": MEMORY,
  "/api/core": CORE,
  "/api/core/proposals": CORE.proposals,
  "/api/personas": PERSONAS,
  "/api/personas/stats": [{ persona_id: "t1", name: "Butler", key: "butler", answers: 20, helped: 12, unfit: 2 }],
  "/api/personas/questionnaire": QUESTIONNAIRE,
  "/api/personas/questionnaire/preview": QPREVIEW,
  "/api/conversations": LIST,
  "/api/conversations/long": { ...LIST[0], persona_ref: "t2", messages: [...messages.map((m, i) =>
    m.role === "assistant" ? { ...m, prompt_hash: i < 20 ? HASH1 : HASH2, persona_ref: i < 20 ? "t1@2" : "t2@2" } : m),
    ...FINDING] },
  "/api/conversations/empty": { ...LIST[1], messages: [] },
  "/api/chat/models": [{ id: "qwen-7b", topcoat: { nodes: [{ node: "node-amd", loaded: true, state: "available" }] } }],
  "/api/chat/settings": {
    user: { system_prompt: null, temperature: null, default_model: null, address: "du", memory_enabled: true,
      escalate_without_asking: false, quality: "balanced", internet: true },
    hub: { default_model: null, fallback_model: "qwen-7b", quality_models: {} },
    effective_model: "qwen-7b",
  },
  "/api/chat/tools": {
    groups: [{ key: "docker", label: "Docker" }, { key: "network", label: "Omada" }, { key: "notes", label: "Notizen" }],
    nodes: [{ name: "tools-a", groups: ["docker"] }, { name: "tools-b", groups: ["network", "notes"] }],
    internet: { node: "search-node" },
  },
};

/** Serves `dist` on a free loopback port; the server is returned (close it when done). */
export async function serve(dist) {
  const server = createServer(async (req, res) => {
    const path = new URL(req.url, "http://x").pathname;
    const file = path === "/" ? "index.html" : normalize(path).replace(/^(\.\.[/\\])+/, "");
    try {
      const body = await readFile(join(dist, file));
      res.writeHead(200, { ...SECURITY_HEADERS, "content-type": MIME[extname(file)] ?? "application/octet-stream" });
      res.end(body);
    } catch {
      res.writeHead(404, SECURITY_HEADERS).end();
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return server;
}
