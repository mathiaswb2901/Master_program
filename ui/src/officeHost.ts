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
  OfficeIdentity,
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

// ---- identity ----------------------------------------------------------------

/** The one quiet line the panel shows about the machine's Office sign-in. */
export interface IdentityLine {
  text: string;
  /** True when the line is a *degrade* — nobody signed in, or signed in but not
   * licensed to edit — rather than a healthy "Signed in as X". Lets the panel
   * tint it a shade softer without re-deriving the meaning. */
  degraded: boolean;
}

/**
 * The identity, reduced to the one line the panel renders — pure, so the three
 * states are unit-tested without a store, a socket or a DOM.
 *
 * Honest by construction, matching the endpoint (`models/office_host.py`):
 *
 * * not signed in → the honest degrade, "Sign into Word to edit";
 * * signed in, licensed → "Signed in as X", where X is the friendly name or,
 *   failing that, the email — we show *who*, and never claim an edit right we
 *   cannot confirm;
 * * signed in but `unlicensed` **or** `unknown` → the same name with a gentle
 *   nudge to sign in: an unconfirmed license degrades exactly as an absent one,
 *   never a fabricated "licensed" (the contract `models/office_host.py` and
 *   `LicenseState` in `types.ts` both state);
 * * signed in but no nameable active account (several accounts, registry cannot
 *   say which) → `null`: show nothing rather than fabricate a name.
 *
 * `null` (identity not fetched yet, or nothing worth saying) renders nothing.
 */
export function identityLine(identity: OfficeIdentity | null): IdentityLine | null {
  if (identity === null) return null;
  if (!identity.signed_in) return { text: "Sign into Word to edit", degraded: true };
  const friendly = identity.active?.display_name ?? identity.active?.email ?? null;
  if (friendly === null) return null;
  if (identity.license === "unlicensed" || identity.license === "unknown") {
    return { text: `Signed in as ${friendly} — sign in to Word to edit`, degraded: true };
  }
  return { text: `Signed in as ${friendly}`, degraded: false };
}

// ---- the store ---------------------------------------------------------------

interface OfficeHostStore {
  /** Null until the first fetch answers. */
  capabilities: OfficeCapabilities | null;
  /** Machine-level Office sign-in (one per machine, not per document). Null until
   * the first fetch answers. */
  identity: OfficeIdentity | null;
  /**
   * Live and settled hosts, **by workspace-relative path**.
   *
   * That key is why this map cannot outlive a workspace: `report.docx` names one
   * document in the project you were in and a different one in the project you
   * moved to, so an entry left behind would back the wrong document's UI. See
   * `closeLive` and `resetForWorkspace`, which are what the office-host tool's
   * workspace-switch guard is made of.
   */
  hosts: Record<string, OfficeHostInfo>;
  init: () => void;
  refreshCapabilities: () => Promise<void>;
  /** Re-read the machine's Office sign-in. Cheap and best-effort; a failure
   * leaves the last answer standing rather than blanking the line. */
  refreshIdentity: () => Promise<void>;
  /** Open (or re-use) a host for this document. Returns the refusal to show,
   * or null when the host is on its way. */
  open: (path: string, rect: PanelRect) => Promise<OfficeHostInfo | null>;
  setBounds: (path: string, rect: PanelRect) => void;
  setVisible: (path: string, visible: boolean) => void;
  /** Give the window back to the desktop, leaving the document open. */
  detach: (path: string) => Promise<void>;
  close: (path: string) => Promise<void>;
  /**
   * Every document with a window still open — the states that are not terminal,
   * `detached` included, because a detached document is one Word still has.
   */
  live: () => string[];
  /**
   * Close all of them and resolve with the file names Office would **not**
   * close: a real window left on the desktop, with the user's unsaved edits in
   * it, which is a fact that has to reach them before the panel that could say
   * it goes away.
   */
  closeLive: () => Promise<string[]>;
  /** Forget the workspace that is gone — after everything in flight has landed,
   * so a late close cannot write a stale path back into an empty map. */
  resetForWorkspace: () => Promise<void>;
}

let started = false;
/**
 * Close/detach calls that have not landed yet.
 *
 * A panel unmounting fires `close()` and does not wait for it, so a workspace
 * reset that cleared `hosts` immediately would race the response writing the old
 * project's path back in. Every mutation that resolves into `hosts` is tracked
 * here and `resetForWorkspace` drains it first.
 */
const inFlight = new Set<Promise<unknown>>();

/** Run `work`, keeping it in `inFlight` until it settles either way. */
async function tracked<T>(work: Promise<T>): Promise<T> {
  inFlight.add(work);
  try {
    return await work;
  } finally {
    inFlight.delete(work);
  }
}

/** The last segment of a workspace-relative path — what the user calls the file. */
export function fileNameOf(path: string): string {
  return path.split("/").pop() ?? path;
}
/** Rect per host id, coalesced to one POST per animation frame — a splitter
 * drag produces a resize per frame and each one would otherwise be a request. */
const pendingBounds = new Map<string, PanelRect>();
let boundsFrame = 0;

export const useOfficeHostStore = create<OfficeHostStore>((set, get) => ({
  capabilities: null,
  identity: null,
  hosts: {},

  init: () => {
    if (started) return;
    started = true;
    void get().refreshCapabilities();
    void get().refreshIdentity();
    // Host lifecycle rides the shared bus; this socket reads only its own
    // frames. A second subscriber is what the bus is for, and it keeps the
    // app's store free of a capability it does not own.
    new ReconnectingSocket("/ws/events", {
      onMessage: (data) => {
        const event = data as WorkspaceEvent;
        if (event.type !== "office_host") return;
        const before = get().hosts[event.host.path];
        set((state) => ({ hosts: { ...state.hosts, [event.host.path]: event.host } }));
        // Re-read the sign-in when a window actually docks: a user who signed
        // into Word between launch and embed should see the line catch up,
        // without polling the registry on every frame. `embedded` is the one
        // transition worth a read — the rest never change who is signed in.
        if (event.host.state === "embedded") void get().refreshIdentity();
        // And re-read what can be docked when a host starts or stops being live.
        // PowerPoint runs one instance for the whole session, so a deck arriving
        // takes `powerpoint` out of `hostable_kinds` and a deck settling puts it
        // back (`services/office_host/service.py`, `_kind_notes`). Without this
        // the *next* deck would be sent to a launch the server has already
        // decided to refuse, and land on a card instead of the preview. Twice
        // per document, not once per frame: only the live/settled edge asks.
        const wasLive = before !== undefined && !TERMINAL.has(before.state);
        if (wasLive !== !TERMINAL.has(event.host.state)) void get().refreshCapabilities();
      },
      onOpen: () => {
        void get().refreshCapabilities();
        void get().refreshIdentity();
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

  refreshIdentity: async () => {
    try {
      set({ identity: await api.getOfficeIdentity() });
    } catch {
      // Best-effort: a failed read leaves the previous answer standing rather
      // than blanking the line, and the next dock or reconnect asks again.
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
    await tracked(
      api
        .detachOfficeHost(host.host_id)
        .then((detached) => {
          set((state) => ({ hosts: { ...state.hosts, [path]: detached } }));
        })
        .catch(() => {
          // The state the panel renders comes from the bus either way.
        }),
    );
  },

  close: async (path) => {
    const host = get().hosts[path];
    // A settled host has nothing left to end, and the unmount that follows a
    // guarded close would otherwise send a second request for the same window.
    if (host === undefined || TERMINAL.has(host.state)) return;
    pendingBounds.delete(host.host_id);
    // Tracked as one unit *including* the write, so a drain that has finished
    // really has seen every host record this call was going to produce.
    await tracked(
      api
        .closeOfficeHost(host.host_id)
        .then((closed) => {
          set((state) => ({ hosts: { ...state.hosts, [path]: closed } }));
        })
        .catch(() => {
          // Already gone server-side; the record it keeps is the truth either way.
        }),
    );
  },

  live: () =>
    Object.entries(get().hosts)
      .filter(([, host]) => hostIsLive(host))
      .map(([path]) => path),

  closeLive: async () => {
    const live = get().live();
    // One at a time: two applications asked to quit at once can each raise a
    // "Save changes?" dialog, and the second one opens behind the first.
    for (const path of live) await get().close(path);
    const settled = get().hosts;
    // Reported **once**, not forever. A refused close leaves the host terminal
    // with `close_failed` set, so it is no longer `live` and a second attempt
    // goes through. That is deliberate: the window is out of Workbench's hands
    // at that point (the server's own sweep keeps re-asking), the user has been
    // told in the sentence that stopped them, and a workspace they can never
    // leave until Word behaves would be a trap rather than a safeguard.
    return live.filter((path) => settled[path]?.close_failed === true).map(fileNameOf);
  },

  resetForWorkspace: async () => {
    // Drained rather than cleared: an unmounting panel fires `close()` without
    // waiting, so clearing first would let that response write the *old*
    // workspace's relative path back into a map that is meant to be empty —
    // which is exactly how `report.docx` from the project you left ends up
    // backing `report.docx` in the project you opened.
    for (let drain = 0; inFlight.size > 0 && drain < MAX_DRAINS; drain += 1) {
      await Promise.allSettled([...inFlight]);
    }
    if (inFlight.size > 0) {
      // Never seen in practice — closes are finite and started by unmounts, not
      // by each other — but a reset that waits forever would hang the switch,
      // and a stale entry is recoverable while a wedged window is not.
      console.warn("office host: workspace reset gave up waiting on in-flight closes");
    }
    set({ hosts: {} });
  },
}));

/** How many times `resetForWorkspace` re-drains before it gives up. Two is
 * enough for the real sequence (the unmount's closes, then anything they
 * started); the rest is headroom. */
const MAX_DRAINS = 10;

const TERMINAL = new Set(["closed", "crashed", "failed"]);

/**
 * Does this host still name a window somebody has to deal with?
 *
 * `detached` counts: that document is on the user's desktop, open, and still
 * ours. Only the terminal states have nothing left behind them. Exported because
 * the panel needs the same question — a document that is *already* docked is
 * hostable whatever `hostable_kinds` says about starting a *new* one.
 */
export function hostIsLive(host: OfficeHostInfo | undefined): boolean {
  return host !== undefined && !TERMINAL.has(host.state);
}

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
