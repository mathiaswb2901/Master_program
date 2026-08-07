import {
  type DockviewApi,
  DockviewReact,
  type DockviewReadyEvent,
  type IDockviewPanelHeaderProps,
} from "dockview";
import { useCallback, useEffect, useState } from "react";

import { applyCaptionTint } from "./captionTint";
import { installCommandKeys } from "./commands";
import { layoutDefaultPanels, setDockApi } from "./dock";
import { DirtyCloseModal, ShellCloseModal } from "./panels/Modal";
import { QuickBar } from "./panels/QuickBar";
import { StatusBar } from "./panels/StatusBar";
import { Toasts } from "./panels/Toasts";
import { groupActionsComponent, panelComponents, panelTabInfo } from "./registry";
import { awaitBackendReady, isTauri, onCloseRequested, setAttention } from "./shell";
import { useStore } from "./store";
import { initToken } from "./token";
import { TOOLS } from "./tools";

// Which panels exist, what they are called and where they dock are properties
// of the registered tools (`tools.ts`) — this file names none of them.
const components = panelComponents(TOOLS);
// The one control a pane's tab strip carries beyond its tabs (DESIGN.md §6.11).
// Registry-supplied, so this file still names no capability — it does not know
// that what it is mounting is the split affordance.
const groupActions = groupActionsComponent(TOOLS);

const BASE_TITLE = "Workbench";

const anyNeedsAttention = (states: Record<string, string>): boolean =>
  Object.values(states).some((state) => state === "needs_attention");

/**
 * Panel chrome, entirely from the descriptor: the tool's glyph, its title, the
 * one badge it contributes, and a close button for a panel that is not in the
 * startup layout.
 *
 * Only those close: a panel the app opened with has no way back if it is
 * dismissed (no layout persistence yet), while one a command opened must be
 * dismissible by the same tab it arrived on — otherwise the split it made stays
 * for the session.
 */
function PanelTab(props: IDockviewPanelHeaderProps) {
  const { icon: Icon, badge: Badge, closable } = panelTabInfo(TOOLS, props.api.component);
  // Subscribed, not read once: a pane bound to something that names itself
  // later renames its own tab (a new agent session is "new session" until its
  // first message), and dockview does not re-render a tab component for that.
  const [title, setTitle] = useState(props.api.title ?? props.api.id);
  useEffect(() => {
    setTitle(props.api.title ?? props.api.id);
    const subscription = props.api.onDidTitleChange((event) => setTitle(event.title));
    return () => subscription.dispose();
  }, [props.api]);
  return (
    <div className="wb-panel-tab u-truncate">
      {Icon !== undefined && <Icon />}
      {title}
      {Badge !== undefined && <Badge />}
      {closable && (
        <button
          type="button"
          className="wb-panel-tab-close"
          aria-label={`Close ${title}`}
          title={`Close ${title}`}
          onClick={(e) => {
            // The tab's own click would activate the panel we are removing.
            e.stopPropagation();
            props.api.close();
          }}
        >
          ×
        </button>
      )}
    </div>
  );
}

const WORKBENCH_THEME = { name: "workbench", className: "dockview-theme-workbench" };

export default function App() {
  const attention = useStore((s) => anyNeedsAttention(s.sessionStates));
  const theme = useStore((s) => s.theme);
  // In a browser the backend is whatever served this page. In the shell it may
  // still be starting — the window opens first, deliberately — and nothing
  // below may open a socket until it answers: the terminal does not reconnect.
  const [backendReady, setBackendReady] = useState(!isTauri());
  // The per-launch auth token must exist before the first REST call or socket
  // opens — every one of them carries it (`api.ts`, `ws.ts`). Fetched
  // concurrently with the backend-ready wait rather than after it, so it never
  // adds to the launch: the shell hands it over with no HTTP, and in a browser
  // the page's own origin already serves the handshake.
  const [tokenReady, setTokenReady] = useState(false);
  // The live dock handle, captured on `onReady` but not handed to the registry
  // until the token exists (see the effect below).
  const [dockApi, setLocalDockApi] = useState<DockviewApi | null>(null);

  useEffect(() => {
    let live = true;
    void awaitBackendReady().then(() => {
      if (live) setBackendReady(true);
    });
    void initToken().then(() => {
      if (live) setTokenReady(true);
    });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    if (backendReady && tokenReady) useStore.getState().init();
  }, [backendReady, tokenReady]);

  // Paint the default arrangement immediately, so the window is never blank —
  // but do not hand the dock to the registry yet. Registering it is what starts
  // layout restore (Layouts' `onDockReady` → `GET /api/layouts`), and that call
  // must carry the token like every other REST call. Gated on `tokenReady` the
  // same way `init()` is: with enforcement on (PR4) an un-tokened restore is
  // 403'd every launch, disarming persistence and reverting the saved
  // arrangement to default — the regression this PR exists to prevent.
  const onDockReady = useCallback((event: DockviewReadyEvent): void => {
    layoutDefaultPanels(event.api);
    setLocalDockApi(event.api);
  }, []);

  useEffect(() => {
    // The registry needs the dock handle for panel focus (Ctrl+1..N), opening a
    // panel outside the startup layout, and the layout system — none of which a
    // user can reach before the handshake resolves anyway.
    if (dockApi !== null && tokenReady) setDockApi(dockApi);
  }, [dockApi, tokenReady]);

  // Attention badge in the window/taskbar title; cleared once attended. The
  // document title is what a browser tab shows; the shell retitles the native
  // window instead, which is the only surface WebView2 puts in the taskbar.
  useEffect(() => {
    document.title = attention ? `● ${BASE_TITLE}` : BASE_TITLE;
    void setAttention(attention);
  }, [attention]);

  // The other half of the window chrome: the caption Windows draws around this
  // page, tinted from the same tokens the page is painted with. Keyed on the
  // theme because that is what the tokens follow — and read *after* the flip has
  // committed, so `captionTint()` sees the palette now on screen rather than the
  // one it is replacing. Inert in a browser tab, which has no window.
  useEffect(() => {
    void applyCaptionTint();
  }, [theme]);

  // Never lose unsaved buffers to a silent window close/refresh. `beforeunload`
  // covers the browser (and an in-shell reload); the shell's own close arrives
  // as onCloseRequested, because WebView2 honors neither on a native close.
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent): void => {
      if (useStore.getState().openFiles.some((f) => f.dirty)) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    let unlisten: (() => void) | null = null;
    let unmounted = false;
    void onCloseRequested(() => useStore.getState().requestShellClose()).then((off) => {
      if (unmounted) off();
      else unlisten = off;
    });
    return () => {
      unmounted = true;
      window.removeEventListener("beforeunload", onBeforeUnload);
      unlisten?.();
    };
  }, []);

  // Every keybinding in the app comes from the command registry (commands.ts).
  useEffect(() => installCommandKeys(), []);

  useEffect(() => () => setDockApi(null), []);

  // Only the panels wait for the backend — the modals stay mounted throughout,
  // so a window closed while it is still starting is still a window that asks
  // first. Nothing is dirty this early, but the guard must not depend on that.
  //
  // The panels wait for the *token* as well, not just the backend: a default
  // panel opens its WebSocket the moment it mounts (the terminal's PTY socket,
  // an agent chat socket), and with enforcement on (M5 item 8, PR4) a handshake
  // offers the token as its subprotocol — so a socket opened before `initToken`
  // resolves is rejected 4403, and the terminal (which does not reconnect) stays
  // dead. In a browser `backendReady` is true on the first render, so gating the
  // dock on it alone would mount panels before the token existed; `tokenReady`
  // is the missing half. It resolves in one same-origin GET, so this is a blink.
  const ready = backendReady && tokenReady;
  return (
    <div className="wb-root">
      {ready ? (
        <>
          <div className="wb-dock">
            <DockviewReact
              components={components}
              defaultTabComponent={PanelTab}
              {...(groupActions !== undefined
                ? { rightHeaderActionsComponent: groupActions }
                : {})}
              theme={WORKBENCH_THEME}
              onReady={onDockReady}
            />
          </div>
          <StatusBar />
          <QuickBar />
        </>
      ) : (
        <div className="wb-boot" role="status">
          Starting the Workbench backend…
        </div>
      )}
      <DirtyCloseModal />
      <ShellCloseModal />
      <Toasts />
    </div>
  );
}
