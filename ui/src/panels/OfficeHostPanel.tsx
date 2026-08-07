/**
 * The Office document view: a *real* Word or Excel window, docked in the panel.
 *
 * This is the tool that claims the `office` open-file kind, and the OnlyOffice
 * panel is now what it falls back *to* rather than what it replaces. Every path
 * that cannot end in a native window ends in a working editor instead: no
 * shell (a browser tab), no Office installed, hosting switched off, PowerPoint,
 * a document already open somewhere else, a launch that failed, an embed that
 * was refused. There is no state in which this renders a broken panel.
 *
 * **Refusals are explanations, not errors.** "This document is already open in
 * another Word window" is the server keeping a promise — Workbench never
 * reparents a window it did not start — so it reads as a sentence about the
 * user's document, with the preview path one click away. The same for
 * PowerPoint, which is preview-only in v1 for a reason the card states.
 *
 * **The rectangle is reported, never assumed.** A hosted window is not laid out
 * by CSS: something has to measure this element every frame it might have moved
 * and tell the server where it is. A zero-sized rectangle is how "the tab went
 * behind another one" arrives — no coupling to dockview, no guessing from the
 * active path — and it becomes `set_visible(false)`, because a real window does
 * not disappear when its `div` does.
 */

import { useEffect, useRef, useState } from "react";

import { ApiError } from "../api";
import { fileNameOf, hostAppKind, physicalRect, useOfficeHostStore } from "../officeHost";
import { parsePaneId } from "../panes";
import type { WorkbenchTool } from "../registry";
import { useStore, type OpenFile } from "../store";
import type { HostAppKind, HostReason, OfficeHostInfo, PanelRect } from "../types";
import { OfficePanel } from "./OfficePanel";

const APP_NAMES: Record<HostAppKind, string> = {
  word: "Microsoft Word",
  excel: "Microsoft Excel",
  powerpoint: "Microsoft PowerPoint",
};

/** States in which a real native window is docked into the pane (or on its way
 * in) and so cannot follow it out of the main window. `detached` is not one of
 * them — that window is already on the desktop — and neither are the terminal
 * states, which have fallen back to an OnlyOffice editor with no native window
 * at all. Read by the pop-out guard below. */
const DOCKED_STATES: ReadonlySet<string> = new Set(["launching", "embedding", "embedded"]);

/** What each terminal reason means, said to the person whose document it is.
 * Every one of them ends with the same offer, so the card never leaves the user
 * without a way to read their file. */
const REFUSALS: Record<HostReason, { title: string; hint: string }> = {
  document_open_elsewhere: {
    title: "Already open somewhere else",
    hint: "Workbench only docks a window it started itself, so it will not take over the copy you already have open. Close it there and try again, or read it here.",
  },
  powerpoint_preview_only: {
    title: "PowerPoint opens as a preview",
    hint: "PowerPoint runs one instance for the whole machine and offers no way to prove a window is ours, so docking it could hijack your own slides. Word and Excel dock for real.",
  },
  launch_failed: {
    title: "The application would not start",
    hint: "Office is installed but refused to open this document. Opening it from the Start menu usually says why.",
  },
  launch_timeout: {
    title: "The application never appeared",
    hint: "It was asked to open this document and did not produce a window. A first run that is still showing a licence or repair dialog is the usual cause.",
  },
  embed_refused: {
    title: "The window would not dock",
    hint: "The document is open, but its window refused to move into this panel.",
  },
  backend_timeout: {
    title: "No answer from the window host",
    hint: "The step that docks the window ran past its deadline. Nobody refused anything — it simply never came back.",
  },
  process_exited: {
    title: "The application closed",
    hint: "The window went away on its own. Nothing was lost that it had already saved.",
  },
  native_hosting_disabled: {
    title: "Native hosting is off",
    hint: "This window cannot dock a real document — a browser tab has no native window to dock into, and WORKBENCH_OFFICE_NATIVE=off turns it off everywhere.",
  },
  unsupported_file: {
    title: "Not an Office document",
    hint: "This file type has no application to dock.",
  },
  user_closed: { title: "Closed", hint: "You closed this document." },
  server_shutdown: {
    title: "Closed at shutdown",
    hint: "Workbench closed this document when the server stopped, so no window was left behind.",
  },
};

function Card({
  title,
  file,
  hint,
  action,
}: {
  title: string;
  file: OpenFile;
  hint: string;
  action?: { label: string; run: () => void };
}) {
  return (
    <div className="wb-office-state">
      <div className="wb-office-card">
        <div className="wb-office-card-title">{title}</div>
        <div className="wb-office-card-file u-truncate" title={file.path}>
          {file.name}
        </div>
        <div className="wb-office-card-hint">{hint}</div>
        {action !== undefined && (
          <button type="button" className="wb-office-btn" onClick={action.run}>
            {action.label}
          </button>
        )}
      </div>
    </div>
  );
}

/**
 * The docked surface: a hole in the page that a native window sits in front of.
 *
 * Nothing is drawn here on purpose. The element exists to have a rectangle, and
 * the rectangle is the whole contract with the shell.
 */
function NativeHost({
  file,
  kind,
  onFallback,
}: {
  file: OpenFile;
  kind: HostAppKind;
  onFallback: (reason: string) => void;
}) {
  const surface = useRef<HTMLDivElement>(null);
  const host = useOfficeHostStore((s) => s.hosts[file.path]);

  useEffect(() => {
    const element = surface.current;
    if (element === null) return;
    const store = useOfficeHostStore.getState();
    let disposed = false;
    let frame = 0;
    let last: PanelRect | null = null;
    let shown: boolean | null = null;
    let opened = false;

    const measure = (): PanelRect | null => {
      const box = element.getBoundingClientRect();
      // A hidden element measures zero — which is the honest signal that the
      // tab is behind another one, or the whole panel is, without this module
      // knowing anything about how the layout hid it.
      if (box.width < 1 || box.height < 1) return null;
      return physicalRect(box, window.devicePixelRatio || 1);
    };

    const tick = (): void => {
      if (disposed) return;
      frame = requestAnimationFrame(tick);
      const rect = measure();
      const visible = rect !== null;
      if (visible !== shown) {
        shown = visible;
        // Not before the host exists: the first `open` carries the rectangle
        // itself, and a `visible` for a host that has not been created yet is
        // a 404 the user would see as a failure.
        if (opened) store.setVisible(file.path, visible);
      }
      if (rect === null) return;
      if (!opened) {
        opened = true;
        // A refusal that *settled a host* is rendered from its state below;
        // one that never created a host at all only exists as this rejection,
        // so it is what turns the panel over to the fallback editor.
        void store.open(file.path, rect).catch((error: unknown) => {
          if (disposed) return;
          onFallback(error instanceof ApiError ? error.detail : String(error));
        });
        last = rect;
        return;
      }
      if (last !== null && sameRect(last, rect)) return;
      last = rect;
      store.setBounds(file.path, rect);
    };
    frame = requestAnimationFrame(tick);

    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      // The tab was closed (or the whole view swapped to the fallback): end the
      // instance we started. Leaving it running would be a Word nobody owns.
      void store.close(file.path);
    };
  }, [file.path, onFallback]);

  const state = host?.state ?? "launching";
  const reason = host?.reason ?? null;
  const app = APP_NAMES[kind];

  return (
    <div className="wb-office-body">
      <div ref={surface} className="wb-office-native" data-state={state} />
      {state === "launching" && (
        <Card title={`Opening in ${app}`} file={file} hint="Starting the application…" />
      )}
      {state === "embedding" && (
        <Card title={`Docking ${app}`} file={file} hint="Moving the window into this panel…" />
      )}
      {state === "detached" && (
        <Card
          title="This document is on your desktop"
          file={file}
          hint={`${app} still has it open, outside Workbench.`}
          action={{
            label: "Bring it back",
            run: () => {
              const box = surface.current?.getBoundingClientRect();
              if (box === undefined) return;
              void useOfficeHostStore
                .getState()
                .open(file.path, physicalRect(box, window.devicePixelRatio || 1));
            },
          }}
        />
      )}
      {state === "crashed" && (
        <Card
          title={`${app} closed`}
          file={file}
          hint={REFUSALS.process_exited.hint}
          action={{ label: "Open a preview instead", run: () => onFallback("the window closed") }}
        />
      )}
      {state === "failed" && reason !== null && (
        <Card
          title={REFUSALS[reason].title}
          file={file}
          hint={REFUSALS[reason].hint}
          action={{ label: "Open a preview here", run: () => onFallback(reason) }}
        />
      )}
      {state === "embedded" && (
        <div className="wb-office-hosted" role="status">
          <span className="wb-office-hosted-dot" aria-hidden="true" />
          {app}
        </div>
      )}
      {host?.close_failed === true && (
        <div className="wb-office-bar" role="alert">
          <span className="wb-office-bar-msg u-truncate">
            {app} would not close — it is on your desktop with unsaved changes.
          </span>
        </div>
      )}
    </div>
  );
}

function sameRect(a: PanelRect, b: PanelRect): boolean {
  return a.x === b.x && a.y === b.y && a.width === b.width && a.height === b.height;
}

/**
 * The document view itself: decide once what can open this file, then stay
 * decided until the file (or the machine's answer) changes.
 */
export function OfficeDocument({ file }: { file: OpenFile }) {
  const capabilities = useOfficeHostStore((s) => s.capabilities);
  const [fallback, setFallback] = useState<string | null>(null);

  useEffect(() => {
    useOfficeHostStore.getState().init();
  }, []);
  // A different document in the same tab starts the decision over.
  useEffect(() => setFallback(null), [file.path]);

  const kind = hostAppKind(file.path);
  if (capabilities === null) {
    return (
      <div className="wb-office">
        <Card title="Opening document" file={file} hint="Checking how this machine opens it…" />
      </div>
    );
  }

  const hostable =
    kind !== null && capabilities.native_hosting && capabilities.hostable_kinds.includes(kind);

  if (fallback !== null || !hostable) {
    return (
      <div className="wb-office-fallback">
        {kind === "powerpoint" && capabilities.native_hosting && (
          <div className="wb-office-note">
            <span className="wb-office-note-msg u-truncate">
              {REFUSALS.powerpoint_preview_only.hint}
            </span>
          </div>
        )}
        <OfficePanel file={file} />
      </div>
    );
  }

  return (
    <div className="wb-office">
      {capabilities.fake_backend && (
        <div className="wb-office-note">
          <span className="wb-office-note-msg u-truncate">
            Simulated host (WORKBENCH_OFFICE_FAKE): no document is really open.
          </span>
        </div>
      )}
      <NativeHost file={file} kind={kind} onFallback={setFallback} />
    </div>
  );
}

// ---- registration -----------------------------------------------------------

/** Right of the status bar: one line about what is really docked. Also the
 * mount point for the host channel — it is the one component that exists for
 * the life of the window, and the channel has to be attached before a document
 * is opened or the server would report that it cannot host. */
function OfficeHostStatus() {
  const hosts = useOfficeHostStore((s) => s.hosts);
  useEffect(() => {
    useOfficeHostStore.getState().init();
  }, []);
  const docked = Object.values(hosts).filter(
    (host: OfficeHostInfo) => host.state === "embedded",
  ).length;
  if (docked === 0) return null;
  return (
    <span className="wb-status-item" title="Real Office windows docked in this window">
      {docked === 1 ? "1 document docked" : `${String(docked)} documents docked`}
    </span>
  );
}

/**
 * Registered ahead of the OnlyOffice tool in `tools.ts`, which is how it claims
 * the `office` kind: `documentViewFor` takes the first enabled tool that offers
 * one. OnlyOffice keeps its own registration and is rendered from inside here,
 * so removing this one line puts the app back exactly as it was.
 */
export const officeHostTool: WorkbenchTool = {
  id: "office-host",
  title: "Office host",
  documentView: {
    kind: "office",
    component: OfficeDocument,
    hostClassName: "wb-office-host",
    // Same reason as OnlyOffice, one layer deeper: a tab switch must not tear
    // a native window down and start Word again.
    keepMounted: true,
  },
  /**
   * A docked Word is not a buffer, and this is where that difference is paid.
   *
   * The dirty-buffer guard cannot see this document — an `office` open file is
   * never marked dirty, because the unsaved paragraph is inside Word and not
   * inside anything this app models — so without a guard here a switch would
   * unmount the panel, fire a close nobody waited for, and leave the user with
   * a Word window on their desktop that the UI can no longer say anything
   * about. Settling *before* the root moves keeps this panel mounted for
   * exactly as long as it might have something to report.
   */
  workspaceSwitchGuard: {
    held: () => useOfficeHostStore.getState().live().map(fileNameOf),
    settle: () => useOfficeHostStore.getState().closeLive(),
  },
  /**
   * The root moved. `hosts` is keyed by workspace-*relative* path, so every key
   * in it now names a file in a project this window has left.
   *
   * Reached on both paths and it has to work on both: the window that asked has
   * already settled its hosts through the guard above, but a window that merely
   * *heard* about the switch has not — its documents were closed by the store's
   * reset, and this drains those closes before clearing so a late response
   * cannot re-seed the map with a path from the old workspace.
   */
  onWorkspaceChanged: () => {
    void useOfficeHostStore.getState().resetForWorkspace();
  },
  /**
   * A pane holding a *real* docked window cannot pop out (M5 item 13).
   *
   * A native Word/Excel window is a child of the **main** window's HWND; a
   * popped-out pane is a WebView2 window that is not in that parent chain, so
   * the docked window cannot follow it and reparenting it there would strand a
   * real application behind an invisible one — the exact orphan the host
   * ownership rules exist to prevent.
   *
   * A docked window only ever lives in the **default (tabbed) Editor pane**: its
   * `keepMounted` view is mounted once there, and a split `editors#<path>` pane
   * shows a "opens in the Editor pane" note rather than a second host
   * (`EditorArea.tsx`). So the veto is exactly that one pane — `editors` with no
   * instance key — and only while a window is docked or on its way in (not once
   * it is `detached` back to the desktop, which is the way out this sentence
   * points at). The pane capability asks the registry, never the store here.
   */
  popoutGuard: {
    blocks: (paneId) => {
      const { toolId, instance } = parsePaneId(paneId);
      if (toolId !== "editors" || instance !== null) return null;
      const docked = dockedHostPath();
      if (docked === null) return null;
      const kind = hostAppKind(docked);
      const app = kind === null ? "A document" : APP_NAMES[kind];
      return (
        `${app} is docked in the Editor pane as a real window, which cannot follow a pane into ` +
        `a separate window. Run "Move the docked document to the desktop" first, then pop out.`
      );
    },
  },
  statusContributions: [{ region: "right", component: OfficeHostStatus }],
  commands: [
    {
      id: "office.detachHost",
      title: "Move the docked document to the desktop",
      when: () => embeddedActiveHost() !== null,
      run: () => {
        const path = embeddedActiveHost();
        if (path !== null) void useOfficeHostStore.getState().detach(path);
      },
    },
  ],
};

/** The path of any open document whose native window is docked (or docking)
 * right now, or null. Every such window lives in the default Editor pane, so
 * this is what the pop-out guard reads to refuse popping that pane out. */
function dockedHostPath(): string | null {
  for (const [path, host] of Object.entries(useOfficeHostStore.getState().hosts)) {
    if (DOCKED_STATES.has(host.state)) return path;
  }
  return null;
}

/** The active editor tab, if what it holds is a document docked right now.
 * Live, not snapshotted: a command's `when` is re-read on every keystroke. */
function embeddedActiveHost(): string | null {
  const path = useStore.getState().activePath;
  if (path === null) return null;
  return useOfficeHostStore.getState().hosts[path]?.state === "embedded" ? path : null;
}
