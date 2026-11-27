import { useCallback, useState } from "react";
import type { ProjectNode } from "./types";

export type DetailTab = "tree" | "timeline" | "sets" | "chains";

/** The selected project's detail: which tab is open (tree, timeline, change sets, chains), whether
 *  the scope editor or the "save as template" form is open, and the node opened in the tree. */
export function useProjectDetail(nodes: ProjectNode[]) {
  const [nodeId, setNodeId] = useState<string | null>(null);
  const [tab, setTab] = useState<DetailTab>("tree");
  const [showScope, setShowScope] = useState(false);
  const [naming, setNaming] = useState(false);
  const [templateName, setTemplateName] = useState("");

  const node = nodes.find((n) => n.id === nodeId) ?? null;

  /** Another entry of the left column was picked: no node open, the tree tab, no scope editor. */
  const reset = useCallback(() => {
    setNodeId(null);
    setShowScope(false);
    setTab("tree");
  }, []);

  /** Open a node of the tree (or null to close it), as an inbox item or a click does. */
  const open = useCallback((id: string | null) => {
    setNodeId(id);
    setTab("tree");
  }, []);

  return {
    nodeId,
    setNodeId,
    node,
    open,
    tab,
    setTab,
    showScope,
    setShowScope,
    naming,
    setNaming,
    templateName,
    setTemplateName,
    reset,
  };
}
