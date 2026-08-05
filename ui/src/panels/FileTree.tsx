import type { IDockviewPanelProps } from "dockview";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";

import { FileTypeIcon } from "../fileIcons";
import { type TreeRow, joinPath, parentPath, visibleRows } from "../filetree";
import type { WorkbenchTool } from "../registry";
import { useStore } from "../store";

import { ConfirmModal } from "./Modal";

/**
 * Row height in pixels. The same number as `--row-height` in
 * `ui/src/design/tokens.css`, and it has to be: virtualisation is arithmetic on
 * it, and a row that measures differently than the scroller thinks would drift
 * by a row every few hundred lines. Asserted against the token in
 * `fileTreeRows.test.ts`.
 */
const ROW_HEIGHT = 26;

/** Rows rendered above and below the viewport. Enough that a fast wheel gesture
 * never shows a gap, small enough that the mounted count stays ~a screenful. */
const OVERSCAN = 8;

/** Vertical padding inside the scroller (`--space-2`, top and bottom). */
const LIST_PAD = 4;

/**
 * A rendered line: a real tree row, or the inline name box for a rename or a
 * new entry. Both are rows in the same list, so the virtualiser's arithmetic
 * covers the input as well and typing a name never shifts anything by a pixel.
 */
type DisplayRow =
  | { display: "row"; row: TreeRow }
  | { display: "input"; key: string; depth: number; initial: string; placeholder: string };

/** Stable identity of a rendered line — the React key, and what the roving tab
 * stop remembers. */
function rowKey(item: DisplayRow): string {
  return item.display === "row" ? item.row.path : item.key;
}

interface CreatingState {
  parent: string;
  kind: "file" | "dir";
}

/**
 * Splice the inline input into the flattened rows.
 *
 * A rename replaces its row; a create goes directly under its parent (or at the
 * top for the workspace root). A create whose parent is not on screen — the
 * folder was collapsed or removed while the box was open — is dropped rather
 * than rendered somewhere arbitrary.
 */
function withInputRow(
  rows: readonly TreeRow[],
  renamingPath: string | null,
  creating: CreatingState | null,
): DisplayRow[] {
  const out: DisplayRow[] = rows.map((row) =>
    row.path === renamingPath
      ? {
          display: "input" as const,
          key: `rename:${row.path}`,
          depth: row.depth,
          initial: row.name,
          placeholder: "New name",
        }
      : { display: "row" as const, row },
  );
  if (creating === null) return out;
  const input: DisplayRow = {
    display: "input",
    key: `create:${creating.parent}`,
    depth: creating.parent === "" ? 0 : -1, // fixed below once the parent is found
    initial: "",
    placeholder: creating.kind === "dir" ? "New folder name" : "New file name",
  };
  if (creating.parent === "") {
    out.unshift(input);
    return out;
  }
  const at = rows.findIndex((row) => row.path === creating.parent);
  if (at < 0) return out;
  out.splice(at + 1, 0, { ...input, depth: rows[at].depth + 1 });
  return out;
}

/**
 * Right-aligned marker on a file an agent changed and the user has not yet
 * opened or dismissed (DESIGN.md §6.2 reserves the dot; §2.6 owns the colour).
 * Never colour alone: the tooltip and the accessible name say who changed it,
 * so the row reads the same to a screen reader as it looks.
 */
function AgentMark({ path }: { path: string }) {
  const entry = useStore((s) => s.provenance[path]);
  if (entry === undefined || entry.agent === null || entry.acknowledged) return null;
  const label = `Changed by ${entry.agent.session_title} (${entry.agent.tool})`;
  return <span className="wb-tree-agent-dot" role="img" aria-label={label} title={label} />;
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
  top,
  initial,
  placeholder,
  onCommit,
  onCancel,
}: {
  depth: number;
  top: number;
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
    // Arrow keys belong to the text box while it is open, not to the tree.
    e.stopPropagation();
  };

  return (
    <div
      className="wb-tree-row wb-tree-rename"
      style={{ top, paddingLeft: 8 + depth * 16 }}
    >
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
  row: TreeRow;
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

  // DESIGN.md §7: menus fully keyboard-operable. Roving focus with wrap on
  // Arrow keys, Home/End jumps, and Tab contained inside the menu — it must
  // never escape into the page behind the open menu.
  useEffect(() => {
    const buttons = (): HTMLButtonElement[] =>
      Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>("button") ?? []);
    buttons()[0]?.focus();
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onClose();
        return;
      }
      const list = buttons();
      if (list.length === 0) return;
      const index = list.indexOf(document.activeElement as HTMLButtonElement);
      const focusAt = (target: number): void => {
        e.preventDefault();
        e.stopPropagation();
        list[(target + list.length) % list.length]?.focus();
      };
      if (e.key === "ArrowDown" || (e.key === "Tab" && !e.shiftKey)) {
        focusAt(index + 1); // index -1 (focus elsewhere) lands on the first item
      } else if (e.key === "ArrowUp" || (e.key === "Tab" && e.shiftKey)) {
        focusAt(index === -1 ? list.length - 1 : index - 1);
      } else if (e.key === "Home") {
        focusAt(0);
      } else if (e.key === "End") {
        focusAt(list.length - 1);
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
      <div ref={menuRef} className="wb-menu" role="menu" aria-orientation="vertical" style={{ left, top }}>
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

// ---- one row ----------------------------------------------------------------

interface RowProps {
  row: TreeRow;
  /** Absolute offset inside the scroller — the virtualiser's arithmetic. */
  top: number;
  index: number;
  /** Total rows, for `aria-setsize`: the DOM no longer holds them all. */
  total: number;
  expanded: boolean;
  focused: boolean;
  onActivate: (row: TreeRow) => void;
  onFocusRow: (index: number, key: string) => void;
  onMenu: (row: TreeRow, x: number, y: number) => void;
}

function TreeRowView({
  row,
  top,
  index,
  total,
  expanded,
  focused,
  onActivate,
  onFocusRow,
  onMenu,
}: RowProps) {
  const selected = useStore((s) => s.activePath === row.path);
  const isDir = row.kind === "dir";

  const onContextMenu = (e: ReactMouseEvent<HTMLButtonElement>): void => {
    e.preventDefault();
    e.stopPropagation();
    if (e.clientX === 0 && e.clientY === 0) {
      // Keyboard-invoked (Shift+F10 / Menu key on the focused row) — anchor
      // the menu to the row instead of the top-left viewport corner.
      const rect = e.currentTarget.getBoundingClientRect();
      onMenu(row, rect.left + 16, rect.bottom);
    } else {
      onMenu(row, e.clientX, e.clientY);
    }
  };

  return (
    <button
      type="button"
      role="treeitem"
      data-index={index}
      aria-expanded={isDir ? expanded : undefined}
      aria-selected={selected}
      aria-level={row.depth + 1}
      aria-posinset={index + 1}
      aria-setsize={total}
      // Roving tabindex: exactly one row is in the tab order, because with
      // virtualisation "tab through every row" was never going to survive —
      // and Arrow keys are what a tree is supposed to answer to anyway.
      tabIndex={focused ? 0 : -1}
      className={"wb-tree-row" + (selected ? " is-selected" : "")}
      style={{ top, paddingLeft: 8 + row.depth * 16 }}
      // Any route into a row owns the tab stop from then on — a click, a Tab
      // from the toolbar, or a programmatic focus. Tracking it on `click`
      // alone would leave the arrow keys operating on a different row than the
      // one the user is looking at.
      onFocus={() => onFocusRow(index, row.path)}
      onClick={() => onActivate(row)}
      onContextMenu={onContextMenu}
      title={row.path}
    >
      {isDir ? (
        <ChevronIcon open={expanded} />
      ) : (
        <span className="wb-tree-icon-slot">
          <FileTypeIcon name={row.name} />
        </span>
      )}
      <span className="wb-tree-name u-truncate">{row.name}</span>
      {!isDir && <AgentMark path={row.path} />}
    </button>
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
  const dirs = useStore((s) => s.dirs);
  const expanded = useStore((s) => s.expandedDirs);
  const rootLoaded = useStore((s) => s.dirs[""] !== undefined);
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [renamingPath, setRenamingPath] = useState<string | null>(null);
  const [creating, setCreating] = useState<CreatingState | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<TreeRow | null>(null);
  /**
   * Which row owns the tab stop — by **path**, not by index.
   *
   * Measured, not theoretical: expanding a folder loads its children
   * asynchronously, and rows arriving *above* the focused one shift every index
   * below them. Remembering the index meant that pressing Down right after Open
   * moved to the row the focus had already slid off. The index is kept only as
   * the fallback for when the remembered row is gone (a collapse, a delete):
   * land where it was rather than jump to the top.
   */
  const [focus, setFocus] = useState<{ key: string | null; index: number }>({
    key: null,
    index: 0,
  });
  /** Set when a key moved the focus, so only *those* moves steal it back. */
  const focusPending = useRef(false);

  const scrollRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(0);

  const rows = useMemo(() => visibleRows(dirs, expanded), [dirs, expanded]);
  const display = useMemo(
    () => withInputRow(rows, renamingPath, creating),
    [rows, renamingPath, creating],
  );

  // The scroller's height drives how many rows are mounted at all, so it is
  // measured rather than assumed — the panel is resizable, and it does not
  // exist at all until the root listing arrives (hence `rootLoaded`: without it
  // the observer is attached to nothing and the tree renders eight rows).
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el === null) return;
    const observer = new ResizeObserver(() => setViewportHeight(el.clientHeight));
    observer.observe(el);
    setViewportHeight(el.clientHeight);
    return () => observer.disconnect();
  }, [rootLoaded]);

  const total = display.length;
  const remembered = focus.key === null ? -1 : display.findIndex((d) => rowKey(d) === focus.key);
  const focusAt = remembered >= 0 ? remembered : Math.max(0, Math.min(focus.index, total - 1));

  const focusRow = useCallback((index: number, key: string) => {
    setFocus((current) =>
      current.key === key && current.index === index ? current : { key, index },
    );
  }, []);
  // The window: what the scroller can show, plus overscan. Independent of how
  // many rows exist, which is the whole point — expanding a 2,000-file folder
  // mounts the same ~40 buttons as expanding an empty one.
  const first = Math.max(0, Math.floor((scrollTop - LIST_PAD) / ROW_HEIGHT) - OVERSCAN);
  const last = Math.min(
    total,
    Math.ceil((scrollTop + viewportHeight - LIST_PAD) / ROW_HEIGHT) + OVERSCAN,
  );
  const visible = display.slice(first, last);

  const scrollRowIntoView = useCallback((index: number) => {
    const el = scrollRef.current;
    if (el === null) return;
    const top = LIST_PAD + index * ROW_HEIGHT;
    let next = el.scrollTop;
    if (top < next) next = top;
    else if (top + ROW_HEIGHT > next + el.clientHeight) next = top + ROW_HEIGHT - el.clientHeight;
    if (next === el.scrollTop) return;
    el.scrollTop = next;
    // The `scroll` event is asynchronous, and the row being moved to may be
    // outside the current window — so the window has to be recomputed in the
    // same render, or the row it is scrolling to would not exist to focus.
    setScrollTop(next);
  }, []);

  const moveFocus = useCallback(
    (index: number) => {
      const clamped = Math.max(0, Math.min(index, total - 1));
      const item = display[clamped];
      focusPending.current = true;
      setFocus({ key: item === undefined ? null : rowKey(item), index: clamped });
      scrollRowIntoView(clamped);
    },
    [display, scrollRowIntoView, total],
  );

  // A row moved into the window by a keyboard move has to actually take focus,
  // and it does not exist until this render — so the DOM lookup happens after.
  useLayoutEffect(() => {
    if (!focusPending.current) return;
    focusPending.current = false;
    scrollRef.current?.querySelector<HTMLElement>(`[data-index="${focusAt}"]`)?.focus();
  });

  const toggle = (path: string): void => useStore.getState().toggleDir(path);

  const activate = (row: TreeRow): void => {
    if (row.kind === "dir") toggle(row.path);
    else void useStore.getState().openFile(row.path);
  };

  const startCreate = (parent: string, kind: "file" | "dir"): void => {
    setRenamingPath(null);
    setCreating({ parent, kind });
    if (parent !== "" && !expanded.has(parent)) useStore.getState().toggleDir(parent);
  };

  const commitCreate = (name: string): void => {
    if (creating === null) return;
    setCreating(null);
    void useStore.getState().createEntry(joinPath(creating.parent, name), creating.kind);
  };

  const commitRename = (path: string, oldName: string, newName: string): void => {
    setRenamingPath(null);
    if (newName === oldName) return;
    void useStore.getState().renameEntry(path, joinPath(parentPath(path), newName));
  };

  const menuItems = (row: TreeRow): MenuItem[] => {
    if (row.kind === "dir") {
      return [
        { label: "New file…", run: () => startCreate(row.path, "file") },
        { label: "New folder…", run: () => startCreate(row.path, "dir") },
        {
          label: "Rename…",
          run: () => {
            setCreating(null);
            setRenamingPath(row.path);
          },
        },
      ];
    }
    return [
      { label: "Open", run: () => void useStore.getState().openFile(row.path) },
      { label: "Rename…", run: () => setRenamingPath(row.path) },
      { label: "Delete…", danger: true, run: () => setConfirmDelete(row) },
    ];
  };

  /**
   * Tree keyboard model (WAI-ARIA): Up/Down move, Right opens a folder or steps
   * into it, Left closes it or steps out to the parent, Home/End jump, Enter and
   * Space activate. It replaces "Tab through every row", which virtualisation
   * ended — and which was never usable on a 2,000-row folder anyway.
   */
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>): void => {
    const current = display[focusAt];
    const row = current !== undefined && current.display === "row" ? current.row : null;
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        moveFocus(focusAt + 1);
        return;
      case "ArrowUp":
        e.preventDefault();
        moveFocus(focusAt - 1);
        return;
      case "Home":
        e.preventDefault();
        moveFocus(0);
        return;
      case "End":
        e.preventDefault();
        moveFocus(total - 1);
        return;
      case "ArrowRight":
        if (row === null || row.kind !== "dir") return;
        e.preventDefault();
        if (expanded.has(row.path)) moveFocus(focusAt + 1);
        else toggle(row.path);
        return;
      case "ArrowLeft": {
        if (row === null) return;
        e.preventDefault();
        if (row.kind === "dir" && expanded.has(row.path)) {
          toggle(row.path);
          return;
        }
        const parent = parentPath(row.path);
        const at = display.findIndex((d) => d.display === "row" && d.row.path === parent);
        if (at >= 0) moveFocus(at);
        return;
      }
      // Enter and Space are deliberately absent: every row is a real <button>,
      // so the browser already activates it — and intercepting them here would
      // either double-fire or have to reimplement what it does for free.
      default:
    }
  };

  if (!rootLoaded) {
    return (
      <div className="wb-empty">
        <div className="wb-empty-hint">Loading workspace…</div>
      </div>
    );
  }

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
      <div
        ref={scrollRef}
        className="wb-filetree"
        role="tree"
        aria-label="Workspace files"
        onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
        onKeyDown={onKeyDown}
      >
        {/* One spacer holds the full scroll height; only the window is mounted.
            `presentation` so the treeitems stay children of the tree. */}
        <div
          className="wb-tree-viewport"
          role="presentation"
          style={{ height: total * ROW_HEIGHT }}
        >
          {visible.map((item, offset) => {
            const index = first + offset;
            const top = index * ROW_HEIGHT;
            if (item.display === "input") {
              return (
                <NameInput
                  key={item.key}
                  depth={item.depth}
                  top={top}
                  initial={item.initial}
                  placeholder={item.placeholder}
                  onCommit={(name) => {
                    if (renamingPath !== null) commitRename(renamingPath, item.initial, name);
                    else commitCreate(name);
                  }}
                  onCancel={() => {
                    setRenamingPath(null);
                    setCreating(null);
                  }}
                />
              );
            }
            return (
              <TreeRowView
                key={item.row.path}
                row={item.row}
                top={top}
                index={index}
                total={total}
                expanded={expanded.has(item.row.path)}
                focused={index === focusAt}
                onActivate={activate}
                onFocusRow={focusRow}
                onMenu={(row, x, y) => setMenu({ row, x, y })}
              />
            );
          })}
        </div>
      </div>
      {menu !== null && (
        <ContextMenu
          items={menuItems(menu.row)}
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

/** Left end of the status bar: which workspace this window is looking at. */
function WorkspaceStatus() {
  const workspace = useStore((s) => s.workspaceName);
  return <span className="wb-status-workspace u-truncate">{workspace}</span>;
}

export const filesTool: WorkbenchTool = {
  id: "files",
  title: "Files",
  panel: {
    component: FileTreePanel,
    defaultLocation: { area: "left", size: 240 },
  },
  statusContributions: [{ region: "left", component: WorkspaceStatus }],
};
