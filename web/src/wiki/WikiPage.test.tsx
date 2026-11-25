import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiClient } from "../api/client";
import { mockFetch } from "../test/mockFetch";
import type { WikiPage as Page, WikiPageDetail } from "../types";
import { linkCitations, parseDoc } from "./doc";
import { WikiPage } from "./WikiPage";

const BODY = [
  "# B12 Projekte: Läufe",
  "",
  "_Erzeugt aus dem Code des Hub-Builds `abc123` am 2026-11-12; jeder Absatz ist geprüft._",
  "",
  "## Zweck",
  "",
  "Die Datei `project_runs.py` führt Aufgaben aus. [E3]",
  "",
  "## Ablauf",
  "",
  "Neuer Absatz. [E7]",
  "",
  "## Belege",
  "",
  "- **E3** `hub/src/topcoat_hub/project_runs.py` Z. 1–40",
  "- **E7** `hub/src/topcoat_hub/project_runs.py` Z. 41–90",
  "",
  "## Nicht belegt, entfernt",
  "",
  "- Eine Behauptung - kein Beleg",
].join("\n");

const PAGE: Page = {
  id: "p1",
  section: "B Hub",
  title: "B12 Projekte: Läufe",
  path: "B Hub/B12 Projekte: Läufe",
  current_version: 2,
  source_node_id: "n1",
  created_at: null,
  updated_at: null,
};

const v = (n: number, body: string) => ({
  version: n,
  parent_version: n > 1 ? n - 1 : null,
  kind: "agent" as const,
  note: "",
  content_hash: "h",
  meta: { build: "abc123", date: "2026-11-12" } as never,
  created_at: "2026-11-12T09:00:00Z",
  body,
});

const DETAIL: WikiPageDetail = {
  ...PAGE,
  shown: v(2, BODY),
  versions: [v(1, ""), v(2, "")],
};

function show(routes: Record<string, unknown>, id?: string) {
  const mock = mockFetch({
    "GET /api/wiki/pages": { sections: [{ name: "B Hub", pages: [PAGE] }] },
    "GET /api/wiki/pages/p1": DETAIL,
    ...routes,
  });
  render(<WikiPage api={new ApiClient({ token: "t", fetchImpl: mock.fetch })} id={id} />);
  return mock;
}

describe("the wiki document", () => {
  it("takes the title, the note, the sources and the removed statements apart", () => {
    const doc = parseDoc(BODY);
    expect(doc.title).toBe("B12 Projekte: Läufe");
    expect(doc.made).toContain("Hub-Builds");
    expect(doc.body.startsWith("## Zweck")).toBe(true);
    expect(doc.body).not.toContain("Belege");
    expect(doc.headings.map((h) => h.id)).toEqual(["zweck", "ablauf"]);
    expect(doc.sources.map((s) => s.ref)).toEqual(["E3", "E7"]);
    expect(doc.removed).toEqual(["Eine Behauptung - kein Beleg"]);
    expect(linkCitations("x [E3] y [E10](#a)")).toBe("x [E3](#cite-E3) y [E10](#a)");
  });

  it("keeps a page of another shape whole", () => {
    const doc = parseDoc("Some text\n\n## A\n\nmore");
    expect(doc.title).toBeNull();
    expect(doc.body).toBe("Some text\n\n## A\n\nmore");
  });
});

describe("WikiPage", () => {
  it("shows the tree by section with the open page marked, and no release status", async () => {
    show({}, "p1");
    const tree = await screen.findByTestId("wiki-tree");
    await waitFor(() => expect(tree.textContent).toContain("B Hub"));
    const link = within(tree).getByRole("link", { name: "B12 Projekte: Läufe" });
    expect(link.getAttribute("href")).toBe("#/wiki/p1");
    expect(link.getAttribute("aria-current")).toBe("page");
    expect(screen.queryByText(/Freigegeben|Freigeben/)).toBeNull();
  });

  it("says that the wiki is empty", async () => {
    show({ "GET /api/wiki/pages": { sections: [] } });
    expect(await screen.findByText(/noch keine Seiten/)).toBeTruthy();
  });

  it("shows the page as a document: one title, its origin, citations that lead to the sources", async () => {
    show({}, "p1");
    const open = await screen.findByTestId("wiki-open");
    await waitFor(() => expect(open.textContent).toContain("Neuer Absatz"));
    // the title once (the header), the section only in the tree
    expect(within(open).getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(open.textContent).not.toContain("B Hub");
    expect(open.textContent).not.toContain("_Erzeugt");
    expect(screen.getByTestId("wiki-status").textContent).toMatch(/Version 2.*Build abc123/);
    const cite = within(open).getByRole("link", { name: "E3" });
    expect(cite.closest("sup")).not.toBeNull();
    expect(cite.getAttribute("href")).toBe("#cite-E3");
    expect(open.querySelector("#cite-E3")?.textContent).toContain("project_runs.py");
    expect(open.querySelector("h2#zweck")).not.toBeNull();
    expect(open.textContent).toContain("Ohne Beleg entfernt (1)");
  });

  it("shows an older version with a way back, and the changes between two versions", async () => {
    show(
      {
        "GET /api/wiki/pages/p1?version=1": { ...DETAIL, shown: v(1, "# B12 Projekte: Läufe\n\nAlt.") },
        "GET /api/wiki/pages/p1/diff?old=1&new=2": { from: 1, to: 2, added: 1, removed: 1, diff: "-Alt.\n+Neuer Absatz." },
      },
      "p1",
    );
    const open = await screen.findByTestId("wiki-open");
    await waitFor(() => expect(open.textContent).toContain("Neuer Absatz"));
    const versions = screen.getByTestId("wiki-versions");
    fireEvent.click(within(versions).getByRole("button", { name: /Änderungen von Version 1 zu 2/ }));
    const diff = await screen.findByTestId("wiki-diff");
    expect(diff.querySelector(".diff-add")?.textContent).toBe("+Neuer Absatz.");
    fireEvent.click(within(versions).getAllByRole("button", { name: "Zeigen" })[0] as HTMLElement);
    expect(await screen.findByText(/Du siehst Version 1/)).toBeTruthy();
  });

  it("moves a page by dragging it onto a folder, and orders it in its header", async () => {
    const other: Page = { ...PAGE, id: "p2", section: "E Betrieb", folder: "E Betrieb", title: "Deploy", path: "E Betrieb/Deploy" };
    const mock = show(
      {
        "GET /api/wiki/pages": {
          sections: [
            { name: "B Hub", pages: [{ ...PAGE, categories: ["Entwurf"] }] },
            { name: "E Betrieb", pages: [other] },
          ],
          categories: ["Entwurf"],
        },
        "PATCH /api/wiki/pages/p2": { ...other, folder: "B Hub" },
        "PATCH /api/wiki/pages/p1": { ...PAGE, folder: "B Hub/Neu", categories: ["Betrieb"] },
      },
      "p1",
    );
    const tree = await screen.findByRole("tree");
    await waitFor(() => expect(tree.textContent).toContain("Deploy"));
    // a category filters the tree
    fireEvent.click(screen.getByRole("button", { name: "Entwurf" }));
    expect(within(tree).queryByRole("link", { name: "Deploy" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Entwurf" }));

    // drag "Deploy" onto the folder "B Hub"
    const data = new Map<string, string>();
    const dataTransfer = {
      setData: (k: string, v: string) => data.set(k, v),
      getData: (k: string) => data.get(k) ?? "",
      get types() {
        return [...data.keys()];
      },
      effectAllowed: "",
    };
    fireEvent.dragStart(within(tree).getByRole("link", { name: "Deploy" }), { dataTransfer });
    const hub = within(tree).getByRole("button", { name: "B Hub schließen" }).parentElement as HTMLElement;
    fireEvent.dragOver(hub, { dataTransfer });
    fireEvent.drop(hub, { dataTransfer });
    await waitFor(() => expect(mock.calls.some((c) => c.method === "PATCH" && c.url === "/api/wiki/pages/p2")).toBe(true));
    const moved = mock.calls.find((c) => c.url === "/api/wiki/pages/p2");
    expect(moved?.body).toEqual({ folder: "B Hub" });

    // the open page's folder, title and categories
    fireEvent.click(await screen.findByRole("button", { name: "Ordner und Kategorien" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Ordner" }), { target: { value: "B Hub/Neu" } });
    fireEvent.change(screen.getByLabelText(/Kategorien \(/), { target: { value: "Betrieb, " } });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await waitFor(() => expect(mock.calls.some((c) => c.method === "PATCH" && c.url === "/api/wiki/pages/p1")).toBe(true));
    const saved = mock.calls.find((c) => c.url === "/api/wiki/pages/p1" && c.method === "PATCH");
    expect(saved?.body).toEqual({ folder: "B Hub/Neu", title: PAGE.title, categories: ["Betrieb"] });
  });
});
