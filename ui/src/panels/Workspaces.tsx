/**
 * The workspace switcher, as a registered capability.
 *
 * Until M5 the workspace was whatever directory the server was launched from,
 * so opening another project meant killing the server and restarting it with an
 * environment variable. This tool makes it a thing you do from inside the app:
 * a status chip that says where you are, a QuickBar picker of where you have
 * been, the OS folder dialog in the desktop shell, and an honest typed-path
 * fallback in a browser tab.
 *
 * Like `Layouts.tsx`, it contributes **no panel** — it changes what every panel
 * is looking at rather than living in one — so it arrives as a command, a status
 * item and one line in `tools.ts`.
 *
 * **It claims no `Alt` chord**, on the Scratchpad's reasoning: a registered
 * chord beats a `shortcuts.md` one and `Alt` is all that file may bind, so a
 * chord taken here is one the user cannot have. Switching projects is a
 * few-times-a-day act with a permanently visible control in the status bar and
 * a QuickBar row, not a reflex like `Alt+M`.
 *
 * **And no `shortcuts.md` kind either, deliberately.** Binding a workspace root
 * from a workspace file would let an untrusted file in one project point the
 * path jail at `C:\` on a keystroke — which is the one thing `docs/shortcuts.md`
 * says an entry may never do (`layout` clears that bar because its whole
 * vocabulary is the name of an arrangement the user saved; a filesystem path is
 * not). See the ROADMAP note under M5 item 5.
 *
 * **Three things this does not touch, and why** (the full reasoning is in
 * `server/src/workbench_server/services/workspaces.py`): running terminals and
 * live agent sessions keep running — a PTY is a shell that was never inside the
 * path jail, and an agent may be mid-turn — and a dirty buffer blocks the switch
 * rather than being discarded.
 */

import { useEffect, useRef, useState, type FormEvent } from "react";
import { create } from "zustand";

import * as api from "../api";
import { notifyWorkspaceChanged, type ToolCommand, type WorkbenchTool } from "../registry";
import { canPickDirectory, pickDirectory } from "../shell";
import { useStore, type QuickPickRow } from "../store";
import { TOOLS } from "../tools";
import type { WorkspaceRef, WorkspaceState, WorkspaceEvent } from "../types";
import { ReconnectingSocket } from "../ws";

import { ConfirmModal } from "./Modal";

import "../styles/workspaces.css";

/** QuickBar section for everything this tool contributes. */
export const WORKSPACE_CATEGORY = "Workspace";

/**
 * The first-run hint is shown once per browser profile, not once per launch.
 *
 * `explicit === false` stays true for as long as the user keeps starting the
 * server from the same directory without ever choosing a folder — which is a
 * perfectly reasonable way to work — so gating the hint on that alone would
 * nag on every launch. The chip's own unchosen styling is the part that
 * *stays*; this is the sentence that explains it once.
 */
const INTRO_SEEN_KEY = "workbench-workspace-intro-seen";

/** Joins recent paths into this tool's `dynamicCommands.key`. A byte no path
 * can hold, so two folder names cannot run together into a third set's key. */
const RECENT_KEY_SEPARATOR = "\u0000";

/**
 * Does this look like a folder path the user typed or pasted?
 *
 * Deliberately about *absolute* paths only. A bare word is a name to filter the
 * recent list by, not a folder to open; `C:\work\thing`, `\\server\share`,
 * `/home/me/thing` and `~/thing` are the four shapes someone pastes. A false
 * positive costs one refused row with the server's own reason on it, which is
 * why this can afford to be forgiving rather than clever.
 */
export function looksLikeAbsolutePath(text: string): boolean {
  return /^(?:[A-Za-z]:[\\/]|\\\\|\/|~(?:[\\/]|$))/.test(text.trim());
}

// ---- state ------------------------------------------------------------------

interface WorkspaceUiState {
  /** Null until the first read lands (or if it failed). */
  current: WorkspaceState | null;
  /** Path awaiting the unsaved-work decision; renders the confirm modal. */
  pending: string | null;
  /** A switch is in flight — the chip says so and a second one is refused. */
  busy: boolean;
  /** The typed-path prompt in a browser tab, where there is no folder dialog. */
  prompting: boolean;
}

/**
 * This tool's own store. zustand is still the only state library and
 * `ui/src/store.ts` is still the home for app-wide state; this is a second
 * *instance* on the `Layouts.tsx` precedent — nothing outside this module reads
 * it. What genuinely is app-wide (the workspace *name* every panel shows, and
 * the reset itself) lives in `useStore`, which is where the switcher reaches
 * for it.
 */
const useWorkspaceUi = create<WorkspaceUiState>()(() => ({
  current: null,
  pending: null,
  busy: false,
  prompting: false,
}));

const toast = (kind: "error" | "warn" | "info" | "success", message: string): void =>
  useStore.getState().pushToast(kind, message);

const detailOf = (err: unknown): string =>
  err instanceof api.ApiError ? err.detail : err instanceof Error ? err.message : String(err);

/** Same key everywhere: a workspace is its path, case-insensitively, because
 * Windows paths are. Mirrors `RecentsStore._key` server-side. */
const samePath = (a: string, b: string): boolean => a.toLowerCase() === b.toLowerCase();

// ---- adopting a workspace ---------------------------------------------------

/**
 * The root this window has already made itself at home in.
 *
 * Not `current.root`: that is set by every `refresh()`, including the first one
 * at startup, and a window that has *just loaded* a workspace has adopted it by
 * loading — re-running the reset would throw away the tree it is fetching, the
 * provenance dismissals it just read from storage, and the layout restore that
 * is in flight. This is the "have I already done the work for this root"
 * question, and only `adopt` answers yes to it.
 */
let knownRoot: string | null = null;

/**
 * The window becomes the new workspace's window.
 *
 * Two halves, in this order, and the order is the point: `adoptWorkspace` throws
 * away everything app-wide that was keyed to the old root (the tree, the open
 * editors, provenance, the session groups) and re-reads it, and only then are
 * the tools told — so a tool that reacts by restoring an arrangement is
 * arranging panels over a window that already agrees about what workspace it is
 * in.
 */
async function adopt(state: WorkspaceState): Promise<void> {
  useWorkspaceUi.setState({ current: state });
  knownRoot = state.root;
  await useStore.getState().adoptWorkspace();
  notifyWorkspaceChanged(TOOLS, state.root);
}

async function refresh(): Promise<WorkspaceState | null> {
  try {
    const state = await api.getWorkspace();
    useWorkspaceUi.setState({ current: state });
    if (state.problem !== null) toast("warn", state.problem);
    return state;
  } catch (err) {
    console.error("workspace read failed", err);
    return null;
  }
}

/** Switch, having already settled whatever the user had unsaved. */
async function performSwitch(path: string): Promise<void> {
  if (useWorkspaceUi.getState().busy) return;
  useWorkspaceUi.setState({ busy: true });
  try {
    const state = await api.switchWorkspace({ path });
    await adopt(state);
    toast("success", `Workspace: ${state.name}`);
  } catch (err) {
    // A refused root is the common case here and the server's sentence is the
    // useful part of it — "does not exist", "is a file, not a folder", the OS's
    // own words for a folder it will not open.
    toast("error", `Could not open that folder — ${detailOf(err)}`);
  } finally {
    useWorkspaceUi.setState({ busy: false });
  }
}

/**
 * Switch to `path`, asking first if anything is unsaved.
 *
 * The same decision the dirty-close guard asks, for the same reason and across
 * every dirty buffer at once: after the switch those paths resolve into another
 * project, so a buffer that was not saved here cannot be saved at all.
 */
export function requestWorkspaceSwitch(path: string): void {
  const trimmed = path.trim();
  if (trimmed === "") return;
  const current = useWorkspaceUi.getState().current;
  if (current !== null && samePath(current.root, trimmed)) return;
  if (useStore.getState().openFiles.some((f) => f.dirty)) {
    useWorkspaceUi.setState({ pending: trimmed });
    return;
  }
  void performSwitch(trimmed);
}

async function resolvePendingSwitch(action: "save" | "discard" | "cancel"): Promise<void> {
  const path = useWorkspaceUi.getState().pending;
  useWorkspaceUi.setState({ pending: null });
  if (path === null || action === "cancel") return;
  if (action === "save") {
    for (const file of useStore.getState().openFiles.filter((f) => f.dirty)) {
      await useStore.getState().saveFile(file.path);
    }
    const failed = useStore.getState().openFiles.filter((f) => f.dirty);
    if (failed.length > 0) {
      // A save that did not land (the conflict bar says why) must not take the
      // workspace with it — that is the loss this prompt exists to prevent.
      toast(
        "error",
        `Staying here: ${failed.map((f) => f.name).join(", ")} could not be saved.`,
      );
      return;
    }
  }
  // "Discard" has to mean it *here*, before the switch: `adoptWorkspace` keeps a
  // dirty buffer on screen as an orphan (that is the right answer when another
  // window moved the root under you), and the user who just chose to discard
  // would get their edits back on a buffer they can no longer save.
  if (action === "discard") {
    for (const file of useStore.getState().openFiles.filter((f) => f.dirty)) {
      useStore.getState().closeFile(file.path);
    }
  }
  await performSwitch(path);
}

// ---- opening a folder -------------------------------------------------------

/**
 * The OS folder dialog, or the honest fallback.
 *
 * There is no browser API that returns a filesystem *path* for a directory, so
 * outside the shell this opens a small prompt for one instead of a dialog that
 * cannot work (`ui/src/shell.ts`).
 */
export function browseForWorkspace(): void {
  if (!canPickDirectory()) {
    useWorkspaceUi.setState({ prompting: true });
    return;
  }
  void pickDirectory().then((path) => {
    if (path !== null) requestWorkspaceSwitch(path);
  });
}

// ---- the picker -------------------------------------------------------------

function recentRow(ref: WorkspaceRef, currentRoot: string | null): QuickPickRow {
  const isCurrent = currentRoot !== null && samePath(ref.path, currentRoot);
  const detail = !ref.exists
    ? `${ref.path} — folder is missing`
    : isCurrent
      ? `${ref.path} — current`
      : ref.path;
  return {
    key: ref.path,
    title: ref.name,
    detail,
    category: "Recent",
    // A folder that is gone stays on the list and is shown as unavailable: a
    // history that silently forgets where you were is worse than one that says
    // a drive is not plugged in. Same for the one you are already in.
    ...(ref.exists && !isCurrent ? {} : { disabled: true }),
    run: () => requestWorkspaceSwitch(ref.path),
  };
}

/** Rows for the picker, given what has been typed. */
export function workspaceRows(state: WorkspaceState | null, query: string): QuickPickRow[] {
  const rows: QuickPickRow[] = [];
  const typed = query.trim();
  // First, because it is the row the user is looking at when they typed it.
  if (looksLikeAbsolutePath(typed)) {
    rows.push({
      key: `open:${typed}`,
      title: `Open ${typed}`,
      detail: "the path you typed",
      category: "Open",
      run: () => requestWorkspaceSwitch(typed),
    });
  }
  rows.push({
    key: "browse",
    title: canPickDirectory() ? "Browse for a folder…" : "Type or paste a folder path…",
    detail: canPickDirectory()
      ? "the system folder dialog"
      : "a browser tab cannot open a folder dialog — the desktop shell can",
    category: "Open",
    run: browseForWorkspace,
  });
  for (const ref of state?.recents ?? []) rows.push(recentRow(ref, state?.root ?? null));
  return rows;
}

export function openWorkspacePicker(): void {
  // Refreshed as it opens: another window may have switched, and a folder on
  // the list may have been removed since the last read.
  void refresh();
  useStore.getState().openQuickPick({
    label: "Switch workspace",
    placeholder: "Recent folders — or paste a path",
    rows: (query) => workspaceRows(useWorkspaceUi.getState().current, query),
  });
}

// ---- the status chip, its modal, and the browser-tab prompt -----------------

function FolderIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M1.8 4.2a1 1 0 0 1 1-1h3l1.4 1.6h5a1 1 0 0 1 1 1v6.4a1 1 0 0 1-1 1h-9.4a1 1 0 0 1-1-1z" />
    </svg>
  );
}

/** The typed-path prompt: the browser tab's answer to a folder dialog. */
function PathPrompt() {
  const [path, setPath] = useState("");
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => input.current?.focus(), []);

  const close = (): void => useWorkspaceUi.setState({ prompting: false });
  const onSubmit = (event: FormEvent): void => {
    event.preventDefault();
    if (path.trim() === "") return;
    close();
    requestWorkspaceSwitch(path);
  };

  return (
    <>
      <div className="wb-menu-backdrop" onClick={close} />
      <div className="wb-workspace-prompt" role="dialog" aria-label="Open a folder as the workspace">
        <div className="wb-workspace-prompt-label u-label">Open a folder</div>
        <form className="wb-workspace-prompt-form" onSubmit={onSubmit}>
          <input
            ref={input}
            className="wb-workspace-prompt-input"
            aria-label="Full path of the folder to open"
            placeholder="C:\work\my-project"
            spellCheck={false}
            value={path}
            onChange={(event) => setPath(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") close();
            }}
          />
          <button
            type="submit"
            className="wb-btn wb-btn-sm wb-btn-outline"
            disabled={path.trim() === ""}
          >
            Open
          </button>
        </form>
        <div className="wb-workspace-prompt-hint">
          A browser tab cannot open a folder dialog. The desktop shell can.
        </div>
      </div>
    </>
  );
}

/** Rendered app-wide from the status bar: confirms a switch over unsaved work. */
function DirtySwitchModal() {
  const pending = useWorkspaceUi((s) => s.pending);
  const openFiles = useStore((s) => s.openFiles);
  if (pending === null) return null;
  const dirty = openFiles.filter((f) => f.dirty);
  const noun = dirty.length === 1 ? "file has" : "files have";
  const resolve = (action: "save" | "discard" | "cancel"): void =>
    void resolvePendingSwitch(action);
  return (
    <ConfirmModal
      title="Switch workspace?"
      message={
        `${dirty.length} ${noun} unsaved changes: ${dirty.map((f) => f.name).join(", ")}. ` +
        "They cannot be saved once this window is looking at another project."
      }
      actions={[
        { label: "Save and switch", kind: "primary", onClick: () => resolve("save") },
        { label: "Discard changes", kind: "outline", onClick: () => resolve("discard") },
        { label: "Cancel", kind: "ghost", onClick: () => resolve("cancel") },
      ]}
      onDismiss={() => resolve("cancel")}
    />
  );
}

/**
 * Left end of the status bar: which folder this window is looking at, as a
 * control rather than a label (DESIGN.md §6.9).
 *
 * It carries the first-run answer too. With no `WORKBENCH_WORKSPACE_ROOT` and
 * no switch this session the server fell back to the directory it was launched
 * from — nobody chose it — and the chip says so rather than presenting it as a
 * decision: `is-unchosen` while it is true, plus one toast the first time.
 */
function WorkspaceStatus() {
  const state = useWorkspaceUi((s) => s.current);
  const busy = useWorkspaceUi((s) => s.busy);
  const prompting = useWorkspaceUi((s) => s.prompting);
  const name = useStore((s) => s.workspaceName);
  const unchosen = state !== null && !state.explicit;
  const label = state?.name ?? name;
  return (
    <div className="wb-workspace-status">
      {prompting && <PathPrompt />}
      <DirtySwitchModal />
      <button
        type="button"
        className={
          "wb-status-chip wb-workspace-chip" +
          (unchosen ? " is-unchosen" : "") +
          (busy ? " is-busy" : "")
        }
        aria-haspopup="dialog"
        title={
          state === null
            ? "Workspace"
            : unchosen
              ? `${state.root} — the folder Workbench was started in. Click to open another.`
              : `${state.root} — click to switch workspace`
        }
        onClick={openWorkspacePicker}
      >
        <FolderIcon />
        <span className="wb-workspace-chip-label u-truncate">{label}</span>
      </button>
    </div>
  );
}

// ---- startup ----------------------------------------------------------------

let started = false;

/**
 * Read the workspace, say what it is on a first run, and follow a switch made
 * anywhere else.
 *
 * The socket is this tool's own, on the `usage.ts` precedent: a second
 * subscriber to the shared bus that reads only its own frames, which keeps a
 * capability out of the app's store. Its `workspace_changed` frame is how a
 * *second* window learns the root moved — the window that asked for the switch
 * has already adopted it, so the root comparison drops the echo.
 */
function start(): void {
  if (started) return;
  started = true;
  void refresh().then((state) => {
    if (state === null) return;
    // Loading *is* adopting: the window is already fetching this workspace's
    // tree and restoring its layout, so record the root without running the
    // reset over the top of them.
    knownRoot = state.root;
    if (state.explicit) return;
    try {
      if (localStorage.getItem(INTRO_SEEN_KEY) !== null) return;
      localStorage.setItem(INTRO_SEEN_KEY, "1");
    } catch {
      // storage unavailable — show the hint rather than swallow it
    }
    toast(
      "info",
      `Showing ${state.root} — the folder Workbench was started in. ` +
        "Click the folder name in the status bar to open another.",
    );
  });
  new ReconnectingSocket("/ws/events", {
    onMessage: (data) => {
      const event = data as WorkspaceEvent;
      if (event.type !== "workspace_changed") return;
      // The echo of our own switch: we adopted it before this frame arrived.
      if (knownRoot !== null && samePath(knownRoot, event.root)) return;
      void refresh().then((state) => {
        if (state !== null) void adopt(state);
      });
    },
    // A reconnect may have spanned a switch made by another window; the frame
    // that announced it is gone, so re-read rather than assume.
    onOpen: () => {
      void refresh().then((state) => {
        if (state === null) return;
        if (knownRoot === null) knownRoot = state.root;
        else if (!samePath(knownRoot, state.root)) void adopt(state);
      });
    },
  });
}

// ---- registration -----------------------------------------------------------

/** One row per recent workspace, so every folder is reachable from the command
 * palette as well as from the picker. Dynamic because the list changes while
 * the app runs (`registry.ts` re-derives them when `key` changes). */
function workspaceCommands(): ToolCommand[] {
  const state = useWorkspaceUi.getState().current;
  return (state?.recents ?? [])
    .filter((ref) => ref.exists && !samePath(ref.path, state?.root ?? ""))
    .map((ref) => ({
      id: `workspace.open.${ref.path}`,
      title: `Open workspace ${ref.name}`,
      detail: () => ref.path,
      category: WORKSPACE_CATEGORY,
      run: () => requestWorkspaceSwitch(ref.path),
    }));
}

export const workspacesTool: WorkbenchTool = {
  id: "workspaces",
  title: "Workspace",
  commands: [
    {
      id: "workspace.switch",
      title: "Switch workspace…",
      detail: () => "recent folders, or a path you paste",
      category: WORKSPACE_CATEGORY,
      run: openWorkspacePicker,
    },
    {
      id: "workspace.open",
      title: "Open a folder as the workspace…",
      detail: () => (canPickDirectory() ? "the system folder dialog" : "type or paste a path"),
      category: WORKSPACE_CATEGORY,
      run: browseForWorkspace,
    },
  ],
  dynamicCommands: {
    key: () =>
      (useWorkspaceUi.getState().current?.recents ?? [])
        .map((ref) => ref.path)
        .join(RECENT_KEY_SEPARATOR),
    build: workspaceCommands,
  },
  statusContributions: [{ region: "left", component: WorkspaceStatus }],
  // Not `onDockReady` because this has nothing to do with the dock; it is just
  // the one moment after the backend is up at which a tool can start.
  onDockReady: (api) => {
    if (api !== null) start();
  },
};
