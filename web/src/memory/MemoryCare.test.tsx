import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { MemoryUsed } from "../chat/MemoryNotes";
import { jsonResponse, mockFetch } from "../test/mockFetch";
import type {
  CareChange,
  MemoryItem,
  MemoryOverview,
  MemoryQuestion,
  MemoryRun,
} from "../types";
import { MemoryPage } from "./MemoryPage";
import { QuestionCards } from "./QuestionCard";

afterEach(() => {
  vi.restoreAllMocks();
});

function note(
  overrides: Partial<MemoryItem> & { id: string; text: string },
): MemoryItem {
  return {
    status: "active",
    evidence: "doc",
    confidence: 0.9,
    source_type: "chat",
    source_ref: {},
    version: 1,
    confirmed_count: 0,
    last_confirmed_at: null,
    disputed_with: null,
    superseded_by: null,
    embedder: "hashing-v1-384",
    created_at: "2026-09-01T10:00:00Z",
    updated_at: "2026-09-01T10:00:00Z",
    kind: "person",
    stage: "sure",
    reliability: 0.9,
    usefulness: 0.5,
    last_verified_at: null,
    verified_count: 0,
    expires_at: null,
    verify_with: null,
    recalled_count: 0,
    used_count: 0,
    last_used_at: null,
    feedback_positive: 0,
    feedback_negative: 0,
    halflife_days: 365,
    ...overrides,
  };
}

const PERSON = note({
  id: "p",
  text: "Wohnt in Hamburg",
  stage: "sure",
  confirmed_count: 2,
});
const INFRA = note({
  id: "i",
  text: "Der lil switch ist absichtlich aus",
  kind: "operational",
  stage: "stale",
  verified_count: 1,
  last_verified_at: "2026-09-20T10:00:00Z",
  used_count: 3,
  recalled_count: 4,
  usefulness: 0.75,
  verify_with: { capability: "net.check", input: { host: "lil-switch" } },
});
const SHELVED = note({
  id: "a",
  text: "Mag Jazz am Montag",
  status: "archived",
  stage: "stale",
});

const CARE_SETTINGS = {
  hour: 3,
  interval_seconds: 900,
  halflife_days: { person: 365, preference: 180, operational: 14, volatile: 1 },
  volatile_ttl_hours: 24,
  check_threshold: 0.5,
  archive: { usefulness: 0.3, reliability: 0.25, idle_days: 90 },
};

const CHANGES: CareChange[] = [
  {
    action: "changed",
    item_id: "i",
    text: "Der Container web läuft auf Port 8080",
    kind: "operational",
    version: 3,
    reason: "care: a live check shows a different state (E1): port is 8081",
    undo: "revert",
    from_version: 2,
    new_text: "Der Container web läuft auf Port 8081",
    evidence: ["E1"],
  },
  {
    action: "expired",
    item_id: "x",
    text: "Dienst X ist down",
    kind: "operational",
    version: 2,
    reason:
      "care: expired on 2026-09-30 10:00 UTC (a volatile state is kept for a day at most)",
    undo: "restore",
  },
  {
    action: "verified",
    item_id: "v",
    text: "Docker läuft auf node-amd",
    kind: "operational",
    version: null,
    reason: "care: a live check confirms it (E1)",
    undo: null,
  },
];

function run(overrides: Partial<MemoryRun> = {}): MemoryRun {
  return {
    id: "r1",
    kind: "care",
    trigger: "nightly",
    status: "ok",
    started_at: "2026-10-01T01:00:00Z",
    finished_at: "2026-10-01T01:05:00Z",
    stats: {
      checked: 3,
      unchanged: 1,
      changed: 1,
      asked: 0,
      expired: 1,
      archived: 0,
      unverifiable: 0,
      changes: CHANGES,
    },
    ...overrides,
  };
}

function overview(
  items: MemoryItem[],
  extra: Partial<MemoryOverview> = {},
): MemoryOverview {
  const counts = {
    active: 0,
    disputed: 0,
    superseded: 0,
    deleted: 0,
    archived: 0,
  };
  for (const i of items) counts[i.status] += 1;
  return {
    items,
    counts,
    blocked: 0,
    enabled: true,
    embedder: { id: "bge-m3", transitional: false },
    running: false,
    runs: [],
    questions: [],
    kinds: { person: 1, preference: 0, operational: 1 },
    care: { running: false, runs: [run()], settings: CARE_SETTINGS },
    ...extra,
  };
}

function setupIn(
  routes: Record<string, unknown>,
  mode: "user" | "care" = "user",
  application?: string,
) {
  const mock = mockFetch(routes);
  render(
    <MemoryPage
      api={new ApiClient({ token: "t", fetchImpl: mock.fetch })}
      mode={mode}
      application={application}
    />,
  );
  return mock;
}

const setup = (routes: Record<string, unknown>) => setupIn(routes);
// the machine note is Admin's own; the person note is the core, which every program reads
const ADMIN_NOTE = { ...INFRA, application: "admin" };
const CORE = overview([PERSON], { applications: { core: 1, admin: 1 } });
const ADMIN = overview([ADMIN_NOTE], { applications: { core: 1, admin: 1 } });
const CORE_PAGE = "GET /api/memory?application=core";
const ADMIN_PAGE = "GET /api/memory?application=admin";

describe("MemoryPage tabs (E3-05)", () => {
  it("shows the core in Settings, with its archive and the tags, and no program's memory", async () => {
    const mock = setup({ [CORE_PAGE]: CORE, [ADMIN_PAGE]: ADMIN });
    const cards = await screen.findAllByTestId("memory-item");
    expect(cards).toHaveLength(1);
    expect(cards[0]?.textContent).toContain("Wohnt in Hamburg");
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent?.replace(/\s+/g, " ").trim())).toEqual(
      ["Über dich (Kern) (1)", "Archiv (0)", "Tags"],
    );
    expect(tabs[0]?.getAttribute("aria-selected")).toBe("true");
    expect(
      mock.calls.some((c) => c.url === "/api/memory?application=admin"),
    ).toBe(false);
  });

  it("shows a program its own memory only, and where the core is", async () => {
    setupIn({ [CORE_PAGE]: CORE, [ADMIN_PAGE]: ADMIN }, "user", "admin");
    const cards = await screen.findAllByTestId("memory-item");
    expect(cards).toHaveLength(1);
    expect(cards[0]?.textContent).toContain(
      "Der lil switch ist absichtlich aus",
    );
    expect(screen.queryByText("Wohnt in Hamburg")).toBeNull();
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent?.replace(/\s+/g, " ").trim())).toEqual(
      ["Admin weiß (1)", "Archiv (0)"],
    );
    expect(screen.getByTestId("core-hint").textContent).toContain(
      "Einstellungen",
    );
  });

  it("asks the hub for the archive and lets a note be restored from it", async () => {
    const mock = setup({
      [CORE_PAGE]: CORE,
      "GET /api/memory?status=archived&application=core": overview([SHELVED], {
        kinds: { person: 0, preference: 0, operational: 0 },
      }),
      "POST /api/memory/a/restore": note({ id: "a", text: SHELVED.text }),
    });
    await screen.findAllByTestId("memory-item");
    fireEvent.click(screen.getByRole("tab", { name: /Archiv/ }));
    const card = await screen.findByTestId("memory-item");
    expect(card.textContent).toContain("Mag Jazz am Montag");
    expect(card.textContent).toContain("Archiviert");
    // the status filter does not apply to the archive
    expect(screen.queryByLabelText("Status")).toBeNull();
    fireEvent.click(
      within(card).getByRole("button", { name: "Wiederherstellen" }),
    );
    await waitFor(() =>
      expect(
        mock.calls.some(
          (c) => c.method === "POST" && c.url === "/api/memory/a/restore",
        ),
      ).toBe(true),
    );
  });

  it("tells an empty archive and an empty program apart from 'no match'", async () => {
    setupIn(
      {
        [ADMIN_PAGE]: overview([]),
        "GET /api/memory?status=archived&application=admin": overview([]),
      },
      "user",
      "admin",
    );
    expect(await screen.findByText(/Hier ist noch nichts/)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /Archiv/ }));
    expect(await screen.findByText("Nichts im Archiv.")).toBeTruthy();
  });
});

describe("per-note care facts (E3-05)", () => {
  it("shows stage, last confirmation, confirmations, usefulness and what it is checked with", async () => {
    setup({ [CORE_PAGE]: CORE });
    await screen.findAllByTestId("memory-item");
    const person = screen.getByTestId("note-care");
    expect(within(person).getByTestId("note-stage").textContent).toBe("sicher");
    expect(within(person).getByTestId("note-confirmations").textContent).toBe(
      "2 Bestätigungen",
    );
    expect(within(person).getByTestId("note-usefulness").textContent).toContain(
      "Nutzen 50 %",
    );

    cleanup();
    setupIn({ [ADMIN_PAGE]: ADMIN }, "user", "admin");
    await screen.findByText("Der lil switch ist absichtlich aus");
    const infra = screen.getByTestId("note-care");
    expect(within(infra).getByTestId("note-stage").textContent).toBe(
      "veraltet, bitte prüfen",
    );
    // a live check counts: 0 statements + 1 check = 1, named as such
    expect(within(infra).getByTestId("note-confirmations").textContent).toBe(
      "1 Bestätigungen (1 per Live-Prüfung)",
    );
    expect(within(infra).getByTestId("note-usefulness").textContent).toBe(
      "Nutzen 75 % (3 von 4 Malen benutzt)",
    );
    expect(infra.textContent).toContain("zuletzt bestätigt");
    expect(infra.textContent).toContain("prüfbar mit net.check");
    // the numbers behind the stage are not shown
    expect(infra.textContent).not.toContain("0.9");
  });

  it("says plainly that a note was never confirmed again", async () => {
    setup({
      [CORE_PAGE]: overview([note({ id: "n", text: "Mag Kaffee" })]),
    });
    await screen.findByTestId("memory-item");
    expect(screen.getByTestId("note-care").textContent).toContain(
      "seit dem Anlegen nicht erneut bestätigt",
    );
  });

  it("archives an active note by hand", async () => {
    const mock = setup({
      [CORE_PAGE]: CORE,
      "POST /api/memory/p/archive": note({
        id: "p",
        text: "x",
        status: "archived",
      }),
    });
    const card = await screen.findByTestId("memory-item");
    fireEvent.click(within(card).getByRole("button", { name: "Archivieren" }));
    await waitFor(() =>
      expect(
        mock.calls.some(
          (c) => c.method === "POST" && c.url === "/api/memory/p/archive",
        ),
      ).toBe(true),
    );
  });
});

describe("care history (E3-05)", () => {
  // the runs live in the admin area: the same page in its care form
  const setup = (routes: Record<string, unknown>) => setupIn(routes, "care");

  it("lists the runs with a short report and, per change, the reason and its undo", async () => {
    const mock = setup({
      "GET /api/memory": CORE,
      "POST /api/memory/i/revert": note({ id: "i", text: "x" }),
      "POST /api/memory/x/restore": note({ id: "x", text: "y" }),
    });
    const section = await screen.findByTestId("care-section");
    const row = within(section).getByTestId("care-run");
    expect(within(row).getByTestId("care-summary").textContent).toContain(
      "geprüft 3, bestätigt 1, korrigiert 1, nachgefragt 0, gestrichen 1, archiviert 0",
    );
    expect(row.textContent).toContain("nachts");
    const changes = within(row).getAllByTestId("care-change");
    expect(changes).toHaveLength(3);
    expect(changes[0]?.textContent).toContain("Korrigiert");
    expect(changes[0]?.textContent).toContain(
      "Der Container web läuft auf Port 8081",
    );
    expect(changes[0]?.textContent).toContain(
      "war: Der Container web läuft auf Port 8080",
    );
    expect(changes[0]?.textContent).toContain("port is 8081");
    expect(changes[1]?.textContent).toContain("Gestrichen (abgelaufen)");
    expect(changes[2]?.textContent).toContain("Per Live-Prüfung bestätigt");
    // a confirmation changed no content: nothing to undo
    expect(within(changes[2] as HTMLElement).queryByRole("button")).toBeNull();

    fireEvent.click(
      within(changes[0] as HTMLElement).getByRole("button", {
        name: "Korrektur rückgängig",
      }),
    );
    await waitFor(() => {
      const call = mock.calls.find(
        (c) => c.method === "POST" && c.url === "/api/memory/i/revert",
      );
      expect(call?.body).toEqual({ version: 2 });
    });
    expect(
      await within(changes[0] as HTMLElement).findByText("Rückgängig gemacht."),
    ).toBeTruthy();

    fireEvent.click(
      within(changes[1] as HTMLElement).getByRole("button", {
        name: "Zurückholen",
      }),
    );
    await waitFor(() =>
      expect(
        mock.calls.some(
          (c) => c.method === "POST" && c.url === "/api/memory/x/restore",
        ),
      ).toBe(true),
    );
  });

  it("starts a run and explains a refusal", async () => {
    const mock = setup({
      "GET /api/memory": CORE,
      "POST /api/memory/care": () =>
        jsonResponse(
          { detail: { code: "no_node_available", message: "none" } },
          409,
        ),
    });
    const section = await screen.findByTestId("care-section");
    fireEvent.click(
      within(section).getByRole("button", { name: "Pflegelauf jetzt starten" }),
    );
    expect(await screen.findByText(/Kein Node ist verfügbar/)).toBeTruthy();
    expect(
      mock.calls.some(
        (c) => c.method === "POST" && c.url === "/api/memory/care",
      ),
    ).toBe(true);
  });

  it("starts the chat digest on request", async () => {
    const mock = setup({
      "GET /api/memory": CORE,
      "POST /api/memory/digest": () => jsonResponse({ started: 3 }, 202),
    });
    const section = await screen.findByTestId("care-section");
    fireEvent.click(
      within(section).getByRole("button", {
        name: "Chats jetzt zusammenfassen",
      }),
    );
    await waitFor(() =>
      expect(
        mock.calls.some(
          (c) => c.method === "POST" && c.url === "/api/memory/digest",
        ),
      ).toBe(true),
    );
  });

  it("shows a running care run and the numbers it works with, read-only", async () => {
    setup({
      "GET /api/memory": overview([PERSON], {
        care: { running: true, runs: [], settings: CARE_SETTINGS },
      }),
    });
    const section = await screen.findByTestId("care-section");
    expect(
      within(section).getByRole("button", { name: "Pflegelauf läuft …" }),
    ).toHaveProperty("disabled", true);
    expect(within(section).getByTestId("care-empty").textContent).toBe(
      "Noch kein Pflegelauf.",
    );
    const numbers = within(section).getByTestId("care-settings");
    expect(numbers.textContent).toContain("03:00 Uhr");
    expect(numbers.textContent).toContain(
      "über dich 365 T, Vorlieben 180 T, deine Systeme 14 T",
    );
    expect(numbers.textContent).toContain("Alter allein archiviert nie");
    expect(within(numbers).queryByRole("textbox")).toBeNull();
  });

  it("does not show a badge for a run that decided everything itself", async () => {
    // the badge in the navigation counts open questions only; a care run with changes opens none
    const mock = setup({
      "GET /api/memory": CORE,
      "GET /api/memory/questions": [],
    });
    await screen.findByTestId("care-section");
    expect(screen.queryByTestId("question-cards")).toBeNull();
    expect(
      mock.calls.filter((c) => c.url === "/api/memory/questions"),
    ).toHaveLength(0);
  });
});

const CARE_QUESTION: MemoryQuestion = {
  id: "q9",
  topic: "care",
  origin: "care",
  status: "open",
  candidates: [
    {
      id: "i",
      text: "Der Container web läuft auf Port 8080",
      at: "2026-09-01T10:00:00Z",
      confirmed_count: 0,
      last_confirmed_at: null,
    },
  ],
  observed: "Der Container web läuft auf Port 8081",
  resolution: null,
  created_at: "2026-10-01T01:00:00Z",
  answered_at: null,
};

describe("clarification question from a care check (E3-05)", () => {
  it("shows what the check saw next to what the user said, and answers like any question", async () => {
    const mock = mockFetch({
      "POST /api/memory/questions/q9/answer": { question_id: "q9" },
    });
    const onDone = vi.fn();
    render(
      <QuestionCards
        api={new ApiClient({ token: "t", fetchImpl: mock.fetch })}
        questions={[CARE_QUESTION]}
        onDone={onDone}
      />,
    );
    const card = screen.getByTestId("question-card");
    expect(within(card).getByRole("heading").textContent).toBe("Notiz prüfen");
    expect(card.textContent).toContain(
      "Eine Prüfung meiner Systeme zeigt etwas anderes",
    );
    expect(within(card).getByTestId("care-observed").textContent).toContain(
      "Port 8081",
    );
    expect(card.textContent).toContain(
      "„Der Container web läuft auf Port 8080“",
    );
    fireEvent.click(
      within(card).getByRole("button", {
        name: "„Der Container web läuft auf Port 8080“ stimmt",
      }),
    );
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(mock.calls[0]?.body).toEqual({ note_id: "i" });
  });
});

describe("feedback under 'n notes used' (E3-05)", () => {
  const REFS = [
    {
      id: "n1",
      text: "Wohnt in Hamburg",
      evidence: "doc" as const,
      status: "active" as const,
    },
    {
      id: "n2",
      text: "Hat einen Hund",
      evidence: "doc" as const,
      status: "active" as const,
    },
  ];

  it("is folded away until opened, then offers helped / did not fit per note", async () => {
    const mock = mockFetch({
      "POST /api/memory/n1/feedback": { id: "n1" },
      "POST /api/memory/n2/feedback": { id: "n2" },
    });
    render(
      <MemoryUsed
        refs={REFS}
        api={new ApiClient({ token: "t", fetchImpl: mock.fetch })}
      />,
    );
    const details = screen.getByTestId("memory-used") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(screen.getByText("2 Notizen verwendet")).toBeTruthy();
    const rows = screen.getAllByTestId("memory-feedback");
    expect(rows).toHaveLength(2);

    fireEvent.click(
      within(rows[0] as HTMLElement).getByRole("button", {
        name: "Diese Notiz hat geholfen",
      }),
    );
    await waitFor(() => expect(mock.calls[0]?.body).toEqual({ helped: true }));
    expect(
      await within(rows[0] as HTMLElement).findByText(
        "Danke – als hilfreich vermerkt.",
      ),
    ).toBeTruthy();
    // the buttons of that note are gone (one word per note and session), the other's remain
    expect(within(rows[0] as HTMLElement).queryByRole("button")).toBeNull();

    fireEvent.click(
      within(rows[1] as HTMLElement).getByRole("button", {
        name: "Diese Notiz war unpassend",
      }),
    );
    await waitFor(() => expect(mock.calls[1]?.body).toEqual({ helped: false }));
    expect(
      await within(rows[1] as HTMLElement).findByText(
        "Danke – als unpassend vermerkt.",
      ),
    ).toBeTruthy();
  });

  it("offers no feedback without a client (a streaming answer is not stored yet)", () => {
    render(<MemoryUsed refs={REFS} />);
    expect(screen.queryByTestId("memory-feedback")).toBeNull();
  });

  it("says when the word could not be saved and keeps the buttons", async () => {
    const mock = mockFetch({
      "POST /api/memory/n1/feedback": () =>
        jsonResponse({ detail: { code: "x", message: "no" } }, 500),
    });
    render(
      <MemoryUsed
        refs={[REFS[0]!]}
        api={new ApiClient({ token: "t", fetchImpl: mock.fetch })}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Diese Notiz hat geholfen" }),
    );
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Das ließ sich nicht speichern.",
    );
    expect(
      screen.getByRole("button", { name: "Diese Notiz war unpassend" }),
    ).toBeTruthy();
  });
});
