/** Single zustand store: files/editors, agent sessions + chats, theme, quickbar. */

import { create } from "zustand";

import * as api from "./api";
import { defineWorkbenchTheme, disposeModel, setModelContent } from "./monaco";
import { withoutTransitions } from "./motion";
import { isOfficePath, preloadDocsApi } from "./office";
import { applyProvenanceSnapshot, prunedDismissed } from "./provenance";
import { cancelShellClose, closeShellWindow } from "./shell";
import { promptInsertText, shellInsertText } from "./shortcuts";
import { terminalHandle } from "./terminalInput";
import { THEME_STORAGE_KEY, type Theme } from "./theme";
import type {
  AgentServerMessage,
  FileChangedEvent,
  FileProvenanceEvent,
  FolderSessions,
  OfficeStatus,
  PlanAnnotation,
  PlanArtifact,
  PlanVerdict,
  ProvenanceEntry,
  SessionInfo,
  SessionLimits,
  SessionState,
  SessionStatusEvent,
  ShortcutEntry,
  ShortcutProblem,
  TranscriptMessage,
  TreeNode,
  UiState,
  WorkspaceEvent,
} from "./types";
import { ReconnectingSocket } from "./ws";

export interface OpenFile {
  path: string;
  name: string;
  /** "text" renders in Monaco; "office" in the OnlyOffice document panel. */
  kind: "text" | "office";
  buffer: string;
  /** Content last synced with disk — dirty when buffer differs. */
  savedContent: string;
  /** Last-known on-disk hash; null after a delete-on-disk (save recreates). */
  hash: string | null;
  dirty: boolean;
  /** Non-null shows the non-modal conflict bar with this message. */
  conflict: string | null;
  /** Non-null when the file could not be loaded (e.g. binary, 415). */
  loadError: string | null;
  /** Bumped to destroy + recreate the office editor (fresh config/key). */
  officeGeneration: number;
  /** Transient "saved" acknowledgment after an office forcesave. */
  saveAck: boolean;
}

export type ToastKind = "error" | "warn" | "info" | "success";

export interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
}

/** One terminal tab: its own PTY socket, kept mounted while the tab exists. */
export interface TerminalTab {
  id: number;
  title: string;
  /** Bumped by Reconnect — remounts the instance against a fresh PTY. */
  generation: number;
  /** PTY session alive: drives the running-process dot (DESIGN.md §6.6). */
  alive: boolean;
  /**
   * The pane that owns this terminal, or null for one in the Terminal panel's
   * tab strip.
   *
   * `id` is a per-session counter and means nothing after a reload; the pane
   * key is the half that survives, because it is what `terminal#<key>` writes
   * into the layout file (`ui/src/panes.ts`). The PTY itself never survives —
   * the socket closes with the page and the server releases it — so what comes
   * back is the pane, its number and a fresh shell.
   */
  paneKey: string | null;
}

export type ChatItem =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string; done: boolean; costUsd: number | null; isError: boolean }
  | {
      kind: "tool";
      /** Server-side call id; `tool_settled` settles exactly this row. */
      id: string;
      tool: string;
      summary: string;
      settled: boolean;
      settledError: boolean;
      /** Result excerpt, once it arrives; the chevron expands it. */
      output: string;
    }
  | {
      kind: "permission";
      requestId: string;
      tool: string;
      description: string;
      decision: "allow" | "deny" | null;
    }
  | { kind: "plan"; plan: PlanArtifact }
  | { kind: "error"; message: string };

export interface ChatState {
  items: ChatItem[];
}

/** What the user has assembled for one plan card. `verdict` is null until the
 * card settles; once set the card renders read-only with their choices marked.
 *
 * Clicking sets it optimistically, but the server's `plan_resolved` frame is
 * authoritative and overwrites it: a plan that timed out (or that another client
 * already answered) settles as what actually happened, so no card ever claims an
 * approval the agent never received. */
export interface PlanDraft {
  /** node_id -> option_id */
  choices: Record<string, string>;
  /** node_id -> free text (question answers and per-node notes alike) */
  annotations: Record<string, string>;
  comment: string;
  verdict: PlanVerdict | null;
}

export const emptyPlanDraft = (): PlanDraft => ({
  choices: {},
  annotations: {},
  comment: "",
  verdict: null,
});

/** Option groups the draft has not answered. A group without a `recommended`
 * option starts unselected (perfectly legal), so "approve" stays blocked until
 * the user actually picks — an approval must never imply an unmade choice. */
export function unchosenOptionGroups(plan: PlanArtifact, draft: PlanDraft): string[] {
  return plan.nodes
    .filter((node) => node.kind === "option_group" && draft.choices[node.node_id] === undefined)
    .map((node) => node.node_id);
}

/** "Finished/failed since last viewed" markers layered over the server state. */
export interface SessionFlags {
  done: boolean;
  error: boolean;
}

/** One row of a `QuickPick`. Deliberately the shape the QuickBar already
 * renders (title, right-hand detail, section header) so a pick surface is the
 * QuickBar rather than a second overlay language (DESIGN.md §6.5). */
export interface QuickPickRow {
  key: string;
  title: string;
  detail?: string;
  category?: string;
  /** Shown, but not choosable — a row whose reason for existing is that the
   * user can see *why* it is unavailable. Put that reason in `detail`; the
   * QuickBar renders it on a real `disabled` button, so the keyboard skips it
   * and a screen reader announces it. */
  disabled?: boolean;
  run: () => void;
}

/**
 * A list some capability asks the QuickBar to show, filtered by typing, instead
 * of files or commands.
 *
 * The rows are a thunk supplied by whoever opened it, which is the whole point:
 * `QuickBar.tsx` stays a surface that names no capability, exactly as `App.tsx`
 * and `commands.ts` do. The split picker (`ui/src/panels/Panes.tsx`) is the
 * first user of it.
 */
export interface QuickPick {
  /** Accessible name of the dialog, e.g. "Split this pane". */
  label: string;
  placeholder: string;
  rows: () => QuickPickRow[];
}

interface WorkbenchStore {
  theme: Theme;
  tree: TreeNode | null;
  openFiles: OpenFile[];
  activePath: string | null;
  folders: FolderSessions[];
  sessionStates: Record<string, SessionState>;
  sessionFlags: Record<string, SessionFlags>;
  /** Server-stated concurrency ceiling, refreshed with the listing. null until
   * the first refresh lands (or if it failed) — treat that as "no prediction". */
  sessionLimits: SessionLimits | null;
  activeSessionId: string | null;
  transcriptView: { session: SessionInfo; messages: TranscriptMessage[] } | null;
  chats: Record<string, ChatState>;
  /** Unsent message text per session — in the store because prompt shortcuts
   * write it from outside the chat component. */
  chatDrafts: Record<string, string>;
  /** Draft + settled state per plan_id (plan ids are unique across sessions). */
  plans: Record<string, PlanDraft>;
  /** Cost of the most recent finished turn, for the status bar. */
  lastCostUsd: number | null;
  quickBarOpen: boolean;
  /** Text the QuickBar opens with — ">" puts it straight into command mode. */
  quickBarPrefill: string;
  /** A one-shot list the QuickBar shows instead of files and commands. */
  quickPick: QuickPick | null;
  terminals: TerminalTab[];
  activeTerminalId: number | null;
  toasts: Toast[];
  /** Path awaiting the "close dirty file?" decision; renders the confirm modal. */
  pendingClosePath: string | null;
  /** True while the native shell's window close is held for the same decision,
   * across every dirty buffer at once. Always false in a browser tab. */
  pendingShellClose: boolean;
  /** GET /api/office/status result, fetched once at startup (retried on
   * office open if that failed); when enabled, api.js is preloaded so the
   * first office tab doesn't pay the script-load latency. */
  officeStatus: OfficeStatus | null;
  /** shortcuts.md entries (workspace merged over global) + what failed to parse. */
  shortcuts: ShortcutEntry[];
  shortcutProblems: ShortcutProblem[];
  /** path -> who last changed it, for the paths an agent is claiming. Entries
   * only ever exist for attributed changes: the server sends a null-agent entry
   * to say "drop this path", never to say "someone unknown did it". */
  provenance: Record<string, ProvenanceEntry>;
  /** Paths whose file bar the user closed. Persisted across reloads, and
   * cleared by the next agent change to that path so a later edit raises the
   * bar again. */
  provenanceDismissed: Record<string, boolean>;

  init: () => void;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
  setQuickBarOpen: (open: boolean, prefill?: string) => void;
  /** Open the QuickBar on a supplied list. Closing it clears the list. */
  openQuickPick: (pick: QuickPick) => void;
  newTerminal: () => void;
  /** The terminal this pane owns, created on first sight of the pane. Returns
   * its id, so the pane can render it without a second lookup. */
  ensureTerminalPane: (paneKey: string) => number;
  /** Lowest pane number no terminal pane is using — what a new terminal pane is
   * called, and stable across a reload because the panes register themselves. */
  nextTerminalPaneKey: () => string;
  closeTerminal: (id: number) => void;
  setActiveTerminal: (id: number) => void;
  restartTerminal: (id: number) => void;
  setTerminalAlive: (id: number, alive: boolean) => void;
  pushToast: (kind: ToastKind, message: string) => void;
  dismissToast: (id: number) => void;

  refreshTree: () => Promise<void>;
  openFile: (path: string) => Promise<void>;
  /** Guarded close: dirty buffers get a confirm modal instead of silent discard. */
  requestCloseFile: (path: string) => void;
  resolvePendingClose: (action: "save" | "discard" | "cancel") => Promise<void>;
  /** The shell asked to close the window: prompt if anything is unsaved. */
  requestShellClose: () => void;
  resolveShellClose: (action: "save" | "discard" | "cancel") => Promise<void>;
  closeFile: (path: string) => void;
  createEntry: (path: string, kind: "file" | "dir") => Promise<void>;
  renameEntry: (path: string, newPath: string) => Promise<void>;
  deleteEntry: (path: string) => Promise<void>;
  setActiveFile: (path: string) => void;
  updateBuffer: (path: string, text: string) => void;
  saveFile: (path: string) => Promise<void>;
  handleFileChanged: (event: FileChangedEvent) => void;
  syncFromDisk: (path: string) => Promise<void>;
  reloadFromDisk: (path: string) => Promise<void>;
  keepMine: (path: string) => Promise<void>;

  refreshProvenance: () => Promise<void>;
  handleFileProvenance: (event: FileProvenanceEvent) => void;
  /** Mark an agent change as seen: the tree marker clears, the attribution
   * stays. Called on open and on dismiss — those two, explicitly, are what
   * "acknowledged" means. */
  acknowledgeProvenance: (path: string) => void;
  /** Close the file bar for this path (and acknowledge it). */
  dismissProvenance: (path: string) => void;

  refreshShortcuts: () => Promise<void>;
  /** Insert a shortcut into its surface. Never executes: shell snippets are
   * typed into the terminal without a newline, prompts land in the chat draft. */
  runShortcut: (entry: ShortcutEntry) => void;
  setChatDraft: (sessionId: string, text: string) => void;

  ensureOfficeStatus: () => Promise<void>;
  checkOfficeChange: (path: string, eventHash: string | null) => Promise<void>;
  reopenOffice: (path: string) => void;

  refreshSessions: () => Promise<void>;
  openSession: (info: SessionInfo) => void;
  /** Open a session known only by id — the file bar's link back to the agent
   * that made the change. A session the server no longer lists (a restart
   * dropped it) says so instead of opening the wrong one. */
  openSessionById: (sessionId: string) => void;
  openLiveSession: (info: SessionInfo) => void;
  /** Start streaming this session without changing what the keyboard talks to
   * — what an agent *pane* does on mount, so four panes stream at once. */
  attachSession: (sessionId: string) => void;
  /** Make this session the one `sendChat`, `interrupt` and a `prompt` shortcut
   * mean. Called when a session pane takes focus: the focused pane is the one
   * you are talking to, exactly as in tmux. */
  focusSession: (sessionId: string) => void;
  openTranscript: (info: SessionInfo) => Promise<void>;
  /** Resolves with the new session's id, so a caller that is opening a *pane*
   * for it can bind the pane to it (`agent#<session_id>`); null if it failed. */
  createSessionIn: (folder: string) => Promise<string | null>;
  resumeSession: () => Promise<void>;
  sendChat: (text: string) => void;
  decidePermission: (requestId: string, allow: boolean) => void;
  setPlanChoice: (planId: string, nodeId: string, optionId: string) => void;
  setPlanAnnotation: (planId: string, nodeId: string, text: string) => void;
  setPlanComment: (planId: string, text: string) => void;
  decidePlan: (planId: string, verdict: Exclude<PlanVerdict, "no_decision">) => void;
  interrupt: () => void;
  handleAgentMessage: (sessionId: string, message: AgentServerMessage) => void;
  handleSessionStatus: (event: SessionStatusEvent) => void;
}

const initialTheme: Theme =
  document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";

let initialized = false;
const loadingPaths = new Set<string>();
let treeRefreshTimer: number | undefined;

const TOAST_AUTO_DISMISS_MS = 6000;
let toastSeq = 0;

/** Serialized problem list already toasted — see `refreshShortcuts`. */
let lastShortcutProblems = "[]";

/**
 * Put the caret back in the chat box after a prompt shortcut fills it.
 *
 * More than one chat can be mounted now — a pane per session is the point of
 * the pane system — so "the chat box" is the one in the **focused pane**, and
 * the first one on screen only as a fallback for a window with no focused pane
 * (dockview marks it `.dv-active-group`). Getting this wrong types a user's
 * prompt into a conversation they were not looking at.
 */
function focusChatInput(): void {
  window.setTimeout(() => {
    const input =
      document.querySelector<HTMLTextAreaElement>(".dv-active-group .wb-chat-input textarea") ??
      document.querySelector<HTMLTextAreaElement>(".wb-chat-input textarea");
    if (input === null) return;
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }, 0);
}

/** A copy of `record` without `key` — used where deleting means "no longer
 * true", not "set to false". */
function without(record: Record<string, boolean>, key: string): Record<string, boolean> {
  if (record[key] === undefined) return record;
  const next = { ...record };
  delete next[key];
  return next;
}

/** Paths whose provenance bar the user closed. Persisted (as a path array)
 * because the bar stands for as long as the attribution does: a dismissal that
 * a reload undid would put the line back above a buffer the user had already
 * decided about. Pruned against the live map on every refresh, so it cannot
 * grow past the server's own bounded set of attributed paths. */
const PROVENANCE_DISMISSED_KEY = "workbench-provenance-dismissed";

function loadDismissed(): Record<string, boolean> {
  const dismissed: Record<string, boolean> = {};
  try {
    const raw = localStorage.getItem(PROVENANCE_DISMISSED_KEY);
    if (raw === null) return dismissed;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return dismissed;
    for (const path of parsed) if (typeof path === "string") dismissed[path] = true;
  } catch {
    // unreadable or unavailable storage — start with nothing dismissed
  }
  return dismissed;
}

function persistDismissed(dismissed: Record<string, boolean>): void {
  try {
    const paths = Object.keys(dismissed).filter((path) => dismissed[path] === true);
    localStorage.setItem(PROVENANCE_DISMISSED_KEY, JSON.stringify(paths));
  } catch {
    // storage unavailable — dismissal just won't survive a reload
  }
}

/** Paths a `file_provenance` frame touched while a `GET /api/provenance` was in
 * flight. Non-null only for the duration of that request (see
 * `applyProvenanceSnapshot` for why the snapshot must not clobber them). */
let provenanceInFlight: Set<string> | null = null;

function errorDetail(err: unknown): string {
  if (err instanceof api.ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return String(err);
}

function emptyFile(
  path: string,
  name: string,
  kind: OpenFile["kind"],
  loadError: string | null,
): OpenFile {
  return {
    path,
    name,
    kind,
    buffer: "",
    savedContent: "",
    hash: null,
    dirty: false,
    conflict: null,
    loadError,
    officeGeneration: 0,
    saveAck: false,
  };
}

export const useStore = create<WorkbenchStore>()((set, get) => {
  const patchFile = (path: string, patch: Partial<OpenFile>): void => {
    set((s) => ({
      openFiles: s.openFiles.map((f) => (f.path === path ? { ...f, ...patch } : f)),
    }));
  };

  const appendChat = (sessionId: string, item: ChatItem): void => {
    set((s) => {
      const chat = s.chats[sessionId] ?? { items: [] };
      return { chats: { ...s.chats, [sessionId]: { items: [...chat.items, item] } } };
    });
  };

  const patchPlan = (planId: string, patch: Partial<PlanDraft>): void => {
    set((s) => ({
      plans: { ...s.plans, [planId]: { ...(s.plans[planId] ?? emptyPlanDraft()), ...patch } },
    }));
  };

  return {
    theme: initialTheme,
    tree: null,
    openFiles: [],
    activePath: null,
    folders: [],
    sessionStates: {},
    sessionFlags: {},
    sessionLimits: null,
    activeSessionId: null,
    transcriptView: null,
    chats: {},
    chatDrafts: {},
    plans: {},
    lastCostUsd: null,
    quickBarOpen: false,
    quickBarPrefill: "",
    quickPick: null,
    terminals: [{ id: 1, title: "Terminal 1", generation: 0, alive: true, paneKey: null }],
    activeTerminalId: 1,
    toasts: [],
    pendingClosePath: null,
    pendingShellClose: false,
    officeStatus: null,
    shortcuts: [],
    shortcutProblems: [],
    provenance: {},
    provenanceDismissed: loadDismissed(),

    init: () => {
      if (initialized) return;
      initialized = true;
      new ReconnectingSocket("/ws/events", {
        onMessage: (data) => {
          const event = data as WorkspaceEvent;
          if (event.type === "file_changed") get().handleFileChanged(event);
          else if (event.type === "session_status") get().handleSessionStatus(event);
          else if (event.type === "shortcuts_changed") void get().refreshShortcuts();
          else if (event.type === "file_provenance") get().handleFileProvenance(event);
        },
        // Re-sync on every (re)connect — covers events missed while offline.
        onOpen: () => {
          void get().refreshTree();
          void get().refreshSessions();
          void get().refreshShortcuts();
          void get().refreshProvenance();
        },
      });
      void get().ensureOfficeStatus();
    },

    setTheme: (theme) => {
      // A theme flip changes a colour on nearly every element, and every one
      // of those carrying a colour transition would start one (DESIGN.md §5:
      // the theme switch never animates). `withoutTransitions` makes it one
      // style recalculation with transitions suppressed.
      withoutTransitions(() => {
        if (theme === "light") document.documentElement.setAttribute("data-theme", "light");
        else document.documentElement.removeAttribute("data-theme");
      });
      try {
        localStorage.setItem(THEME_STORAGE_KEY, theme);
      } catch {
        // storage unavailable — theme just won't persist
      }
      defineWorkbenchTheme(theme); // after the attribute flips: reads live tokens
      set({ theme });
    },

    toggleTheme: () => {
      get().setTheme(get().theme === "dark" ? "light" : "dark");
    },

    // A plain open always leaves pick mode: `Ctrl+P` mid-pick means "never
    // mind, find me a file", not "filter the panes by filename".
    setQuickBarOpen: (open, prefill = "") =>
      set({ quickBarOpen: open, quickBarPrefill: prefill, quickPick: null }),

    openQuickPick: (pick) => set({ quickBarOpen: true, quickBarPrefill: "", quickPick: pick }),

    newTerminal: () =>
      set((s) => {
        const id = ++terminalSeq;
        return {
          terminals: [
            ...s.terminals,
            { id, title: `Terminal ${id}`, generation: 0, alive: true, paneKey: null },
          ],
          activeTerminalId: id,
        };
      }),

    ensureTerminalPane: (paneKey) => {
      const existing = get().terminals.find((t) => t.paneKey === paneKey);
      if (existing !== undefined) return existing.id;
      const id = ++terminalSeq;
      set((s) => ({
        terminals: [
          ...s.terminals,
          { id, title: `Terminal ${paneKey}`, generation: 0, alive: true, paneKey },
        ],
      }));
      return id;
    },

    nextTerminalPaneKey: () => {
      // Derived from the terminals that exist, never from a counter: a counter
      // restarts at 1 on every reload while the restored panes are still called
      // 1 and 2, and the third pane would collide with the first.
      //
      // The tab strip's numbers are in the same set on purpose. They are two
      // numbering spaces — a tab is called `Terminal <id>` and a pane
      // `Terminal <key>` — and a window showing two different shells both
      // labelled "Terminal 1" is worse than a number being skipped.
      const taken = new Set(get().terminals.map((t) => t.paneKey ?? String(t.id)));
      let next = 1;
      while (taken.has(String(next))) next += 1;
      return String(next);
    },

    closeTerminal: (id) =>
      set((s) => {
        // Unmounting the instance closes its socket, which releases the PTY.
        const index = s.terminals.findIndex((t) => t.id === id);
        const terminals = s.terminals.filter((t) => t.id !== id);
        let activeTerminalId = s.activeTerminalId;
        if (activeTerminalId === id) {
          activeTerminalId = terminals[Math.min(index, terminals.length - 1)]?.id ?? null;
        }
        return { terminals, activeTerminalId };
      }),

    setActiveTerminal: (id) => set({ activeTerminalId: id }),

    restartTerminal: (id) =>
      set((s) => ({
        terminals: s.terminals.map((t) =>
          t.id === id ? { ...t, generation: t.generation + 1, alive: true } : t,
        ),
      })),

    setTerminalAlive: (id, alive) =>
      set((s) => ({
        terminals: s.terminals.map((t) => (t.id === id ? { ...t, alive } : t)),
      })),

    pushToast: (kind, message) => {
      const id = ++toastSeq;
      set((s) => ({ toasts: [...s.toasts, { id, kind, message }] }));
      window.setTimeout(() => get().dismissToast(id), TOAST_AUTO_DISMISS_MS);
    },

    dismissToast: (id) => {
      set((s) =>
        s.toasts.some((t) => t.id === id)
          ? { toasts: s.toasts.filter((t) => t.id !== id) }
          : s,
      );
    },

    refreshTree: async () => {
      try {
        set({ tree: await api.getTree() });
      } catch (err) {
        console.error("tree refresh failed", err);
        get().pushToast("error", `File tree refresh failed: ${errorDetail(err)}`);
      }
    },

    openFile: async (path) => {
      // Opening it IS seeing it: half of the acknowledge rule (the other half
      // is dismissing the bar). Fires even for an already-open tab, because
      // clicking a marked file in the tree is exactly how a user attends to it.
      get().acknowledgeProvenance(path);
      if (get().openFiles.some((f) => f.path === path)) {
        set({ activePath: path });
        return;
      }
      if (loadingPaths.has(path)) return;
      const name = path.split("/").pop() ?? path;
      if (isOfficePath(path)) {
        // No content fetch — the OnlyOffice panel pulls its own editor config.
        set((s) => ({
          openFiles: [...s.openFiles, emptyFile(path, name, "office", null)],
          activePath: path,
        }));
        void get().ensureOfficeStatus();
        return;
      }
      loadingPaths.add(path);
      try {
        const fc = await api.getFileContent(path);
        set((s) => ({
          openFiles: [
            ...s.openFiles,
            {
              ...emptyFile(path, name, "text", null),
              buffer: fc.content,
              savedContent: fc.content,
              hash: fc.hash,
            },
          ],
          activePath: path,
        }));
      } catch (err) {
        const detail = err instanceof api.ApiError ? err.detail : String(err);
        set((s) => ({
          openFiles: [...s.openFiles, emptyFile(path, name, "text", detail)],
          activePath: path,
        }));
      } finally {
        loadingPaths.delete(path);
      }
    },

    requestCloseFile: (path) => {
      const file = get().openFiles.find((f) => f.path === path);
      if (file !== undefined && file.dirty) set({ pendingClosePath: path });
      else get().closeFile(path);
    },

    resolvePendingClose: async (action) => {
      const path = get().pendingClosePath;
      set({ pendingClosePath: null });
      if (path === null || action === "cancel") return;
      if (action === "save") {
        await get().saveFile(path);
        const file = get().openFiles.find((f) => f.path === path);
        // Save failed (conflict bar explains why) — keep the file open.
        if (file !== undefined && file.dirty) return;
      }
      get().closeFile(path);
    },

    requestShellClose: () => {
      // Nothing to lose: let the window go without a prompt.
      if (!get().openFiles.some((f) => f.dirty)) {
        void closeShellWindow();
        return;
      }
      set({ pendingShellClose: true });
    },

    resolveShellClose: async (action) => {
      if (action === "cancel") {
        set({ pendingShellClose: false });
        void cancelShellClose();
        return;
      }
      if (action === "save") {
        for (const file of get().openFiles.filter((f) => f.dirty)) {
          await get().saveFile(file.path);
        }
        const failed = get().openFiles.filter((f) => f.dirty);
        if (failed.length > 0) {
          // A save that did not land (the conflict bar says why) must not take
          // the window down with it — that is the loss this prompt exists for.
          set({ pendingShellClose: false });
          void cancelShellClose();
          get().pushToast(
            "error",
            `Not closing: ${failed.map((f) => f.name).join(", ")} could not be saved.`,
          );
          return;
        }
      }
      set({ pendingShellClose: false });
      await closeShellWindow();
    },

    closeFile: (path) => {
      disposeModel(path);
      set((s) => {
        const index = s.openFiles.findIndex((f) => f.path === path);
        const openFiles = s.openFiles.filter((f) => f.path !== path);
        let activePath = s.activePath;
        if (activePath === path) {
          activePath = openFiles[Math.min(index, openFiles.length - 1)]?.path ?? null;
        }
        return { openFiles, activePath };
      });
    },

    createEntry: async (path, kind) => {
      try {
        await api.createEntry({ path, kind });
      } catch (err) {
        get().pushToast("error", `Create failed: ${errorDetail(err)}`);
        return;
      }
      await get().refreshTree();
      if (kind === "file") void get().openFile(path);
    },

    renameEntry: async (path, newPath) => {
      // Open files under the old name: remap after the rename. Dirty buffers
      // would silently lose edits through close/reopen — block those up front.
      const prefix = path + "/";
      const affected = get().openFiles.filter(
        (f) => f.path === path || f.path.startsWith(prefix),
      );
      if (affected.some((f) => f.dirty)) {
        get().pushToast("warn", "Save or close unsaved files under the old name first.");
        return;
      }
      try {
        await api.renameEntry({ path, new_path: newPath });
      } catch (err) {
        get().pushToast("error", `Rename failed: ${errorDetail(err)}`);
        return;
      }
      const activeWas = get().activePath;
      for (const f of affected) get().closeFile(f.path);
      await get().refreshTree();
      const remap = (p: string): string =>
        p === path ? newPath : p.startsWith(prefix) ? newPath + p.slice(path.length) : p;
      for (const f of affected) await get().openFile(remap(f.path));
      if (activeWas !== null && get().openFiles.some((f) => f.path === remap(activeWas))) {
        set({ activePath: remap(activeWas) });
      }
    },

    deleteEntry: async (path) => {
      try {
        await api.deleteEntry(path);
      } catch (err) {
        get().pushToast("error", `Delete failed: ${errorDetail(err)}`);
        return;
      }
      // The user confirmed the delete — dirty state no longer guards anything.
      if (get().openFiles.some((f) => f.path === path)) get().closeFile(path);
      void get().refreshTree();
    },

    setActiveFile: (path) => {
      // Bringing a tab forward is opening it, so it acknowledges too — an agent
      // can change a file that is open behind the active one.
      get().acknowledgeProvenance(path);
      set({ activePath: path });
    },

    updateBuffer: (path, text) => {
      set((s) => ({
        openFiles: s.openFiles.map((f) =>
          f.path === path ? { ...f, buffer: text, dirty: text !== f.savedContent } : f,
        ),
      }));
    },

    saveFile: async (path) => {
      const file = get().openFiles.find((f) => f.path === path);
      if (!file || file.loadError !== null) return;
      if (file.kind === "office") {
        // Fire-and-forget flush of pending edits to disk; a subtle "Saved"
        // acknowledgment shows on success (the editor autosaves regardless).
        try {
          await api.postOfficeForcesave(path);
          patchFile(path, { saveAck: true });
          window.clearTimeout(officeAckTimers.get(path));
          officeAckTimers.set(
            path,
            window.setTimeout(() => {
              officeAckTimers.delete(path);
              patchFile(path, { saveAck: false });
            }, 1600),
          );
        } catch {
          // office disabled or Document Server unreachable — nothing to flush
        }
        return;
      }
      const content = file.buffer;
      try {
        const res = await api.putFileContent({
          path,
          content,
          expected_hash: file.hash ?? undefined,
        });
        set((s) => ({
          openFiles: s.openFiles.map((f) =>
            f.path === path
              ? {
                  ...f,
                  savedContent: content,
                  hash: res.hash,
                  dirty: f.buffer !== content,
                  conflict: null,
                }
              : f,
          ),
        }));
      } catch (err) {
        if (err instanceof api.ApiError && err.status === 409) {
          patchFile(path, {
            conflict: "Changed on disk since last load — not saved.",
          });
        } else {
          patchFile(path, {
            conflict: `Save failed: ${err instanceof Error ? err.message : String(err)}`,
          });
          get().pushToast("error", `Save failed for ${path}: ${errorDetail(err)}`);
        }
      }
    },

    handleFileChanged: (event) => {
      window.clearTimeout(treeRefreshTimer);
      treeRefreshTimer = window.setTimeout(() => {
        void get().refreshTree();
      }, 500);

      const file = get().openFiles.find((f) => f.path === event.path);
      if (!file || file.loadError !== null) return;
      if (file.kind === "office") {
        // Coalesce, then ask the server whose save this was (see checkOfficeChange).
        scheduleOfficeCheck(event.path, event.hash);
        return;
      }
      if (event.hash !== null && event.hash === file.hash) return; // echo of our own save
      // Coalesce bursts (a single save often surfaces as delete+modify on
      // Windows) and verify against a fresh GET rather than the event payload.
      scheduleFileSync(event.path);
    },

    syncFromDisk: async (path) => {
      try {
        const fc = await api.getFileContent(path);
        const file = get().openFiles.find((f) => f.path === path);
        if (!file || file.loadError !== null) return;
        if (fc.hash === file.hash) return; // echo of our own save
        if (!file.dirty) {
          // Apply to the model first (preserves cursor/scroll) and store the
          // value as Monaco holds it so dirty-tracking compares like with like.
          const effective = setModelContent(path, fc.content) ?? fc.content;
          set((s) => ({
            openFiles: s.openFiles.map((f) =>
              f.path === path
                ? {
                    ...f,
                    buffer: effective,
                    savedContent: effective,
                    hash: fc.hash,
                    dirty: false,
                    conflict: null,
                  }
                : f,
            ),
          }));
        } else {
          patchFile(path, { conflict: "Changed on disk while you have unsaved edits." });
        }
      } catch (err) {
        if (err instanceof api.ApiError && err.status === 404) {
          patchFile(path, { hash: null, conflict: "Deleted on disk — saving re-creates it." });
        }
        // other errors are transient; the next watcher event retries
      }
    },

    reloadFromDisk: async (path) => {
      try {
        const fc = await api.getFileContent(path);
        const effective = setModelContent(path, fc.content) ?? fc.content;
        set((s) => ({
          openFiles: s.openFiles.map((f) =>
            f.path === path
              ? {
                  ...f,
                  buffer: effective,
                  savedContent: effective,
                  hash: fc.hash,
                  dirty: false,
                  conflict: null,
                }
              : f,
          ),
        }));
      } catch {
        get().closeFile(path); // gone on disk — nothing to reload
      }
    },

    keepMine: async (path) => {
      const file = get().openFiles.find((f) => f.path === path);
      if (!file) return;
      try {
        // Accept the disk version as the new base so the next Ctrl+S overwrites it.
        const fc = await api.getFileContent(path);
        patchFile(path, {
          hash: fc.hash,
          savedContent: fc.content,
          dirty: file.buffer !== fc.content,
          conflict: null,
        });
      } catch {
        patchFile(path, { hash: null, dirty: true, conflict: null });
      }
    },

    refreshProvenance: async () => {
      // Frames that arrive before the response does describe a *later* state
      // than it carries, so they are recorded here and re-applied on top.
      const touched = new Set<string>();
      provenanceInFlight = touched;
      try {
        const map = await api.getProvenance();
        // A later refresh started while this one was out (two reconnects in
        // quick succession): its snapshot is the fresher one, so drop this
        // response whole rather than let it overwrite what that one applies.
        if (provenanceInFlight !== touched) return;
        set((s) => ({
          provenance: applyProvenanceSnapshot(map.entries, s.provenance, touched),
          // A dismissal for a path nobody attributes any more (a server restart
          // forgets the whole map) is dead weight — drop it here rather than
          // let the persisted set outlive what it describes.
          provenanceDismissed: prunedDismissed(s.provenanceDismissed, map.entries),
        }));
        persistDismissed(get().provenanceDismissed);
      } catch (err) {
        console.error("provenance refresh failed", err);
      } finally {
        if (provenanceInFlight === touched) provenanceInFlight = null;
      }
    },

    handleFileProvenance: (event) => {
      const entry = event.entry;
      provenanceInFlight?.add(entry.path);
      const before = get().provenanceDismissed;
      set((s) => {
        if (entry.agent === null) {
          // The user (or something outside Workbench) wrote this path — or the
          // server evicted it: the agent claim is no longer true, so it goes.
          if (s.provenance[entry.path] === undefined) return {};
          const provenance = { ...s.provenance };
          delete provenance[entry.path];
          return { provenance, provenanceDismissed: without(s.provenanceDismissed, entry.path) };
        }
        return {
          provenance: { ...s.provenance, [entry.path]: entry },
          // A fresh, unacknowledged change re-raises a bar the user closed for
          // the previous one; an acknowledgment echo leaves it closed.
          provenanceDismissed: entry.acknowledged
            ? s.provenanceDismissed
            : without(s.provenanceDismissed, entry.path),
        };
      });
      const after = get().provenanceDismissed;
      if (after !== before) persistDismissed(after);
    },

    acknowledgeProvenance: (path) => {
      const entry = get().provenance[path];
      if (entry === undefined || entry.acknowledged) return;
      set((s) => {
        const current = s.provenance[path];
        if (current === undefined) return {};
        return { provenance: { ...s.provenance, [path]: { ...current, acknowledged: true } } };
      });
      // Server-side so every window agrees and a reconnect refetch keeps it.
      void api.acknowledgeProvenance({ path }).catch(() => {
        // Best-effort: the marker is a nudge, not state worth a toast.
      });
    },

    dismissProvenance: (path) => {
      set((s) => ({ provenanceDismissed: { ...s.provenanceDismissed, [path]: true } }));
      persistDismissed(get().provenanceDismissed);
      get().acknowledgeProvenance(path);
    },

    refreshShortcuts: async () => {
      try {
        const state = await api.getShortcuts();
        set({ shortcuts: state.entries, shortcutProblems: state.problems });
        // Toast once per distinct problem set: a reload that changes nothing
        // (or fixes everything) must not re-nag on every watcher event.
        const signature = JSON.stringify(state.problems);
        if (signature === lastShortcutProblems) return;
        lastShortcutProblems = signature;
        const first = state.problems[0];
        if (first === undefined) return;
        const more = state.problems.length > 1 ? ` (+${state.problems.length - 1} more)` : "";
        get().pushToast("warn", `${first.file}: ${first.message}${more}`);
      } catch (err) {
        console.error("shortcuts refresh failed", err);
      }
    },

    runShortcut: (entry) => {
      // Insertion only. A kind a registered tool *carries out* never reaches
      // here (commands.ts routes it), and a kind nothing knows is a no-op
      // rather than text delivered to the wrong surface.
      if (entry.kind !== "shell" && entry.kind !== "prompt") return;
      if (entry.kind === "shell") {
        const id = get().activeTerminalId;
        const handle = id === null ? null : terminalHandle(id);
        if (handle === null) {
          get().pushToast("warn", "No terminal open — Alt+T opens one, then try again.");
          return;
        }
        // No trailing newline, ever: the user presses Enter (see shellInsertText).
        // A handle outlives its socket, so an undelivered insert must say so —
        // silence here reads as "it typed" and invites an Enter into a dead tab.
        if (!handle.send(shellInsertText(entry.body))) {
          get().pushToast("warn", "Terminal is not connected — reconnect it, then try again.");
          return;
        }
        // After the QuickBar unmounts, so the keyboard lands where the text did.
        window.setTimeout(() => handle.focus(), 0);
        return;
      }
      const sessionId = get().activeSessionId;
      if (sessionId === null) {
        get().pushToast("warn", "No agent session open — start one, then try again.");
        return;
      }
      set((s) => ({
        chatDrafts: {
          ...s.chatDrafts,
          [sessionId]: promptInsertText(s.chatDrafts[sessionId] ?? "", entry.body),
        },
      }));
      focusChatInput();
    },

    setChatDraft: (sessionId, text) =>
      set((s) => ({ chatDrafts: { ...s.chatDrafts, [sessionId]: text } })),

    ensureOfficeStatus: async () => {
      if (get().officeStatus !== null || officeStatusPending) return;
      officeStatusPending = true;
      try {
        const status = await api.getOfficeStatus();
        set({ officeStatus: status });
        // Warm the connection + script cache before the first office tab.
        if (status.enabled && status.server_url !== null) preloadDocsApi(status.server_url);
      } catch (err) {
        console.error("office status failed", err);
      } finally {
        officeStatusPending = false; // on failure: retried on the next office open
      }
    },

    checkOfficeChange: async (path, eventHash) => {
      // The editor's own saves also land on disk and fire watcher events; the
      // server remembers the hash of its last Document Server save so we can
      // tell those apart from external (agent/git) edits.
      let lastSave: string | null = null;
      try {
        lastSave = (await api.getOfficeLastSave(path)).hash;
      } catch {
        // endpoint unavailable — treat as unknown and offer the reopen
      }
      const file = get().openFiles.find((f) => f.path === path);
      if (!file || file.kind !== "office") return;
      if (eventHash !== null && eventHash === lastSave) return; // editor's own save
      patchFile(path, { conflict: "Document changed outside the editor." });
    },

    reopenOffice: (path) => {
      // New generation -> the panel destroys the editor, refetches the config
      // (new document.key for the changed bytes) and recreates it.
      set((s) => ({
        openFiles: s.openFiles.map((f) =>
          f.path === path
            ? { ...f, conflict: null, officeGeneration: f.officeGeneration + 1 }
            : f,
        ),
      }));
    },

    refreshSessions: async () => {
      try {
        // Fetched with the listing so the two never disagree: the pane picker
        // greys "New agent session" off these numbers, and a stale count would
        // grey it while the server was still willing (or offer it while it was
        // not). A failure here must not fail the listing — the picker simply
        // stops predicting and the 429 toast explains it after the fact.
        const [folders, limits] = await Promise.all([
          api.getSessions(),
          api.getSessionLimits().catch(() => null),
        ]);
        if (limits !== null) set({ sessionLimits: limits });
        for (const group of folders) {
          group.sessions.sort((a, b) => b.updated_at - a.updated_at);
        }
        folders.sort(
          (a, b) => (b.sessions[0]?.updated_at ?? 0) - (a.sessions[0]?.updated_at ?? 0),
        );
        // Rebuild states/flags from the listing: keep the client-side entry
        // (WS events keep it fresher than the poll) but drop sessions that no
        // longer exist — otherwise a needs_attention from a session evicted by
        // a backend restart wedges the attention dot/title badge forever.
        set((s) => {
          const states: Record<string, SessionState> = {};
          const flags: Record<string, SessionFlags> = {};
          for (const group of folders) {
            for (const ses of group.sessions) {
              states[ses.session_id] = s.sessionStates[ses.session_id] ?? ses.state;
              const kept = s.sessionFlags[ses.session_id];
              if (kept !== undefined) flags[ses.session_id] = kept;
            }
          }
          return { folders, sessionStates: states, sessionFlags: flags };
        });
      } catch (err) {
        console.error("sessions refresh failed", err);
        get().pushToast("error", `Session list refresh failed: ${errorDetail(err)}`);
      }
    },

    openSession: (info) => {
      if (info.live) get().openLiveSession(info);
      else void get().openTranscript(info);
    },

    openSessionById: (sessionId) => {
      const info = get()
        .folders.flatMap((group) => group.sessions)
        .find((session) => session.session_id === sessionId);
      if (info === undefined) {
        get().pushToast("warn", "That session is no longer available in this workspace.");
        return;
      }
      get().openSession(info);
    },

    openLiveSession: (info) => {
      const id = info.session_id;
      ensureAgentSocket(id);
      set((s) => ({
        activeSessionId: id,
        transcriptView: null,
        chats: id in s.chats ? s.chats : { ...s.chats, [id]: { items: [] } },
        sessionFlags: { ...s.sessionFlags, [id]: { done: false, error: false } },
      }));
    },

    attachSession: (sessionId) => {
      ensureAgentSocket(sessionId);
      set((s) =>
        sessionId in s.chats ? {} : { chats: { ...s.chats, [sessionId]: { items: [] } } },
      );
    },

    focusSession: (sessionId) => {
      get().attachSession(sessionId);
      // A transcript and a live pane cannot both be what the keyboard means, so
      // taking focus into a session pane closes the transcript view — the same
      // thing `openLiveSession` does, minus the flag reset, because arriving at
      // a pane is not the same event as opening a session.
      set({ activeSessionId: sessionId, transcriptView: null });
    },

    openTranscript: async (info) => {
      try {
        const transcript = await api.getTranscript(info.folder, info.session_id);
        set({ transcriptView: { session: info, messages: transcript.messages }, activeSessionId: null });
      } catch (err) {
        console.error("transcript load failed", err);
        get().pushToast("error", `Transcript load failed: ${errorDetail(err)}`);
      }
    },

    createSessionIn: async (folder) => {
      try {
        const info = await api.createSession({ folder });
        set((s) => ({ sessionStates: { ...s.sessionStates, [info.session_id]: info.state } }));
        get().openLiveSession(info);
        void get().refreshSessions();
        return info.session_id;
      } catch (err) {
        console.error("session create failed", err);
        // The ceiling reads as a ceiling, not as an unexplained failure: 429 is
        // the only refusal here a user can act on, and what they do about it is
        // finish a session or raise the setting. The picker greys the row off
        // `sessionLimits` before this, but the count can move under a split
        // that was already in flight — so this stays the honest last word.
        if (err instanceof api.ApiError && err.status === 429) {
          const max = get().sessionLimits?.max_concurrent ?? null;
          get().pushToast(
            "warn",
            `All ${max === null ? "" : String(max) + " "}session slots are busy. ` +
              "Finish one, or raise WORKBENCH_MAX_CONCURRENT_SESSIONS.",
          );
        } else {
          get().pushToast("error", `New session failed: ${errorDetail(err)}`);
        }
        void get().refreshSessions();
        return null;
      }
    },

    resumeSession: async () => {
      const view = get().transcriptView;
      if (!view) return;
      try {
        const info = await api.createSession({
          folder: view.session.folder,
          resume_session_id: view.session.session_id,
        });
        const items: ChatItem[] = view.messages.map((m) =>
          m.role === "user"
            ? { kind: "user", text: m.text }
            : { kind: "assistant", text: m.text, done: true, costUsd: null, isError: false },
        );
        set((s) => ({
          chats: { ...s.chats, [info.session_id]: { items } },
          sessionStates: { ...s.sessionStates, [info.session_id]: info.state },
        }));
        get().openLiveSession(info);
        void get().refreshSessions();
      } catch (err) {
        console.error("session resume failed", err);
        get().pushToast("error", `Session resume failed: ${errorDetail(err)}`);
      }
    },

    sendChat: (text) => {
      const id = get().activeSessionId;
      if (!id) return;
      // The server names a session from its **first** user message, and until
      // this the listing only refreshed for a session it had never seen — so a
      // conversation stayed "new session" everywhere until the next reload.
      // That was survivable with one Agent panel and is not with four panes:
      // four tabs reading "new session" are four panes you cannot tell apart.
      const named = (get().chats[id]?.items ?? []).some((item) => item.kind === "user");
      ensureAgentSocket(id).send({ type: "user_message", text });
      appendChat(id, { kind: "user", text });
      set((s) => ({
        sessionStates: { ...s.sessionStates, [id]: "working" },
        chatDrafts: { ...s.chatDrafts, [id]: "" },
      }));
      if (!named) scheduleSessionsRefresh();
    },

    decidePermission: (requestId, allow) => {
      const id = get().activeSessionId;
      if (!id) return;
      ensureAgentSocket(id).send({ type: "permission_decision", request_id: requestId, allow });
      set((s) => {
        const chat = s.chats[id];
        if (!chat) return {};
        return {
          chats: {
            ...s.chats,
            [id]: {
              items: chat.items.map((item) =>
                item.kind === "permission" && item.requestId === requestId
                  ? { ...item, decision: allow ? "allow" : "deny" }
                  : item,
              ),
            },
          },
        };
      });
    },

    setPlanChoice: (planId, nodeId, optionId) => {
      const draft = get().plans[planId] ?? emptyPlanDraft();
      if (draft.verdict !== null) return; // settled cards are read-only
      patchPlan(planId, { choices: { ...draft.choices, [nodeId]: optionId } });
    },

    setPlanAnnotation: (planId, nodeId, text) => {
      const draft = get().plans[planId] ?? emptyPlanDraft();
      if (draft.verdict !== null) return;
      patchPlan(planId, { annotations: { ...draft.annotations, [nodeId]: text } });
    },

    setPlanComment: (planId, text) => {
      const draft = get().plans[planId] ?? emptyPlanDraft();
      if (draft.verdict !== null) return;
      patchPlan(planId, { comment: text });
    },

    decidePlan: (planId, verdict) => {
      const id = get().activeSessionId;
      if (!id) return;
      const draft = get().plans[planId] ?? emptyPlanDraft();
      if (draft.verdict !== null) return; // one answer per plan
      // "approve" means "proceed with the chosen options": every option group
      // must have one, or the agent would guess at a decision it asked for.
      // The Approve button is already disabled for this; belt and braces.
      const plan = (get().chats[id]?.items ?? []).find(
        (item): item is Extract<ChatItem, { kind: "plan" }> =>
          item.kind === "plan" && item.plan.plan_id === planId,
      );
      if (
        verdict === "approve" &&
        plan !== undefined &&
        unchosenOptionGroups(plan.plan, draft).length > 0
      ) {
        return;
      }
      const annotations: PlanAnnotation[] = Object.entries(draft.annotations)
        .map(([nodeId, text]) => ({ node_id: nodeId, text: text.trim() }))
        .filter((a) => a.text !== "");
      ensureAgentSocket(id).send({
        type: "plan_decision",
        response: {
          plan_id: planId,
          verdict,
          choices: draft.choices,
          annotations,
          comment: draft.comment.trim(),
        },
      });
      patchPlan(planId, { verdict });
    },

    interrupt: () => {
      const id = get().activeSessionId;
      if (!id) return;
      ensureAgentSocket(id).send({ type: "interrupt" });
    },

    handleAgentMessage: (sessionId, message) => {
      switch (message.type) {
        case "text_delta":
          set((s) => {
            const chat = s.chats[sessionId] ?? { items: [] };
            const items = [...chat.items];
            const last = items[items.length - 1];
            if (last !== undefined && last.kind === "assistant" && !last.done) {
              items[items.length - 1] = { ...last, text: last.text + message.text };
            } else {
              items.push({
                kind: "assistant",
                text: message.text,
                done: false,
                costUsd: null,
                isError: false,
              });
            }
            return { chats: { ...s.chats, [sessionId]: { items } } };
          });
          break;
        case "tool_use":
          appendChat(sessionId, {
            kind: "tool",
            id: message.id,
            tool: message.tool,
            summary: message.summary,
            settled: false,
            settledError: false,
            output: "",
          });
          break;
        case "tool_settled":
          // One row settles on its own result — no waiting for the turn to end.
          set((s) => {
            const chat = s.chats[sessionId];
            if (chat === undefined) return {};
            return {
              chats: {
                ...s.chats,
                [sessionId]: {
                  items: chat.items.map((item) =>
                    item.kind === "tool" && item.id === message.id
                      ? {
                          ...item,
                          settled: true,
                          settledError: !message.ok,
                          output: message.output_excerpt,
                        }
                      : item,
                  ),
                },
              },
            };
          });
          break;
        case "permission_request":
          // The server replays still-pending prompts on (re)connect, so the
          // same request_id can arrive twice — render it once.
          set((s) => {
            const chat = s.chats[sessionId] ?? { items: [] };
            const seen = chat.items.some(
              (item) => item.kind === "permission" && item.requestId === message.request_id,
            );
            if (seen) return {};
            const item: ChatItem = {
              kind: "permission",
              requestId: message.request_id,
              tool: message.tool,
              description: message.description,
              decision: null,
            };
            return { chats: { ...s.chats, [sessionId]: { items: [...chat.items, item] } } };
          });
          break;
        case "plan_presented":
          set((s) => {
            const chat = s.chats[sessionId] ?? { items: [] };
            const seen = chat.items.some(
              (item) => item.kind === "plan" && item.plan.plan_id === message.plan.plan_id,
            );
            if (seen) return {}; // replay of a card already on screen
            const draft = emptyPlanDraft();
            // Start on the agent's recommendation: Approve then means "yes, as
            // proposed". The user can still switch before deciding.
            for (const node of message.plan.nodes) {
              if (node.kind !== "option_group") continue;
              const recommended = node.options.find((option) => option.recommended);
              if (recommended) draft.choices[node.node_id] = recommended.option_id;
            }
            const item: ChatItem = { kind: "plan", plan: message.plan };
            return {
              chats: { ...s.chats, [sessionId]: { items: [...chat.items, item] } },
              plans:
                s.plans[message.plan.plan_id] !== undefined
                  ? s.plans
                  : { ...s.plans, [message.plan.plan_id]: draft },
            };
          });
          break;
        case "plan_resolved":
          // Authoritative: overwrites an optimistic verdict (a click that raced
          // the timeout) and settles cards on clients that never answered.
          set((s) => {
            const draft = s.plans[message.plan_id];
            if (draft === undefined) return {}; // card belongs to another client
            return {
              plans: { ...s.plans, [message.plan_id]: { ...draft, verdict: message.verdict } },
            };
          });
          break;
        case "status":
          set((s) => ({
            sessionStates: { ...s.sessionStates, [message.session_id]: message.state },
          }));
          break;
        case "turn_done":
          set((s) => {
            const chat = s.chats[sessionId];
            const items = (chat?.items ?? []).map((item): ChatItem => {
              if (item.kind === "assistant" && !item.done) {
                return { ...item, done: true, costUsd: message.cost_usd, isError: message.is_error };
              }
              // Rows the SDK never delivered a result for (interrupted turns,
              // tools without a call id) still have to stop looking busy.
              if (item.kind === "tool" && !item.settled) {
                return { ...item, settled: true, settledError: message.is_error };
              }
              return item;
            });
            const viewing = s.activeSessionId === sessionId;
            const current = s.sessionFlags[sessionId] ?? { done: false, error: false };
            return {
              chats: chat ? { ...s.chats, [sessionId]: { items } } : s.chats,
              lastCostUsd: message.cost_usd ?? s.lastCostUsd,
              sessionStates: { ...s.sessionStates, [sessionId]: "idle" },
              sessionFlags: {
                ...s.sessionFlags,
                [sessionId]: {
                  done: current.done || !viewing,
                  error: current.error || (message.is_error && !viewing),
                },
              },
            };
          });
          break;
        case "agent_error":
          appendChat(sessionId, { kind: "error", message: message.message });
          set((s) => {
            const viewing = s.activeSessionId === sessionId;
            const current = s.sessionFlags[sessionId] ?? { done: false, error: false };
            return {
              sessionFlags: {
                ...s.sessionFlags,
                [sessionId]: { done: current.done, error: current.error || !viewing },
              },
            };
          });
          break;
      }
    },

    handleSessionStatus: (event) => {
      // Arrives on /ws/events for EVERY session, including ones this client has
      // no agent socket for — that is the whole point of the frame.
      const known = get().sessionStates[event.session_id] !== undefined;
      set((s) => ({
        sessionStates: { ...s.sessionStates, [event.session_id]: event.state },
      }));
      // A session we have never listed (started by another window, or created
      // since our last poll): pull the listing so it gets a title and a row.
      if (!known) scheduleSessionsRefresh();
    },
  };
});

// ---- terminals ---------------------------------------------------------------

/** Tab ids/titles are monotonic for the app's lifetime — "Terminal 3" stays
 * "Terminal 3" after 1 and 2 are closed, so the tab strip never renumbers. */
let terminalSeq = 1;

// ---- session listing refresh (debounced) -------------------------------------

let sessionsRefreshTimer: number | undefined;

/** Coalesce the bursts a starting session produces (idle -> working -> …). */
function scheduleSessionsRefresh(): void {
  window.clearTimeout(sessionsRefreshTimer);
  sessionsRefreshTimer = window.setTimeout(() => {
    void useStore.getState().refreshSessions();
  }, 400);
}

// ---- per-file sync coalescing ----------------------------------------------

const fileSyncTimers = new Map<string, number>();

function scheduleFileSync(path: string): void {
  const pending = fileSyncTimers.get(path);
  if (pending !== undefined) window.clearTimeout(pending);
  fileSyncTimers.set(
    path,
    window.setTimeout(() => {
      fileSyncTimers.delete(path);
      void useStore.getState().syncFromDisk(path);
    }, 150),
  );
}

// ---- office: status guard, forcesave ack timers, change-check coalescing ---

let officeStatusPending = false;

const officeAckTimers = new Map<string, number>();

const officeCheckTimers = new Map<string, { timer: number; hash: string | null }>();

/** Coalesce watcher bursts (a save often surfaces as delete+modify on Windows),
 * keeping the last non-null hash seen in the burst. */
function scheduleOfficeCheck(path: string, hash: string | null): void {
  const pending = officeCheckTimers.get(path);
  if (pending !== undefined) window.clearTimeout(pending.timer);
  const kept = hash ?? pending?.hash ?? null;
  officeCheckTimers.set(path, {
    hash: kept,
    timer: window.setTimeout(() => {
      officeCheckTimers.delete(path);
      void useStore.getState().checkOfficeChange(path, kept);
    }, 150),
  });
}

// ---- agent sockets (module-level; one per live session, kept for app lifetime)

const agentSockets = new Map<string, ReconnectingSocket>();

function ensureAgentSocket(sessionId: string): ReconnectingSocket {
  const existing = agentSockets.get(sessionId);
  if (existing) return existing;
  const socket = new ReconnectingSocket(`/ws/agent/${sessionId}`, {
    onMessage: (data) => {
      useStore.getState().handleAgentMessage(sessionId, data as AgentServerMessage);
    },
    // The server does not have this session (4404 — see `ws.ts`). Drop the
    // entry rather than caching a socket that will never carry a frame again,
    // so nothing later mistakes it for a live one.
    onGone: () => agentSockets.delete(sessionId),
  });
  agentSockets.set(sessionId, socket);
  return socket;
}

// ---- UI-state push: active/open/dirty files -> PUT /api/agents/ui-state -----

function uiStateSnapshot(s: WorkbenchStore): UiState {
  return {
    active_file: s.activePath,
    open_files: s.openFiles.map((f) => f.path),
    dirty_files: s.openFiles.filter((f) => f.dirty).map((f) => f.path),
  };
}

if (import.meta.env.DEV) {
  // Dev-console access to the store, e.g. __wbStore.getState().
  (globalThis as Record<string, unknown>).__wbStore = useStore;
}

let uiStateTimer: number | undefined;
let lastPushed = JSON.stringify(uiStateSnapshot(useStore.getState()));

useStore.subscribe((s) => {
  const snapshot = JSON.stringify(uiStateSnapshot(s));
  if (snapshot === lastPushed) return;
  window.clearTimeout(uiStateTimer);
  uiStateTimer = window.setTimeout(() => {
    lastPushed = snapshot;
    api.putUiState(JSON.parse(snapshot) as UiState).catch(() => {
      // best-effort context push; retried on the next change
    });
  }, 300);
});
