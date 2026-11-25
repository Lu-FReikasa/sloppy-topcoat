import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { ShelfPage } from "../library/ShelfPage";
import { mockFetch } from "../test/mockFetch";
import { ProgramSettings } from "./ProgramSettings";

describe("ProgramSettings", () => {
  it("shows the contract of the program's employee in words", async () => {
    const mock = mockFetch({
      "GET /api/ui/workspaces/dev/agent": {
        tool_scope: { nodes: null, groups: ["workshop"], internet: false },
        contract: {
          key: "dev",
          title: "Entwicklung",
          purpose: "Code, Tests und Branches.",
          tasks: [{ text: "Code erklären", example: "Wo lädt der Hub ein Modell?" }],
          tools: ["das Repository in der Werkstatt lesen"],
          never: ["auf main pushen"],
          needs_click: [],
          talks_to: [{ to: "Verwaltung", for: "Fragen zum laufenden System" }],
        },
      },
      "GET /api/chat/tools?application=dev": { groups: [], nodes: [], internet: { node: null } },
      "GET /api/memory/tags": { tags: [], programs: [], responsible: [], max_depth: 4 },
    });
    render(<ProgramSettings api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} workspace="dev" />);
    const card = await screen.findByTestId("program-contract");
    expect(card.textContent).toContain("Wofür Entwicklung da ist");
    expect(card.textContent).toContain("Code, Tests und Branches.");
    expect(card.textContent).toContain("„Wo lädt der Hub ein Modell?“");
    expect(card.textContent).toContain("auf main pushen");
    expect(card.textContent).toContain("Verwaltung: Fragen zum laufenden System");
    expect(card.textContent).not.toContain("Was deinen Klick braucht"); // an empty part is left out
  });

  it("shows the frame, what the program may use and the sensitive branches it may see", async () => {
    const health = {
      id: "h",
      parent_id: "p",
      name: "health",
      description: "",
      responsible: null,
      sensitive: true,
      released: ["research"],
      effective: { responsible: "person", sensitive: true, released: ["research"] },
      created_by: "seed",
      reviewed: true,
      root: false,
      notes: 0,
    };
    const mock = mockFetch({
      "GET /api/ui/workspaces/research/agent": {
        tool_scope: { nodes: null, groups: ["notes"], internet: true },
        frame: { tone: "Thorough and neutral.", rules: ["Name the sources."] },
      },
      "GET /api/chat/tools?application=research": {
        groups: [{ key: "notes", label: "the user's notes", via: "hub" }],
        nodes: [],
        internet: { node: "web-node" },
      },
      "GET /api/memory/tags": { tags: [health], programs: [], responsible: [], max_depth: 4 },
    });
    render(<ProgramSettings api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} workspace="research" />);
    const tools = await screen.findByTestId("program-tools");
    expect(tools.textContent).toContain("Wissensdatenbank");
    expect(tools.textContent).toContain("Das Web");
    const frame = screen.getByTestId("program-frame");
    expect(frame.textContent).toContain("Thorough and neutral.");
    expect(frame.textContent).toContain("Name the sources.");
    const box = (await screen.findByRole("checkbox")) as HTMLInputElement;
    expect(box.checked).toBe(true); // released for this program
  });

  it("lists what the program may use even when no machine offers it now", async () => {
    const mock = mockFetch({
      "GET /api/ui/workspaces/research/agent": {
        tool_scope: { nodes: null, groups: ["notes"], internet: true },
      },
      "GET /api/chat/tools?application=research": { groups: [], nodes: [], internet: { node: null } },
    });
    render(<ProgramSettings api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} workspace="research" />);
    const tools = await screen.findByTestId("program-tools");
    await waitFor(() => expect(tools.textContent).toContain("gerade bietet es keine verbundene Maschine an"));
    expect(tools.textContent).toContain("Wissensdatenbank");
    expect(tools.textContent).not.toContain("Keine Werkzeuge");
  });

  it("says when a program has no tools at all", async () => {
    const mock = mockFetch({
      "GET /api/ui/workspaces/inbox/agent": {
        tool_scope: { nodes: null, groups: [], internet: false },
      },
      "GET /api/chat/tools?application=inbox": { groups: [], nodes: [], internet: { node: null } },
    });
    render(<ProgramSettings api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} workspace="inbox" />);
    expect(await screen.findByText(/Keine Werkzeuge/)).toBeTruthy();
  });
});

describe("ShelfPage", () => {
  it("holds the documents only: the conversations are in the archive", async () => {
    const mock = mockFetch({
      "GET /api/artifacts": { artifacts: [], unread: 0 },
      "GET /api/reports": { reports: [] },
    });
    render(<ShelfPage api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} application="dev" />);
    await screen.findByText(/Noch keine Dokumente/);
    expect(screen.queryByRole("button", { name: "Chats" })).toBeNull();
    expect(mock.calls.some((c) => c.url.startsWith("/api/conversations"))).toBe(false);
  });
});
