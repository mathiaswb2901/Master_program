import type { IDockviewPanelProps } from "dockview";
import { useState } from "react";

import { useStore } from "../store";
import type { TreeNode } from "../types";

function sortedChildren(node: TreeNode): TreeNode[] {
  return (node.children ?? [])
    .slice()
    .sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === "dir" ? -1 : 1));
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <span className="wb-tree-icon-slot">
      <svg
        className={"wb-chevron" + (open ? " is-open" : "")}
        width="12"
        height="12"
        viewBox="0 0 12 12"
        aria-hidden="true"
      >
        <path
          d="M4.5 2.5 8 6l-3.5 3.5"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}

function FileIcon() {
  return (
    <span className="wb-tree-icon-slot">
      <svg className="wb-file-icon" width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
        <path
          d="M4 1.5h5L12.5 5v9a.5.5 0 0 1-.5.5H4a.5.5 0 0 1-.5-.5v-12a.5.5 0 0 1 .5-.5Zm5 0V5h3.5"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.2"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}

interface RowProps {
  node: TreeNode;
  depth: number;
  expanded: ReadonlySet<string>;
  onToggle: (path: string) => void;
}

function TreeRow({ node, depth, expanded, onToggle }: RowProps) {
  const selected = useStore((s) => s.activePath === node.path);
  const isDir = node.kind === "dir";
  const isOpen = expanded.has(node.path);
  return (
    <>
      <button
        type="button"
        role="treeitem"
        aria-expanded={isDir ? isOpen : undefined}
        aria-selected={selected}
        className={"wb-tree-row" + (selected ? " is-selected" : "")}
        style={{ paddingLeft: 8 + depth * 16 }}
        onClick={() => {
          if (isDir) onToggle(node.path);
          else void useStore.getState().openFile(node.path);
        }}
        title={node.path}
      >
        {isDir ? <ChevronIcon open={isOpen} /> : <FileIcon />}
        <span className="u-truncate">{node.name}</span>
      </button>
      {isDir &&
        isOpen &&
        sortedChildren(node).map((child) => (
          <TreeRow
            key={child.path}
            node={child}
            depth={depth + 1}
            expanded={expanded}
            onToggle={onToggle}
          />
        ))}
    </>
  );
}

export function FileTreePanel(_props: IDockviewPanelProps) {
  const tree = useStore((s) => s.tree);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  const toggle = (path: string): void => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  if (!tree) {
    return (
      <div className="wb-empty">
        <div className="wb-empty-hint">Loading workspace…</div>
      </div>
    );
  }
  return (
    <div className="wb-filetree" role="tree" aria-label="Workspace files">
      {sortedChildren(tree).map((child) => (
        <TreeRow key={child.path} node={child} depth={0} expanded={expanded} onToggle={toggle} />
      ))}
    </div>
  );
}
