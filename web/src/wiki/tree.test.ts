import { describe, expect, it } from "vitest";
import type { WikiPage, WikiTree } from "../types";
import { allFolders, buildWikiTree, countPages, pageFilter } from "./tree";

const page = (id: string, folder: string, title: string, categories: string[] = []): WikiPage => ({
  id,
  section: folder,
  folder,
  title,
  path: `${folder}/${title}`,
  categories,
  current_version: 1,
  source_node_id: null,
  created_at: null,
  updated_at: null,
});

const TREE: WikiTree = {
  sections: [
    { name: "B Hub", pages: [page("1", "B Hub", "B12 Projekte", ["Entwurf"]), page("2", "B Hub", "B2 Gateway")] },
    { name: "B Hub/Wissen", pages: [page("3", "B Hub/Wissen", "Notizen", ["Entwurf", "Betrieb"])] },
    { name: "E Betrieb", pages: [page("4", "E Betrieb", "Deploy", ["Betrieb"])] },
  ],
};

describe("buildWikiTree", () => {
  it("nests folders by their path and sorts names as people count", () => {
    const root = buildWikiTree(TREE);
    expect(root.folders.map((f) => f.name)).toEqual(["B Hub", "E Betrieb"]);
    const hub = root.folders[0]!;
    expect(hub.pages.map((p) => p.title)).toEqual(["B2 Gateway", "B12 Projekte"]);
    expect(hub.folders.map((f) => f.path)).toEqual(["B Hub/Wissen"]);
    expect(countPages(hub)).toBe(3);
  });

  it("leaves out folders without a page that passes the filter", () => {
    const root = buildWikiTree(TREE, pageFilter("", ["betrieb"]));
    expect(root.folders.map((f) => f.path)).toEqual(["B Hub", "E Betrieb"]);
    expect(root.folders[0]!.pages).toEqual([]);
    expect(root.folders[0]!.folders[0]!.pages.map((p) => p.id)).toEqual(["3"]);
    const searched = buildWikiTree(TREE, pageFilter("gate", []));
    expect(searched.folders.map((f) => f.name)).toEqual(["B Hub"]);
  });
});

describe("allFolders", () => {
  it("names every folder a page can go to, the parents too", () => {
    expect(allFolders(TREE)).toEqual(["B Hub", "B Hub/Wissen", "E Betrieb"]);
  });
});
