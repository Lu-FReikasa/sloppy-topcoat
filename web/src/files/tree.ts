// The folder tree of a repository, built from the flat list of its files (`repo.list`).

export interface TreeNode {
  name: string;
  /** Relative to the repository, with `/`; "" for the root. */
  path: string;
  dir: boolean;
  children: TreeNode[];
}

export interface Row {
  node: TreeNode;
  depth: number;
}

function sorted(nodes: TreeNode[]): TreeNode[] {
  return nodes.sort(
    (a, b) =>
      Number(b.dir) - Number(a.dir) ||
      a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
  );
}

/** Folders first, then files, each sorted by name. */
export function buildTree(files: readonly string[]): TreeNode {
  const root: TreeNode = { name: "", path: "", dir: true, children: [] };
  const dirs = new Map<string, TreeNode>([["", root]]);
  for (const file of files) {
    const parts = file.split("/").filter(Boolean);
    let parent = root;
    parts.forEach((name, i) => {
      const path = parts.slice(0, i + 1).join("/");
      const last = i === parts.length - 1;
      if (last) {
        parent.children.push({ name, path, dir: false, children: [] });
        return;
      }
      let dir = dirs.get(path);
      if (!dir) {
        dir = { name, path, dir: true, children: [] };
        dirs.set(path, dir);
        parent.children.push(dir);
      }
      parent = dir;
    });
  }
  for (const dir of dirs.values()) sorted(dir.children);
  return root;
}

/** `a/b/c.py` -> `["a", "a/b"]`: the folders that have to be open to see the file. */
export function parentsOf(path: string): string[] {
  const parts = path.split("/").filter(Boolean);
  return parts.slice(0, -1).map((_, i) => parts.slice(0, i + 1).join("/"));
}

/** The rows to draw: open folders show their children. A filter shows only the files whose path
 *  contains it (and the folders on the way, all open). */
export function visibleRows(
  root: TreeNode,
  open: ReadonlySet<string>,
  filter = "",
): Row[] {
  const want = filter.trim().toLowerCase();
  const keeps = (node: TreeNode): boolean =>
    node.dir
      ? node.children.some(keeps)
      : node.path.toLowerCase().includes(want);
  const rows: Row[] = [];
  const walk = (node: TreeNode, depth: number) => {
    for (const child of node.children) {
      if (want && !keeps(child)) continue;
      rows.push({ node: child, depth });
      if (child.dir && (want || open.has(child.path))) walk(child, depth + 1);
    }
  };
  walk(root, 0);
  return rows;
}

export function findNode(root: TreeNode, path: string): TreeNode | null {
  if (path === "") return root;
  let node = root;
  for (const part of path.split("/").filter(Boolean)) {
    const next = node.children.find((c) => c.name === part);
    if (!next) return null;
    node = next;
  }
  return node;
}
