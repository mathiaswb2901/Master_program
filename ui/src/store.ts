/** Single zustand store: files/editors, agent sessions + chats, theme, quickbar. */

import { create } from "zustand";

import * as api from "./api";
import { defineWorkbenchTheme, disposeModel, setModelContent } from "./monaco";
import { isOfficePath } from "./office";
import { THEME_STORAGE_KEY, type Theme } from "./theme";
import type {
  AgentServerMessage,
  FileChangedEvent,
  FolderSessions,
  OfficeStatus,
  SessionInfo,
  SessionState,
  TranscriptMessage,
  TreeNode,
  UiState,
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

export type ChatItem =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string; done: boolean; costUsd: number | null; isError: boolean }
  | { kind: "tool"; tool: string; summary: string; settled: boolean; settledError: boolean }
  | {
      kind: "permission";
      requestId: string;
      tool: string;
      description: string;
      decision: "allow" | "deny" | null;
    }
  | { kind: "error"; message: string };

export interface ChatState {
  items: ChatItem[];
}

/** "Finished/failed since last viewed" markers layered over the server state. */
export interface SessionFlags {
  done: boolean;
  error: boolean;
}

interface WorkbenchStore {
  theme: Theme;
  tree: TreeNode | null;
  openFiles: OpenFile[];
  activePath: string | null;
  folders: FolderSessions[];
  sessionStates: Record<string, SessionState>;
  sessionFlags: Record<string, SessionFlags>;
  activeSessionId: string | null;
  transcriptView: { session: SessionInfo; messages: TranscriptMessage[] } | null;
  chats: Record<string, ChatState>;
  quickBarOpen: boolean;
  terminalGeneration: number;
  /** GET /api/office/status result, fetched once on first office open. */
  officeStatus: OfficeStatus | null;

  init: () => void;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
  setQuickBarOpen: (open: boolean) => void;
  newTerminal: () => void;

  refreshTree: () => Promise<void>;
  openFile: (path: string) => Promise<void>;
  closeFile: (path: string) => void;
  setActiveFile: (path: string) => void;
  updateBuffer: (path: string, text: string) => void;
  saveFile: (path: string) => Promise<void>;
  handleFileChanged: (event: FileChangedEvent) => void;
  syncFromDisk: (path: string) => Promise<void>;
  reloadFromDisk: (path: string) => Promise<void>;
  keepMine: (path: string) => Promise<void>;

  ensureOfficeStatus: () => Promise<void>;
  checkOfficeChange: (path: string, eventHash: string | null) => Promise<void>;
  reopenOffice: (path: string) => void;

  refreshSessions: () => Promise<void>;
  openSession: (info: SessionInfo) => void;
  openLiveSession: (info: SessionInfo) => void;
  openTranscript: (info: SessionInfo) => Promise<void>;
  createSessionIn: (folder: string) => Promise<void>;
  resumeSession: () => Promise<void>;
  sendChat: (text: string) => void;
  decidePermission: (requestId: string, allow: boolean) => void;
  interrupt: () => void;
  handleAgentMessage: (sessionId: string, message: AgentServerMessage) => void;
}

const initialTheme: Theme =
  document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";

let initialized = false;
const loadingPaths = new Set<string>();
let treeRefreshTimer: number | undefined;

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

  return {
    theme: initialTheme,
    tree: null,
    openFiles: [],
    activePath: null,
    folders: [],
    sessionStates: {},
    sessionFlags: {},
    activeSessionId: null,
    transcriptView: null,
    chats: {},
    quickBarOpen: false,
    terminalGeneration: 0,
    officeStatus: null,

    init: () => {
      if (initialized) return;
      initialized = true;
      new ReconnectingSocket("/ws/events", {
        onMessage: (data) => {
          const event = data as FileChangedEvent;
          if (event.type === "file_changed") get().handleFileChanged(event);
        },
        // Re-sync on every (re)connect — covers events missed while offline.
        onOpen: () => {
          void get().refreshTree();
          void get().refreshSessions();
        },
      });
    },

    setTheme: (theme) => {
      if (theme === "light") document.documentElement.setAttribute("data-theme", "light");
      else document.documentElement.removeAttribute("data-theme");
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

    setQuickBarOpen: (open) => set({ quickBarOpen: open }),

    newTerminal: () => set((s) => ({ terminalGeneration: s.terminalGeneration + 1 })),

    refreshTree: async () => {
      try {
        set({ tree: await api.getTree() });
      } catch (err) {
        console.error("tree refresh failed", err);
      }
    },

    openFile: async (path) => {
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

    setActiveFile: (path) => set({ activePath: path }),

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

    ensureOfficeStatus: async () => {
      if (get().officeStatus !== null || officeStatusPending) return;
      officeStatusPending = true;
      try {
        set({ officeStatus: await api.getOfficeStatus() });
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
        const folders = await api.getSessions();
        for (const group of folders) {
          group.sessions.sort((a, b) => b.updated_at - a.updated_at);
        }
        folders.sort(
          (a, b) => (b.sessions[0]?.updated_at ?? 0) - (a.sessions[0]?.updated_at ?? 0),
        );
        set((s) => {
          const states = { ...s.sessionStates };
          for (const group of folders) {
            for (const ses of group.sessions) {
              if (!(ses.session_id in states)) states[ses.session_id] = ses.state;
            }
          }
          return { folders, sessionStates: states };
        });
      } catch (err) {
        console.error("sessions refresh failed", err);
      }
    },

    openSession: (info) => {
      if (info.live) get().openLiveSession(info);
      else void get().openTranscript(info);
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

    openTranscript: async (info) => {
      try {
        const transcript = await api.getTranscript(info.folder, info.session_id);
        set({ transcriptView: { session: info, messages: transcript.messages }, activeSessionId: null });
      } catch (err) {
        console.error("transcript load failed", err);
      }
    },

    createSessionIn: async (folder) => {
      try {
        const info = await api.createSession({ folder });
        set((s) => ({ sessionStates: { ...s.sessionStates, [info.session_id]: info.state } }));
        get().openLiveSession(info);
        void get().refreshSessions();
      } catch (err) {
        console.error("session create failed", err);
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
      }
    },

    sendChat: (text) => {
      const id = get().activeSessionId;
      if (!id) return;
      ensureAgentSocket(id).send({ type: "user_message", text });
      appendChat(id, { kind: "user", text });
      set((s) => ({ sessionStates: { ...s.sessionStates, [id]: "working" } }));
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
            tool: message.tool,
            summary: message.summary,
            settled: false,
            settledError: false,
          });
          break;
        case "permission_request":
          appendChat(sessionId, {
            kind: "permission",
            requestId: message.request_id,
            tool: message.tool,
            description: message.description,
            decision: null,
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
              if (item.kind === "tool" && !item.settled) {
                return { ...item, settled: true, settledError: message.is_error };
              }
              return item;
            });
            const viewing = s.activeSessionId === sessionId;
            const current = s.sessionFlags[sessionId] ?? { done: false, error: false };
            return {
              chats: chat ? { ...s.chats, [sessionId]: { items } } : s.chats,
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
  };
});

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
