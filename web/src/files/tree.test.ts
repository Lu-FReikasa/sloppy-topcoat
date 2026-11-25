import { describe, expect, it } from "vitest";
import { buildTree, findNode, parentsOf, visibleRows } from "./tree";

const FILES = [
  "README.md",
  "hub/src/a.py",
  "hub/src/b.py",
  "hub/tests/t.py",
  "web/App.tsx",
];

describe("the folder tree", () => {
  it("puts folders before files and sorts both by name", () => {
    const root = buildTree(["z.txt", "b/x.py", "a.txt"]);
    expect(root.children.map((c) => c.name)).toEqual(["b", "a.txt", "z.txt"]);
  });

  it("shows the children of open folders only", () => {
    const root = buildTree(FILES);
    expect(visibleRows(root, new Set()).map((r) => r.node.path)).toEqual([
      "hub",
      "web",
      "README.md",
    ]);
    const rows = visibleRows(root, new Set(["hub", "hub/src"]));
    expect(rows.map((r) => [r.node.path, r.depth])).toEqual([
      ["hub", 0],
      ["hub/src", 1],
      ["hub/src/a.py", 2],
      ["hub/src/b.py", 2],
      ["hub/tests", 1],
      ["web", 0],
      ["README.md", 0],
    ]);
  });

  it("filters by path and opens the folders on the way", () => {
    const root = buildTree(FILES);
    const rows = visibleRows(root, new Set(), "tests");
    expect(rows.map((r) => r.node.path)).toEqual([
      "hub",
      "hub/tests",
      "hub/tests/t.py",
    ]);
  });

  it("names the folders a file needs open, and finds nodes by path", () => {
    expect(parentsOf("hub/src/a.py")).toEqual(["hub", "hub/src"]);
    expect(parentsOf("README.md")).toEqual([]);
    const root = buildTree(FILES);
    expect(findNode(root, "hub/src")?.dir).toBe(true);
    expect(findNode(root, "hub/src/a.py")?.dir).toBe(false);
    expect(findNode(root, "nope/x")).toBeNull();
  });
});
