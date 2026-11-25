// web/src/project/collapse.test.tsx
import { describe, expect, it } from "vitest";
import { visibleNodes } from "./ProjectPage";
import type { ProjectNode } from "./types";

describe("visibleNodes collapse logic", () => {
  const parent: ProjectNode = {
    id: "p",
    parent_id: null,
    number: 1,
    title: "Parent",
    kind: "task",
  } as unknown as ProjectNode;

  const child1: ProjectNode = {
    id: "c1",
    parent_id: "p",
    number: 2,
    title: "Child 1",
    kind: "task",
  } as unknown as ProjectNode;

  const child2: ProjectNode = {
    id: "c2",
    parent_id: "p",
    number: 3,
    title: "Child 2",
    kind: "task",
  } as unknown as ProjectNode;

  const child3: ProjectNode = {
    id: "c3",
    parent_id: "p",
    number: 4,
    title: "Child 3",
    kind: "task",
  } as unknown as ProjectNode;

  const child4: ProjectNode = {
    id: "c4",
    parent_id: "p",
    number: 5,
    title: "Child 4",
    kind: "task",
  } as unknown as ProjectNode;

  const child5: ProjectNode = {
    id: "c5",
    parent_id: "p",
    number: 6,
    title: "Child 5",
    kind: "task",
  } as unknown as ProjectNode;

  const leaf: ProjectNode = {
    id: "q",
    parent_id: null,
    number: 7,
    title: "Leaf",
    kind: "task",
  } as unknown as ProjectNode;

  const nodes: ProjectNode[] = [
    parent,
    child1,
    child2,
    child3,
    child4,
    child5,
    leaf,
  ];

  it("shows correct childCount and hasChildren when nothing is collapsed", () => {
    const rows = visibleNodes(nodes, new Set<string>());
    const parentRow = rows.find((r) => r.node.id === "p");
    const leafRow = rows.find((r) => r.node.id === "q");

    expect(parentRow).toBeDefined();
    expect(leafRow).toBeDefined();

    if (parentRow) {
      expect(parentRow.childCount).toBe(5);
      expect(parentRow.hasChildren).toBe(true);
    }

    if (leafRow) {
      expect(leafRow.childCount).toBe(0);
      expect(leafRow.hasChildren).toBe(false);
    }
  });

  it("hides children when parent is collapsed", () => {
    const rows = visibleNodes(nodes, new Set<string>(["p"]));
    const ids = rows.map((r) => r.node.id);

    expect(ids).toEqual(["p", "q"]);
    // Ensure no child nodes are present
    expect(ids).not.toContain("c1");
    expect(ids).not.toContain("c2");
    expect(ids).not.toContain("c3");
    expect(ids).not.toContain("c4");
    expect(ids).not.toContain("c5");
  });
});
