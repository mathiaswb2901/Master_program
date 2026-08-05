/**
 * The native Office host, on the UI side: a command channel and a small store.
 *
 * Two jobs, deliberately in one module because they are two halves of one seam:
 *
 * 1. **The channel.** A real Word window is docked by Rust, and a
 *    `#[tauri::command]` can only be called from this page — so the server
 *    pushes `HostCommand` frames down `/ws/office-host` and this module
 *    turns each one into a Tauri call and acks it. The webview is a courier
 *    here, not a decision-maker: it never decides *whether* to embed, only
 *    performs what the service that owns the lifecycle asked for. That is what
 *    keeps one authority for a window that outlives any given page.
 * 2. **The state.** Host lifecycle events ride `/ws/events` like everything
 *    else, and the panel renders them. This keeps its own zustand store rather
 *    than growing the app's: a tool owns its state next to its panel, which is
 *    the same rule that keeps its commands and its CSS there (see
 *    `docs/tools.md`). Same library, same conventions, one file to delete if the
 *    capability ever goes away.
 *
 * **Units cross here, exactly once.** Everything on the wire is *physical*
 * pixels (`PanelRect`); the Rust commands take *CSS* pixels and multiply by the
 * window's own scale factor. So this module divides by `devicePixelRatio` on
 * the way in and multiplies on the way out, and does it in one pair of
 * functions that a unit test can hold. Doing it anywhere else would make the
 * page a second DPI authority — the exact bug `host/geometry.rs` documents.
 */

import { create } from "zustand";

import * as api from "./api";
import { callShell, isTauri } from "./shell";
import type {
  HostAppKind,
  HostCommand,
  HostCommandAck,
  OfficeCapabilities,
  OfficeHostInfo,
  PanelRect,
  WorkspaceEvent,
} from "./types";
import { ReconnectingSocket } from "./ws";

/**
 * How much of the guest's own chrome the panel hides, in CSS pixels.
 *
 * Word and Excel draw their title strip *inside* their client area, so
 * stripping `WS_CAPTION` does not remove it: the only way to hide it is to
 * offset the window up behind the clip child. Zero keeps the strip — measured
 * on Office 16 at 250%, that strip is where the document name and the quick
 * access toolbar live, and hiding it costs the ribbon's top row as well. It is
 * a constant rather than a setting because it is a fact about the application,
 * not a preference; when the two applications need different numbers this
 * becomes a lookup on the kind the command already knows.
 */
export const CAPTION_INSET_CSS = 0;

/** Which file kinds the native host can claim, mirroring the server's table. */
const KIND_BY_EXTENSION: Record<string, HostAppKind> = {
  doc: "word",
  docx: "word",
  docm: "word",
  dot: "word",
  dotx: "word",
  rtf: "word",
  xls: "excel",
  xlsx: "excel",
  xlsm: "excel",
  csv: "excel",
  ppt: "powerpoint",
  pptx: "powerpoint",
};

export function hostAppKind(path: string): HostAppKind | null {
  const extension = path.split(".").pop()?.toLowerCase() ?? "";
  return KIND_BY_EXTENSION[extension] ?? null;
}

// ---- units -------------------------------------------------------------------

/** A DOM rectangle as the physical pixels the wire speaks in.
 *
 * Rounded, and sized from the rounded edges rather than rounded separately, so
 * two panels sharing a splitter cannot leave a one-pixel seam — the same rule
 * `geometry.rs` applies on the other side. */
export function physicalRect(rect: DOMRectReadOnly, dpr: number): PanelRect {
  const left = Math.round(rect.left * dpr);
  const top = Math.round(rect.top * dpr);
  const right = Math.round(rect.right * dpr);
  const bottom = Math.round(rect.bottom * dpr);
  return {
    x: left,
    y: top,
    width: Math.max(1, right - left),
    height: Math.max(1, bottom - top),
  };
}

/** …and back, for the Rust side. Exact: the shell's `scale_factor()` is the
 * same number as `devicePixelRatio`, so this divide and Rust's multiply cancel
 * and the physical rectangle the server asked for is the one that lands. */
export function cssRect(rect: PanelRect, dpr: number): PanelRect {
  return {
    x: rect.x / dpr,
    y: rect.y / dpr,
    width: rect.width / dpr,
    height: rect.height / dpr,
  };
}

// ---- the channel -------------------------------------------------------------

/** What a command turns into: the Tauri command name and its arguments. */
export interface ShellCall {
  command: string;
  args: Record<string, unknown>;
}

/**
 * One command → one Tauri call. Pure, so the mapping is unit-tested without a
 * socket, a shell or a window.
 *
 * `null` for an action this build does not know: a server one version ahead
 * gets an honest refusal instead of a call with the wrong arguments.
 */
export function shellCallFor(command: HostCommand, dpr: number): ShellCall | null {
  const hostId = command.host_id;
  switch (command.action) {
    case "embed":
      if (command.rect === null || command.window_id === null) return null;
      return {
        command: "host_embed",
        args: {
          hostId,
          windowId: command.window_id,
          rect: cssRect(command.rect, dpr),
          captionInset: CAPTION_INSET_CSS,
        },
      };
    case "set_bounds":
      if (command.rect === null) return null;
      return { command: "host_set_bounds", args: { hostId, rect: cssRect(command.rect, dpr) } };
    case "set_visible":
      return { command: "host_set_visible", args: { hostId, visible: command.visible !== false } };
    case "detach":
      return { command: "host_detach", args: { hostId } };
    case "close":
      return { command: "host_close", args: { hostId } };
    default:
      return null;
  }
}

/** A Rust `HostError`, as it arrives over the IPC boundary. */
interface ShellFailure {
  code?: string;
  message?: string;
}

function ackFor(command: HostCommand, error: unknown): HostCommandAck {
  const failure = (error ?? {}) as ShellFailure;
  return {
    type: "host_command_ack",
    command_id: command.command_id,
    ok: false,
    code: typeof failure.code === "string" ? failure.code : null,
    message:
      typeof failure.message === "string"
        ? failure.message
        : error instanceof Error
          ? error.message
          : String(error),
  };
}

/** Run one command and produce its ack. Exported for the tests, which hand it a
 * recording `invoke` instead of the shell. */
export async function runCommand(
  command: HostCommand,
  dpr: number,
  invoke: (call: ShellCall) => Promise<unknown>,
): Promise<HostCommandAck> {
  const call = shellCallFor(command, dpr);
  if (call === null) {
    return {
      type: "host_command_ack",
      command_id: command.command_id,
      ok: false,
      code: "unsupported",
      message: `this shell cannot ${command.action}`,
    };
  }
  try {
    await invoke(call);
    return { type: "host_command_ack", command_id: command.command_id, ok: true, code: null, message: null };
  } catch (error) {
    return ackFor(command, error);
  }
}

// ---- the store ---------------------------------------------------------------

interface OfficeHostStore {
  /** Null until the first fetch answers. */
  capabilities: OfficeCapabilities | null;
  /** Live and settled hosts, by workspace-relative path. */
  hosts: Record<string, OfficeHostInfo>;
  init: () => void;
  refreshCapabilities: () => Promise<void>;
  /** Open (or re-use) a host for this document. Returns the refusal to show,
   * or null when the host is on its way. */
  open: (path: string, rect: PanelRect) => Promise<OfficeHostInfo | null>;
  setBounds: (path: string, rect: PanelRect) => void;
  setVisible: (path: string, visible: boolean) => void;
  /** Give the window back to the desktop, leaving the document open. */
  detach: (path: string) => Promise<void>;
  close: (path: string) => Promise<void>;
}

let started = false;
/** Rect per host id, coalesced to one POST per animation frame — a splitter
 * drag produces a resize per frame and each one would otherwise be a request. */
const pendingBounds = new Map<string, PanelRect>();
let boundsFrame = 0;

export const useOfficeHostStore = create<OfficeHostStore>((set, get) => ({
  capabilities: null,
  hosts: {},

  init: () => {
    if (started) return;
    started = true;
    void get().refreshCapabilities();
    // Host lifecycle rides the shared bus; this socket reads only its own
    // frames. A second subscriber is what the bus is for, and it keeps the
    // app's store free of a capability it does not own.
    new ReconnectingSocket("/ws/events", {
      onMessage: (data) => {
        const event = data as WorkspaceEvent;
        if (event.type !== "office_host") return;
        set((state) => ({ hosts: { ...state.hosts, [event.host.path]: event.host } }));
      },
      onOpen: () => {
        void get().refreshCapabilities();
      },
    });
    if (isTauri()) startChannel(() => void get().refreshCapabilities());
  },

  refreshCapabilities: async () => {
    try {
      set({ capabilities: await api.getOfficeCapabilities() });
    } catch {
      // A server that cannot answer is a server that cannot host: leaving the
      // answer null keeps the panel on its "checking" card, and the next
      // reconnect asks again.
    }
  },

  open: async (path, rect) => {
    try {
      const host = await api.openOfficeHost({ path, rect });
      set((state) => ({ hosts: { ...state.hosts, [path]: host } }));
      return host.state === "failed" ? host : null;
    } catch (error) {
      if (error instanceof api.ApiError) {
        // 503 (hosting unavailable) and 409 (a refusal) are both answers, not
        // crashes. The panel decides what to show; capabilities are re-read
        // because the shell may have gone away since the last look.
        void get().refreshCapabilities();
      }
      throw error;
    }
  },

  setBounds: (path, rect) => {
    const host = get().hosts[path];
    if (host === undefined || TERMINAL.has(host.state)) return;
    pendingBounds.set(host.host_id, rect);
    if (boundsFrame !== 0) return;
    boundsFrame = requestAnimationFrame(() => {
      boundsFrame = 0;
      for (const [hostId, next] of pendingBounds) {
        void api.setOfficeHostBounds(hostId, next).catch(() => {
          // A host that settled between the frame and the request. The state
          // the panel renders comes from the event bus, not from here.
        });
      }
      pendingBounds.clear();
    });
  },

  setVisible: (path, visible) => {
    const host = get().hosts[path];
    if (host === undefined || TERMINAL.has(host.state)) return;
    void api.setOfficeHostVisible(host.host_id, visible).catch(() => {});
  },

  detach: async (path) => {
    const host = get().hosts[path];
    if (host === undefined || host.state !== "embedded") return;
    pendingBounds.delete(host.host_id);
    try {
      const detached = await api.detachOfficeHost(host.host_id);
      set((state) => ({ hosts: { ...state.hosts, [path]: detached } }));
    } catch {
      // The state the panel renders comes from the bus either way.
    }
  },

  close: async (path) => {
    const host = get().hosts[path];
    if (host === undefined) return;
    pendingBounds.delete(host.host_id);
    try {
      const closed = await api.closeOfficeHost(host.host_id);
      set((state) => ({ hosts: { ...state.hosts, [path]: closed } }));
    } catch {
      // Already gone server-side; the record it keeps is the truth either way.
    }
  },
}));

const TERMINAL = new Set(["closed", "crashed", "failed"]);

/**
 * Hold the command socket open for the life of the window.
 *
 * Never closed on purpose: the socket *is* how the server knows a shell is
 * attached, so dropping it would take native hosting away from documents that
 * are still docked. `ReconnectingSocket` handles a server restart, and
 * `onOpen` re-reads capabilities because attaching is what makes hosting
 * possible in the first place.
 */
function startChannel(onAttached: () => void): void {
  const dpr = (): number => window.devicePixelRatio || 1;
  const socket = new ReconnectingSocket("/ws/office-host", {
    onMessage: (data) => {
      const command = data as HostCommand;
      if (command.type !== "host_command") return;
      void runCommand(command, dpr(), (call) => callShell(call.command, call.args)).then((ack) =>
        socket.send(ack),
      );
    },
    onOpen: onAttached,
  });
}
