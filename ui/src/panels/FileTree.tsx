import type { IDockviewPanelProps } from "dockview";
import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";

import { FileTypeIcon } from "../fileIcons";
import { useStore } from "../store";
import type { TreeNode } from "../types";

import { ConfirmModal } from "./Modal";

function sortedChildren(node: TreeNode): TreeNode[] {
  return (node.children ?? [])
    .slice()
    .sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === "dir" ? -1 : 1));
}

function parentOf(path: string): string {
  return path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
}

function joinPath(parent: string, name: string): string {
  return parent === "" ? name : `${parent}/${name}`;
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

// ---- inline name editing (rename + create) ---------------------------------

function NameInput({
  depth,
  initial,
  placeholder,
  onCommit,
  onCancel,
}: {
  depth: number;
  initial: string;
  placeholder: string;
  onCommit: (name: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initial);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>): void => {
    if (e.key === "Enter") {
      e.preventDefault();
      const name = value.trim();
      if (name !== "") onCommit(name);
      else onCancel();
    } else if (e.key === "Escape") {
      e.preventDefault();
      onCancel();
    }
  };

  return (
    <div className="wb-tree-row wb-tree-rename" style={{ paddingLeft: 8 + depth * 16 }}>
      <span className="wb-tree-icon-slot" />
      <input
        ref={inputRef}
        className="wb-tree-input"
        value={value}
        placeholder={placeholder}
        spellCheck={false}
        aria-label={placeholder}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        onBlur={onCancel}
      />
    </div>
  );
}

// ---- context menu -----------------------------------------------------------

interface MenuState {
  node: TreeNode;
  x: number;
  y: number;
}

interface MenuItem {
  label: string;
  danger?: boolean;
  run: () => void;
}

function ContextMenu({ items, x, y, onClose }: { items: MenuItem[]; x: number; y: number; onClose: () => void }) {
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    menuRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  // Keep the menu inside the viewport.
  const left = Math.min(x, window.innerWidth - 200);
  const top = Math.min(y, window.innerHeight - items.length * 28 - 16);

  return (
    <>
      <div
        className="wb-menu-backdrop"
        onClick={onClose}
        onContextMenu={(e) => {
          e.preventDefault();
          onClose();
        }}
      />
      <div ref={menuRef} className="wb-menu" role="menu" style={{ left, top }}>
        {items.map((item) => (
          <button
            key={item.label}
            type="button"
            role="menuitem"
            className={"wb-menu-item" + (item.danger === true ? " is-danger" : "")}
            onClick={() => {
              onClose();
              item.run();
            }}
          >
            {item.label}
          </button>
        ))}
      </div>
    </>
  );
}

// ---- tree rows --------------------------------------------------------------

interface CreatingState {
  parent: string;
  kind: "file" | "dir";
}

interface TreeCallbacks {
  onToggle: (path: string) => void;
  onMenu: (node: TreeNode, x: number, y: number) => void;
  renamingPath: string | null;
  onRenameCommit: (node: TreeNode, newName: string) => void;
  onRenameCancel: () => void;
  creating: CreatingState | null;
  onCreateCommit: (name: string) => void;
  onCreateCancel: () => void;
}

interface RowProps {
  node: TreeNode;
  depth: number;
  expanded: ReadonlySet<string>;
  cb: TreeCallbacks;
}

function TreeRow({ node, depth, expanded, cb }: RowProps) {
  const selected = useStore((s) => s.activePath === node.path);
  const isDir = node.kind === "dir";
  const isOpen = expanded.has(node.path);

  if (cb.renamingPath === node.path) {
    return (
      <NameInput
        depth={depth}
        initial={node.name}
        placeholder="New name"
        onCommit={(name) => cb.onRenameCommit(node, name)}
        onCancel={cb.onRenameCancel}
      />
    );
  }

  const onContextMenu = (e: ReactMouseEvent): void => {
    e.preventDefault();
    e.stopPropagation();
    cb.onMenu(node, e.clientX, e.clientY);
  };

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
          if (isDir) cb.onToggle(node.path);
          else void useStore.getState().openFile(node.path);
        }}
        onContextMenu={onContextMenu}
        title={node.path}
      >
        {isDir ? (
          <ChevronIcon open={isOpen} />
        ) : (
          <span className="wb-tree-icon-slot">
            <FileTypeIcon name={node.name} />
          </span>
        )}
        <span className="u-truncate">{node.name}</span>
      </button>
      {isDir && isOpen && cb.creating !== null && cb.creating.parent === node.path && (
        <NameInput
          depth={depth + 1}
          initial=""
          placeholder={cb.creating.kind === "dir" ? "New folder name" : "New file name"}
          onCommit={cb.onCreateCommit}
          onCancel={cb.onCreateCancel}
        />
      )}
      {isDir &&
        isOpen &&
        sortedChildren(node).map((child) => (
          <TreeRow key={child.path} node={child} depth={depth + 1} expanded={expanded} cb={cb} />
        ))}
    </>
  );
}

// ---- panel ------------------------------------------------------------------

function NewEntryIcon({ kind }: { kind: "file" | "dir" }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {kind === "file" ? (
        <path d="M4 1.5h5L12.5 5v9a.5.5 0 0 1-.5.5H4a.5.5 0 0 1-.5-.5v-12a.5.5 0 0 1 .5-.5ZM9 1.5V5h3.5M8 7.5v4M6 9.5h4" />
      ) : (
        <path d="M1.5 3.5h4l1.5 2h7.5v7a.5.5 0 0 1-.5.5H2a.5.5 0 0 1-.5-.5v-9ZM8 8v4M6 10h4" />
      )}
    </svg>
  );
}

export function FileTreePanel(_props: IDockviewPanelProps) {
  const tree = useStore((s) => s.tree);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [renamingPath, setRenamingPath] = useState<string | null>(null);
  const [creating, setCreating] = useState<CreatingState | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<TreeNode | null>(null);

  const toggle = (path: string): void => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const startCreate = (parent: string, kind: "file" | "dir"): void => {
    setRenamingPath(null);
    setCreating({ parent, kind });
    if (parent !== "") setExpanded((prev) => new Set(prev).add(parent));
  };

  const commitCreate = (name: string): void => {
    if (creating === null) return;
    setCreating(null);
    void useStore.getState().createEntry(joinPath(creating.parent, name), creating.kind);
  };

  const commitRename = (node: TreeNode, newName: string): void => {
    setRenamingPath(null);
    if (newName === node.name) return;
    void useStore.getState().renameEntry(node.path, joinPath(parentOf(node.path), newName));
  };

  const menuItems = (node: TreeNode): MenuItem[] => {
    if (node.kind === "dir") {
      return [
        { label: "New file…", run: () => startCreate(node.path, "file") },
        { label: "New folder…", run: () => startCreate(node.path, "dir") },
        { label: "Rename…", run: () => setRenamingPath(node.path) },
      ];
    }
    return [
      { label: "Open", run: () => void useStore.getState().openFile(node.path) },
      { label: "Rename…", run: () => setRenamingPath(node.path) },
      { label: "Delete…", danger: true, run: () => setConfirmDelete(node) },
    ];
  };

  if (!tree) {
    return (
      <div className="wb-empty">
        <div className="wb-empty-hint">Loading workspace…</div>
      </div>
    );
  }

  const cb: TreeCallbacks = {
    onToggle: toggle,
    onMenu: (node, x, y) => setMenu({ node, x, y }),
    renamingPath,
    onRenameCommit: commitRename,
    onRenameCancel: () => setRenamingPath(null),
    creating,
    onCreateCommit: commitCreate,
    onCreateCancel: () => setCreating(null),
  };

  return (
    <div className="wb-filetree-panel">
      <div className="wb-filetree-toolbar">
        <span className="u-label">Files</span>
        <span className="wb-filetree-toolbar-actions">
          <button
            type="button"
            className="wb-icon-btn"
            aria-label="New file in workspace root"
            title="New file"
            onClick={() => startCreate("", "file")}
          >
            <NewEntryIcon kind="file" />
          </button>
          <button
            type="button"
            className="wb-icon-btn"
            aria-label="New folder in workspace root"
            title="New folder"
            onClick={() => startCreate("", "dir")}
          >
            <NewEntryIcon kind="dir" />
          </button>
        </span>
      </div>
      <div className="wb-filetree" role="tree" aria-label="Workspace files">
        {creating !== null && creating.parent === "" && (
          <NameInput
            depth={0}
            initial=""
            placeholder={creating.kind === "dir" ? "New folder name" : "New file name"}
            onCommit={commitCreate}
            onCancel={() => setCreating(null)}
          />
        )}
        {sortedChildren(tree).map((child) => (
          <TreeRow key={child.path} node={child} depth={0} expanded={expanded} cb={cb} />
        ))}
      </div>
      {menu !== null && (
        <ContextMenu
          items={menuItems(menu.node)}
          x={menu.x}
          y={menu.y}
          onClose={() => setMenu(null)}
        />
      )}
      {confirmDelete !== null && (
        <ConfirmModal
          title={`Delete ${confirmDelete.name}?`}
          message={`${confirmDelete.path} will be deleted from disk. This cannot be undone from Workbench.`}
          actions={[
            {
              label: "Delete",
              kind: "primary",
              onClick: () => {
                const path = confirmDelete.path;
                setConfirmDelete(null);
                void useStore.getState().deleteEntry(path);
              },
            },
            { label: "Cancel", kind: "ghost", onClick: () => setConfirmDelete(null) },
          ]}
          onDismiss={() => setConfirmDelete(null)}
        />
      )}
    </div>
  );
}
