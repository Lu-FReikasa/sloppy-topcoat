import type {
  CoreItem,
  CoreOverview,
  CoreProposal,
  Persona,
  PersonaLibrary,
  Questionnaire,
} from "../types";

export function coreItem(o: Partial<CoreItem> & { id: string; text: string }): CoreItem {
  return {
    status: "active",
    evidence: "doc",
    confidence: 1,
    source_type: "user",
    source_ref: {},
    version: 1,
    confirmed_count: 0,
    last_confirmed_at: null,
    disputed_with: null,
    superseded_by: null,
    embedder: null,
    created_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-10-01T10:00:00Z",
    kind: "rule",
    core: true,
    scope: "user",
    in_core: true,
    ...o,
  };
}

export function proposal(o: Partial<CoreProposal> & { id: string }): CoreProposal {
  return {
    action: "add",
    kind: "rule",
    text: "Antworte immer knapp.",
    example: null,
    core: true,
    weak: false,
    reason: "the user asked for it in a conversation",
    evidence: { quote: "antworte bitte immer knapp" },
    diff: { before: {}, after: { text: "Antworte immer knapp." } },
    target_id: null,
    origin: "chat",
    status: "proposed",
    created_at: "2026-10-01T10:00:00Z",
    decided_at: null,
    ...o,
  };
}

export function core(o: Partial<CoreOverview> = {}): CoreOverview {
  return {
    budget: {
      total: 1000,
      used: 420,
      context: 16384,
      parts: {
        system: { budget: 200, used: 190 },
        persona: { budget: 120, used: 120 },
        rules: { budget: 300, used: 60 },
        profile: { budget: 250, used: 30 },
        household: { budget: 250, used: 10 },
        recent: { budget: 150, used: 0 },
      },
      cut: [],
      examples_dropped: false,
    },
    rules: [coreItem({ id: "r1", text: "Nenne immer zuerst das Ergebnis." })],
    profile: [coreItem({ id: "p1", kind: "person", text: "Wohnt in Hamburg", core: false })],
    household: [
      coreItem({ id: "h1", kind: "operational", scope: "household", text: "Zwei Etagen" }),
    ],
    system: [
      { id: "s1", lang: "en", text: "Stick to evidence.", version: 1 },
      { id: "s2", lang: "de", text: "Halte dich an Belege.", version: 1 },
    ],
    recent: [],
    proposals: [],
    preview:
      "# Core — applies to every answer, before anything else\n\n## System rules (fixed)\nStick to evidence.",
    admin: false,
    ...o,
  };
}

export function persona(o: Partial<Persona> & { id: string; name: string }): Persona {
  return {
    key: null,
    version: 1,
    scope: "user",
    status: "active",
    components: { role: { de: "Du bist ruhig." }, humor: 1 },
    example: {},
    defaults: {},
    text: "Du bist ruhig. Antworte kurz.",
    tokens: 20,
    truncated: false,
    ref: `${o.id}@1`,
    ...o,
  };
}

export const BUTLER = persona({
  id: "t-butler",
  key: "butler",
  name: "Butler",
  scope: "household",
  text: "Du bist mein Haus-Assistent.",
});
export const NEUTRAL = persona({
  id: "t-neutral",
  key: "neutral",
  name: "Sachlich",
  scope: "household",
  text: "Du bist ein sachlicher Assistent.",
});

export function library(o: Partial<PersonaLibrary> = {}): PersonaLibrary {
  return {
    templates: [BUTLER, NEUTRAL],
    own: [],
    default_id: null,
    max_tokens: 400,
    wizard: { state: null, show: false, banner: false },
    blocks: {
      humor: [0, 1, 2],
      length: ["short", "long"],
      ask: ["ask_first", "read_first"],
      address: ["du", "sie"],
    },
    ...o,
  };
}

const loc = (de: string, en: string) => ({ de, en });

export const QUESTIONNAIRE: Questionnaire = {
  version: 1,
  roles: ["butler", "friend", "copilot"],
  none_role: "neutral",
  scenes: ["plex", "network", "everything"].map((id, n) => ({
    id,
    question: loc(`Frage ${n + 1}?`, `Question ${n + 1}?`),
    answers: [
      { id: "a", role: "butler", text: loc(`Antwort A${n + 1}`, `Answer A${n + 1}`) },
      { id: "b", role: "friend", text: loc(`Antwort B${n + 1}`, `Answer B${n + 1}`) },
      { id: "c", role: "copilot", text: loc(`Antwort C${n + 1}`, `Answer C${n + 1}`) },
    ],
  })),
  questions: [
    {
      id: "address",
      default: "du",
      text: loc("Anrede?", "Address?"),
      options: [
        { value: "du", label: loc("du", "informal") },
        { value: "sie", label: loc("Sie", "formal") },
      ],
    },
    {
      id: "humor",
      default: "1",
      text: loc("Humor?", "Humor?"),
      options: [
        { value: "0", label: loc("kein", "none") },
        { value: "1", label: loc("trocken", "dry") },
      ],
    },
  ],
};
